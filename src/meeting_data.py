"""预定会议室的「抢占任务清单」：界面上直接填，存 config/prep/<配置类型>.json。

和 ad_prep 的关系：ad_prep 是「一张字段→值的平表，整批共用」；这里是一个**列表**，
每条独立（不同日期、不同时段、不同人数），所以另起一份，不套用那边的形状。

存盘结构：
    {"tasks": [ {...}, {...} ], "updated_at": "2026-08-19 20:00:00"}

一条任务：
    enabled        是否参与本轮抢占
    repeat_weekly  每周循环。false=只抢 date 那一天；true=每周 weekday 抢一次，
                   抢完一周自动排下一周，直到用户点停止
    date           'YYYY-MM-DD'，repeat_weekly=false 时用
    weekday        1=周一 … 7=周日，repeat_weekly=true 时用
    start / end    'HH:MM'，必须落在整点或半点（后台就是 30 分钟一格）
    min_capacity   容纳人数下限（接口的 capacity 条件实测是「≥」）
    building       优先/限定的楼栋，形如「国正中心/2号楼」
    building_only  true=刚需，只在该楼栋抢；false=该楼栋优先，抢不到退到其它楼
    room           指定会议室（填了就只盯这一间，其余条件失效）
    subject        会议主题（后台必填）
    remarks        备注

⚠ 时段用「半小时格下标」而不是字符串比较：后台返回的 meetingCalendarResponseList
  就是定长 48 的数组，下标 i 代表 [i*30min, (i+1)*30min)。所有可用性判断都在下标
  空间里做，避免又是字符串又是时间对象两套逻辑对不齐。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta

from .paths import user_path

log = logging.getLogger(__name__)

SLOTS_PER_DAY = 48
WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

BAD_CHARS = r':\/?*[]<>|' + '"'

DEFAULT_TASK = {
    "enabled": True,
    "repeat_weekly": False,
    "date": "",
    "weekday": 1,
    "start": "14:00",
    "end": "15:00",
    "min_capacity": 6,
    "building": "国正中心/2号楼",
    "building_only": False,
    "room": "",
    "subject": "会议",
    "remarks": "",
}


def _safe_stem(name: str) -> str:
    return "".join(ch for ch in str(name) if ch not in BAD_CHARS).strip() or "预定会议室"


def path_for(cfg: dict):
    return user_path("config", "prep", f"{_safe_stem(cfg.get('name', '预定会议室'))}.json")


# ---------------- 时段 ↔ 半小时格 ----------------

_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_hhmm(text: str) -> tuple[int, int] | None:
    m = _HHMM.match(str(text or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if mi not in (0, 30) or h < 0 or h > 24 or (h == 24 and mi != 0):
        return None
    return h, mi


def to_slot(text: str) -> int | None:
    """'14:00' → 28。'24:00' → 48（只作为结束时间用）。"""
    hm = parse_hhmm(text)
    return None if hm is None else hm[0] * 2 + (1 if hm[1] == 30 else 0)


def slot_range(start: str, end: str) -> tuple[int, int] | None:
    """返回 [i0, i1)，即要占掉的格子下标区间。非法返回 None。"""
    i0, i1 = to_slot(start), to_slot(end)
    if i0 is None or i1 is None or i1 <= i0 or i0 >= SLOTS_PER_DAY or i1 > SLOTS_PER_DAY:
        return None
    return i0, i1


def submit_times(day: date, start: str, end: str) -> tuple[str, str]:
    """拼提交接口要的 start/end 字符串。

    ⚠ 结束时间正好是次日 00:00 时要写成当天 23:59:00 —— 前端 getMeetingTime()
      就是这么处理的（它拿 selectedEndTime+30min，撞到 00:00 就减 1 分钟）。
      直接写 '24:00:00' 或者滚到第二天，后台都不认。
    """
    d = day.strftime("%Y-%m-%d")
    if to_slot(end) == SLOTS_PER_DAY:
        return f"{d} {start}:00", f"{d} 23:59:00"
    return f"{d} {start}:00", f"{d} {end}:00"


def duration_text(start: str, end: str) -> str:
    rng = slot_range(start, end)
    if not rng:
        return ""
    mins = (rng[1] - rng[0]) * 30
    return f"{mins // 60}小时{mins % 60}分" if mins % 60 else f"{mins // 60}小时"


# ---------------- 目标日期 / 开放时刻 ----------------

def upcoming_dates(task: dict, today: date, count: int = 8) -> list[date]:
    """这条任务接下来要抢的日期。

    单次任务就一个日期（已经过去的返回空）。每周循环任务从今天起往后数 count 个
    同一星期几 —— 包含今天，因为「今天是周三、想订下周三」这种情况下今天就是
    上一轮的目标日，得让调用方自己按已完成记录跳过。
    """
    if not task.get("repeat_weekly"):
        d = parse_date(task.get("date"))
        return [d] if d and d >= today else []

    wd = int(task.get("weekday") or 1)
    wd = min(7, max(1, wd))
    ahead = (wd - 1 - today.weekday()) % 7
    first = today + timedelta(days=ahead)
    return [first + timedelta(days=7 * k) for k in range(count)]


def parse_date(text) -> date | None:
    s = str(text or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


OPEN_TIME = time(10, 0, 0)      # 每天的翻页时刻
LEAD_WORKDAYS = 6                # 10 点后可订到「第 6 个工作日」（今天算第 1 个）

# 后端原文（2026-08-20 提交窗口外日期时它自己吐出来的）：
#   「10点之前只能预定5个工作日之内的会议室，10点之后才可预定第6个工作日的会议室」
RULE_TEXT = "10点之前只能预定5个工作日之内的会议室，10点之后才可预定第6个工作日的会议室"


def is_workday(d: date) -> bool:
    """周一~周五算工作日。

    ⚠ 这里**不认法定节假日，也不认调休补班**。本地没有权威日历，硬编一份迟早过期。
      所以这个函数只用来算「大概哪天开抢」，好让机器人知道该在哪天醒过来；
      真正扣扳机之前，执行器一定会拿 /meeting/order/{id}/reservable/date 跟服务端
      核一次（见 MeetingRunner._wait_for_window）。节假日把窗口推后了也不会抢空，
      顶多是多轮询几轮。
    """
    return d.weekday() < 5


def nth_workday_from(start: date, n: int) -> date:
    """从 start 起数第 n 个工作日，**start 当天算第 1 个**（前提是它本身是工作日）。

    start 落在周末时，从它之后的第一个工作日开始数。
    """
    d, count = start, 0
    for _ in range(400):            # 防跑飞；正常几步就返回
        if is_workday(d):
            count += 1
            if count >= n:
                return d
        d += timedelta(days=1)
    return d


def horizon_on(day: date, after_open_time: bool) -> date:
    """站在 day 这一天，最远能订到哪天。

    10 点前是第 5 个工作日，10 点后是第 6 个 —— 就是 RULE_TEXT 那句话。
    """
    return nth_workday_from(day, LEAD_WORKDAYS if after_open_time else LEAD_WORKDAYS - 1)


def open_moment(target: date, open_time: time = OPEN_TIME) -> datetime:
    """target 这一天的预定窗口，最早在什么时刻打开。

    做法是从 target 往回走，一直走到「那天 10 点还够得着 target」的最早那天。

    ⚠ 别写成「target 减 N 天」。规则是按工作日数的，减固定天数只在「不跨周末、
      不遇节假日」时碰巧对得上 —— 原来那版就是按 7 个自然日算的，撞上周末纯属巧合。

    对过的点（都实测过）：
      target 08-26(周三) → 08-19(周三) 10:00     08-19 下午确实已经能订 08-26
      target 08-27(周四) → 08-20(周四) 10:00     08-20 下午确实已经能订 08-27

    目标日落在周末也说得通：规则限的是「最远能订到哪天」这个上界，
    上界之内的周末照样能订。比如 target 周六 08-22，上界要 ≥ 08-22，
    从 08-17(周一) 起数第 6 个工作日是 08-24 ≥ 08-22，所以 08-17 10:00 就开了。
    """
    d = target
    earliest = None
    for _ in range(120):
        if horizon_on(d, True) >= target:
            earliest = d
            d -= timedelta(days=1)
        else:
            break
    return datetime.combine(earliest or target, open_time)


# ---------------- 读写 ----------------

def normalize(task: dict) -> dict:
    """补齐缺省字段 + 收敛类型。老存盘缺字段也能直接用。"""
    t = dict(DEFAULT_TASK)
    t.update({k: v for k, v in (task or {}).items() if k in DEFAULT_TASK})
    t["enabled"] = bool(t["enabled"])
    t["repeat_weekly"] = bool(t["repeat_weekly"])
    t["building_only"] = bool(t["building_only"])
    try:
        t["min_capacity"] = max(1, int(float(t["min_capacity"] or 1)))
    except (TypeError, ValueError):
        t["min_capacity"] = 1
    try:
        t["weekday"] = min(7, max(1, int(t["weekday"] or 1)))
    except (TypeError, ValueError):
        t["weekday"] = 1
    for k in ("date", "start", "end", "building", "room", "subject", "remarks"):
        t[k] = str(t[k] or "").strip()
    # ⚠ 时间一律补零成 HH:MM。校验放过了「9:00」这种写法（人这么填很自然），
    #   但提交接口一直收的是补零格式，别赌后台宽容 —— 前端从来没发过不补零的。
    for k in ("start", "end"):
        hm = parse_hhmm(t[k])
        if hm:
            t[k] = "%02d:%02d" % hm
    return t


def load(cfg: dict) -> dict:
    p = path_for(cfg)
    tasks = []
    if p.exists():
        try:
            doc = json.loads(p.read_text(encoding="utf-8")) or {}
            tasks = [normalize(t) for t in (doc.get("tasks") or [])]
        except (OSError, ValueError):
            log.warning("抢占任务读取失败，当作空清单：%s", p, exc_info=True)
    return {"tasks": tasks}


def save(cfg: dict, doc: dict) -> str:
    p = path_for(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tasks": [normalize(t) for t in (doc or {}).get("tasks", [])],
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


# ---------------- 校验 ----------------

def validate(task: dict, today: date | None = None) -> list[str]:
    """返回人话的问题清单。空列表 = 这条可以跑。"""
    today = today or date.today()
    t = normalize(task)
    issues = []

    if not t["subject"]:
        issues.append("「会议主题」没填（后台必填）")

    if t["repeat_weekly"]:
        if not 1 <= t["weekday"] <= 7:
            issues.append("「每周几」不对")
    else:
        d = parse_date(t["date"])
        if d is None:
            issues.append(f"「日期」填的不是日期：{t['date'] or '（空）'}")
        elif d < today:
            issues.append(f"「日期」{d} 已经过去了")

    if parse_hhmm(t["start"]) is None:
        issues.append(f"「开始时间」要填 HH:MM 且只能是整点或半点，现在是「{t['start']}」")
    if parse_hhmm(t["end"]) is None:
        issues.append(f"「结束时间」要填 HH:MM 且只能是整点或半点，现在是「{t['end']}」")
    if parse_hhmm(t["start"]) and parse_hhmm(t["end"]) and slot_range(t["start"], t["end"]) is None:
        issues.append(f"「结束时间」要晚于开始时间：{t['start']} ~ {t['end']}")

    if t["min_capacity"] < 1:
        issues.append("「容纳人数」要大于 0")
    if t["building_only"] and not t["building"] and not t["room"]:
        issues.append("勾了「只要这栋楼」但没选楼栋")
    return issues


def describe(task: dict) -> str:
    """预览行的标题，一眼能认出是哪条。"""
    t = normalize(task)
    when = (f"每周{WEEKDAY_NAMES[t['weekday'] - 1][1]}" if t["repeat_weekly"]
            else (t["date"] or "未填日期"))
    where = t["room"] or (f"{t['building']}{'（只要这栋）' if t['building_only'] else '优先'}"
                          if t["building"] else "不限楼栋")
    return f"{when} {t['start']}~{t['end']} · ≥{t['min_capacity']}人 · {where}"
