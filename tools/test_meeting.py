"""抢会议室的离线自测：不开浏览器、不发一个请求，用假接口跑 _grab 的判定。

跑法：python tools\test_meeting.py

⚠ 这里只测**判定逻辑**（候选怎么筛、什么时候收摊、失败要不要记进断点）。
  选择器/接口对不对不在这份测试的范围里，那只有实跑能验。
"""
from __future__ import annotations

import sys
import tempfile
import time as _t
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.meeting_api import ApiError                      # noqa: E402
from src.meeting_runner import MeetingRunner              # noqa: E402
from src.ui import BaseUI                                 # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        print(f"  \u2717 {name}" + (f"  {detail}" if detail else ""))


class QuietUI(BaseUI):
    """把界面上打的每一句都记下来 —— 有几条断言就是冲着「界面上到底看得见什么」去的。"""

    def __init__(self):
        super().__init__()
        self.lines = []

    def log(self, msg: str, level: str = "info"):
        self.lines.append(msg)


def room(rid, name, cap=10, loc="国正中心/2号楼", free=True):
    return {"roomId": rid, "roomName": name, "capacity": cap, "location": loc,
            "meetingCalendarResponseList": [{"available": free} for _ in range(48)]}


class FakeApi:
    """按剧本回话的假接口。reserve 每次从 replies[roomId] 里取下一句。

    busy_after={roomId: n}：这间房在第 n 次 reserve 之后被「别人订走」，
    从此不再出现在候选里 —— 用来复现「候选在轮询中途变少」这件真事。
    """

    def __init__(self, rooms, replies, until_days=30, busy_after=None):
        self.rooms = rooms
        self.replies = replies
        self.until_days = until_days
        self.busy_after = busy_after or {}
        self.reserved = []

    def spaces(self, day, min_capacity=None, **kw):
        out = []
        for r in self.rooms:
            if min_capacity and int(r["capacity"]) < int(min_capacity):
                continue
            n = self.busy_after.get(r["roomId"])
            if n is not None and self.reserved.count(r["roomId"]) >= n:
                r = dict(r, meetingCalendarResponseList=[{"available": False}] * 48)
            out.append(r)
        return out

    def reservable_until(self, room_id):
        d = date.today() + timedelta(days=self.until_days)
        import datetime as _dt
        return int(_dt.datetime.combine(d, _dt.time()).timestamp() * 1000)

    def reserve(self, room_id, **kw):
        self.reserved.append(room_id)
        seq = self.replies.get(room_id) or ["已被预定"]
        msg = seq[0] if len(seq) == 1 else seq.pop(0)
        if msg == "ok":
            return {"ok": True, "message": "预定成功"}
        return {"ok": False, "error": msg}


def runner(tmp, **over):
    s = {"state_file": str(Path(tmp) / "state.json"), "resume": True,
         "cdp_url": "", "timeout": 5000, "result_file": str(Path(tmp) / "r.csv")}
    s.update(over)
    cfg = {"name": "预定会议室", "grab": {
        "grab_timeout_seconds": 3, "poll_ms": 1, "idle_poll_ms": 1,
        "give_up_rounds": 2, "heartbeat_seconds": 999, "retry_room_seconds": 0.01,
        "tries_per_round": 5}}
    r = MeetingRunner(s, cfg, QuietUI())
    return r


TASK = {"repeat_weekly": False, "date": "", "weekday": 1, "start": "14:00",
        "end": "15:00", "min_capacity": 6, "building": "", "building_only": False,
        "room": "", "subject": "会议", "remarks": ""}
ME = {"user_id": 1, "user_name": "我"}


