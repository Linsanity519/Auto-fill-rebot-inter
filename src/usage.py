"""使用埋点：一次运行落一行 JSONL，主页的数据源。

为什么要新写一份，不从现有文件刨：
  · output/result.csv 每次跑都是 "w" 覆盖写，只剩最后一次
  · output/state.json 只记「哪几行做过」，没时间没历史，用户还会主动清
  · output/run.log 里压根没写「跑了几条、成功几条」——界面上那些日志走
    WebUI.log 只推前端，不落盘
详见 docs/界面方案/主页-使用统计调研.md §1。

⚠ 三条铁律，改这个文件之前先读：
  1. 只记数量和状态，绝不记业务内容（人群名称、价格、活动ID、会议室主题…）。
     这份文件是要汇总到公共目录、给所有人看的。
  2. 身份只有一个匿名指纹（用户名+机器名 取 sha1 前 8 位），明文不落盘。
     指纹只用来数「有多少个人」和认出「哪条是我自己的」，反推不回是谁。
  3. 埋点永远不能挡业务。写不进去、共享盘连不上，都只往 run.log 记一句就算了，
     绝不往上抛。

汇总方式（settings.yaml 的 usage.share_dir）：
  本机永远写 output/usage.jsonl；配了共享目录的话，每次落点之后把整份文件
  拷成 {share_dir}/{uid}.jsonl。
  ⚠ 是「整份覆盖」不是「追加」，这是有意的：
    · 一人一个文件，永远只有自己在写，没有并发写的问题
    · 拷贝是幂等的，上周没连上共享盘的那些事件，这次连上会自动补齐
    · 走的是临时文件 + os.replace，别人读到的要么是旧的完整版要么是新的完整版
"""
import getpass
import hashlib
import json
import logging
import os
import platform
import shutil
import socket
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from .paths import user_path

log = logging.getLogger(__name__)

LOCAL_FILE = "usage.jsonl"

_uid_cache = None
_share_failed = False       # 共享盘连不上就别每次都去撞，一轮里试一次够了


def _uid() -> str:
    """匿名指纹：同一台机器同一个人恒定，换台机器会变成另一个人。

    用户名和机器名都不落盘，只落 hash 的前 8 位。8 位（32bit）在百人规模下
    撞库概率可以忽略（生日悖论下 100 人碰一次约十万分之一）。
    """
    global _uid_cache
    if _uid_cache:
        return _uid_cache
    try:
        raw = f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:
        raw = "unknown"
    _uid_cache = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return _uid_cache


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _conf(settings: dict) -> dict:
    return (settings or {}).get("usage") or {}


def enabled(settings: dict) -> bool:
    return bool(_conf(settings).get("enabled", True))


def local_path() -> Path:
    return user_path("output", LOCAL_FILE)


def record(settings: dict, event: str, **fields):
    """落一条事件。任何异常都吞掉——统计不能挡业务。"""
    if not enabled(settings):
        return
    try:
        row = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            "uid": _uid(),
            "ver": _app_version(),
            "os": platform.system().lower(),
        }
        row.update({k: v for k, v in fields.items() if v is not None})

        path = local_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        log.warning("埋点写入失败（不影响运行）", exc_info=True)
        return

    _sync_async(settings)


# 六个 Runner 各写各的结果字典，但「状态」这一列的字面值就这几种。
# 归一放这一处，别让口径分歧渗进统计。
#   not_extendable：AB 那边「这个实验压根不能延期」，是「没事可做」不是「做失败了」，
#   算进 skipped，不然成功率会被这类无关项拖下去。
_STATUS_ALIAS = {"dry_run": "dry", "not_extendable": "skipped"}


def count_status(results) -> dict:
    out = {"ok": 0, "failed": 0, "skipped": 0, "dry": 0}
    for r in results or []:
        st = str((r or {}).get("状态", "")).strip()
        st = _STATUS_ALIAS.get(st, st)
        if st in out:
            out[st] += 1
    return out


# ---------------------------------------------------------------- 失败分类
# ⚠ 这一段是整个埋点里唯一「看过错误原文」的地方，但它**只输出下面这些固定枚举值**，
#   原文一个字都不会落盘 —— 因为错误消息里经常带着页面上的业务文案
#   （人群名称、活动名、选项值），那些不能进统计。
#   规则从上往下匹配，先命中先算：一条「等了 8 秒没返回任何选项」既像超时又像找不到，
#   算超时更贴近真相。
_FAIL_RULES = [
    ("browser_lost", ("target closed", "browser has been closed", "websocket",
                      "connection refused", "浏览器没连上", "浏览器断")),
    ("page_rejected", ("提交被拒", "页面报错", "没保存成功", "弹窗没关闭", "校验不通过")),
    ("timeout", ("timeout", "timed out", "超时", "没等到", "等了", "秒后", "还停在")),
    ("selector_miss", ("找不到", "没找到", "取不到", "not found", "no element",
                       "没有选项", "定位不到")),
    ("empty_required", ("必填", "数据为空", "没有值")),
]

