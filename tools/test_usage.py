"""埋点 + 统计口径的场景测试。改 src/usage.py 之后跑一遍：

    python tools\\test_usage.py

不联网、不碰浏览器、不写用户的 output/ —— 全在临时目录里跑完就删。
加新口径时请在这里补一条场景，别只在脑子里验。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src import usage  # noqa: E402

# 埋点写失败时会 log.warning(exc_info=True)，那是预期行为，别让异常栈刷满测试输出
import logging  # noqa: E402
logging.disable(logging.CRITICAL)

# ⚠ 下面的口径测试会把 read_events 打桩。文件相关的测试必须先还原，
#   否则读到的是上一个测试留下的假数据（踩过一次，测试自己骗了自己）
_REAL_READ_EVENTS = usage.read_events

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"    {detail}" if detail and not cond else ""))


def run(ts, uid, form, ok, failed=0, skipped=0, dry=0, total=None, seconds=60,
        wait=0, mode="auto", retry_of=None, stopped=None):
    r = {"ts": ts, "event": "run_finished", "uid": uid, "form": form, "mode": mode,
         "ok": ok, "failed": failed, "skipped": skipped, "dry": dry,
         "total": total if total is not None else ok + failed + skipped + dry,
         "seconds": seconds, "wait_seconds": wait}
    if retry_of:
        r["retry_of"] = retry_of
    if stopped:
        r["stopped"] = stopped
    return r


def with_events(rows):
    usage.read_events = lambda settings: rows
    return usage.summarize({})


# ============================================================ 口径
def test_empty():
    print("\n[空数据] 第一次打开，什么都没有")
    s = with_events([])
    check("不崩", isinstance(s, dict))
    check("人数为 0", s["people"] == 0)
    check("累计为 0", s["totals"]["items"] == 0)
    check("成功率是 None 不是 0", s["totals"]["ok_rate"] is None)
    check("周趋势仍然铺满 12 周", len(s["weeks"]) == 12)


def test_single_user():
    print("\n[单人] 只有自己")
    me = usage._uid()
    s = with_events([run("2026-08-20T10:00:00+08:00", me, "DMP延期", ok=9, failed=1, seconds=100, wait=10)])
    check("跑过的人数 = 1", s["people"] == 1)
    check("成功 9 条", s["totals"]["items"] == 9)
    check("机器代劳扣掉等人（100-10）", s["totals"]["seconds"] == 90.0)
    check("成功率 9/10", abs(s["totals"]["ok_rate"] - 0.9) < 1e-9)
    check("单人时「我的」和「全部」是同一份", s["mine"]["totals"] == s["totals"])


def test_multi_user():
    print("\n[多人] 我的战绩不能和全团队混在一起")
    me = usage._uid()
    s = with_events([
        run("2026-08-19T10:00:00+08:00", "OTHER1", "DMP延期", ok=100, seconds=1000),
        run("2026-08-20T10:00:00+08:00", me, "价格配置", ok=3, seconds=30),
        {"ts": "2026-08-18T09:00:00+08:00", "event": "app_open", "uid": "LURKER"},
    ])
    check("跑过的只算 2 人", s["people"] == 2, f"实际 {s['people']}")
    check("打开过的算 3 人", s["people_opened"] == 3)
    check("全团队 103 条", s["totals"]["items"] == 103)
    check("我自己只有 3 条", s["mine"]["totals"]["items"] == 3)
    check("我的耗时也只算自己的", s["mine"]["totals"]["seconds"] == 30.0)


def test_excluded():
    print("\n[不该计入的] 重跑 / 空跑")
    me = usage._uid()
    s = with_events([
        run("2026-08-20T10:00:00+08:00", me, "DMP延期", ok=10, seconds=100),
        run("2026-08-20T11:00:00+08:00", me, "DMP延期", ok=2, seconds=20, retry_of="abc"),
        run("2026-08-20T12:00:00+08:00", me, "DMP延期", ok=0, dry=5, seconds=50, mode="dry"),
    ])
    check("重跑不进累计条数", s["totals"]["items"] == 10, f"实际 {s['totals']['items']}")
    check("重跑不进累计耗时", s["totals"]["seconds"] == 100.0)
    check("重跑单独计数", s["retries"] == 1)
    check("空跑单独计数", s["dry_runs"] == 1)
    check("只算一次真实运行", s["totals"]["runs"] == 1)


def test_stopped():
    print("\n[中途停止] 剩下没跑的不能算失败")
    me = usage._uid()
    s = with_events([run("2026-08-20T10:00:00+08:00", me, "资源位投放",
                         ok=3, failed=0, total=20, seconds=60, stopped=True)])
    check("成功率不被没跑的拖累", s["totals"]["ok_rate"] == 1.0)
    check("attempted 记的是这批总数", s["totals"]["attempted"] == 20)


def test_dirty():
    print("\n[脏数据] 一条歪的不能干掉整个主页")
    me = usage._uid()
    s = with_events([
        run("2026-08-20T10:00:00+08:00", me, "DMP延期", ok=5, seconds=50),
        {"ts": "2026-08-20T11:00:00+08:00", "event": "run_finished", "uid": "X",
         "form": None, "ok": None, "total": "十条", "seconds": "abc", "mode": "auto"},
        {"event": "run_finished"},                 # 连时间都没有
        "这一行根本不是字典",
        None,
    ])
    check("不崩", isinstance(s, dict))
    check("好数据照常统计", s["totals"]["items"] == 5, f"实际 {s['totals']['items']}")
    check("坏数据按 0 计，不污染总数", s["totals"]["seconds"] == 50.0)
    check("form 为空归到「(未知)」", any(f["name"] == "(未知)" for f in s["forms"]))


def test_week_boundary():
    print("\n[跨周] 本周的数只算本周")
    from datetime import datetime, timedelta
    me = usage._uid()
    now = datetime.now().astimezone()
    last_week = now - timedelta(days=8)
    s = with_events([
        run(now.isoformat(timespec="seconds"), me, "DMP延期", ok=3, seconds=30),
        run(last_week.isoformat(timespec="seconds"), me, "DMP延期", ok=99, seconds=990),
    ])
    check("本周只有 3 条", s["week"]["items"] == 3, f"实际 {s['week']['items']}")
    check("累计是两周之和", s["totals"]["items"] == 102)


# ============================================================ 分类器
def test_fail_kinds():
    print("\n[失败分类] 只输出枚举，错误原文不外泄")
    cases = [
        ("Timeout 30000ms exceeded", "timeout"),
        ("「人群名称」搜「新客拉新包」等了 8 秒没返回任何选项。", "timeout"),
        ("单元层 填「生效平台」失败：按 label 找不到表单项", "selector_miss"),
        ("点了确定但弹窗没关闭，提交被拒。页面报错：['日期不合法']", "page_rejected"),
        ("计划层 必填字段「计划名称」没有值", "empty_required"),
        ("Target closed", "browser_lost"),
        ("八竿子打不着的错", "other"),
    ]
    for text, want in cases:
        got = usage._fail_kind(text)
        check(f"{want:14s} ← {text[:26]}", got == want, f"得到 {got}")

    res = [{"状态": "failed", "错误": "创意 上传素材 Timeout 30000ms exceeded"},
           {"状态": "failed", "错误": "计划层 必填字段「计划名称」没有值"},
           {"状态": "ok", "错误": ""}]
    d = usage.fail_detail(res)
    check("只统计 failed 的", sum(d["fail_kinds"].values()) == 2)
    check("认出卡在哪一层", d["fail_stages"] == {"创意": 1, "计划层": 1}, str(d))
    blob = json.dumps(d, ensure_ascii=False)
    check("输出里没有错误原文", "Timeout" not in blob and "计划名称" not in blob, blob)
    check("没有失败时不占字段", usage.fail_detail([{"状态": "ok"}]) == {})


def test_bad_fields():
    print("\n[校验失败列名] 只出列名，用户填的值不能带出来")
    Row = types.SimpleNamespace
    rows = [
        Row(issues=["人群ID：必填但为空",
                    "卡种：「年度大会员VIP尊享版」不是有效值（可选：连续包年、连续包月…）"]),
        Row(issues=["人群ID：必填但为空"]),
        Row(issues=["第3项-限制类型：必填但为空", "没有任何明细项"]),
        Row(issues=[]),
    ]
    out = usage.bad_fields(rows)
    blob = json.dumps(out, ensure_ascii=False)
    check("按列名合并计数", out.get("人群ID") == 2, blob)
    check("明细项去掉「第N项-」前缀", out.get("限制类型") == 1, blob)
    check("没有冒号的归到 (其他)", out.get("(其他)") == 1, blob)
    check("用户填的值没泄漏", "年度大会员VIP尊享版" not in blob, blob)
    check("可选值清单也没泄漏", "连续包年" not in blob, blob)


def test_percentiles():
    print("\n[单条耗时分位]")
    check("空输入不占字段", usage.percentiles([]) == {})
    p = usage.percentiles([1, 2, 3, 4, 5, 6, 7, 8, 9, 100])
    check("p50 在中间", 5 <= p["item_p50"] <= 6, str(p))
    check("p90 抓得住长尾", p["item_p90"] >= 9, str(p))


def test_status_alias():
    print("\n[状态归一]")
    c = usage.count_status([{"状态": "dry_run"}, {"状态": "not_extendable"},
                            {"状态": "ok"}, {"状态": "failed"}, {"状态": "莫名其妙"}])
    check("dry_run → dry", c["dry"] == 1, str(c))
    check("not_extendable → skipped（不算失败）", c["skipped"] == 1 and c["failed"] == 1, str(c))


# ============================================================ 写入
def test_write_and_switch():
    print("\n[写入] 落盘 / 开关 / 目录不可写都不能挡业务")
    tmp = Path(tempfile.mkdtemp(prefix="usage-test-"))
    orig = usage.local_path
    usage.local_path = lambda: tmp / "usage.jsonl"
    try:
        s = {"usage": {"enabled": True}}
        usage.record(s, "app_open", entry="test")
        usage.record(s, "run_finished", form="X", ok=1, total=1, seconds=1.0, entry="test")
        lines = (tmp / "usage.jsonl").read_text(encoding="utf-8").strip().split("\n")
        check("两条都落了", len(lines) == 2)
        row = json.loads(lines[1])
        check("带匿名指纹", len(row.get("uid", "")) == 8)
        check("带版本号", bool(row.get("ver")))
        check("None 值不落盘", "scope" not in row)

        n = len(lines)
        usage.record({"usage": {"enabled": False}}, "app_open")
        after = len((tmp / "usage.jsonl").read_text(encoding="utf-8").strip().split("\n"))
        check("开关关掉就不写", after == n)

        usage.local_path = lambda: Path("Z:/根本不存在的盘/usage.jsonl")
        usage.record(s, "app_open")            # 不能抛
        check("写不进去也不抛异常", True)
    finally:
        usage.local_path = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_share_dedupe():
    print("\n[汇总去重] 自己的数据在本机和汇总目录各有一份，不能算两遍")
    usage.read_events = _REAL_READ_EVENTS      # 还原被口径测试打的桩
    tmp = Path(tempfile.mkdtemp(prefix="usage-share-"))
    orig = usage.local_path
    usage.local_path = lambda: tmp / "local.jsonl"
    try:
        me = usage._uid()
        mine = run("2026-08-20T10:00:00+08:00", me, "DMP延期", ok=5, seconds=50)
        other = run("2026-08-20T11:00:00+08:00", "OTHER", "价格配置", ok=7, seconds=70)
        share = tmp / "share"
        share.mkdir()
        (share / f"{me}.jsonl").write_text(json.dumps(mine, ensure_ascii=False) + "\n", encoding="utf-8")
        (share / "OTHER.jsonl").write_text(json.dumps(other, ensure_ascii=False) + "\n", encoding="utf-8")
        (tmp / "local.jsonl").write_text(json.dumps(mine, ensure_ascii=False) + "\n", encoding="utf-8")

        evs = usage.read_events({"usage": {"share_dir": str(share)}})
        check("自己那份只读一次", len(evs) == 2, f"读到 {len(evs)} 条")
        check("别人的读到了", any(e.get("uid") == "OTHER" for e in evs))
    finally:
        usage.local_path = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_broken_file():
    print("\n[半行/乱码文件] 跳过坏行，不丢好行")
    usage.read_events = _REAL_READ_EVENTS      # 还原被口径测试打的桩
    tmp = Path(tempfile.mkdtemp(prefix="usage-broken-"))
    orig = usage.local_path
    usage.local_path = lambda: tmp / "u.jsonl"
    try:
        good = json.dumps(run("2026-08-20T10:00:00+08:00", usage._uid(), "DMP延期", ok=5),
                          ensure_ascii=False)
        (tmp / "u.jsonl").write_text(
            good + "\n{\"半行\": \n\n乱码乱码\n" + good + "\n", encoding="utf-8")
        evs = usage.read_events({})
        check("两条好的都读到", len(evs) == 2, f"读到 {len(evs)}")
    finally:
        usage.local_path = orig
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================ 上报到企微表格
# ⚠ 1.1.16 删掉了三段测试：test_report_roundtrip / test_webhook_payload /
#   test_report_header_mismatch。它们测的是「每周累计 + 上报记账 + 表头契约」，
#   那套机制本身已经删了（回传改成一次运行一条）。老格式的**解析**还留着，
#   由 tools/test_collect.py 那边和 collect_usage --clipboard 负责。

def test_saving():
    print("\n[省时口径] 省下 = 人工基准 × 条数（不减机器实跑）")
    conf = usage.saving_conf({"usage": {"saving": {
        "mode": "baseline", "default_seconds": 60,
        "per_item_seconds": {"资源位投放": 480, "预定会议室": 0}}}})
    check("按配置类型取各自的基准", usage.human_seconds(conf, "资源位投放", 10) == 4800)
    check("没列出来的用兜底基准", usage.human_seconds(conf, "价格配置", 10) == 600)
    check("基准填 0 = 不按时长算价值", usage.human_seconds(conf, "预定会议室", 10) == 0)
    # ⚠ 2026-09-09 口径：省下的就是人工要花的，机器实跑不参与相减。
    check("省下的 = 人工要花的", usage.saved_seconds(conf, "资源位投放", 10, 600) == 4800)
    check("机器实跑再长也不从省下里扣",
          usage.saved_seconds(conf, "资源位投放", 10, 99999) == 4800)
    check("基准 0 的类型仍然是 0",
          usage.saved_seconds(conf, "预定会议室", 10, 600) == 0)

    mult = usage.saving_conf({"usage": {"saving": {"mode": "multiplier", "multiplier": 3}}})
    check("倍数口径：人工 = 机器 × 倍数", usage.human_seconds(mult, "任意", 5, 100) == 300)
    check("倍数口径：省下 = 人工 = 机器 × 倍数", usage.saved_seconds(mult, "任意", 5, 100) == 300)

    # ⚠ 回归：倍数口径下 parse_report 曾经在配置类型上循环，把机器耗时乘了 N 遍
    #   （七个配置类型 → 624 秒算成 13104 秒）。它只在团队快照里出现，本机页面看不到，
    #   所以特别容易漏。
    FORMS7 = [f"类型{i}" for i in range(7)]
    header7 = usage.report_header(FORMS7)
    row7 = ["2026-08-17", "abc", "", "1.0", 1, 38, 1, 624] + [38] + [0] * 6 + ["", ""]
    got7 = usage.parse_report([header7] + [row7], FORMS7, mult)
    check("倍数口径下团队汇总不会按配置类型数翻倍",
          got7["totals"]["human"] == 624 * 3 and got7["totals"]["saved"] == 624 * 3,
          f"human={got7['totals']['human']} saved={got7['totals']['saved']}（应为 1872 / 1872）")
    check("省时按条数摊到各配置类型",
          [f["saved"] for f in got7["forms"] if f["ok"]] == [624 * 3],
          str([(f["name"], f["saved"]) for f in got7["forms"] if f["ok"]]))

    # 聚合层：两个配置类型各按各的基准算，不能拿总条数乘一个数
    from datetime import datetime, timedelta
    now = datetime.now().astimezone()
    runs = [{"ts": now.isoformat(timespec="seconds"), "event": "run_finished", "uid": "x",
             "form": "资源位投放", "ok": 10, "failed": 0, "skipped": 0, "total": 10,
             "seconds": 600, "wait_seconds": 0},
            {"ts": now.isoformat(timespec="seconds"), "event": "run_finished", "uid": "x",
             "form": "价格配置", "ok": 10, "failed": 0, "skipped": 0, "total": 10,
             "seconds": 100, "wait_seconds": 0}]
    agg = usage._aggregate(runs, 4, conf)
    check("聚合按配置类型分别算", agg["totals"]["saved"] == 4800 + 600,
          str(agg["totals"]))
    check("机器实跑还是照实记", agg["totals"]["seconds"] == 700, str(agg["totals"]))


def test_week_key_normalize():
    print("\n[日期归一] 表格会把 2026-08-10 显示成 2026/8/10")
    check("斜杠转横杠补零", usage.norm_week("2026/8/10") == "2026-08-10",
          usage.norm_week("2026/8/10"))
    check("本来就规范的不动", usage.norm_week("2026-08-10") == "2026-08-10")
    check("空值不炸", usage.norm_week(None) == "" and usage.norm_week("") == "")
    check("不是日期的原样返回", usage.norm_week("第34周") == "第34周")

    FORMS = ["DMP延期"]
    header = usage.report_header(FORMS)
    # 同一周，一行是刚写进去的格式，一行是表格转换后的格式 —— 必须并成一周
    r1 = ["2026-08-10", "aaa", "甲", "1.0.0", 1, 5, 0, 60, 5, "2026-08-12 10:00", "x"]
    r2 = ["2026/8/10", "bbb", "乙", "1.0.0", 1, 7, 0, 70, 7, "2026-08-13 10:00", "x"]
    got = usage.parse_report([header, r1, r2], FORMS)
    check("两种写法归到同一周", list(got["weeks"].keys()) == ["2026-08-10"], str(got["weeks"]))
    check("同周条数相加", got["weeks"]["2026-08-10"]["items"] == 12, str(got["weeks"]))


def _from_line(header, form_names, line):
    """收集端那一步：一条 webhook 单行 JSON → parse_report 认识的行。

    ⚠ 这是 tools/collect_usage.py 里同一套还原逻辑的测试替身。改了那边记得改这里。
    ⚠ 「周」不在消息里（1.0.20 起不发了），从「最后活跃」反推 —— 和收集端
      tools/collect_usage.py 的 _week 一个口径。
    """
    d = json.loads(line)
    forms = d.get("分类型") or {}
    wk = usage.norm_week(d.get("周")) or usage.week_of(str(d.get("最后活跃") or ""))
    return ([wk, d.get("指纹", ""), d.get("花名", ""), d.get("版本", ""),
             d.get("次数", 0), d.get("成功", 0), d.get("失败", 0), d.get("机器秒", 0)]
            + [forms.get(n, 0) for n in form_names]
            + [d.get("最后活跃", ""), ""])


def test_webhook_migration():
    print("\n[换群迁移] 存量机器的 webhook.txt 还是旧 key 时，自动改用新地址")
    from src import report
    tmp = Path(tempfile.mkdtemp(prefix="usage-webhook-"))
    orig = report.user_path
    report.user_path = lambda *parts: tmp.joinpath(*parts)
    try:
        cfg = tmp / "config"
        cfg.mkdir()
        hook = cfg / "webhook.txt"
        old = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=53d90b8b-f8f8-4c02-83bc-52ec1369ac29"
        new = report.BUNDLED_WEBHOOK

        hook.write_text("# 注释\n" + old + "\n", encoding="utf-8")
        check("旧 key → 换成随包发的新地址", report._webhook_from_file() == new,
              report._webhook_from_file())
        check("feedback 兜底也跟着换", report.feedback_webhook_url({}) == new)

        other = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=99999999-0000-0000-0000-000000000000"
        hook.write_text(other + "\n", encoding="utf-8")
        check("别人自己配的 key 不动", report._webhook_from_file() == other,
              report._webhook_from_file())

        hook.write_text(new + "\n", encoding="utf-8")
        check("已经是新 key 就原样返回", report._webhook_from_file() == new)

        hook.unlink()
        check("文件缺失仍然静默不上报（clone 打的包）",
              report._webhook_from_file() == "")

        check("settings.yaml 显式配的最优先",
              report.webhook_url({"usage": {"webhook_url": other}}) == other)
    finally:
        report.user_path = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_outbox():
    """1.1.14 起回传发的是「这一次运行」，不是「这一周的累计」。

    这一段盯死四件事：发的内容对不对、发不出去会不会丢、发成功了会不会重发、
    两个通道（企微群 / 智能表格）各记各的账。
    """
    from src import report

    print("\n[回传] 一次运行一条 + 发件箱（发出去才划掉）")
    usage.read_events = _REAL_READ_EVENTS
    tmp = Path(tempfile.mkdtemp(prefix="usage-outbox-"))
    o_local, o_outbox = usage.local_path, report.outbox_path
    o_post, o_pj, o_sheet = report._post, report._post_json, report.sheet_webhook_url
    usage.local_path = lambda: tmp / "usage.jsonl"
    report.outbox_path = lambda: tmp / "outbox.jsonl"
    report.sheet_webhook_url = lambda s: ""          # 默认只测群通道，别打到真表上
    S = {"usage": {"webhook_url": "https://example.invalid/hook"}}
    try:
        row = usage.record(
            S, "run_finished", run_id="r1", form="常规商广", mode="auto", scope="unit",
            total=14, ok=8, failed=1, skipped=5, seconds=700.0, wait_seconds=88.0,
            fail_kinds={"selector_miss": 1})
        check("record 会把落盘那一行还回来", isinstance(row, dict) and row.get("run_id") == "r1")

        d = report.run_payload(row)
        check("发的是这一次运行，不是累计",
              d["run"] == "r1" and d["类型"] == "常规商广" and d["总"] == 14
              and d["成"] == 8 and d["败"] == 1 and d["跳"] == 5, str(d))
        check("时间 / 版本 / 模式都在",
              bool(d["时间"]) and bool(d["版本"]) and d["模式"] == "全自动", str(d))
        # ⚠ 机器秒是净时长：700 − 88。混成一个数就再也分不开「机器在跑」和「人在看」
        check("机器秒扣掉了等人确认的时间", d["机器秒"] == 612 and d["等人秒"] == 88, str(d))
        check("失败明细带上了", d.get("失败明细") == {"selector_miss": 1}, str(d))
        check("标了来源是实时", d.get("来源") == "实时", str(d))
        check("一条消息不会超企微上限",
              len(json.dumps(d, ensure_ascii=False).encode("utf-8")) < 1800)

        report.enqueue(S, row)
        check("进了发件箱", report.pending(S) == 1)

        sent = []

        def boom(url, text):
            raise RuntimeError("内网抽风")

        report._post = boom
        res = report.push(S)
        check("发失败时如实回报", res["sent"] == 0 and res["failed"] == 1)
        check("发失败不丢数据，下次还在", report.pending(S) == 1)

        usage.record(S, "run_finished", run_id="r2", form="DMP延期", mode="dry",
                     total=3, ok=3, seconds=30.0, wait_seconds=0)
        report.enqueue(S, usage.record(S, "run_finished", run_id="r3", form="预定会议室",
                                       mode="auto", total=1, ok=0, failed=1,
                                       seconds=600.0, wait_seconds=0))
        check("没交给 enqueue 的那条不会自己进队", report.pending(S) == 2)

        report._post = lambda url, text: sent.append(text) or True
        res = report.push(S)
        check("连上之后一次补齐", res["sent"] == 2 and res["failed"] == 0, str(res))
        check("补发是打包发的，不是一条一个消息刷屏", len(sent) == 1, f"发了 {len(sent)} 条消息")
        check("发成功就划掉了", report.pending(S) == 0)
        got = json.loads(sent[0])
        check("打包发出去的是个数组", isinstance(got, list) and len(got) == 2)
        check("run 都带着（收集端按它去重）",
              {x["run"] for x in got} == {"r1", "r3"}, str([x["run"] for x in got]))

        report.push(S)
        check("没东西可发时不发空消息", len(sent) == 1)

        report.outbox_path = lambda: Path("Z:/根本不存在的盘/outbox.jsonl")
        report.enqueue(S, row)                 # 不能抛
        check("发件箱写不进去也不抛异常", True)
    finally:
        usage.local_path, report.outbox_path = o_local, o_outbox
        report._post, report._post_json, report.sheet_webhook_url = o_post, o_pj, o_sheet
        shutil.rmtree(tmp, ignore_errors=True)


def test_sheet_channel():
    """智能表格通道：报文格式、两个通道各记各的账、补历史只做一次。"""
    from src import report

    print("\n[回传·表格] 字段 ID / 毫秒时间戳 / 两个通道各记各的账")
    usage.read_events = _REAL_READ_EVENTS
    tmp = Path(tempfile.mkdtemp(prefix="usage-sheet-"))
    o_local, o_outbox = usage.local_path, report.outbox_path
    o_post, o_pj, o_sheet = report._post, report._post_json, report.sheet_webhook_url
    o_userpath = report.user_path
    usage.local_path = lambda: tmp / "usage.jsonl"
    report.outbox_path = lambda: tmp / "outbox.jsonl"
    report.user_path = lambda *a: tmp / a[-1]         # 补历史的标记文件也落到 tmp
    report.sheet_webhook_url = lambda s: "https://example.invalid/sheet"
    S = {"usage": {"webhook_url": "https://example.invalid/hook"}}
    try:
        row = usage.record(S, "run_finished", run_id="r1", form="常规商广", mode="auto",
                           scope="unit", total=14, ok=8, failed=1, skipped=5,
                           seconds=700.0, wait_seconds=88.0)
        body = report._sheet_body([report.run_payload(row)])
        vals = body["add_records"][0]["values"]
        F = report.SHEET_FIELDS
        # ⚠ 表格 webhook 按**字段 ID** 取键，不是字段名 —— 这是它和 CLI 最容易混的一处
        check("values 的 key 是字段 ID 不是字段名",
              F["配置类型"] in vals and "配置类型" not in vals, str(list(vals)[:4]))
        check("类型/条数都映射对了",
              vals[F["配置类型"]] == "常规商广" and vals[F["成"]] == 8
              and vals[F["机器秒"]] == 612, str(vals))
        # ⚠ 表格的日期只认毫秒时间戳字符串，CLI 那套可读日期串在这儿写不进去
        check("时间是毫秒时间戳字符串",
              vals[F["时间"]].isdigit() and len(vals[F["时间"]]) == 13, vals[F["时间"]])
        check("表里没有的字段直接丢掉，不是报错",
              all(k in F.values() for k in vals), str(list(vals)))

        # 表格通 / 群不通：下次只补群，表格不能重发（重发会在表里多一行）
        report.enqueue(S, row)
        posts = {"sheet": 0, "group": 0}

        def ok_json(url, body, timeout=None):
            posts["sheet"] += 1
            return {"errcode": 0}

        def bad_post(url, text):
            posts["group"] += 1
            raise RuntimeError("群不通")

        report._post_json, report._post = ok_json, bad_post
        report.push(S)
        check("表格发成功了，但整条还留着（群还没发出去）", report.pending(S) == 1)

        report._post = lambda url, text: True
        report.push(S)
        check("补发只补群，表格不重发", posts["sheet"] == 1, f"表格被打了 {posts['sheet']} 次")
        check("两个通道都成了才划掉", report.pending(S) == 0)

        # 补历史：只做一次
        report._post_json = lambda url, body, timeout=None: {"errcode": 0}
        n1 = report.backfill(S)
        n2 = report.backfill(S)
        check("补历史把本机的历史运行捞出来了", n1 >= 1, f"补了 {n1} 条")
        check("补历史只做一次", n2 == 0, f"第二次又补了 {n2} 条")
        left = report._read_outbox()
        check("补出来的标了来源",
              bool(left) and all(e["d"].get("来源") == "补历史" for e in left), str(left[:1]))

        # 老埋点没有 run_id 时，凑一个稳定的
        old = {"uid": "abc", "ts": "2026-08-01T10:00:00+08:00", "form": "X"}
        check("老埋点没 run_id 也能凑出稳定的一个",
              report._legacy_run_id(old) == report._legacy_run_id(dict(old)))

        # 1.1.14 的裸 payload 也要认
        (tmp / "outbox.jsonl").write_text(
            json.dumps({"v": 2, "run": "old1", "类型": "X"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        e = report._read_outbox()
        check("1.1.14 那种裸 payload 也认得出来",
              len(e) == 1 and e[0]["d"].get("run") == "old1" and e[0]["s"] == [], str(e))
    finally:
        usage.local_path, report.outbox_path, report.user_path = o_local, o_outbox, o_userpath
        report._post, report._post_json, report.sheet_webhook_url = o_post, o_pj, o_sheet
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=" * 56)
    print("埋点 / 统计口径 场景测试")
    print("=" * 56)
    for fn in (test_empty, test_single_user, test_multi_user, test_excluded, test_stopped,
               test_dirty, test_week_boundary, test_fail_kinds, test_bad_fields,
               test_percentiles, test_status_alias, test_write_and_switch,
               test_share_dedupe, test_broken_file, test_saving,
               test_week_key_normalize, test_webhook_migration,
               test_outbox, test_sheet_channel):
        fn()
    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗ " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
