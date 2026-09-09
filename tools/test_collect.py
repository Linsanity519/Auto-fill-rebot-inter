"""收集端离线自测：build_team 的口径。不连网、不碰表格。

    python tools\test_collect.py

⚠ 它测的是「表里的行 → 首页那份 team.json」这一步的算法。
  SQL 对不对、表结构变没变，只有 `python tools\collect_usage.py --dry` 能验。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import collect_usage as C          # noqa: E402
from src import usage              # noqa: E402

PASS = FAIL = 0

CONF = usage.saving_conf({"usage": {"saving": {
    "mode": "baseline", "default_seconds": 60,
    "per_item_seconds": {"常规商广": 600, "资源位投放": 420, "预定会议室": 0}}}})


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}" + (f"　{detail}" if detail else ""))


def run(rid, day, uid, form, mode="全自动", ok=1, bad=0, skip=0, sec=100,
        wait=0, retry=""):
    return {"run": rid, "日": day, "指纹": uid, "版本": "1.1.15", "类型": form,
            "模式": mode, "重跑": retry, "成": ok, "败": bad, "跳": skip,
            "机器秒": sec, "等人秒": wait}


def test_basic():
    print("\n[口径] 一次运行一行 → 首页那份数")
    runs = [
        run("a", "2026-09-09", "u1", "常规商广", ok=8, bad=1, skip=5, sec=612),
        run("b", "2026-09-09", "u2", "资源位投放", ok=9, sec=410),
    ]
    t = C.build_team(runs, [], CONF)
    check("人数按指纹去重", t["people"] == 2, str(t["people"]))
    check("条数只算成功的", t["totals"]["items"] == 17, str(t["totals"]))
    check("失败单独计", t["totals"]["failed"] == 1, str(t["totals"]))
    check("机器秒照实加", t["totals"]["seconds"] == 1022, str(t["totals"]))
    # 8×600 + 9×420 = 4800 + 3780
    check("省时按各类型的人工基准算",
          t["totals"]["saved"] == 8580.0, str(t["totals"]["saved"]))
    check("省下的就是人工要花的（不减机器实跑）",
          t["totals"]["saved"] == t["totals"]["human"], str(t["totals"]))
    check("一次做对率 = 成 /（成+败）",
          abs(t["totals"]["ok_rate"] - 17 / 18) < 1e-9, str(t["totals"]["ok_rate"]))
    check("分类型各自一行",
          [(f["name"], f["ok"]) for f in t["forms"]] == [("资源位投放", 9), ("常规商广", 8)],
          str(t["forms"]))


def test_excluded():
    print("\n[不进累计] 空跑和重跑")
    runs = [
        run("a", "2026-09-09", "u1", "常规商广", ok=8, sec=600),
        run("dry", "2026-09-09", "u1", "常规商广", mode="空跑", ok=5, sec=60),
        run("re", "2026-09-09", "u1", "常规商广", ok=3, sec=40, retry="a"),
    ]
    t = C.build_team(runs, [], CONF)
    check("空跑不进累计", t["totals"]["items"] == 8, str(t["totals"]))
    check("重跑不进累计", t["totals"]["runs"] == 1, str(t["totals"]))


def test_dupes_are_caller_job():
    """去重是 collect_from_sheet 干的，build_team 只管算 —— 这里把边界写清楚。

    ⚠ 表格 webhook 没有幂等键：客户端读超时（服务端其实写进去了）后补发，
      同一次运行会留下两行。真发生过。
    """
    print("\n[去重] 同一个运行ID 出现两次时，谁负责")
    runs = [run("a", "2026-09-09", "u1", "常规商广", ok=8, sec=600)] * 2
    t = C.build_team(runs, [], CONF)
    check("build_team 不自己去重（重复行会被算两遍）",
          t["totals"]["items"] == 16, str(t["totals"]))
    check("所以 collect_from_sheet 必须先按运行ID去重", True)


def test_legacy():
    print("\n[老数据] 1.1.15 之前的周累计，粒度不同，另算")
    legacy = [{"周": "2026-08-17", "指纹": "u1", "版本": "1.0.18", "次数": 7,
               "成功": 38, "失败": 1, "机器秒": 624, "最后活跃": "2026-08-24 11:35",
               "分类型": '{"常规商广": 38}'}]
    t = C.build_team([], legacy, CONF)
    check("次数用「次数」那一列，不是行数", t["totals"]["runs"] == 7, str(t["totals"]))
    check("条数用「成功」那一列", t["totals"]["items"] == 38, str(t["totals"]))
    check("省时按分类型的基准算", t["totals"]["saved"] == 38 * 600, str(t["totals"]))
    check("周按「周」那一列归",
          list(t["weeks"]) == ["2026-08-17"], str(t["weeks"]))

    # ⚠ 老归档的分类型是逐键取最大值合出来的，加起来可能比「成功」还大（实测 22→34）。
    #   总数必须以「成功」为准，否则首页那个「累计处理」会莫名其妙变多。
    bad = [{"周": "2026-08-31", "指纹": "u1", "次数": 10, "成功": 22, "失败": 8,
            "机器秒": 284, "最后活跃": "2026-09-04 20:01",
            "分类型": '{"AB实验延期": 22, "常规资源位批量开关": 12}'}]
    t2 = C.build_team([], bad, CONF)
    check("分类型加起来比总数大时，总数仍以「成功」为准",
          t2["totals"]["items"] == 22, str(t2["totals"]["items"]))


def test_shape():
    print("\n[形状] 必须和 usage.parse_report 的返回值同形（首页直接读它）")
    t = C.build_team([run("a", "2026-09-09", "u1", "常规商广")], [], CONF)
    for k in ("people", "totals", "forms", "weeks", "actives"):
        check(f"有 {k}", k in t)
    for k in ("runs", "items", "failed", "seconds", "human", "saved", "ok_rate"):
        check(f"totals 有 {k}", k in t["totals"])
    w = list(t["weeks"].values())[0]
    check("weeks 的每一项有 items/seconds/saved",
          {"items", "seconds", "saved"} <= set(w), str(w))
    a = t["actives"][0]
    check("actives 的每一项有 uid/last/items/runs",
          {"uid", "name", "last", "items", "runs"} <= set(a), str(a))


def test_docid():
    print("\n[表 ID] 直接粘链接也要认得出来")
    import os
    old = os.environ.get("STATS_DOCID")
    try:
        os.environ["STATS_DOCID"] = "https://doc.weixin.qq.com/smartsheet/s3_ABC123?scode=xx"
        check("从链接里抠得出来", C.stats_docid() == "s3_ABC123", C.stats_docid())
        os.environ["STATS_DOCID"] = "s3_ABC123"
        check("直接给 ID 也行", C.stats_docid() == "s3_ABC123", C.stats_docid())
    finally:
        if old is None:
            os.environ.pop("STATS_DOCID", None)
        else:
            os.environ["STATS_DOCID"] = old


def main() -> int:
    for fn in (test_basic, test_excluded, test_dupes_are_caller_job,
               test_legacy, test_shape, test_docid):
        fn()
    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