# 卡在哪一层：白名单，命中才输出。这几个词是 filler 的 scope 前缀
# （见 ad_runner 的 af.fill(..., scope="计划层 ")），不是自由文本。
_STAGES = ("活动层", "计划层", "单元层", "创意", "素材")


def _fail_kind(text: str) -> str:
    low = text.lower()
    for kind, keys in _FAIL_RULES:
        if any(k in low or k in text for k in keys):
            return kind
    return "other"


def fail_detail(results) -> dict:
    """失败的类别分布 + 卡在哪一层。没有失败就返回空 dict（不占字段）。"""
    kinds, stages = {}, {}
    for r in results or []:
        if str((r or {}).get("状态", "")).strip() != "failed":
            continue
        text = str(r.get("错误") or r.get("说明") or "")
        k = _fail_kind(text)
        kinds[k] = kinds.get(k, 0) + 1
        for s in _STAGES:
            if s in text:
                stages[s] = stages.get(s, 0) + 1
                break
    out = {}
    if kinds:
        out["fail_kinds"] = kinds
    if stages:
        out["fail_stages"] = stages
    return out


# ---------------------------------------------------------------- 校验失败的列名
def bad_fields(rows) -> dict:
    """预检没过的行，是哪几列没填对。

    ⚠ 只取「列名：说明」里冒号左边那半截 —— 列名是 Excel 模板的表头，不是业务内容；
      冒号右边经常带着用户填的值（「『年度大会员』不是有效值」），绝不能带出去。
    """
    out = {}
    for r in rows or []:
        for issue in (getattr(r, "issues", None) or []):
            text = str(issue)
            name = text.split("：", 1)[0].strip() if "：" in text else ""
            # 明细项的前缀是「第N项-列名」，去掉序号只留列名，不然每行一个 key
            if "-" in name and name.startswith("第"):
                name = name.split("-", 1)[1].strip()
            if not name or len(name) > 24:
                name = "(其他)"
            out[name] = out.get(name, 0) + 1
    return out


