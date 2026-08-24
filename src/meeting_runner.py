"""预定会议室的执行器：等窗口开 → 第一时间抢 → 抢不到就接着试。

和这个项目其它执行器最大的不同：**这不是「填表提交」，是「掐点抢占」**。
所以有三处是特意和 Runner 不一样的：

1. 不点 DOM，直接发接口（见 meeting_api 的模块注释）。慢一个数量级就抢不到。
2. 不逐条确认。0 点那一下没有「请核对」的余地，弹窗弹出来位置早没了。
   界面上选「逐条确认」也不会问，只有「空跑」有意义（只找不订，见 dry_run）。
3. run() 可能长期不返回。每周循环的任务抢完一周就排下一周，一直挂着，
   直到用户点停止 —— 这就是它的正常形态，不是卡住了。
4. 「抢不到」也要有个头。掐 10:00 那种要一直守（别人随时可能取消），但目标日
   **本来就在可预定范围内**时守下去毫无意义 —— 该试的一轮就试完了。所以这种
   情况连着 give_up_rounds 轮没有能打的候选就收摊，报「没有符合条件的会议室」
   并说清楚是「一间空的都没有」还是「有空房但服务端拒了」，见 _nothing_left()。
   2026-08-24 之前没有这一条：一次抢不到就是雷打不动空转满 600 秒，而且每轮取到
   的还是同样那几间房（_rank 是确定性的），46 秒能对它们发三百多次预定请求。

抢占窗口（后端 2026-08-20 自己吐出来的原文）：
    「10点之前只能预定5个工作日之内的会议室，10点之后才可预定第6个工作日的会议室」

所以是**每天 10:00 翻一页，按工作日数**，不是零点、也不是自然日。
目标日 D 的开抢时刻由 meeting_data.open_moment(D) 按这条规则算。

⚠ 但本地算不准「工作日」—— 法定节假日和调休补班没有权威日历。所以真正扣扳机前
  一定会拿 /meeting/order/{id}/reservable/date 跟服务端核一次（_wait_for_window）：
  本地负责「大概哪天该醒」，服务端负责「现在到底能不能订」。节假日把窗口推后了
  也不会抢空，顶多多轮询几轮。

⚠ 窗口没开时 spaces 接口返回 0 条，没法提前拿到「那天有哪些房」。但会议室花名册
  跟日期无关，所以预热用**窗口内的日期**查一次拿到房间清单（roomId/容量/位置），
  排好候选顺序；到点直接对第一名发预定，省掉「查询→再提交」的那一次往返。
  窗口刚开的一瞬间几乎所有房都是空的，这一枪命中率很高。
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .browser import Browser
from .meeting_api import ApiError, MeetingApi, is_admin_url
from .meeting_data import (
    OPEN_TIME, RULE_TEXT, SLOTS_PER_DAY, describe, load as load_tasks, normalize,
    open_moment, parse_hhmm, slot_range, submit_times, upcoming_dates, validate,
)
from .preview import PreviewRow
from .ui import BaseUI, ConsoleUI, Stopped

log = logging.getLogger(__name__)

# 「抢不到」的正常回话，遇到就换下一个候选，不当异常。别的报错（签名不对、
# 登录态掉了）要原样冒出来，不能被这个清单吞掉。
TAKEN_HINTS = ("已被预定", "已预定", "不可预定", "已被占用", "冲突", "请刷新")

# 「窗口还没开」的回话。这类不能当成「这间房没了」去换下一个候选 —— 换谁都一样，
# 应该继续等。后端原文见 meeting_data.RULE_TEXT。
NOT_OPEN_HINTS = ("工作日之内", "10点之后", "10点之前")


def _parse_open_time(text) -> time:
    """'10:00' -> time(10, 0)。填错就退回默认，不让一个笔误把整轮抢占带偏。"""
    hm = parse_hhmm(text) if text else None
    return time(hm[0], hm[1]) if hm else OPEN_TIME


class MeetingRunner:
    def __init__(self, settings: dict, form_cfg: dict, ui: BaseUI | None = None):
        self.s = settings
        self.f = form_cfg
        self.ui = ui or ConsoleUI()
        self.state_path = Path(settings["state_file"])
        self.state = self._load_state()
        self.auto = False           # 这个 mode 用不上，留着是为了和别的执行器同形

        o = form_cfg.get("grab") or {}
        self.open_time = _parse_open_time(o.get("open_time"))    # 每天几点翻页，默认 10:00
        self.window_poll_ms = int(o.get("window_poll_ms", 300))  # 翻页时刻前后，多快问一次服务端
        self.window_burst_s = float(o.get("window_burst_seconds", 180))  # 快问持续多久
        self.window_idle_s = float(o.get("window_idle_seconds", 300))    # 快问过了还没开，多久问一次
        self.preroll_ms = int(o.get("preroll_ms", 800))          # 提前多久醒过来盯着
        self.blind_seconds = float(o.get("blind_seconds", 3))    # 盲抢阶段持续多久
        self.blind_tries = int(o.get("blind_tries", 6))          # 盲抢最多打几枪
        self.poll_ms = int(o.get("poll_ms", 700))                 # 查询驱动阶段的轮询间隔
        self.tries_per_round = int(o.get("tries_per_round", 5))   # 每轮最多试几个候选
        self.idle_poll_ms = int(o.get("idle_poll_ms", 3000))      # 一个能打的候选都没有时，降到这个间隔
        self.retry_room_s = float(o.get("retry_room_seconds", 20))  # 同一间房两次重试之间至少隔多久
        self.give_up_rounds = int(o.get("give_up_rounds", 3))     # 目标日已在窗口内时，连着几轮没得打就收摊
        self.heartbeat_s = float(o.get("heartbeat_seconds", 30))  # 长时间盯着时，多久报一次「还活着」
        self.grab_timeout = float(o.get("grab_timeout_seconds", 600))
        self.reminder = int(o.get("reminder_time", 2))            # 2 = 提前15分钟，和页面默认一致
        self.skew = 0.0                                            # 服务器时间 - 本机时间
        self._bad_slots: set = set()                               # 已经抱怨过「时间格数不对」的房，别重复刷屏

    # ---------- 状态 ----------
    @staticmethod
    def task_key(task: dict) -> str:
        """任务的内容指纹。用它记「哪条任务的哪一天已经抢到了」。

        ⚠ 不能用清单里的下标当 key：用户在界面上删一条，后面所有任务的下标
          就全错位了，已完成记录会安到别的任务头上。
        """
        t = normalize(task)
        raw = json.dumps([t["repeat_weekly"], t["date"], t["weekday"], t["start"],
                          t["end"], t["subject"]], ensure_ascii=False, sort_keys=True)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]

    def _load_state(self) -> dict:
        if self.s.get("resume") and self.state_path.exists():
            try:
                st = json.loads(self.state_path.read_text(encoding="utf-8"))
                return st.get(self.f["name"], {"booked": {}})
            except (OSError, ValueError):
                log.warning("断点文件读不了，当作从头开始", exc_info=True)
        return {"booked": {}}

    def _save_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        allst = {}
        if self.state_path.exists():
            try:
                allst = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        allst[self.f["name"]] = self.state
        self.state_path.write_text(json.dumps(allst, ensure_ascii=False, indent=2),
                                   encoding="utf-8")

    def clear_state(self):
        self.state = {"booked": {}, "given_up": {}}
        self._save_state()

    def _mark(self, bucket: str, task: dict, day: date):
        lst = self.state.setdefault(bucket, {}).setdefault(self.task_key(task), [])
        if day.isoformat() not in lst:
            lst.append(day.isoformat())
        self._save_state()

    def _booked(self, task: dict) -> set:
        return set(self.state.get("booked", {}).get(self.task_key(task), []))

    def _settled(self, task: dict) -> set:
        """这条任务不用再管的日期：抢到了的，和试到超时放弃了的。

        ⚠ 放弃的也必须记下来。不记的话，每周循环的任务一旦有一周没抢到，
          _next_day 会一直返回同一天（那天早就在窗口内了，等待时间为 0），
          于是原地死循环重试同一天，后面几周永远轮不到。
        """
        return self._booked(task) | set(
            self.state.get("given_up", {}).get(self.task_key(task), []))

    # ---------- 预检 ----------
    def _tasks(self) -> list[dict]:
        """任务清单：优先用界面传下来的，没有就读存盘。"""
        given = self.s.get("meeting_tasks")
        if given is not None:
            return [normalize(t) for t in given]
        return load_tasks(self.f)["tasks"]

    def preview(self) -> list[PreviewRow]:
        """校验任务清单 + 算出每条什么时候开抢。不碰浏览器。"""
        today = date.today()
        out = []
        for i, task in enumerate(self._tasks()):
            if not task.get("enabled", True):
                continue
            issues = validate(task, today)
            settled = self._settled(task)
            days = [d for d in upcoming_dates(task, today) if d.isoformat() not in settled]
            if not issues and not days:
                issues.append("这条已经了结过了（抢到过、或试到超时放弃过）。要重抢先点「清除断点」")

            nxt = days[0] if days else None
            when = ""
            if nxt:
                openat = open_moment(nxt, self.open_time)
                when = ("目标 %s，现在就能抢" % nxt if datetime.now() >= openat
                        else "目标 %s，%s 开抢" % (nxt, openat.strftime("%m-%d %H:%M:%S")))
            out.append(PreviewRow(
                index=i + 1, name=describe(task), kind=when,
                detail_count=len(days), issues=issues, done=False,
                payload={"header": {"任务": describe(task), "开抢时刻": when},
                         "items": [{"目标日期": d.isoformat()} for d in days[:8]],
                         "task": task},
            ))
        return out

    # ---------- 候选筛选 ----------
    @staticmethod
    def _matches(room: dict, task: dict) -> bool:
        loc = str(room.get("location") or "")
        name = str(room.get("roomName") or "")
        want_room = task.get("room") or ""
        if want_room:
            return want_room in name
        if task.get("building_only") and task.get("building"):
            return task["building"] in loc
        return True

    @staticmethod
    def _rank(room: dict, task: dict) -> tuple:
        """排序键：楼栋优先 → 容量最贴近 → roomId 稳定兜底。

        容量按「最贴近下限」排，是为了别拿 24 人的大会议室去开 4 人的会 ——
        抢占本来就挤，占了大房是给别人添堵。
        """
        loc = str(room.get("location") or "")
        prefer = 0 if (task.get("building") and task["building"] in loc) else 1
        cap = int(room.get("capacity") or 0)
        return (prefer, max(0, cap - int(task.get("min_capacity") or 1)), int(room.get("roomId") or 0))

    def _candidates(self, rooms: list[dict], task: dict, need_free: tuple | None = None) -> list[dict]:
        """按条件筛 + 排序。need_free 给了就再要求那段格子全空。"""
        out = []
        for r in rooms:
            if int(r.get("capacity") or 0) < int(task.get("min_capacity") or 1):
                continue
            if not self._matches(r, task):
                continue
            if need_free and not self._free(r, *need_free):
                continue
            out.append(r)
        return sorted(out, key=lambda r: self._rank(r, task))

    def _free(self, room: dict, i0: int, i1: int) -> bool:
        """这间房在 [i0, i1) 这段格子里是不是全空。

        ⚠ 格子数不是 48 的房只能跳过（没法判断哪一格对应哪半小时），但**必须吭一声**：
          静默剔除的话，用户看到的是「没有符合条件的会议室」，而真实原因是接口
          返回的形状不对 —— 这两者的处理办法完全不同，混在一起就没法查。
          每间房只抱怨一次，别把 600 秒的轮询变成刷屏。
        """
        slots = room.get("meetingCalendarResponseList") or []
        if len(slots) != SLOTS_PER_DAY:
            rid = room.get("roomId")
            if rid not in self._bad_slots:
                self._bad_slots.add(rid)
                msg = (f"{room.get('roomName')}（roomId={rid}）返回了 {len(slots)} 个时间格，"
                       f"应该是 {SLOTS_PER_DAY} 个，判断不了空闲，跳过这间")
                log.warning(msg)
                self.ui.log(f"  {msg}", "warn")
            return False
        return all(bool((slots[i] or {}).get("available")) for i in range(i0, min(i1, SLOTS_PER_DAY)))

    # ---------- 时钟 ----------
    def _now(self) -> datetime:
        """按服务器时间算的「现在」。抢占全程只用这个，不用裸 datetime.now()。"""
        return datetime.now() + timedelta(seconds=self.skew)

    def _sleep(self, seconds: float):
        """可被停止/暂停打断的等待。长等待切成小段，别让「停止」按钮按下去没反应。"""
        end = _time.monotonic() + max(0.0, seconds)
        while True:
            self.ui.checkpoint()
            left = end - _time.monotonic()
            if left <= 0:
                return
            _time.sleep(min(left, 0.25))

    # ---------- 抢一次 ----------
    def _try_reserve(self, api: MeetingApi, room: dict, task: dict, day: date, me: dict) -> dict:
        start, end = submit_times(day, task["start"], task["end"])
        # ⚠ 空跑早在 _grab 入口就分流到 _dry_probe 了，正常走不到这里。
        #   这一道留着是兜底 —— 提交是不可逆的，宁可多一层挡板。
        if self.s.get("dry_run"):
            return {"ok": False, "error": "空跑，未提交", "dry": True}
        return api.reserve(
            room_id=int(room["roomId"]), start=start, end=end,
            subject=task["subject"], user_id=me["user_id"],
            reminder=self.reminder, remarks=task.get("remarks", ""))

    def _grab(self, api: MeetingApi, task: dict, day: date, me: dict, warm: list[dict]) -> dict:
        """把 day 这一天抢下来。返回 {ok, room, error}。"""
        rng = slot_range(task["start"], task["end"])
        if not rng:
            return {"ok": False, "error": "时段不合法"}
        i0, i1 = rng
        day_str = day.isoformat()
        openat = open_moment(day, self.open_time)
        label = f"{day_str} {task['start']}~{task['end']}"

        # ⚠ 空跑绝不能跟着等窗口。空跑的意义就是「现在就告诉我这条任务靠不靠谱」，
        #   跟着等的话，一条一周后的任务能让整个空跑挂在那儿几天不出声
        #   （实测过：目标 8/28、窗口 8/21 才开，命令行空跑就那么卡住了）。
        if self.s.get("dry_run"):
            return self._dry_probe(api, task, day, (i0, i1), label, openat)

        # ---- 等到服务端真的放开这一天 ----
        # ⚠ 判据是服务端的 reservable/date，不是本地按公式算的时刻。本地算「工作日」
        #   不认法定节假日和调休，只能用来决定「哪天醒过来」；能不能订得问服务端。
        probe_room = int(warm[0]["roomId"])
        was_waiting = self._now() < openat
        self._wait_for_window(api, day, probe_room, label)
        if was_waiting:
            self.ui.log(f"「{label}」窗口已打开，开抢", "ok")
        else:
            self.ui.log(f"「{label}」已在可预定范围内，立刻开抢")

        opened_at = self._now()          # 窗口确认打开的那一刻，盲抢的计时基准
        deadline = _time.monotonic() + self.grab_timeout
        tried = set()

        # ---- 盲抢 ----
        # 只在「刚等到窗口打开」时做：那一瞬间几乎所有房都还空着，直接对候选第一名
        # 发预定，省掉一次「查询→再提交」的往返。目标日本来就已经在窗口内的话不盲抢，
        # 那时候房多半被订掉一部分了，盲打纯属浪费往返，直接进查询驱动更快。
        if was_waiting:
            for room in warm[:self.blind_tries]:
                self.ui.checkpoint()
                if self._now() > opened_at + timedelta(seconds=self.blind_seconds):
                    break
                res = self._try_reserve(api, room, task, day, me)
                if res.get("ok"):
                    return {"ok": True, "room": room}
                err = res.get("error", "")
                # 窗口其实还没开（本地把工作日算早了，或者服务端刚好卡在边界上）：
                # 换哪个候选都一样，别把候选打空，退回去继续等。
                if self._is_not_open(err):
                    self.ui.log(f"  服务端说还没开放，继续等：{err}", "warn")
                    self._wait_for_window(api, day, probe_room, label)
                    opened_at = self._now()
                    continue
                tried.add(room["roomId"])
                if not self._is_taken(err):
                    self.ui.log(f"  {room.get('roomName')}：{err}", "warn")

        # ---- 查询驱动 ----
        # ⚠ 这一段的全部状态都是为了一件事：**别对同一批房反复打枪**。
        #   `_rank` 的排序键是确定性的，所以「每轮重查一次再取前 N 名」取到的
        #   永远是同样那几间 —— 2026-08-24 的实测日志里，46 秒对同样 5 间房发了
        #   三百多次预定请求，界面上就是同样几个名字滚个不停。所以：
        #     · cooling  回话是「被占」的房。它可能因为别人取消而放出来，不能摘掉，
        #                但要压一段时间再问；顺带让候选自然轮转到后面几名。
        #     · dead     不是「被占」的失败（自己同时段已有会、没权限、时段不合法…）。
        #                这类再问一百遍还是同一句话，两次之后就摘掉。
        rounds = 0
        idle_rounds = 0                # 连着几轮一个能打的候选都没有
        last_err = "超时没抢到"
        seen_cands = 0                 # 见过的最大候选数，用来区分「没房」和「房都被占」
        cooling: dict = {}             # roomId -> 可以再试的 monotonic 时刻
        dead: dict = {}                # roomId -> 摘掉它的原因（连着两次同类失败才摘）
        rejected: dict = {}            # roomId -> 最近一次「非被占」的失败原文
        strikes: dict = {}             # roomId -> 非「被占」的失败次数
        logged: set = set()            # 已经落过盘的失败原文，避免刷屏 run.log
        started = _time.monotonic()
        next_beat = started + self.heartbeat_s

        while _time.monotonic() < deadline:
            self.ui.checkpoint()
            rounds += 1
            try:
                rooms = api.spaces(day_str, task["min_capacity"])
            except ApiError as e:
                last_err = str(e)
                self.ui.log(f"  查询失败，稍后重试：{e}", "warn")
                self._sleep(self.poll_ms / 1000.0)
                continue

            cands = self._candidates(rooms, task, need_free=(i0, i1))
            seen_cands = max(seen_cands, len(cands))

            now_m = _time.monotonic()
            fresh = [r for r in cands
                     if r["roomId"] not in dead and cooling.get(r["roomId"], 0.0) <= now_m]

            for room in fresh[:self.tries_per_round]:
                self.ui.checkpoint()
                rid = room["roomId"]
                tried.add(rid)
                res = self._try_reserve(api, room, task, day, me)
                if res.get("ok"):
                    return {"ok": True, "room": room}
                last_err = res.get("error", "")

                if self._is_not_open(last_err):
                    self.ui.log(f"  服务端说还没开放，继续等：{last_err}", "warn")
                    self._wait_for_window(api, day, probe_room, label)
                    # 回到「掐点抢」的形态：窗口刚开的那一刻满盘皆空，之前攒的
                    # cooling/dead 全部作废，也不能再按「已在窗口内」提前收摊。
                    was_waiting = True
                    cooling.clear()
                    dead.clear()
                    rejected.clear()
                    strikes.clear()
                    idle_rounds = 0
                    break

                # ⚠ 失败原文以前只在界面上一闪而过，run.log 里一个字都没有 ——
                #   事后没法回答「到底是被抢走了还是参数不对」。而 TAKEN_HINTS 是
                #   照着后台的口气猜的（接口文档只抓到了成功和窗口外两种原文），
                #   猜漏了就会一直走下面的「非正常失败」分支。所以每种原文落一次盘。
                if last_err not in logged:
                    logged.add(last_err)
                    log.info("预定被拒：%s(roomId=%s) %s → %s",
                             room.get("roomName"), rid, label, last_err)

                if self._is_taken(last_err):
                    cooling[rid] = now_m + self.retry_room_s
                    continue

                # ⚠ 第一次就记，不能等它攒够两次被摘掉才记 —— 摘房要两次 strike，
                #   而「已在窗口内」的任务三轮就收摊了，等 dead 攒起来根本来不及。
                #   结论文案要是只看 dead，后台明明说的是「您在该时间段已有会议」，
                #   报出来却成了「全被占用」，等于把人往错的方向指。
                rejected[rid] = last_err
                strikes[rid] = strikes.get(rid, 0) + 1
                if strikes[rid] >= 2:
                    dead[rid] = last_err
                    self.ui.log(f"  {room.get('roomName')} 连着 {strikes[rid]} 次"
                                f"「{last_err}」，不再试它", "warn")
                else:
                    cooling[rid] = now_m + self.retry_room_s
                    self.ui.log(f"  {room.get('roomName')}：{last_err}", "warn")

            # ---- 收摊判定 ----
            idle_rounds = 0 if fresh else idle_rounds + 1

            # 候选被服务端全盘拒绝，而且不是「被占」——这不是「等等就有」，是这条
            # 任务本身有问题（同时段自己已经有会、没有该楼栋权限之类）。等下去
            # 只会把 600 秒耗光，两种模式都直接收，把原因原样报出去。
            if cands and len(dead) >= len(cands):
                reasons = list(dead.values())
                why = max(set(reasons), key=reasons.count)
                return {"ok": False,
                        "error": f"符合条件的 {len(cands)} 间全被服务端拒绝：{why}"}

            # 目标日已经在可预定范围内（不是掐点抢），连着几轮没有能打的候选 ——
            # 说明该试的都试过了，继续空转到 10 分钟超时没有任何意义。
            if not was_waiting and idle_rounds >= self.give_up_rounds:
                return {"ok": False,
                        "error": self._nothing_left(task, seen_cands, tried, rejected)}

            if _time.monotonic() >= next_beat:
                next_beat = _time.monotonic() + self.heartbeat_s
                self.ui.log(f"  「{label}」还盯着：符合条件 {seen_cands} 间"
                            f"（{len(cooling)} 间刚被占、{len(dead)} 间已排除），"
                            f"已试 {len(tried)} 间，"
                            f"已等 {self._human(_time.monotonic() - started)}，"
                            f"还剩 {self._human(deadline - _time.monotonic())}")

            if rounds == 1 and not cands:
                self.ui.log("  当前没有满足条件的空房，继续盯着（别人取消就立刻补上）")

            # ⚠ 有能打的候选才快轮。空转时还按 700ms 一轮，等于 10 分钟里把 spaces
            #   的分页翻上千遍 —— 对内网后台是纯添堵，对抢占一点帮助都没有。
            self._sleep((self.poll_ms if fresh else self.idle_poll_ms) / 1000.0)

        return {"ok": False, "error": f"{last_err}（试过 {len(tried)} 间，等了"
                                      f" {self._human(self.grab_timeout)}）"}

    def _nothing_left(self, task: dict, seen: int, tried: set, rejected: dict) -> str:
        """目标日已经在可预定范围内、却一间也拿不下时的结论文案。

        ⚠ 这句话是用户唯一能看到的结论，不能是「超时没抢到」这种等于没说的话 ——
          「一间空的都没有」和「有空房但都被拒」要让人一眼分得开：前者该放宽条件，
          后者该去看是不是自己同时段已经有会了。
        """
        where = task.get("room") or (
            f"{task['building']}{'（只要这栋）' if task.get('building_only') else '（优先）'}"
            if task.get("building") else "不限楼栋")
        cond = f"{where} · ≥{task.get('min_capacity')}人 · {task['start']}~{task['end']}"
        if not seen:
            return (f"没有符合条件的会议室：{cond}，这个时段一间空的都没有"
                    f"（放宽人数或楼栋条件再试）")
        if rejected:
            reasons = list(rejected.values())
            why = max(set(reasons), key=reasons.count)
            rest = seen - len(rejected)
            return (f"没有符合条件的会议室：{cond} 符合的 {seen} 间里，"
                    f"{len(rejected)} 间被服务端拒绝（{why}）"
                    + (f"，其余 {rest} 间被占" if rest > 0 else ""))
        return (f"没有符合条件的会议室：{cond} 符合的 {seen} 间全被占用，"
                f"试过 {len(tried)} 间都没拿下（目标日已在可预定范围内，不再空等）")

    def _dry_probe(self, api: MeetingApi, task: dict, day: date, rng: tuple,
                   label: str, openat: datetime) -> dict:
        """空跑：马上查一次「这个时段现在有多少间符合条件」，不等窗口、不提交。

        ⚠ 目标日还没开放时 spaces 返回 0 条，直接查会得出「一间都没有」这种
          吓人又没用的结论。所以那种情况改查「最远可订的那天」当样本，
          并且在日志里说清楚查的是哪天 —— 宁可标注清楚，也不能让人误以为
          看到的就是目标日的真实情况。
        """
        probe = day
        note = ""
        if self._now() < openat:
            probe = self.furthest or date.today()
            note = f"（{day} 的窗口 {openat:%m-%d %H:%M} 才开，这里拿最远可订的 {probe} 当样本）"

        rooms = api.spaces(probe.isoformat(), task["min_capacity"])
        cands = self._candidates(rooms, task, need_free=rng)
        names = "、".join(f"{c.get('roomName')}（{c.get('location')}）" for c in cands[:5])
        self.ui.log(f"  [空跑] 「{label}」符合条件且空着的有 {len(cands)} 间{note}")
        self.ui.log(f"         {names or '一间都没有 —— 放宽人数或楼栋条件试试'}",
                    "info" if cands else "warn")
        return {"ok": False, "error": "空跑，未提交", "dry": True, "candidates": len(cands)}

    @staticmethod
    def _is_taken(err: str) -> bool:
        return any(h in str(err) for h in TAKEN_HINTS)

    @staticmethod
    def _is_not_open(err: str) -> bool:
        """这条报错是不是「窗口还没开」而不是「这间房没了」。

        两者要分开处理：房没了就换下一个候选，窗口没开就得退回去等 ——
        当成前者的话，会在窗口打开前把候选一个个打空。
        """
        return any(h in str(err) for h in NOT_OPEN_HINTS)

    @staticmethod
    def _human(seconds: float) -> str:
        seconds = int(max(0, seconds))
        if seconds < 60:
            return f"{seconds}秒"
        if seconds < 3600:
            return f"{seconds // 60}分{seconds % 60}秒"
        if seconds < 86400:
            return f"{seconds // 3600}小时{seconds % 3600 // 60}分"
        return f"{seconds // 86400}天{seconds % 86400 // 3600}小时"

    # ---------- 主循环 ----------
    def run(self, records: list[dict] | None = None):
        if records is None:
            records = [r.payload for r in self.preview() if not r.issues]
        tasks = [normalize(r["task"]) for r in records if r.get("task")]
        if not tasks:
            self.ui.log("没有可执行的抢占任务", "warn")
            return []

        dry = bool(self.s.get("dry_run"))
        stats = {"ok": 0, "failed": 0, "skipped": 0, "dry": 0}
        outcome = [None] * len(tasks)       # 每条任务一个最终结果，和 records 一一对应
        occurrences = []                     # 每一次抢占的流水，写 CSV
        fatal = ""                           # 整轮挂掉的原因（不是「用户点了停止」）

        self.ui.log(f"共 {len(tasks)} 条抢占任务" + ("（空跑：只找不订）" if dry else ""))
        if not dry:
            self.ui.log("⚠ 抢占模式不会逐条弹窗确认 —— 窗口开的那一瞬间没有等人点确认的余地", "warn")
        self.ui.progress(0, len(tasks), stats)

        try:
            with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
                api = MeetingApi(self._pick_page(b))
                api.ensure_page(self.s["timeout"])
                me = api.me()
                self.ui.log(f"登录身份：{me['user_name']}（userId={me['user_id']}）")

                self.skew = api.clock_skew()
                if abs(self.skew) >= 1:
                    self.ui.log(f"本机时钟比服务器{'慢' if self.skew > 0 else '快'}"
                                f" {abs(self.skew):.1f} 秒，抢占已按服务器时间校准", "warn")

                warm_all = self._warmup(api, tasks)
                self._read_horizon(api, warm_all)

                # 每条任务一个游标：接下来要抢哪一天
                pending = list(range(len(tasks)))
                while pending:
                    self.ui.checkpoint()
                    picked = self._pick_next(tasks, pending)
                    if picked is None:
                        break
                    idx, day = picked
                    task = tasks[idx]

                    warm = self._candidates(warm_all, task)
                    if not warm:
                        msg = "没有任何会议室满足条件（人数/楼栋/指定会议室对不上）"
                        outcome[idx] = self._result(idx, task, "failed", msg)
                        stats["failed"] += 1
                        self.ui.log(f"[{idx + 1}] {describe(task)} —— {msg}", "error")
                        pending.remove(idx)
                        self.ui.progress(len(tasks) - len(pending), len(tasks), stats)
                        continue

                    self.ui.log(f"[{idx + 1}] {describe(task)} —— 候选 {len(warm)} 间，"
                                f"首选 {warm[0].get('roomName')}（{warm[0].get('location')}）")

                    res = self._grab(api, task, day, me, warm)
                    occurrences.append({
                        "任务": describe(task), "目标日期": day.isoformat(),
                        "时段": f"{task['start']}~{task['end']}",
                        "结果": "ok" if res.get("ok") else ("dry_run" if res.get("dry") else "failed"),
                        "会议室": (res.get("room") or {}).get("roomName", ""),
                        "位置": (res.get("room") or {}).get("location", ""),
                        "错误": res.get("error", ""),
                    })

                    if res.get("ok"):
                        room = res["room"]
                        self._mark("booked", task, day)
                        stats["ok"] += 1
                        outcome[idx] = self._result(
                            idx, task, "ok", "",
                            room=f"{room.get('roomName')}（{room.get('location')}）", day=day)
                        self.ui.log(f"[{idx + 1}] 抢到了：{room.get('roomName')} "
                                    f"{room.get('location')} {day} {task['start']}~{task['end']}", "ok")
                    elif res.get("dry"):
                        stats["dry"] += 1
                        outcome[idx] = self._result(idx, task, "dry_run", "", day=day)
                    else:
                        self._mark("given_up", task, day)
                        stats["failed"] += 1
                        outcome[idx] = self._result(idx, task, "failed", res.get("error", ""), day=day)
                        self.ui.log(f"[{idx + 1}] 没抢到：{res.get('error')}", "error")

                    # 每周循环：这一周了结了（抢到或放弃）就立刻排下一周，一直挂着
                    # 直到用户点停止。单次任务到此为止。
                    # ⚠ 空跑不排下一周：空跑本来就是「看一眼当前有什么」，
                    #   循环下去只会刷屏，也永远不会结束。
                    if task.get("repeat_weekly") and not dry:
                        nxt = self._next_day(task, after=day)
                        if nxt:
                            self.ui.log(f"[{idx + 1}] 每周循环：下一轮目标 {nxt}，"
                                        f"{open_moment(nxt, self.open_time):%m-%d %H:%M:%S} 开抢")
                            # ⚠ 别忘了这一下：pending 没变，但 stats 变了（刚抢到/刚放弃）。
                            #   不刷的话，挂着跑几天的任务界面上永远停在 0，
                            #   看不出它到底干成了几件事。
                            self.ui.progress(len(tasks) - len(pending), len(tasks), stats)
                            continue          # 留在 pending 里，下一轮重新排队
                    pending.remove(idx)
                    self.ui.progress(len(tasks) - len(pending), len(tasks), stats)

        except Stopped:
            self.ui.log("已停止", "warn")
        except ApiError as e:
            self.ui.log(f"接口出错，本轮中断：{e}", "error")
            log.exception("接口出错")
            fatal = f"接口出错，整轮中断：{e}"
        except Exception as e:
            # ⚠ 别只兜 ApiError：浏览器那头的异常（页面被关掉、上下文被跳转打断）
            #   是 playwright 自己的异常类型，漏出去会跳过下面的 finally 里那份
            #   收尾报告，用户只看到一个红色堆栈，不知道跑到哪一条了。
            self.ui.log(f"运行中断：{e}", "error")
            log.exception("运行中断")
            fatal = f"运行中断：{e}"
        finally:
            # ⚠ 「没轮到」和「整轮挂了」必须分开报。2026-08-24 11:31 那次
            #   me() 读登录信息返回了一个 404 网页，整轮一条都没跑，收尾却把它
            #   记成 skipped「还没轮到就停了」—— 埋点和界面上都看不出真实原因，
            #   用户只知道「没轮到 1 条」，完全查不下去。
            results = []
            for i, o in enumerate(outcome):
                if o:
                    results.append(o)
                elif fatal:
                    stats["failed"] += 1
                    results.append(self._result(i, tasks[i], "failed", fatal))
                else:
                    stats["skipped"] += 1
                    results.append(self._result(i, tasks[i], "skipped", "还没轮到就停了"))
            self._write_results(occurrences)
            self._report(results, stats, dry)

        return results

    # ---------- 主循环的零件 ----------
    def _pick_page(self, b: Browser):
        """挑一个已经开在行政平台上的标签页；没有才用 Browser 默认那个。

        ⚠ Browser 拿的是 contexts[0].pages[0]，也就是「最早开的那个标签页」——
          对别的执行器没问题，但这里如果直接用它，ensure_page 会把用户正在看的
          某个别的后台页面导航走（实测把广告后台的单元页顶掉了）。抢会议室是
          挂着跑几天的活，更不能乱动用户的标签页。
        """
        for ctx in b.browser.contexts:
            for p in ctx.pages:
                try:
                    if is_admin_url(p.url):
                        p.set_default_timeout(self.s["timeout"])
                        return p
                except Exception:
                    continue
        # ⚠ 兜底是「新开一个标签页」，不是征用当前这个。抢会议室要挂着跑几天，
        #   把用户正在看的页面导航走是不能接受的（实测顶掉过广告后台的单元页）。
        #   新开的这个跑完不关：下次再跑时上面那个循环就能认出来并复用，不会越积越多。
        try:
            ctx = b.browser.contexts[0] if b.browser.contexts else None
            if ctx is not None:
                page = ctx.new_page()
                page.set_default_timeout(self.s["timeout"])
                self.ui.log("没找到开着行政平台的标签页，已新开一个（不动你正在看的页面）")
                return page
        except Exception:
            log.warning("新开标签页失败，退回用当前标签页", exc_info=True)
        self.ui.log("新开标签页失败，只能把当前标签页导航过去", "warn")
        return b.page

    def _warmup(self, api: MeetingApi, tasks: list[dict]) -> list[dict]:
        """拿一份会议室花名册。

        ⚠ 必须用**窗口内**的日期查：窗口外 spaces 返回 0 条（实测）。花名册本身
          和日期无关，所以用今天查完全够用，拿的是 roomId/容量/位置这些静态信息。
        """
        need = min((int(t.get("min_capacity") or 1) for t in tasks), default=1)
        rooms = api.spaces(date.today().isoformat(), need)
        self.ui.log(f"会议室花名册：{len(rooms)} 间（容纳 ≥{need} 人）")
        return rooms

    def _read_horizon(self, api: MeetingApi, rooms: list[dict]):
        """问服务端「现在最远能订到哪天」，只为在日志里给个交代。

        ⚠ 这个值不参与算开抢时刻 —— 开抢时刻按 meeting_data.open_moment（10 点 + 工作日）
          算，真正能不能订由 _wait_for_window 每次现问服务端。这里纯粹是开跑时
          让人一眼看到「现在的边界在哪」，好对得上预检里显示的开抢时刻。
        """
        self.furthest = None
        if not rooms:
            return
        try:
            until_ms = api.reservable_until(int(rooms[0]["roomId"]))
            self.furthest = datetime.fromtimestamp(until_ms / 1000).date()
        except (ApiError, KeyError, TypeError, ValueError):
            self.ui.log("没问出「最远可订到哪天」，不影响抢占（到点会现问服务端）", "warn")
            return
        self.ui.log(f"服务端现在最远可订到 {self.furthest}（规则：{RULE_TEXT}）")

    def _window_open(self, api: MeetingApi, day: date, room_id: int) -> bool:
        """服务端认不认「day 现在可以订」。问不出来时按「还没开」处理。

        ⚠ 宁可误判成「还没开」也不能误判成「开了」：判成没开只是多等一轮，
          判成开了会让盲抢阶段把候选一个个打空，真开的时候候选已经用完了。
        """
        try:
            return datetime.fromtimestamp(api.reservable_until(room_id) / 1000).date() >= day
        except (ApiError, TypeError, ValueError):
            return False

    def _wait_for_window(self, api: MeetingApi, day: date, room_id: int, label: str) -> bool:
        """等到服务端真的放开 day 这一天。已经放开就立刻返回 True。

        本地只负责「大概哪天该醒」（open_moment 按 10 点 + 工作日算），服务端负责
        「现在到底能不能订」。这样法定节假日 / 调休把窗口推后了也不会抢空 ——
        本地算早了，无非是多问几轮。

        节奏：
          · 离预计开抢时刻还早 → 睡到「预计时刻 - preroll」
          · 到了附近 → 每 window_poll_ms 问一次，持续 window_burst_s
          · 还没开（多半是本地把工作日算早了）→ 降到每 window_idle_s 问一次，
            并且把下一次快问对齐到下一个 10:00
        """
        openat = open_moment(day, self.open_time)
        announced = False
        while True:
            self.ui.checkpoint()
            if self._window_open(api, day, room_id):
                return True

            now = self._now()
            # 预计时刻还没到就等它；已经过了就盯住下一个翻页时刻
            aim = openat if now < openat else self._next_flip(now)
            lead = (aim - now).total_seconds() - self.preroll_ms / 1000.0

            if lead > 0:
                if not announced:
                    self.ui.log(f"「{label}」等窗口打开：{aim:%Y-%m-%d %H:%M:%S}"
                                f"（还有 {self._human(lead)}）")
                    announced = True
                # 切成不超过 window_idle_s 的段，中途也回来问一次 ——
                # 万一行政临时放开了，不至于傻等到预计时刻
                self._sleep(min(lead, self.window_idle_s))
                continue

            announced = False
            since = (now - aim).total_seconds()
            self._sleep(self.window_poll_ms / 1000.0 if since <= self.window_burst_s
                        else self.window_idle_s)

    def _next_flip(self, now: datetime) -> datetime:
        """下一个翻页时刻（每天 open_time）。"""
        today_at = datetime.combine(now.date(), self.open_time)
        return today_at if now < today_at else datetime.combine(
            now.date() + timedelta(days=1), self.open_time)

    def _next_day(self, task: dict, after: date | None = None) -> date | None:
        """这条任务接下来要抢的日期：跳过已了结的（抢到 / 放弃），也跳过 after 及之前的。"""
        settled = self._settled(task)
        # 每周循环的任务要能一直排下去，这里往后铺一年；preview 只看最近几周就够了
        for d in upcoming_dates(task, date.today(), count=53):
            if after and d <= after:
                continue
            if d.isoformat() in settled:
                continue
            return d
        return None

    def _pick_next(self, tasks: list[dict], pending: list[int]):
        """挑「最先要动手」的那条。

        串行处理是有意的：抢一次就是几百毫秒的事，而窗口时刻不同的任务本来就该
        按先后排队。挑最早的那条去等，等于所有别的任务都还没到点，不会误事。
        """
        best = None
        for i in list(pending):
            day = self._next_day(tasks[i])
            if day is None:
                pending.remove(i)
                continue
            at = open_moment(day, self.open_time)
            if best is None or at < best[2]:
                best = (i, day, at)
        return (best[0], best[1]) if best else None

    # ---------- 输出 ----------
    def _result(self, idx, task, status, error, room="", day=None):
        return {
            "序号": idx + 1,
            "状态": status,
            "错误": error,
            "任务": describe(task),
            "目标日期": day.isoformat() if day else "",
            "抢到的会议室": room,
            "会议主题": task.get("subject", ""),
        }

    def _write_results(self, rows):
        if not rows:
            return
        path = Path(self.s["result_file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("w", newline="", encoding="utf-8-sig") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
        except PermissionError:
            self.ui.log(f"{path} 被占用（是不是用 Excel 开着？），结果没写进去", "warn")

    def _report(self, results, stats, dry):
        lines = [f"配置类型：{self.f['name']}", f"共 {len(results)} 条抢占任务"]
        if dry:
            lines.append(f"空跑 {stats['dry']} 条（只找不订）")
        else:
            lines.append(f"抢到 {stats['ok']} 条")
            if stats["failed"]:
                lines.append(f"没抢到 {stats['failed']} 条 ← 看结果表的「错误」列")
            if stats["skipped"]:
                lines.append(f"没轮到 {stats['skipped']} 条")
        lines += ["", f"明细：{self.s['result_file']}"]
        bad = stats["failed"] > 0
        self.ui.finished("抢占结束" if not bad else "抢占结束（有没抢到的）",
                         "\n".join(lines), not bad)
