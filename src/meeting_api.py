"""行政管理平台「会务预定」的接口封装。

⚠ 为什么不点 DOM：这是抢占场景。窗口开的那一瞬间（每天 10:00 整）全公司都在刷，
  点 DOM 要等 SPA 渲染完日历、再定位到具体半小时格子，实测这一套下来是「秒」级；
  而这套页面的三个接口是现成的，直接发请求是「百毫秒」级。
  差的这一个数量级就是抢得到和抢不到的区别。

⚠ 所有请求都在用户那个已登录页面的 origin 里用 fetch 发（page.evaluate），
  不在 Python 侧自己带 cookie —— 和这个项目其它执行器一样，脚本永远不碰
  账号密码，登录态完全依赖用户自己的浏览器。

接口清单（都在 <行政平台>/f1-space 下，见 ORIGINS；2026-08-19/20 实测）：
  POST /meeting/order/spaces                     查某天所有会议室 + 48 个半小时格的占用
  GET  /meeting/order/{roomId}/reservable/date   该会议室最远可预定到哪天（毫秒时间戳）
  POST /meeting/order                            提交预定（要签名，见 sign()）
  PUT  /meeting/myreservation/cancel             取消预定
  GET  /settings/emps/baseInfo                   当前登录人（拿 userId）

⚠ spaces 的 location 条件是坏的：传楼栋 id 返回 0 条，传字符串（nodeKey/code/
  中文名）后端直接 500（"For input string: xxx"）。所以楼栋一律不走服务端过滤，
  改成客户端拿返回里的 location 字段（形如「国正中心/2号楼/5F」）自己匹配。
  capacity 条件是好的，实测语义是「≥」，能有效减少要翻的页数，保留。
"""
from __future__ import annotations

import hashlib
import logging
import re
from email.utils import parsedate_to_datetime

log = logging.getLogger(__name__)

BASE = "/f1-space"

# ⚠ 这套后台有两个域名：biliapi.net 会跳到 bilibili.co。只认一个的话，
#   已经开着的那个标签页会认不出来（于是去征用用户正在看的别的页），
#   而且每次 ensure_page 都觉得「不在目标域名上」，白导航一遍。
#   2026-08-20 实测：goto biliapi.net 之后，页面落在 bilibili.co。
ORIGINS = ("https://administration.biliapi.net", "https://administration.bilibili.co")
ORIGIN = ORIGINS[0]
PAGE_URL = f"{ORIGIN}{BASE}/web/index.html#/home/meeting/reserve"

# ⚠ 认标签页时**不能带 scheme**。2026-08-24 实测：登录态过期后走一遍 SSO，
#   页面会落在 http://administration.bilibili.co/f1-space/...（明文，不是 https）。
#   只认 https 的话 _pick_page 认不出这个已经开着的标签页，于是每跑一次新开一个；
#   而新开的那个 goto(PAGE_URL) 之后同样落在 http，于是**永远不会被复用** ——
#   这东西要挂着跑几天，标签页会一次次堆积。
#   （明文 origin 上 fetch 照样带得上 cookie，实测 me() 正常返回；万一哪天不行，
#     me() 里那层「落定后重试」会兜住。）
HOSTS = tuple(o.split("//", 1)[1] for o in ORIGINS)


def is_admin_url(url: str) -> bool:
    """这个地址是不是行政管理平台（两个域名都算，http/https 都算）。"""
    u = str(url or "")
    host = u.split("//", 1)[1].split("/", 1)[0].split("?", 1)[0] if "//" in u else ""
    return host in HOSTS

SLOTS_PER_DAY = 48          # 一天 48 个半小时格，index 0 = 00:00~00:30

# 在页面里发请求的通用跳板。返回 {ok, status, body}，body 已经 JSON.parse 过。
# ⚠ 不要在这里 throw：任何一次网络抖动都不该让整轮抢占崩掉，一律返回结构化结果，
#   由调用方决定是重试还是放弃。
_FETCH_JS = r"""
async ({method, path, body}) => {
  try {
    const opt = {method, credentials: 'include', headers: {'Content-Type': 'application/json'}};
    if (body !== null && body !== undefined) opt.body = JSON.stringify(body);
    const t0 = Date.now();
    const r = await fetch(path, opt);
    const text = await r.text();
    let parsed;
    try { parsed = JSON.parse(text); } catch (e) { parsed = {raw: text.slice(0, 500)}; }
    // date 是服务器时间，用来算本机时钟偏差（见 MeetingApi.clock_skew）
    return {ok: r.ok, status: r.status, body: parsed,
            date: r.headers.get('date'), sentAt: t0, doneAt: Date.now()};
  } catch (e) {
    return {ok: false, status: 0, body: {message: String(e)}};
  }
}
"""


