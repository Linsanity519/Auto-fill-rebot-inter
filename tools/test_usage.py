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
def test_report_roundtrip():
    print("\n[上报格式] 建行 → TSV → 读回来，数要对得上")
    from datetime import datetime, timedelta
    from src import report
    FORMS = ["DMP延期", "资源位投放", "原生商广"]
    me = usage._uid()
    now = datetime.now().astimezone()

    def ev(form, ok, days, sec=120, wait=0, failed=0):
        return {"ts": (now - timedelta(days=days)).isoformat(timespec="seconds"),
                "event": "run_finished", "uid": me, "form": form, "mode": "auto",
                "ok": ok, "failed": failed, "skipped": 0, "dry": 0, "total": ok + failed,
                "seconds": sec, "wait_seconds": wait}

    usage.read_events = lambda s: [ev("DMP延期", 12, 0), ev("资源位投放", 3, 1, sec=600, wait=120),
                                   ev("DMP延期", 7, 8, failed=1), ev("原生商广", 99, 400)]
    tmp = Path(tempfile.mkdtemp(prefix="usage-report-"))
    orig_mark = usage.reported_path
    usage.reported_path = lambda: tmp / "usage-reported.json"
    try:
        header = usage.report_header(FORMS)
        rows = usage.report_rows({}, FORMS, nickname="子凡")
        check("表头列数 = 数据列数", all(len(r) == len(header) for r in rows),
              f"表头 {len(header)}，行 {[len(r) for r in rows]}")
        # ⚠ 2026-08-21 改了口径：欠着没上报的周**全部**补报，不再只报最近两周。
        #   原来那样，上报失败超过两周的数据就永远补不回来了（实测真丢过）。
        check("欠着的周全都补报（含 400 天前那次）", len(rows) == 3, f"报了 {len(rows)} 行")
        check("400 天前那周在里面", any("2025" in r[0] for r in rows), str([r[0] for r in rows]))
        # 本周两次：120 秒（没等人）+ 600 秒里扣掉 120 秒等人 = 600；不扣的话会是 720
        this_week = [r for r in rows if r[0] == usage.week_of(now)][0]
        check("机器代劳扣掉了等人的时间", this_week[7] == 600,
              f"得到 {this_week[7]}，不扣应为 720")

        # 记账之后就不该再报同样的内容；数据变了才重新报
        usage.mark_reported(rows)
        again = usage.report_rows({}, FORMS, nickname="子凡")
        check("上报成功记账后不再重复贴", again == [], f"又报了 {len(again)} 行")
        usage.read_events = lambda s: [ev("DMP延期", 12, 0), ev("资源位投放", 3, 1, sec=600, wait=120),
                                       ev("DMP延期", 7, 8, failed=1), ev("原生商广", 99, 400),
                                       ev("DMP延期", 5, 0)]
        changed = usage.report_rows({}, FORMS, nickname="子凡")
        check("这周又跑了就重新报这一周", [r[0] for r in changed] == [usage.week_of(now)],
              str([r[0] for r in changed]))
        check("失败没记账 → 下次仍然补报",
              len(usage.report_rows({}, FORMS, nickname="子凡")) == 1)

        # 往返：行 → webhook 那条单行 JSON → 收集端解析回行 → 汇总
        # ⚠ 这一段就是线上真实链路的形状，别用别的方式凑数据来测
        table = [header] + [_from_line(header, FORMS,
                                       json.dumps(report._payload(header, r, FORMS),
                                                  ensure_ascii=False))
                            for r in rows]
        got = usage.parse_report(table, FORMS)
        check("往返后人数 = 1", got["people"] == 1)
        check("往返后成功条数对得上", got["totals"]["items"] == sum(r[5] for r in rows),
              f"{got['totals']['items']} vs {sum(r[5] for r in rows)}")
        check("按配置类型分得开",
              {f["name"]: f["ok"] for f in got["forms"] if f["ok"]}
              == {"DMP延期": 19, "资源位投放": 3, "原生商广": 99},
              str(got["forms"]))

        # ---- 回归：什么算「这一周变了」 ----
        # ⚠ 原来签名里含「版本」，于是每升一次级、全部历史周的签名同时失配，
        #   几十周前的旧数据被原样重发一遍。实测 1.0.19 升级当天，统计群里
        #   刷出了 08-17、08-24 两条早就发过的周 —— 这三条就是防它回来的。
        usage.mark_reported(usage.report_rows({}, FORMS, nickname="子凡"))
        real_ver = usage._app_version
        usage._app_version = lambda: "9.9.9"
        try:
            after = usage.report_rows({}, FORMS, nickname="子凡")
            check("升级之后不重发历史", after == [], f"又报了 {[r[0] for r in after]}")
        finally:
            usage._app_version = real_ver
        renamed = usage.report_rows({}, FORMS, nickname="换了个花名")
        check("改花名也不重发历史", renamed == [], f"又报了 {[r[0] for r in renamed]}")

        # ⚠ 加一个新配置类型：每周那一段「分类型列」会整体右移，签名不能因此失配
        #   （1.0.21 加「价格策略批量开关」当天，统计群里刷出 08-21、08-25 —— 就是这个）
        more = usage.report_rows({}, FORMS + ["价格策略批量开关"], nickname="子凡")
        check("加一个新配置类型也不重发历史", more == [],
              f"又报了 {[r[0] for r in more]}")

        # 老格式的记账要能迁过来 —— 不然「改签名口径」这个动作**自己**就会让所有
        # 老记账失配，升级当天再刷一遍全历史
        every = usage.report_rows({}, FORMS, nickname="子凡", only_changed=False)

        def _v1(r):     # 1.0.19 之前：周|指纹|花名|版本|次数|成功|失败|秒|<分类型…>|最后活跃
            return "|".join(str(v) for v in list(r)[:-1])

        def _v2(r):     # 1.0.20 / 1.0.21：v2|周|指纹|次数|成功|失败|秒|<分类型…>|最后活跃
            r = list(r)[:-1]
            keep = [v for i, v in enumerate(r) if usage.REPORT_FIXED[i:i + 1] not in (["花名"], ["版本"])]
            return "|".join(["v2"] + [str(v) for v in keep])

        for tag, fn, forms in (("v1（含花名/版本）", _v1, FORMS),
                               ("v2（含分类型列）+ 之后又加了配置类型", _v2, FORMS + ["价格策略批量开关"])):
            usage.reported_path().write_text(
                json.dumps({r[0]: fn(r) for r in every}, ensure_ascii=False), encoding="utf-8")
            left = usage.report_rows({}, forms, nickname="子凡")
            check(f"{tag} 的老记账能迁过来（升级当天不刷屏）", left == [],
                  f"又报了 {[r[0] for r in left]}")
    finally:
        usage.reported_path = orig_mark
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================ 省时口径
def test_saving():
    print("\n[省时口径] 人工基准 × 条数 − 机器实跑")
    conf = usage.saving_conf({"usage": {"saving": {
        "mode": "baseline", "default_seconds": 60,
        "per_item_seconds": {"资源位投放": 480, "预定会议室": 0}}}})
    check("按配置类型取各自的基准", usage.human_seconds(conf, "资源位投放", 10) == 4800)
    check("没列出来的用兜底基准", usage.human_seconds(conf, "价格配置", 10) == 600)
    check("基准填 0 = 不按时长算价值", usage.human_seconds(conf, "预定会议室", 10) == 0)
    check("省下的 = 人工 − 机器", usage.saved_seconds(conf, "资源位投放", 10, 600) == 4200)
    check("机器比人工还慢时不算负数",
          usage.saved_seconds(conf, "预定会议室", 10, 600) == 0)

    mult = usage.saving_conf({"usage": {"saving": {"mode": "multiplier", "multiplier": 3}}})
    check("倍数口径：人工 = 机器 × 倍数", usage.human_seconds(mult, "任意", 5, 100) == 300)
    check("倍数口径：省下 = 机器 ×（倍数−1）", usage.saved_seconds(mult, "任意", 5, 100) == 200)

    # ⚠ 回归：倍数口径下 parse_report 曾经在配置类型上循环，把机器耗时乘了 N 遍
    #   （七个配置类型 → 624 秒算成 13104 秒）。它只在团队快照里出现，本机页面看不到，
    #   所以特别容易漏。
    FORMS7 = [f"类型{i}" for i in range(7)]
    header7 = usage.report_header(FORMS7)
    row7 = ["2026-08-17", "abc", "", "1.0", 1, 38, 1, 624] + [38] + [0] * 6 + ["", ""]
    got7 = usage.parse_report([header7] + [row7], FORMS7, mult)
    check("倍数口径下团队汇总不会按配置类型数翻倍",
          got7["totals"]["human"] == 624 * 3 and got7["totals"]["saved"] == 624 * 2,
          f"human={got7['totals']['human']} saved={got7['totals']['saved']}（应为 1872 / 1248）")
    check("省时按条数摊到各配置类型",
          [f["saved"] for f in got7["forms"] if f["ok"]] == [624 * 2],
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
    check("聚合按配置类型分别算", agg["totals"]["saved"] == (4800 - 600) + (600 - 100),
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


def test_webhook_payload():
    print("\n[webhook 消息] 一行 JSON 发出去，复制回来还能解析")
    from src import report
    FORMS = ["DMP延期", "资源位投放"]
    header = usage.report_header(FORMS)
    row = ["2026-08-17", "abc12345", "子凡", "1.0.5", 3, 38, 1, 624, 38, 0,
           "2026-08-20 21:43", "2026-08-21 13:00"]
    pl = report._payload(header, row, FORMS)
    check("字段对得上", pl["成功"] == 38 and pl["机器秒"] == 624, str(pl))
    # 群里那条消息越短越好：能算出来的、和没人填的，都不发
    check("不发「周」（收集端从最后活跃反推）", "周" not in pl, str(pl))
    check("不发「花名」（真人名字，少露一处是一处）", "花名" not in pl, str(pl))
    check("「版本」还在（判断谁没升级就靠它）", pl["版本"] == "1.0.5", str(pl))
    check("零的配置类型不占位置", pl["分类型"] == {"DMP延期": 38}, str(pl["分类型"]))
    line = json.dumps(pl, ensure_ascii=False)
    check("一条消息就一行（换行会把群消息拆散、也没法逐行解析）", "\n" not in line)
    check("没超过企微 2048 字节上限", len(line.encode("utf-8")) < 2048,
          f"{len(line.encode('utf-8'))} 字节")
    back = _from_line(header, FORMS, line)
    # 花名那一列现在还原不回来（本来就没发过），其余七列必须原样回来
    check("复制回来还原得回去", back[0] == row[0] and back[1] == row[1]
          and back[3:8] == row[3:8], str(back[:8]))
    check("周从最后活跃反推得对", back[0] == "2026-08-17", back[0])

    # 配置类型多到顶上限时，宁可丢明细也要把总数发出去
    many = [f"配置类型{i:02d}" for i in range(200)]
    big_header = usage.report_header(many)
    big_row = (["2026-08-17", "abc12345", "子" * 20, "1.0.5", 3, 38, 1, 624]
               + [7] * len(many) + ["2026-08-20 21:43", ""])
    big = report._payload(big_header, big_row, many)
    check("超长时砍掉明细保住总数",
          big["分类型"] == {} and big["成功"] == 38,
          str(big)[:120])
    check("砍完确实在上限内",
          len(json.dumps(big, ensure_ascii=False).encode("utf-8")) < 2048)


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


def test_report_header_mismatch():
    print("\n[表头对不上] 宁可显示「还没有」，也不能把错位的列当真数据")
    FORMS = ["DMP延期"]
    check("空表返回空", usage.parse_report([], FORMS) == {})
    check("表头不对返回空",
          usage.parse_report([["时间", "谁", "干了啥"], ["x", "y", "z"]], FORMS) == {})


def main():
    print("=" * 56)
    print("埋点 / 统计口径 场景测试")
    print("=" * 56)
    for fn in (test_empty, test_single_user, test_multi_user, test_excluded, test_stopped,
               test_dirty, test_week_boundary, test_fail_kinds, test_bad_fields,
               test_percentiles, test_status_alias, test_write_and_switch,
               test_share_dedupe, test_broken_file, test_report_roundtrip, test_saving,
               test_week_key_normalize, test_webhook_payload, test_webhook_migration,
               test_report_header_mismatch):
        fn()
    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗ " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
