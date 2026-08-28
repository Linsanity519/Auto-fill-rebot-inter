"""src/pt_runner.py 的纯逻辑测试（不开浏览器）：

  方向解析 / 关键词与日期解析 / 行分类 / 按范围选行

    python tools\\test_pt_runner.py
"""
import logging
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.disable(logging.CRITICAL)

from src.pt_runner import PtToggleRunner        # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("　" + str(detail)) if detail and not cond else ""))


TMP = Path(tempfile.mkdtemp(prefix="ptrunner_"))


def mk(direction=None, params="", strategies="", scope="keyword",
       yaml_direction="on", date_from="", date_to=""):
    s = {"screenshot_dir": str(TMP), "state_file": str(TMP / "state.json"),
         "result_file": str(TMP / "r.csv"), "cdp_url": "http://127.0.0.1:9222",
         "timeout": 15000, "resume": False,
         "pt_scope": scope, "toggle_params": params, "toggle_strategies": strategies,
         "toggle_date_from": date_from, "toggle_date_to": date_to}
    if direction is not None:
        s["toggle_direction"] = direction
    cfg = {"name": "价格策略批量开关", "direction": yaml_direction, "ledger": "价格策略开关"}
    return PtToggleRunner(s, cfg, None)


def test_direction():
    print("\n[方向解析]")
    ok("界面切 off → 关闭", mk(direction="off").direction == "off")
    ok("界面切 on → 开启", mk(direction="on").direction == "on")
    ok("界面没给 → 退回 yaml direction", mk(direction=None, yaml_direction="off").direction == "off")
    ok("都没有 → 默认 on", PtToggleRunner(
        {"screenshot_dir": str(TMP), "state_file": str(TMP / "s.json"), "resume": False},
        {"name": "x"}, None).direction == "on")
    r = mk(direction="on")
    ok("on 的文案", (r._verb, r._click_link, r._target_link) == ("开启", "开启", "关闭"))
    r = mk(direction="off")
    ok("off 的文案", (r._verb, r._click_link, r._target_link) == ("关闭", "关闭", "开启"))


def test_tokens_and_dates():
    print("\n[关键词/清单拆词 · 日期区间]")
    r = mk(params="连续\n年度, 季度")
    ok("按行 + 逗号拆", r._tokens() == ["连续", "年度", "季度"], r._tokens())
    ok("留空 → 空列表", mk(params="")._tokens() == [])
    ok("date_from 归一", mk(date_from="2026/8/5")._since() == "2026-08-05", mk(date_from="2026/8/5")._since())
    ok("date_to 归一", mk(date_to="2026-8-31")._until() == "2026-08-31", mk(date_to="2026-8-31")._until())
    ok("没填日期 → None", mk()._since() is None and mk()._until() is None)
    ok("垃圾日期 → None", mk(date_from="下周")._since() is None)


def test_classify():
    print("\n[行分类]")
    on = mk(direction="on")
    off = mk(direction="off")

    def row(link, group="ogv dmp人群包"):
        return {"name": "x", "group": group, "link": link, "state": ""}

    ok("开启方向 · 链接=开启 → toggle", on._classify(row("开启"))[0] == "toggle")
    ok("开启方向 · 链接=关闭（已开）→ done", on._classify(row("关闭"))[0] == "done")
    ok("开启方向 · 不限人群 → block",
       on._classify(row("开启", "不限"))[0] == "block")
    ok("开启方向 · 不限但已开 → 仍 done（不拦已经开着的）",
       on._classify(row("关闭", "不限"))[0] == "done")
    ok("关闭方向 · 链接=关闭 → toggle", off._classify(row("关闭"))[0] == "toggle")
    ok("关闭方向 · 链接=开启（已关/未开）→ done", off._classify(row("开启"))[0] == "done")
    ok("关闭方向 · 不限人群不拦（关是安全的）",
       off._classify(row("关闭", "不限"))[0] == "toggle")
    ok("链接文字异常 → block", on._classify(row(""))[0] == "block")


def test_pick_keyword_all():
    print("\n[按范围选行]")
    rows = [
        {"name": "测试-连续包年-A", "group": "ogv dmp人群包", "link": "开启", "state": "未开启"},
        {"name": "测试-连续包月-B", "group": "ogv dmp人群包", "link": "关闭", "state": "已开启"},
        {"name": "回归-年度大会员-C", "group": "不限", "link": "开启", "state": "未开启"},
    ]
    r = mk(scope="all")
    got, why = r._pick(rows, "186")
    ok("all → 全给", len(got) == 3 and why == "")

    r = mk(scope="keyword", params="连续")
    got, why = r._pick(rows, "186")
    ok("keyword『连续』→ A、B 两行", [g["name"] for g in got] == ["测试-连续包年-A", "测试-连续包月-B"])

    r = mk(scope="keyword", params="不存在")
    got, why = r._pick(rows, "186")
    ok("keyword 没命中 → 空 + 有原因", got == [] and "一个都没命中" in why, why)

    r = mk(scope="keyword", params="")
    got, why = r._pick(rows, "186")
    ok("keyword 留空 → 等于整页", len(got) == 3)

    r = mk(scope="list", params="测试-连续包年-A\n回归-年度大会员-C")
    got, why = r._pick(rows, "186")
    ok("list → 按名字精确对上那两行",
       [g["name"] for g in got] == ["测试-连续包年-A", "回归-年度大会员-C"], [g["name"] for g in got])

    r = mk(scope="list", params="不在表里的")
    got, why = r._pick(rows, "186")
    ok("list 全没对上 → 空 + 原因", got == [] and "一个都没对上" in why, why)

    r = mk(scope="list", params="")
    got, why = r._pick(rows, "186")
    ok("list 没填 → 空 + 提示填", got == [] and "一个人群名称都没填" in why, why)


def main():
    print("=" * 56)
    print("pt_runner 纯逻辑测试")
    print("=" * 56)
    try:
        for fn in (test_direction, test_tokens_and_dates, test_classify, test_pick_keyword_all):
            fn()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗ " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