_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def html_error(text: str, status) -> str | None:
    """返回的是网页而不是 JSON 时，折成一句人话；不是网页就返回 None。

    ⚠ 不折的话，_FETCH_JS 兜的那 500 个字符会**原样进 run.log** ——
      2026-08-24 11:31 那条 ERROR 就是一整段 `<!DOCTYPE html>` 加 CSS，
      日志翻半天看不出「其实是登录态/落地页不对」。
    """
    head = str(text or "")[:400].lstrip().lower()
    if not (head.startswith("<!doctype html") or head.startswith("<html") or "<html" in head):
        return None
    m = _TITLE.search(str(text))
    title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    return (f"接口返回的是网页不是数据（HTTP {status}"
            + (f"：{title}" if title else "") + "）"
            + " —— 多半是登录态掉了，或者这个标签页没落在行政平台上，"
              "去浏览器里打开行政平台确认已登录后再跑")


class ApiError(RuntimeError):
    """接口层面的硬错误（登录态掉了、后端 500）。抢不到会议室不算，见 reserve()。"""


class MeetingApi:
    def __init__(self, page):
        self.page = page

    # ---------------- 底层 ----------------
    def ensure_page(self, timeout: int = 30000):
        """把页面弄到行政平台的 origin 上，并等它稳定下来。

        ⚠ fetch 用的是相对路径，跨 origin 就带不上 cookie 了 —— 所以必须确认
          当前页确实在行政平台上，不能想当然。两个域名都算，见 ORIGINS。

        ⚠ 光等 domcontentloaded 不够：这套 SPA 落地后自己还会再跳一次（hash 路由
          初始化），紧接着发 evaluate 会撞上「Execution context was destroyed」。
          所以这里多等一个 networkidle，拿不到就退回固定等待。
        """
        if not is_admin_url(self.page.url):
            self.page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=timeout)
        try:
            self.page.wait_for_load_state("networkidle", timeout=min(timeout, 15000))
        except Exception:
            self.page.wait_for_timeout(2000)

    def _call(self, method: str, path: str, body=None) -> dict:
        """发一个请求。页面正好在跳转时重试一次。

        ⚠ 只重试「上下文没了」这一类：那是页面自己在跳转，重试是对的。业务失败
          （被人抢先）绝不能在这里重试 —— 那是 reserve() 的语义，重试会变成
          对同一间房连打两枪。
        """
        arg = {"method": method, "path": BASE + path, "body": body}
        for attempt in (0, 1):
            try:
                return self.page.evaluate(_FETCH_JS, arg) or {"ok": False, "status": 0, "body": {}}
            except Exception as e:
                if attempt or "context was destroyed" not in str(e).lower():
                    raise
                log.info("页面正在跳转，等它落地后重发：%s", path)
                self.page.wait_for_timeout(1500)
        return {"ok": False, "status": 0, "body": {}}

    @staticmethod
    def _err(res: dict) -> str:
        """把后端的报错拼成一句人话。

        ⚠ 内网这套后台出错时 message 常常只有一句 Internal Server Error，
          真正有用的（比如「For input string: GZ2」）埋在 data.message 里。
        """
        rb = res.get("body") or {}
        # 网页优先折成一句人话，否则 500 个字符的 HTML 会灌进日志和界面
        pretty = html_error(rb.get("raw") or "", res.get("status"))
        if pretty:
            return pretty
        msg = rb.get("message") or rb.get("raw") or f"HTTP {res.get('status')}"
        inner = rb.get("data")
        if isinstance(inner, dict) and inner.get("message"):
            msg = f"{msg}（{inner['message']}）"
        return str(msg)

    def _data(self, res: dict, what: str):
        rb = res.get("body") or {}
        if not res.get("ok") or rb.get("success") is False:
            raise ApiError(f"{what}失败：{self._err(res)}")
        return rb.get("data")

    def clock_skew(self, samples: int = 3) -> float:
        """服务器时间 - 本机时间，单位秒。本机快了是正数（该晚点开抢）。

        ⚠ 抢占是掐 10:00:00 整点的，本机时钟偏个几秒就可能整轮扑空 —— 早了会被
          后台以「10点之后才可预定…」挡掉，晚了位置早没了。所以开跑前实测一次，
          用响应头的 Date 校准。

        Date 头只精确到秒，所以单次采样最大 1 秒误差；取几次的中位数收敛一点。
        取不到（代理把头去掉了之类）返回 0.0，也就是「按本机时间来」，
        再叠加 runner 里的提前量兜底。
        """
        import statistics

        got = []
        for _ in range(max(1, samples)):
            res = self._call("GET", "/settings/emps/baseInfo")
            stamp = res.get("date")
            if not stamp:
                continue
            try:
                srv = parsedate_to_datetime(stamp).timestamp()
            except (TypeError, ValueError):
                continue
            # 请求发出到收到的中点，当作服务器那个时刻对应的本机时间
            mid_local = ((res.get("sentAt", 0) + res.get("doneAt", 0)) / 2) / 1000.0
            if mid_local <= 0:
                continue
            got.append(srv - mid_local)
        return float(statistics.median(got)) if got else 0.0

    # ---------------- 业务 ----------------
    def me(self, timeout: int = 30000) -> dict:
        """当前登录人。userId 是提交预定和算签名都要用的。

        ⚠ 失败一次要自己救一次。2026-08-24 11:31 实测：这个接口返回了一个
          「请求的网页不存在」的 404 网页，整轮抢占当场中断；紧接着 11:32 手动
          重跑就成功了。也就是说它是**可恢复的瞬时状态**（新开的标签页还没落地、
          或者 SPA 的 hash 路由还在跳，fetch 的相对路径打到了没有 /f1-space 的
          落地页上）。为这个让整轮白跑不值得，所以等页面落定后再试一次。
        """
        try:
            data = self._data(self._call("GET", "/settings/emps/baseInfo"), "读取登录信息")
        except ApiError as first:
            log.info("读取登录信息失败，等页面落定后再试一次：%s", first)
            try:
                # ⚠ 这里**只能**用 ensure_page，不能无条件 goto(PAGE_URL)。
                #   ensure_page 已经在页面不在行政平台上时才导航，在的话只等它安静下来。
                #   2026-08-24 实测：页面本来好好地停在行政平台上，硬 goto 一次会触发
                #   一轮 SSO 弹跳（落到 http 的 oauth/callback），有几率直接把标签页
                #   打成 chrome-error://，比原来那个瞬时失败严重得多 —— 救人反倒推下水。
                self.ensure_page(timeout)
                self.page.wait_for_timeout(800)
            except Exception:
                raise first
            # 第二次还不行就是真不行了（多半是登录态掉了），原样抛出去
            data = self._data(self._call("GET", "/settings/emps/baseInfo"), "读取登录信息")
        if not data or not data.get("userId"):
            raise ApiError("读取登录信息失败：没拿到 userId，登录态可能已经掉了")
        return {"user_id": data["userId"], "user_name": data.get("userName", "")}

    def reservable_until(self, room_id: int) -> int:
        """该会议室最远可预定到的日期（毫秒时间戳，值本身是那天的 00:00）。

        ⚠ 这个值只说「最远能订到**哪一天**」，没说它是在一天中的几点翻的 ——
          第一版就是把「日期是零点」错当成「零点翻页」，凭空推出了个 00:00 开抢。
          真正的规则是每天 10:00 翻一页、按工作日数，见 meeting_data.RULE_TEXT。

        这个接口的用处是**给窗口做权威判定**：本地按规则算「哪天该醒」，
        扣扳机前拿它核一次「现在到底能不能订」，这样节假日/调休也不会抢空。
        """
        return int(self._data(
            self._call("GET", f"/meeting/order/{room_id}/reservable/date"), "查可预定截止日"))

    def spaces(self, book_date: str, min_capacity: int | None = None,
               page_size: int = 100, max_pages: int = 8) -> list[dict]:
        """某天所有会议室 + 每间 48 个半小时格的占用状态。翻页翻到底。

        ⚠ 全站 318 间（2026-08-19 实测），page_size=100 要翻 4 页。max_pages 是防跑飞的
          保险，不是业务上限；真到上限会记一条警告，不静默截断。
        """
        conds = [{"field": "bookDate", "opt": "=", "values": [book_date]}]
        if min_capacity:
            conds.append({"field": "capacity", "opt": "=", "values": [str(int(min_capacity))]})

        out: list[dict] = []
        for pn in range(1, max_pages + 1):
            body = {"conditions": conds, "columns": [], "draw": 2,
                    "offset": 0, "pageNumber": pn, "pageSize": page_size}
            data = self._data(self._call("POST", "/meeting/order/spaces", body), "查询会议室")
            if not isinstance(data, list) or not data:
                break
            out.extend(data)
            if len(data) < page_size:
                break
        else:
            log.warning("查询会议室翻到了 %s 页上限，可能还有没取到的", max_pages)
        return out

    @staticmethod
    def sign(room_id, start: str, end: str, reminder: int, user_id) -> str:
        """提交预定的防重放签名：md5(roomId + start + end + reminderTime + userId)。

        ⚠ 这是从前端压缩包（static/js/26.*.js 里的 createMeeting）逆出来的，拼接顺序
          和这几个值的字符串形态都不能动 —— 数字直接转十进制字符串，start/end 是
          'YYYY-MM-DD HH:mm:00'。2026-08-19 用一次真实预定 + 取消验证过。
        """
        raw = f"{room_id}{start}{end}{reminder}{user_id}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def reserve(self, room_id: int, start: str, end: str, subject: str, user_id,
                reminder: int = 2, content: str = "", remarks: str = "",
                attendees: list | None = None) -> dict:
        """提交预定。成功返回 {'ok': True}，被别人抢先/校验不过返回 {'ok': False, 'error': ...}。

        ⚠ 抢不到不算异常：0 点那一下必然有一堆「已被预定」，调用方要能立刻接着
          试下一个候选，所以这里不抛异常，只有连接层面的问题才由 _call 抛出去。
        """
        body = {
            "roomId": room_id, "start": start, "end": end,
            "subject": subject, "content": content or "",
            "attendeeList": attendees or [], "reminderTime": reminder,
            "services": [], "remarks": remarks or "", "userId": user_id,
            "attachments": [],
            "sign": self.sign(room_id, start, end, reminder, user_id),
        }
        res = self._call("POST", "/meeting/order", body)
        rb = res.get("body") or {}
        msg = str(rb.get("message") or "")

        # ⚠⚠ 不能只看 success 字段 —— 这个后台**拒绝时也返回 success: true**，
        #   把拒绝理由塞在 message 里。2026-08-20 实测，提交一个还没开放的日期：
        #     {"success": true, "message":
        #      "10点之前只能预定5个工作日之内的会议室，10点之后才可预定第6个工作日的会议室"}
        #   而「我的预定」里查无此条 —— 也就是根本没建出来。
        #   照着 success 判的话，机器人会把「没抢到」记成「抢到了」，写进断点、
        #   不再重试，人到点去开会才发现没房。这是这套东西能犯的最坏的错。
        #   所以改成「message 里明确说了成功才算成功」，其余一律当失败。
        #   成功时的原文是「预定成功」，见 docs/预定会议室-接口抓取.md。
        if res.get("ok") and rb.get("success") and "成功" in msg:
            return {"ok": True, "message": msg}
        return {"ok": False, "error": msg or self._err(res)}

    def cancel(self, reservation_id: int, reason: str = "机器人取消") -> dict:
        res = self._call("PUT", "/meeting/myreservation/cancel",
                         {"id": reservation_id, "reason": reason})
        rb = res.get("body") or {}
        if res.get("ok") and rb.get("success"):
            return {"ok": True}
        return {"ok": False, "error": self._err(res)}

    def my_reservations(self, page_size: int = 20) -> list[dict]:
        """「我的预定」列表。抢到之后回查一次，确认真的落库了。"""
        body = {"columns": [], "conditions": [], "draw": 1,
                "offset": 0, "pageNumber": 1, "pageSize": page_size}
        data = self._data(
            self._call("POST", "/meeting/myreservation/launch/table", body), "查我的预定")
        return data if isinstance(data, list) else []
