"""src/dl_runner.py 的纯逻辑测试（不开浏览器）：

  方向 / 层级解析、关键词与清单拆词、行分类、按范围选行、结果行的形状

    python tools\\test_dl_runner.py
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

from src.dl_runner import DlToggleRunner        # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("　" + str(detail)) if detail and not cond else ""))


TMP = Path(tempfile.mkdtemp(prefix="dlrunner_"))


def mk(direction=None, level=None, activity="708", params="", scope="keyword",
       yaml_direction="on", yaml_level="unit"):
    s = {"screenshot_dir": str(TMP), "state_file": str(TMP / "state.json"),
         "result_file": str(TMP / "r.csv"), "cdp_url": "http://127.0.0.1:9222",
         "timeout": 15000, "resume": False,
         "toggle_scope": scope, "toggle_params": params, "toggle_activity": activity}
    if direction is not None:
        s["toggle_direction"] = direction
    if level is not None:
        s["toggle_level"] = level
    cfg = {"name": "常规资源位批量开关", "direction": yaml_direction, "level": yaml_level}
    return DlToggleRunner(s, cfg, None)


def row(id_, name, state, unit_id=None):
    return {"key": id_, "id": id_, "name": name, "state": state,
            "act": "708", "act_name": "子凡测试25年", "unit_id": unit_id or id_}


def test_direction_and_level():
    print("\n[方向 / 层级解析]")
    ok("界面切 off → 暂停", mk(direction="off").direction == "off")
    ok("界面没给 → 退回 yaml", mk(direction=None, yaml_direction="off").direction == "off")
    ok("on 的动词是「启动」", mk(direction="on")._verb == "启动")
    ok("off 的动词是「暂停」", mk(direction="off")._verb == "暂停")
    ok("层级 creative", mk(level="creative").level == "creative")
    ok("层级文案跟着变",
       (mk(level="creative")._level_label, mk(level="creative")._id_label) == ("创意", "创意ID"))
    ok("单元层的文案", (mk(level="unit")._level_label, mk(level="unit")._id_label) == ("单元", "单元ID"))
    ok("认不出的层级 → 退回 unit", mk(level="banner").level == "unit")
    ok("界面没给层级 → 退回 yaml", mk(level=None, yaml_level="creative").level == "creative")


def test_tokens_and_scope():
    print("\n[拆词 / 范围]")
    r = mk(params="小黄条\nbanner, 弹窗")
    ok("按行 + 逗号拆", r._tokens() == ["小黄条", "banner", "弹窗"], r._tokens())
    ok("留空 → 空表", mk(params="")._tokens() == [])
    ok("默认范围 keyword", mk()._scope() == "keyword")
    ok("范围 list", mk(scope="list")._scope() == "list")
    ok("范围 ledger（本工具操作过的）", mk(scope="ledger")._scope() == "ledger")
    ok("认不出的范围 → 落回 keyword", mk(scope="随便写的")._scope() == "keyword")
    # 界面老键名（pt 那套用的 pt_scope）也认，省得两处不同步时静默走默认
    r = mk()
    r.s.pop("toggle_scope")
    r.s["pt_scope"] = "list"
    ok("pt_scope 也认", r._scope() == "list")


def test_classify():
    print("\n[行分类]")
    on, off = mk(direction="on"), mk(direction="off")
    ok("开启方向：已暂停 → 要点",
       on._classify(row("1", "a", "已暂停"))[0] == "toggle")
    ok("开启方向：投放中 → 已达标",
       on._classify(row("1", "a", "投放中"))[0] == "done")
    ok("关闭方向：投放中 → 要点",
       off._classify(row("1", "a", "投放中"))[0] == "toggle")
    ok("关闭方向：已暂停 → 已达标",
       off._classify(row("1", "a", "已暂停"))[0] == "done")
    for st in ("未开始", "已完成", "已终止"):
        act, kind, why = on._classify(row("1", "a", st))
        ok(f"{st} → 跳过并说清原因", act == "block" and st in kind and "既没有启动也没有暂停" in why, why)
    act, _, why = on._classify(row("1", "a", "外星状态"))
    ok("没见过的状态 → 不敢动", act == "block" and "没见过" in why, why)


def test_pick():
    print("\n[按范围选行]")
    rows = [row("41031", "测试0831v2", "已暂停"),
            row("41032", "测试0831v4", "投放中"),
            row("41140", "zf测试-影视小黄条-在期", "未开始")]

    r = mk(params="小黄条")
    got, why = r._pick(rows)
    ok("keyword 命中名称子串", [x["id"] for x in got] == ["41140"], got)

    got, why = mk(params="")._pick(rows)
    ok("keyword 留空 → 整批", len(got) == 3 and why == "")

    got, why = mk(params="不存在的词")._pick(rows)
    ok("keyword 没命中 → 空 + 原因", got == [] and "一个都没命中" in why, why)

    got, why = mk(scope="list", params="41031,41032")._pick(rows)
    ok("list 按 ID 精确匹配", [x["id"] for x in got] == ["41031", "41032"], got)

    got, why = mk(scope="list", params="41031")._pick(rows)
    ok("list 的 ID 不做子串（41031 不会带出 410311 那类）",
       [x["id"] for x in got] == ["41031"], got)

    got, why = mk(scope="list", params="影视小黄条")._pick(rows)
    ok("list 里的非数字当名称子串", [x["id"] for x in got] == ["41140"], got)

    got, why = mk(scope="list", params="")._pick(rows)
    ok("list 没填 → 空 + 提示", got == [] and "一个 ID / 名称都没填" in why, why)


def test_activity_required():
    print("\n[活动ID 留空怎么办]")
    r = mk(activity="")
    try:
        r._collect(None)
        ok("没填活动ID、也没给ID清单 → 要报错", False)
    except ValueError as e:
        ok("没填活动ID、也没给ID清单 → 说清两条出路",
           "活动ID" in str(e) and "按清单" in str(e), e)
    except Exception as e:                      # 别的异常说明报错路径走歪了
        ok("没填活动ID、也没给ID清单 → 说清两条出路", False, repr(e))


def test_id_list():
    print("\n[逐个 ID 查]")
    r = mk(activity="", scope="list", params="138073\n138074, 138073\n某创意")
    ok("活动ID 留空 + 按清单 → 取纯数字、去重、保序",
       r._id_list() == ["138073", "138074"], r._id_list())
    ok("填了活动ID → 不走逐个查",
       mk(activity="708", scope="list", params="138073")._id_list() == [])
    ok("范围不是按清单 → 不走逐个查",
       mk(activity="", scope="keyword", params="138073")._id_list() == [])
    ok("清单里一个数字都没有 → 不走逐个查",
       mk(activity="", scope="list", params="小黄条")._id_list() == [])
    act, kind, why = mk(activity="", scope="list", params="1")._classify(
        {"id": "1", "missing": "列表里查不到这个创意ID"})
    ok("查不到的 ID → 单独标出来、不静默少一条",
       act == "block" and kind == "没找到" and "查不到" in why, (act, kind, why))


def test_result_shape():
    print("\n[结果行]")
    r = mk(direction="off", level="creative")
    res = r._res({"id": "138073", "name": "测试测试"}, "ok", "", "将暂停投放")
    ok("结果行带层级/活动/ID/名称",
       res["层级"] == "创意" and res["活动ID"] == "708"
       and res["创意ID"] == "138073" and res["名称"] == "测试测试", res)
    ok("方向写的是人话", res["方向"] == "暂停投放", res)


def main():
    print("=" * 56)
    print("dl_runner 纯逻辑测试")
    print("=" * 56)
    try:
        for fn in (test_direction_and_level, test_tokens_and_scope, test_classify,
                   test_pick, test_activity_required, test_id_list,
                   test_result_shape):
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