def test_dead_vs_cands():
    print("\n[收摊判定] 累积的「死房」不能拿去和当轮候选比总数")
    tmp = tempfile.mkdtemp()
    r = runner(tmp)
    day = date.today() + timedelta(days=1)

    # A 被服务端硬拒（两次进 dead），随后被别人订走、退出候选；B 全程只是「已被预定」。
    # 老逻辑拿 len(dead)=1 和「当轮候选只剩 B 这 1 间」比，判成「全被服务端拒绝」
    # 提前收摊 —— 而 B 压根不在 dead 里，报出来的原因还是 A 的那句，全错。
    api = FakeApi([room(1, "A"), room(2, "B")],
                  {1: ["您在该时间段已有会议"], 2: ["已被预定"]},
                  busy_after={1: 2})
    res = r._grab(api, dict(TASK), day, ME, [room(1, "A"), room(2, "B")])
    err = res.get("error") or ""
    check("死房退出候选后，不把剩下的房也算成「被服务端拒绝」",
          "全被服务端拒绝" not in err, err)
    # ⚠ 断言的是「别把 A 的理由安到 B 头上」，不是「结论里不许出现 A 的理由」——
    #   A 确实被这么拒过，抹掉它反而是漏报。要求是：两间房各自的回话分开说。
    check("A 的理由只安在 A 头上，B 的回话另说",
          "1 间被服务端拒绝（您在该时间段已有会议）" in err
          and "其余 1 间的回话是「已被预定」" in err, err)

    # 两间都被硬拒 → 这才是真的「全被拒绝」，要立刻收摊并原样报原因
    api2 = FakeApi([room(1, "A"), room(2, "B")],
                   {1: ["您在该时间段已有会议"], 2: ["您在该时间段已有会议"]})
    res2 = r._grab(api2, dict(TASK), day, ME, [room(1, "A"), room(2, "B")])
    err2 = res2.get("error") or ""
    check("两间都被硬拒时确实收摊并报原因",
          "2 间被服务端拒绝" in err2 and "已有会议" in err2, err2)


def test_success():
    print("\n[抢到] 第一枪就中")
    tmp = tempfile.mkdtemp()
    r = runner(tmp)
    day = date.today() + timedelta(days=1)
    api = FakeApi([room(1, "A")], {1: ["ok"]})
    res = r._grab(api, dict(TASK), day, ME, [room(1, "A")])
    check("返回抢到的那间", res.get("ok") and res["room"]["roomName"] == "A", str(res))


def test_given_up_only_weekly():
    print("\n[断点] 单次任务抢不到，不该被永久标成「了结过」")
    tmp = tempfile.mkdtemp()
    r = runner(tmp)
    once = dict(TASK, date=(date.today() + timedelta(days=1)).isoformat())
    every = dict(TASK, repeat_weekly=True, weekday=3)

    r._mark("given_up", every, date.today() + timedelta(days=1))
    check("每周循环的照旧记（不记会原地死循环）", r._settled(every))
    check("单次任务默认没有任何了结记录", not r._settled(once))


def test_candidates():
    print("\n[候选] 人数/楼栋/空闲怎么筛，顺序怎么排")
    tmp = tempfile.mkdtemp()
    r = runner(tmp)
    rooms = [room(1, "大", cap=24), room(2, "刚好", cap=8),
             room(3, "小", cap=4), room(4, "别栋", cap=8, loc="其它楼")]
    task = dict(TASK, min_capacity=8, building="国正中心/2号楼")
    cands = r._candidates(rooms, task)
    check("人数不够的筛掉", all(c["capacity"] >= 8 for c in cands))
    check("优先本楼栋、容量最贴近的排前面",
          [c["roomName"] for c in cands][:2] == ["刚好", "大"],
          str([c["roomName"] for c in cands]))
    busy = room(5, "占着", cap=8, free=False)
    check("要求空闲时，占着的不进候选",
          not r._candidates([busy], task, need_free=(28, 30)))
    check("只要这栋时，别栋的不进候选",
          [c["roomName"] for c in r._candidates(rooms, dict(task, building_only=True))]
          == ["刚好", "大"])


