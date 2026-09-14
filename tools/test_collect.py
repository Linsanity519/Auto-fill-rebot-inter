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


def _fake_cli(stdout: dict, returncode: int):
    """把 sheet_query 里的 subprocess.run / shutil.which 换成假的，返回还原函数。"""
    import json
    import shutil
    import subprocess

    o_run, o_which = subprocess.run, shutil.which

    class R:
        pass

    def fake_run(*a, **kw):
        r = R()
        r.returncode = returncode
        r.stdout = json.dumps(stdout, ensure_ascii=False).encode("utf-8")
        r.stderr = b""
        return r

    subprocess.run, shutil.which = fake_run, (lambda name: "wecom-cli")

    def restore():
        subprocess.run, shutil.which = o_run, o_which
    return restore


EXPIRED = {
    "errcode": 850003, "errmsg": "authorization expired, hint: [1]",
    "help_message": "当前机器人「文档」使用权限已过期\\n若你是智能机器人创建者，可以"
                    "[点击这里](https://work.weixin.qq.com/ai/aiHelper/authorizationList"
                    "?aibotid=123&type=1)授权当前机器人文档使用权限；",
}


def test_auth_expired():
    print("\n[授权过期] 退出码非 0、原因在 stdout 里，也要认出来并抠出链接")
    restore = _fake_cli(EXPIRED, 1)
    try:
        try:
            C.sheet_query("s3_x", "SELECT 1")
            check("过期要抛 DocAuthExpired", False, "没抛")
        except C.DocAuthExpired as e:
            check("过期要抛 DocAuthExpired", True)
            check("链接抠出来了", e.link == "https://work.weixin.qq.com/ai/aiHelper/"
                  "authorizationList?aibotid=123&type=1", e.link)
        except Exception as e:
            check("过期要抛 DocAuthExpired", False, f"抛的是 {type(e).__name__}：{e}")
    finally:
        restore()

    restore = _fake_cli({"errcode": 40058, "errmsg": "bad sql"}, 1)
    try:
        try:
            C.sheet_query("s3_x", "SELECT 1")
            check("别的错误照常抛", False, "没抛")
        except C.DocAuthExpired:
            check("别的错误不能当成授权过期", False)
        except RuntimeError as e:
            check("别的错误照常抛，原因不再只剩「执行失败」", "40058" in str(e), str(e))
    finally:
        restore()

    restore = _fake_cli({"errcode": 0, "values": ['{"rows":[{"运行ID":"a"}]}']}, 0)
    try:
        check("正常时照常返回 rows", C.sheet_query("s3_x", "SELECT 1") == [{"运行ID": "a"}])
    finally:
        restore()


def test_auth_check():
    print("\n[授权探测] 过期才发群、一天一句、没发出去不落标记、恢复后清标记")
    import shutil
    import tempfile

    import check_doc_auth as A

    tmp = Path(tempfile.mkdtemp(prefix="doc-auth-"))
    mark = tmp / "mark.txt"
    sent = []
    ok_send = lambda t: sent.append(t) or True   # noqa: E731
    expired = lambda d: ("expired", "https://work.weixin.qq.com/x")   # noqa: E731
    try:
        code, _ = A.run_check("s3_x", ok_send, mark=mark, today="2026-09-14",
                              do_probe=lambda d: ("ok", ""))
        check("正常：退出码 0、不发群", code == 0 and not sent)

        code, msg = A.run_check("s3_x", ok_send, mark=mark, today="2026-09-14",
                                do_probe=lambda d: ("error", "找不到 wecom-cli"))
        check("CLI/网络问题：退出码 1、不发群", code == 1 and not sent, msg)

        code, _ = A.run_check("", ok_send, mark=mark, do_probe=expired)
        check("没配表 ID：退出码 1、不发群", code == 1 and not sent)

        code, _ = A.run_check("s3_x", lambda t: False, mark=mark, today="2026-09-14",
                              do_probe=expired)
        check("群没发出去：不落标记（下次还会再试）", code == 2 and not mark.exists())

        code, _ = A.run_check("s3_x", ok_send, mark=mark, today="2026-09-14",
                              do_probe=expired, dry=True)
        check("--dry：不发群", code == 2 and not sent)

        code, _ = A.run_check("s3_x", ok_send, mark=mark, today="2026-09-14", do_probe=expired)
        check("过期：发了一句，带链接", code == 2 and len(sent) == 1
              and "https://work.weixin.qq.com/x" in sent[0], str(sent))
        A.run_check("s3_x", ok_send, mark=mark, today="2026-09-14", do_probe=expired)
        check("同一天再跑不重复发", len(sent) == 1, f"发了 {len(sent)} 句")
        A.run_check("s3_x", ok_send, mark=mark, today="2026-09-15", do_probe=expired)
        check("第二天还过期，再提醒一次", len(sent) == 2, f"发了 {len(sent)} 句")
        check("群消息里不带表 ID", all("s3_x" not in t for t in sent))

        A.run_check("s3_x", ok_send, mark=mark, today="2026-09-15",
                    do_probe=lambda d: ("ok", ""))
        check("授权恢复后清掉标记", not mark.exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    for fn in (test_basic, test_excluded, test_dupes_are_caller_job,
               test_legacy, test_shape, test_docid, test_auth_expired, test_auth_check):
        fn()
    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