# ---------------------------------------------------------------- 环境
def dpi_scale() -> float | None:
    """屏幕缩放倍数（125% → 1.25）。

    界面在高 DPI 下错位过一次（见 docs/界面方案/DPI修复-前.png），
    以后再有人报「界面糊了/错位」，先看这个值对不对得上。
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        return round(ctypes.windll.user32.GetDpiForSystem() / 96.0, 2)
    except Exception:
        return None


def chrome_version(cdp_url: str | None) -> str | None:
    """挂着的那个 Chrome 是什么版本。页面结构随浏览器版本变过，出问题时能对号。"""
    if not cdp_url:
        return None
    try:
        import json as _json
        import urllib.request
        with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=1.0) as r:
            ver = (_json.load(r) or {}).get("Browser") or ""
        return ver.split("/")[-1] or None       # "Chrome/151.0.7922.138" → "151.0.7922.138"
    except Exception:
        return None


def percentiles(values, ps=(50, 90)) -> dict:
    """单条耗时的分位数。区分「整体慢」和「个别卡死」—— 只看总时长看不出来。"""
    xs = sorted(v for v in (values or []) if v is not None and v >= 0)
    if not xs:
        return {}
    out = {}
    for p in ps:
        k = min(len(xs) - 1, int(round((p / 100.0) * (len(xs) - 1))))
        out[f"item_p{p}"] = round(xs[k], 1)
    return out


def _app_version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:
        return "?"


# ---------------------------------------------------------------- 汇总
def _sync_async(settings: dict):
    """把本机这份拷到共享目录。放后台线程——共享盘不可达时，Windows 要卡十几秒
    才超时，绝不能让它卡在 run() 的 finally 里。"""
    share = (_conf(settings).get("share_dir") or "").strip()
    if not share or _share_failed:
        return
    threading.Thread(target=_sync, args=(share,), daemon=True).start()


def _sync(share: str):
    global _share_failed
    try:
        dst_dir = Path(share)
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / f"{_uid()}.jsonl"
        tmp = dst_dir / f".{_uid()}.tmp"
        shutil.copyfile(local_path(), tmp)
        os.replace(tmp, dst)          # 别人要么读到旧的完整版，要么读到新的完整版
    except Exception:
        _share_failed = True          # 这一轮别再试了
        log.warning("汇总目录写不进去（不影响运行）：%s", share, exc_info=True)


# ---------------------------------------------------------------- 读回来
def read_events(settings: dict) -> list[dict]:
    """本机 + 汇总目录里所有人的事件。坏行跳过，不让一行脏数据毁掉整个主页。

    ⚠ 同一个 uid 在两边都有时以汇总目录里那份为准 —— 本机这份是它的来源，
      内容一样，重复读会把自己的数据算两遍。
    """
    by_uid: dict[str, list[dict]] = {}

    share = (_conf(settings).get("share_dir") or "").strip()
    if share:
        try:
            for p in sorted(Path(share).glob("*.jsonl")):
                rows = _read_file(p)
                if rows:
                    by_uid[rows[0].get("uid", p.stem)] = rows
        except Exception:
            log.warning("读汇总目录失败：%s", share, exc_info=True)

    mine = _read_file(local_path())
    if mine:
        by_uid[_uid()] = mine         # 本机这份永远是最新的，覆盖汇总里可能过时的自己

    out = [r for rows in by_uid.values() for r in rows]
    out.sort(key=lambda r: r.get("ts", ""))
    return out


def _read_file(path: Path) -> list[dict]:
    rows = []
    try:
        if not path.exists():
            return rows
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue          # 半行/乱码，跳过就是了
                if isinstance(row, dict):
                    rows.append(row)
    except Exception:
        log.warning("读埋点文件失败：%s", path, exc_info=True)
    return rows


# ---------------------------------------------------------------- 省了多少时间
# ⚠ 口径 2026-08-21 改过一次，改之前先读 docs/界面方案/主页-使用统计调研.md §2.4：
#   老口径「机器跑了多久 = 帮你省了多久」实测归实测，但它把人当成了和脚本一样快的
#   机器 —— 人在页面上填一条资源位单元要翻三段表单、几十个字段，脚本二十秒的事
#   人工得七八分钟。照老口径报出去，这工具的价值被自己压掉了一大截。
#
#   新口径：**人工基准 × 成功条数 − 机器净耗时**。
#     · 基准写在 settings.yaml 的 usage.saving.per_item_seconds，按配置类型一条一个数，
#       谁都能改、改完历史数据自动跟着重算（埋点里存的永远是实测秒数，不存换算结果）。
#     · 为什么不用「机器耗时 × 2/3」这种倍数：那个数会跟着脚本的快慢反着走 ——
#       脚本优化快了，报出来的「省时」反而变少；某次网络卡了十分钟，反倒成了大功一件。
#       人工基准和脚本快慢无关，是这件事本身值多少时间。
#     · 真想要倍数也留了口子：usage.saving.mode: multiplier + multiplier: 3。
DEFAULT_HUMAN_SECONDS = 120


def saving_conf(settings: dict) -> dict:
    c = dict(_conf(settings).get("saving") or {})
    c.setdefault("mode", "baseline")
    c.setdefault("multiplier", 3)
    c.setdefault("default_seconds", DEFAULT_HUMAN_SECONDS)
    c.setdefault("per_item_seconds", {})
    return c


def _numf(v, dflt: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


def human_seconds(conf: dict, form: str, items: int, machine: float = 0.0) -> float:
    """同样这批东西，人工做要多久（估）。

    baseline   人工基准 × 条数 —— 默认，和脚本快慢无关
    multiplier 机器净耗时 × 倍数 —— 想要极简口径时用
    """
    items = max(0, _num(items))
    if str(conf.get("mode")) == "multiplier":
        return max(0.0, machine) * max(1.0, _numf(conf.get("multiplier"), 3))
    per = conf.get("per_item_seconds") or {}
    base = _numf(per.get(form, conf.get("default_seconds")), DEFAULT_HUMAN_SECONDS)
    return max(0.0, base) * items


def saved_seconds(conf: dict, form: str, items: int, machine: float) -> float:
    """净省下的时间 = 人工要花的 − 机器实际花的。不会是负数。

    ⚠ 基准填 0 的配置类型（抢会议室）等于「不按时长算价值」，直接是 0。
    """
    return max(0.0, human_seconds(conf, form, items, machine) - max(0.0, machine))


# ---------------------------------------------------------------- 聚合（主页的数）
# ⚠ 口径写死在这里，改之前先读 docs/界面方案/主页-使用统计调研.md §2.4：
#   · 只认 run_finished，且「重跑」和「空跑」不进累计（各自单独计数）
#   · 「机器实跑」= 墙钟耗时 − 等人点确认的时长。逐条确认时人就坐在旁边，
#     那段时间不能算机器替你干活
#   · 「省下工时」= 人工基准 × 条数 − 机器实跑（见上面 saved_seconds），
#     人工那半截是估的、机器那半截是实测的，界面上必须分开说
#   · 「一次做对率」= ok / (ok + failed)。跳过的不算错，不进分母
def summarize(settings: dict, weeks: int = 12) -> dict:
    # ⚠ 在入口一次性把非字典挡掉。read_events 读文件时已经过滤过，但数据源以后会
    #   多一个（企微表格），那边解析出什么形状不由这里控制 —— 挡在入口最省心
    rows = [r for r in (read_events(settings) or []) if isinstance(r, dict)]
    me = _uid()
    runs, retries, dry_runs, opens = [], 0, 0, 0
    ran_uids, seen_uids = set(), set()

    for r in rows:
        uid = r.get("uid")
        if uid:
            seen_uids.add(uid)
        ev = r.get("event")
        if ev == "app_open":
            opens += 1
        elif ev == "run_finished":
            if uid:
                ran_uids.add(uid)
            if r.get("retry_of"):
                retries += 1
            elif r.get("mode") == "dry":
                dry_runs += 1
            else:
                runs.append(r)

    conf = saving_conf(settings)
    mine = [r for r in runs if r.get("uid") == me]
    agg_all = _aggregate(runs, weeks, conf)
    agg_mine = _aggregate(mine, weeks, conf) if len(mine) != len(runs) else agg_all

    out = {
        "since": (min((r.get("ts", "") for r in rows if r.get("ts")), default="")[:10]),
        # ⚠ 「多少人在用」只数真跑过东西的人。打开过没跑的单独记 people_opened ——
        #   把「点开看了一眼」算成使用者，这个数就没意义了
        "people": len(ran_uids),
        "people_opened": len(seen_uids),
        "shared": bool((_conf(settings).get("share_dir") or "").strip()),
        "me": me,
        "opens": opens, "retries": retries, "dry_runs": dry_runs,
        "saving": {"mode": conf["mode"], "multiplier": conf["multiplier"],
                   "per_item_seconds": conf["per_item_seconds"],
                   "default_seconds": conf["default_seconds"]},
    }
    out.update(agg_all)
    # 「我的战绩」单独一份。只有本机数据时两者相同，接上汇总之后才分得开 ——
    # 不分的话首页那句「你跑了 X 条」会在接通汇总的那天悄悄变成全团队的数
    out["mine"] = agg_mine
    return out


def _aggregate(runs: list, weeks: int, conf: dict | None = None) -> dict:
    """把一组运行记录压成主页要的那些数。runs 里已经排除了重跑和空跑。

    ⚠ 省时（saved）在这里按每一次运行单独算再累加，不能拿总数去算 ——
      人工基准是按配置类型分的，一次「资源位投放」和一次「DMP延期」的一条
      根本不是一回事。
    """
    conf = conf or saving_conf({})
    forms: dict[str, dict] = {}
    week_buckets: dict[str, dict] = {}
    longest = None
    ok = failed = skipped = items_total = 0
    seconds_total = human_total = saved_total = 0.0

    for r in runs:
        net = _net_seconds(r)
        name = r.get("form") or "(未知)"
        r_ok = _num(r.get("ok"))
        r_human = human_seconds(conf, name, r_ok, net)
        r_saved = saved_seconds(conf, name, r_ok, net)
        f = forms.setdefault(name, {
            "runs": 0, "ok": 0, "failed": 0, "skipped": 0, "total": 0,
            "seconds": 0.0, "human": 0.0, "saved": 0.0, "last": "",
        })
        f["runs"] += 1
        for k in ("ok", "failed", "skipped", "total"):
            f[k] += _num(r.get(k))
        f["seconds"] += net
        f["human"] += r_human
        f["saved"] += r_saved
        f["last"] = max(f["last"], str(r.get("ts") or ""))

        ok += r_ok
        failed += _num(r.get("failed"))
        skipped += _num(r.get("skipped"))
        items_total += _num(r.get("total"))
        seconds_total += net
        human_total += r_human
        saved_total += r_saved

        key = _week_key(str(r.get("ts") or ""))
        if key:
            b = week_buckets.setdefault(key, {"items": 0, "seconds": 0.0, "saved": 0.0})
            b["items"] += r_ok
            b["seconds"] += net
            b["saved"] += r_saved

        if longest is None or net > longest["seconds"]:
            longest = {"seconds": round(net, 1), "items": r_ok,
                       "form": name, "ts": r.get("ts", "")}

    this_week = week_buckets.get(_week_key_of(datetime.now().astimezone()),
                                 {"items": 0, "seconds": 0.0, "saved": 0.0})
    return {
        "totals": {
            "runs": len(runs), "items": ok, "failed": failed, "skipped": skipped,
            "attempted": items_total, "seconds": round(seconds_total, 1),
            "human": round(human_total, 1), "saved": round(saved_total, 1),
            "ok_rate": (ok / (ok + failed)) if (ok + failed) else None,
        },
        "week": {"items": this_week["items"], "seconds": round(this_week["seconds"], 1),
                 "saved": round(this_week["saved"], 1)},
        "longest": longest,
        "forms": [dict(name=k, **{kk: (round(vv, 1) if isinstance(vv, float) else vv)
                                  for kk, vv in v.items()})
                  for k, v in sorted(forms.items(), key=lambda kv: -kv[1]["ok"])],
        "weeks": _week_series(week_buckets, weeks),
        "recent": [{"ts": r.get("ts", ""), "form": r.get("form") or "(未知)",
                    "mode": r.get("mode", ""), "uid": r.get("uid", ""),
                    "ok": _num(r.get("ok")), "total": _num(r.get("total")),
                    "seconds": round(_net_seconds(r), 1)}
                   for r in sorted(runs, key=lambda x: str(x.get("ts") or ""))[-8:]][::-1],
    }


def _num(v) -> int:
    """脏数据不能把整个主页干掉。

    ⚠ 这是多人汇总下的必修课：一百台机器里只要有一台写歪一行（半行、类型不对、
      字段缺失），聚合一抛异常，**所有人的首页都白屏**。宁可这一条算 0。
    """
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _net_seconds(r: dict) -> float:
    """机器真正在干活的时长。命令行版没有 wait_seconds 这一项，缺了当 0。"""
    try:
        net = float(r.get("seconds") or 0) - float(r.get("wait_seconds") or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, net)


def _week_key(ts: str) -> str:
    try:
        return _week_key_of(datetime.fromisoformat(ts))
    except Exception:
        return ""


def _week_key_of(dt) -> str:
    from datetime import timedelta
    d = dt.date()
    return (d - timedelta(days=d.weekday())).isoformat()      # 该周的周一


def _week_series(buckets: dict, weeks: int) -> list:
    """近 N 周，没数据的周也要占位 —— 空档本身是信息（那阵子没人用）。"""
    from datetime import timedelta
    today = datetime.now().astimezone().date()
    monday = today - timedelta(days=today.weekday())
    out = []
    for i in range(weeks - 1, -1, -1):
        key = (monday - timedelta(weeks=i)).isoformat()
        b = buckets.get(key, {"items": 0, "seconds": 0.0, "saved": 0.0})
        out.append({"week": key, "label": key[5:], "items": b["items"],
                    "seconds": round(b["seconds"], 1),
                    "saved": round(b.get("saved", 0.0), 1)})
    return out


# ---------------------------------------------------------------- 上报到企微表格
# 每人每周一行。键是「周 + 指纹」，所以一行只属于某个人的某一周，各写各的。
# ⚠ 列顺序就是这张表的契约：改了等于改表结构，upsert 会发现表头对不上、
#   重铺表头，老数据的列会错位。要改先想清楚存量怎么办。
REPORT_FIXED = ["周", "指纹", "花名", "版本", "运行次数", "成功", "失败", "机器代劳秒"]
REPORT_TAIL = ["最后活跃", "上报时间"]


def report_header(form_names) -> list:
    """表头。七个配置类型各占一列，加一个配置类型就多一列。"""
    return REPORT_FIXED + list(form_names) + REPORT_TAIL


def norm_week(text) -> str:
    """把「周」这一列归一成 YYYY-MM-DD。

    ⚠ 企微表格会把 2026-08-10 识别成日期类型，复制回来变成 2026/8/10 ——
      不归一的话「同一周」就对不上，每跑一次都会新增一行（踩过）。
    """
    t = str(text or "").strip().replace("/", "-")
    parts = t.split("-")
    if len(parts) == 3 and all(p.strip().isdigit() for p in parts):
        y, m, d = (int(p) for p in parts)
        return f"{y:04d}-{m:02d}-{d:02d}"
    return t


def week_of(ts_or_date) -> str:
    """所在周的周一。周是统计的最小粒度 —— 这类工具是攒一批集中跑，
    按天看全是尖刺和空洞。"""
    from datetime import timedelta
    d = ts_or_date
    if isinstance(d, str):
        try:
            d = datetime.fromisoformat(d)
        except ValueError:
            return ""
    d = d.date() if hasattr(d, "date") else d
    return (d - timedelta(days=d.weekday())).isoformat()


# 上一次成功写进团队表的是什么内容。键是周，值是那一行的指纹。
# ⚠ 这个文件是「没上报的数据不会丢」的全部依据：只要某一周的本地数据和这里
#   对不上，下一次有机会连表格时就会把它重新写一遍 —— 中间失败几次都没关系。
REPORTED_FILE = "usage-reported.json"


def reported_path():
    return user_path("output", REPORTED_FILE)


def load_reported() -> dict:
    try:
        p = reported_path()
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                return {}
            # 老版本写下的签名格式不一样，读进来就地迁移（见 _migrate_sign）
            return {str(k): _migrate_sign(str(v)) for k, v in d.items()}
    except Exception:
        log.warning("上报记录读不了，当成没报过", exc_info=True)
    return {}


def save_reported(marks: dict):
    try:
        p = reported_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(marks or {}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.warning("上报记录写不进去（下次会重报一遍，不丢数据）", exc_info=True)


# 签名要排掉的列（下标按 REPORT_FIXED）。「上报时间」是最后一列，另外单独切。
_SIGN_SKIP = (REPORT_FIXED.index("花名"), REPORT_FIXED.index("版本"))
SIGN_TAG = "v2"          # 签名格式代号，load_reported 靠它认出老记账并迁过来


def _row_sign(row) -> str:
    """一行的指纹，用来判断「这一周的数字变了没有」。

    ⚠ 不含最后那列「上报时间」——那列每次都变，含进去就永远是「变了」。
    ⚠ 也不含「版本」和「花名」。这是修掉的一个真 bug：版本号原来在签名里，
      于是**每升一次级、全部历史周的签名同时变化**，report_rows 判定「都变了」，
      把几十周前的旧数据原样重发一遍。实测 1.0.19 升级当天统计群里刷出了
      08-17、08-24 两条早就发过的周。
      签名要回答的是「数字变了没有」——版本号变了不是数字变了。
    """
    row = list(row)[:-1]
    return "|".join([SIGN_TAG]
                    + [str(v) for i, v in enumerate(row) if i not in _SIGN_SKIP])


def _migrate_sign(sign: str) -> str:
    """老格式（含花名/版本）的签名 → 新格式。

    ⚠ 为什么必须迁、不能让它自然失配一次：不迁的话「修掉重发」这个改动**自己**
      会让所有老记账对不上，升级当天再刷一遍全历史 —— 正好就是这次要修掉的
      那个毛病。迁完这一次就彻底安静了。
    ⚠ 老花名里真有个「|」会迁歪，代价是那一周多发一条，仅此而已。
    """
    if not sign or sign.startswith(SIGN_TAG + "|"):
        return sign
    parts = sign.split("|")
    return "|".join([SIGN_TAG]
                    + [p for i, p in enumerate(parts) if i not in _SIGN_SKIP])


def weekly_buckets(settings: dict) -> dict:
    """本机全部历史，按周压成桶。只用自己的数据，不掺别人的。"""
    rows_ev = [r for r in (read_events(settings) or []) if isinstance(r, dict)]
    me = _uid()
    mine = [r for r in rows_ev
            if r.get("event") == "run_finished" and r.get("uid") == me
            and not r.get("retry_of") and r.get("mode") != "dry"]

    buckets = {}
    for r in mine:
        wk = week_of(str(r.get("ts") or ""))
        if not wk:
            continue
        b = buckets.setdefault(wk, {"runs": 0, "ok": 0, "failed": 0, "seconds": 0.0,
                                    "forms": {}, "last": ""})
        b["runs"] += 1
        b["ok"] += _num(r.get("ok"))
        b["failed"] += _num(r.get("failed"))
        b["seconds"] += _net_seconds(r)
        name = r.get("form") or "(未知)"
        b["forms"][name] = b["forms"].get(name, 0) + _num(r.get("ok"))
        b["last"] = max(b["last"], str(r.get("ts") or ""))
    return buckets


def report_rows(settings: dict, form_names, nickname: str = "",
                only_changed: bool = True) -> list:
    """把本机的统计压成「每周一行」，返回**还没成功写进表格的那些周**。

    ⚠ 2026-08-21 改成「按差异补报全部历史」，原来是「只报本周和上周」。
      原因：实测有人用过一天、数据没进表。只报最近两周的话，凡是上报失败
      （没登录企微文档 / 表格没加载完 / 关得太快后台线程被掐）的那几周，
      过了两周就永远补不回来了 —— 而失败是静默的，谁都不知道少了。
      现在的做法是：本地留一份「上次成功写进去的是什么」，对不上就重写。
      一次失败不要紧，下一次连上表格时会把欠的全部补上。

    only_changed=False 时把所有周都吐出来（重建整张表用）。
    """
    me = _uid()
    marks = load_reported() if only_changed else {}
    now_text = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    out = []
    for wk, b in sorted(weekly_buckets(settings).items()):
        row = [wk, me, nickname or "", _app_version(),
               b["runs"], b["ok"], b["failed"], int(round(b["seconds"]))] \
            + [b["forms"].get(n, 0) for n in form_names] \
            + [b["last"][:16].replace("T", " "), now_text]
        if only_changed and marks.get(wk) == _row_sign(row):
            continue                     # 这一周和上次成功写进去的一模一样，不用再贴
        out.append(row)
    return out


def mark_reported(rows):
    """标记这几行已经成功写进表格了。"""
    marks = load_reported()
    for row in rows or []:
        row = list(row)
        if row:
            marks[str(row[0])] = _row_sign(row)
    save_reported(marks)


def pending_weeks(settings: dict, form_names, nickname: str = "") -> int:
    """还有几周没上报上去。首页拿它提醒人 —— 静默失败是最难发现的那种坏。"""
    try:
        return len(report_rows(settings, form_names, nickname))
    except Exception:
        log.warning("算待上报周数失败", exc_info=True)
        return 0


def parse_report(table, form_names, conf: dict | None = None) -> dict:
    """把整张表还原成主页要的全团队数字。

    表头对不上就当没有数据 —— 宁可显示「还没有」，也不能把错位的列当真数据。

    ⚠ 省时是在这里按「每个配置类型的条数 × 人工基准」现算的，表里存的还是
      实测秒数 —— 改了 settings 里的基准，全团队的历史数字跟着一起变，
      不用回头动表。
    """
    conf = conf or saving_conf({})
    header = report_header(form_names)
    if not table or [c.strip() for c in table[0][:len(REPORT_FIXED)]] != REPORT_FIXED:
        return {}

    i_forms = len(REPORT_FIXED)
    people, runs, ok, failed, seconds = set(), 0, 0, 0, 0
    human = saved = 0.0
    forms = {n: 0 for n in form_names}
    form_saved: dict[str, float] = {}
    weeks = {}
    who = {}

    for r in table[1:]:
        if len(r) < len(REPORT_FIXED) or not r[1].strip():
            continue
        wk, uid = norm_week(r[0]), r[1].strip()
        people.add(uid)
        r_ok, r_fail = _num(r[5]), _num(r[6])
        r_sec = _num(r[7])
        runs += _num(r[4]); ok += r_ok; failed += r_fail; seconds += r_sec
        # 这一行各配置类型各干了多少条
        row_counts = {}
        for j, name in enumerate(form_names):
            k = i_forms + j
            if k < len(r):
                cnt = _num(r[k])
                forms[name] += cnt
                row_counts[name] = cnt

        # 这一行「人工要花多久」
        # ⚠ 两种口径的算法**结构不一样**，不能共用一个循环：
        #   baseline   —— 按配置类型各自的基准分别算，再加起来
        #   multiplier —— 只跟机器耗时有关，整行算一次
        #   踩过：倍数口径下在配置类型上循环，等于把机器耗时乘了 7 遍
        #   （七个配置类型 → 624 秒算成了 13104 秒）
        if str(conf.get("mode")) == "multiplier":
            row_human = human_seconds(conf, "", 0, r_sec)
        else:
            row_human = sum(human_seconds(conf, n, c, r_sec) for n, c in row_counts.items())
        row_saved = max(0.0, row_human - r_sec)
        human += row_human
        saved += row_saved

        # 省时分摊到各配置类型：按条数占比。表里没有「每个配置类型各花了多少秒」，
        # 只能这么摊 —— 界面上那根横条要的就是个相对量级
        row_items = sum(row_counts.values())
        if row_items:
            for n, c in row_counts.items():
                form_saved[n] = form_saved.get(n, 0.0) + row_saved * c / row_items
        if wk:
            b = weeks.setdefault(wk, {"items": 0, "seconds": 0, "saved": 0.0})
            b["items"] += r_ok
            b["seconds"] += r_sec
            b["saved"] += row_saved
        last = r[i_forms + len(form_names)].strip() if len(r) > i_forms + len(form_names) else ""
        prev = who.get(uid)
        if not prev or last > prev["last"]:
            who[uid] = {"uid": uid, "name": (r[2].strip() if len(r) > 2 else ""), "last": last}

    return {
        "people": len(people),
        "totals": {"runs": runs, "items": ok, "failed": failed, "seconds": seconds,
                   "human": round(human, 1), "saved": round(saved, 1),
                   "ok_rate": (ok / (ok + failed)) if (ok + failed) else None},
        "forms": [{"name": n, "ok": v, "saved": round(form_saved.get(n, 0.0), 1)}
                  for n, v in sorted(forms.items(), key=lambda kv: -kv[1])],
        "weeks": weeks,
        "actives": sorted(who.values(), key=lambda x: x["last"], reverse=True)[:12],
    }


# ---------------------------------------------------------------- 全团队数据（快照）
# ⚠ 这是一个**随分发包发出去的静态快照**，不是实时数据，首页上必须标「截至 X/X」。
#
#   为什么不做实时：读企微文档要么开浏览器（前端有感知、依赖登录态、实测坏过），
#   要么用文档机器人的 apikey —— 而那把 key 没法限权，一把覆盖持有人名下所有文档，
#   不能打进发给一百个人的 exe。所以同事端读不到活数据，只能读随包的快照。
#
#   快照怎么来：同事端 webhook 上报 → 统计群 → 收集端（tools/collect_usage.py）
#   整理成 config/team.json → 下次发版带出去。整条链路见
#   docs/界面方案/主页-使用统计调研.md §2.5。
def team_path():
    """⚠ 在 config/ 不在 output/：它是随包分发的配置，不是本机运行产物。"""
    return user_path("config", "team.json")


def save_team(data: dict):
    """只有收集端（你自己的机器）会调它，同事端只读不写。"""
    try:
        p = team_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(data or {})
        payload["synced_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        log.warning("团队快照写入失败（不影响运行）", exc_info=True)


def _team_url(settings: dict) -> str:
    return (((settings or {}).get("usage") or {}).get("team_url") or "").strip()


def fetch_team(settings: dict) -> bool:
    """从 GitHub 拉一份最新的团队快照盖在本地，返回「有没有真的更新」。

    ⚠ 为什么需要它：team.json 原本是**打进安装包**的，所以首页那个「N 人在用 /
      团队共省了多少时间」最新只到上次发版 —— 中间收集了新数据也送不到同事眼前。
      改成运行时拉之后，收集端 git push 一次，大家下次打开就看到（raw 有几分钟
      CDN 缓存）。发版和统计新鲜度就此解耦。

    ⚠ 三条铁律照旧：失败只写日志、绝不抛、绝不挡业务。拉不到就继续用本地那份。
    """
    url = _team_url(settings)
    if not url:
        return False
    try:
        import urllib.request      # 和本文件里取 Chrome 版本那处一样，用到才 import

        req = urllib.request.Request(url, headers={"User-Agent": "ConfigAssistant/1"})
        with urllib.request.urlopen(req, timeout=6) as r:
            raw = r.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise ValueError("团队快照过大")
        remote = json.loads(raw.decode("utf-8"))
        if not isinstance(remote, dict) or "totals" not in remote:
            raise ValueError("团队快照格式不对")
    except Exception:
        log.info("团队快照拉取失败（用本地那份）", exc_info=True)
        return False

    # 远端不比本地新就不动 —— 收集端自己那台机器上，本地往往才是最新的
    local = load_team()
    if local and str(remote.get("synced_at", "")) <= str(local.get("synced_at", "")):
        return False

    try:
        p = team_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(remote, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
        log.info("团队快照已更新到 %s", remote.get("synced_at", "?"))
        return True
    except OSError:
        log.warning("团队快照写入失败（不影响运行）", exc_info=True)
        return False


def load_team() -> dict:
    try:
        p = team_path()
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except Exception:
        log.warning("团队快照读不了", exc_info=True)
    return {}


# ---------------------------------------------------------------- 自查
def _cli():
    """python -m src.usage —— 主页还没做之前，先用这个看埋点有没有正常落。"""
    import sys

    import yaml

    if sys.stdout:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    settings = yaml.safe_load(user_path("config", "settings.yaml").read_text(encoding="utf-8"))
    rows = read_events(settings)
    runs = [r for r in rows if r.get("event") == "run_finished"]
    print(f"本机指纹：{_uid()}    文件：{local_path()}")
    print(f"事件 {len(rows)} 条，其中运行 {len(runs)} 次，来自 {len({r.get('uid') for r in rows})} 个人\n")
    for r in runs[-15:]:
        print(f"  {r.get('ts')}  {r.get('form')}  {r.get('mode')}  "
              f"成功 {r.get('ok', 0)}/{r.get('total', 0)}  "
              f"耗时 {r.get('seconds', 0):.0f}s（等人 {r.get('wait_seconds', 0):.0f}s）")


if __name__ == "__main__":
    _cli()