def test_swept_stops():
    print("\n[收摊判定] 当轮候选扫完一遍就收摊，不再空转到超时")
    tmp = tempfile.mkdtemp()
    # ⚠ 关键在 retry_room_seconds 设得比 grab_timeout 还长：这就是真机上的形态 ——
    #   冷却还没过，idle_rounds 那条判据永远等不到「连着几轮没得打」。
    #   2026-09-10 之前这里会一路空转到 grab_timeout。
    r = runner(tmp)
    r.retry_room_s = 999
    r.grab_timeout = 30
    r.tries_per_round = 5
    rooms = [room(i, f"R{i}") for i in range(1, 16)]
    api = FakeApi(rooms, {i: ["该时段已被预定"] for i in range(1, 16)})
    t0 = _t.monotonic()
    res = r._grab(api, dict(TASK), date.today() + timedelta(days=1), ME, rooms)
    cost = _t.monotonic() - t0
    check("15 间扫完一圈就收摊，不等满 grab_timeout", cost < 10, f"{cost:.1f}s")
    check("15 间每间只打一枪，不反复挨同一句", len(api.reserved) == 15,
          f"发了 {len(api.reserved)} 次预定")
    check("结论引用后台原话，不写「全被占用」",
          "该时段已被预定" in (res.get("error") or "")
          and "全被占用" not in (res.get("error") or ""), res.get("error"))


def test_reply_surfaced():
    print("\n[说人话] 后台的拒绝原文要浮到界面上，且同一句不刷屏")
    tmp = tempfile.mkdtemp()
    r = runner(tmp)
    r.retry_room_s = 999
    r.heartbeat_s = 0          # 让心跳每轮都打，好验它的措辞
    rooms = [room(i, f"R{i}") for i in range(1, 6)]
    # 5 间房、两种回话：界面上应该正好出现两条「服务端回话」
    api = FakeApi(rooms, {1: ["空间异常，预定失败"], 2: ["该时段已被预定"],
                          3: ["该时段已被预定"], 4: ["该时段已被预定"],
                          5: ["该时段已被预定"]})
    r._grab(api, dict(TASK), date.today() + timedelta(days=1), ME, rooms)
    said = [x for x in r.ui.lines if "服务端回话" in x]
    check("每种没见过的原文都打到界面上", len(said) == 2, str(said))
    check("原文是后台的原话", any("空间异常，预定失败" in x for x in said)
          and any("该时段已被预定" in x for x in said), str(said))
    beats = [x for x in r.ui.lines if "还盯着" in x]
    check("心跳不再说「刚被占」这种替后台下的结论",
          not any("刚被占" in x for x in r.ui.lines), str(beats))


def test_blocked_stops_now():
    print("\n[提交被挡] 安全校验这类回话，换房没用，必须当场收摊")
    tmp = tempfile.mkdtemp()
    r = runner(tmp)
    r.retry_room_s = 999
    rooms = [room(i, f"R{i}") for i in range(1, 16)]
    # 2026-09-10 真机原话。注意它带「请刷新」——以前正是这三个字把它误判成「被占」。
    msg = "预定失败，浏览器安全校验未通过，请刷新页面后重试"
    api = FakeApi(rooms, {i: [msg] for i in range(1, 16)})
    res = r._grab(api, dict(TASK), date.today() + timedelta(days=1), ME, rooms)
    check("第一间就收摊，不再挨个试", len(api.reserved) == 1,
          f"发了 {len(api.reserved)} 次预定")
    check("结论带上后台原话", msg in (res.get("error") or ""), res.get("error"))
    check("说清这不是「没抢到」", "换哪间会议室都一样" in (res.get("error") or ""),
          res.get("error"))
    check("「请刷新」不再被当成「被占」", not MeetingRunner._is_taken(msg))


def main():
    test_candidates()
    test_success()
    test_dead_vs_cands()
    test_given_up_only_weekly()
    test_swept_stops()
    test_reply_surfaced()
    test_blocked_stops_now()
    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
