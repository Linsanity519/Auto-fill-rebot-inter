"""src/ab_runner.py 的场景测试（列表接口 / 搜索连发 / 翻页 / 兜底）。改那个文件之后跑一遍：

    python tools\\test_ab_runner.py

不联网、不碰浏览器。测的是 DMP 在 1.1.18 线上真出过、AB 结构一模一样的那些事：
  · 点完筛选固定等几秒就读列表 → 读到上一屏
  · 翻页时接口出错 → 旧代码当成「最后一页」，静默收工
  · 预检列出来的实验没走到 → 必须记成失败
外加 AB 自己的：搜索时页面连发两个请求（先按旧页码、再按第 1 页）。
选择器准不准只有实跑能验，这里不管。
"""
import logging
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src import ab_runner as A     # noqa: E402
from src.filler import FillError   # noqa: E402
from src.ui import BaseUI          # noqa: E402

logging.disable(logging.CRITICAL)
PASS, FAIL = [], []
LIST = "http://abtest.bilibili.co/ab/v3/experiment/list?name=&userId=55798&currentSize={p}&perPageSize=15&queryParam={q}"


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("　" + detail) if detail and not cond else ""))


class FakePage:
    """带假时钟的 page：wait_for_timeout 推进时间，并触发到点的事件。"""

    def __init__(self):
        self.t = 0
        self.events = []        # [(时刻, fn)]
        self.dom = []           # 表格里当前的实验 ID

    def at(self, dt, fn):
        self.events.append((self.t + dt, fn))
        self.events.sort(key=lambda e: e[0])

    def wait_for_timeout(self, ms):
        self.t += ms
        while self.events and self.events[0][0] <= self.t:
            self.events.pop(0)[1]()

    def on(self, *a):
        pass


class UI(BaseUI):
    def __init__(self):
        self.lines = []

    def log(self, msg, level="info"):
        self.lines.append((level, msg))


def runner(scope="mine"):
    tmp = Path(tempfile.mkdtemp())
    s = {"screenshot_dir": str(tmp), "state_file": str(tmp / "state.json"),
         "result_file": str(tmp / "r.csv"), "timeout": 1000, "ab_scope": scope}
    f = {"name": "AB实验延期", "list_ready_timeout": 1000, "page_retries": 2}
    return A.AbRunner(s, f, UI())


# ------------------------------------------------------------ 接口响应的判读
class Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Req:
    def __init__(self, resp, method="GET", url=LIST.format(p=1, q="")):
        self._resp, self.method, self.url = resp, method, url

    def response(self):
        return self._resp


def body(ids, page=1, pages=18):
    return {"code": 200, "msg": "success",
            "items": [{"id": i, "runStatus": 2, "expirationTime": "2026-11-28 23:59:59"} for i in ids],
            "pageVO": {"currentSize": page, "perPageSize": 15, "totalPageSize": pages,
                       "totalSize": pages * 15}}


def watch_with(resp):
    w = A._ListWatch(FakePage(), r"/ab/v3/experiment/list\?")
    r = Req(resp)
    w._on_request(r)
    w._on_finished(r)
    return w


def test_read():
    print("\n[_ListWatch 判读接口响应]")
    ok("正常响应（code=200）→ 拿到 body",
       watch_with(Resp(200, body([1, 2], page=3)))._read()["pageVO"]["currentSize"] == 3)
    ok("code=0 也认", watch_with(Resp(200, {"code": 0, "items": []}))._read() == {"code": 0, "items": []})

    for name, resp in [
        ("HTTP 502", Resp(502, body([1]))),
        ("code=500（后台报错）", Resp(200, {"code": 500, "msg": "服务繁忙"})),
        ("没有 items", Resp(200, {"code": 200, "pageVO": {}})),
    ]:
        try:
            watch_with(resp)._read()
            ok(f"{name} → 抛错（不能当成空列表）", False, "没抛")
        except FillError as e:
            ok(f"{name} → 抛错（不能当成空列表）", "返回异常" in str(e), str(e))

    ok("响应体读不到 → {}（退回看页面，不抛）",
       watch_with(Resp(200, ValueError("No resource")))._read() == {})

    w = A._ListWatch(FakePage(), r"/ab/v3/experiment/list\?")
    w._on_request(Req(None, method="OPTIONS"))
    ok("OPTIONS 预检不算列表请求", w.sent == 0 and w.pending == 0)
    w._on_request(Req(None, url="http://abtest.bilibili.co/ab/v3/experiment/statistics"))
    ok("同批的 /experiment/statistics 不算列表请求", w.sent == 0)

    w = A._ListWatch(FakePage(), r"/ab/v3/experiment/list\?")
    r = Req(None)
    r.failure = "net::ERR_CONNECTION_RESET"
    w._on_request(r)
    w._on_failed(r)
    try:
        w._read()
        ok("请求失败 → 抛错并说出原因", False, "没抛")
    except FillError as e:
        ok("请求失败 → 抛错并说出原因", "ERR_CONNECTION_RESET" in str(e), str(e))

    d = A._ReqLog(FakePage(), r"/ab/v3/experiment/(\d+)(?:[?#]|$)")
    d._on_request(Req(None, url="http://abtest.bilibili.co/ab/v3/experiment/15937"))
    ok("详情接口：抓得出实验 ID", d.last_group() == "15937")
    d = A._ReqLog(FakePage(), r"/ab/v3/experiment/(\d+)(?:[?#]|$)")
    d._on_request(Req(None, url=LIST.format(p=1, q="")))
    ok("详情接口：列表请求不算", d.sent == 0)


# ------------------------------------------------------------ 搜索连发两个请求
def send(page, w, url, ids, dt_sent, dt_done, dt_render=None):
    """在 page 的时间线上排一个列表请求：dt_sent 发出、dt_done 回来、dt_render 表格换成它。"""
    req = Req(Resp(200, body(ids, page=int(re.search(r"currentSize=(\d+)", url).group(1)))), url=url)
    page.at(dt_sent, lambda: w._on_request(req))
    page.at(dt_done, lambda: w._on_finished(req))
    if dt_render is not None:
        page.at(dt_render, lambda: setattr(page, "dom", [str(i) for i in ids]))


def search_rig():
    r = runner("id_list")
    page = FakePage()
    page.dom = ["1", "2", "3"]
    r._watch = A._ListWatch(page, r"/ab/v3/experiment/list\?")
    r._dom_keys = lambda p: p.dom
    r.f["list_ready_timeout"] = 45000
    return r, page, r._watch


def test_search_race():
    print("\n[搜索时页面连发两个请求]")
    want = lambda u: A._param(u, "queryParam") == "123" and A._param(u, "currentSize") == "1"  # noqa: E731

    # ① 第一个（旧页码）回来时第二个还没发：先渲染旧页码那一批，再换成第 1 页那一批
    r, page, w = search_rig()

    def act():
        send(page, w, LIST.format(p=2, q="123"), [123], 0, 100, 150)
        send(page, w, LIST.format(p=1, q="123"), [123, 9], 300, 500, 550)
    got = r._refresh(page, act, "搜「123」", accept=want)
    ok("读到的是第 1 页那一个（不是旧页码那个）",
       got["pageVO"]["currentSize"] == 1 and page.dom == ["123", "9"], f"{got.get('pageVO')}")
    ok("　而且没有等满围栏", page.t < 5000, f"用了 {page.t} ms")

    # ② 页面压根没渲染旧页码那一批，直接换成第 1 页的：不能死等旧那批对上
    r, page, w = search_rig()

    def act2():
        send(page, w, LIST.format(p=2, q="123"), [123], 0, 100, None)
        send(page, w, LIST.format(p=1, q="123"), [123, 9], 300, 500, 550)
    got = r._refresh(page, act2, "搜「123」", accept=want)
    ok("旧那批没渲染 → 跟到新那一个，不等满 45 秒",
       got["pageVO"]["currentSize"] == 1 and page.t < 5000, f"用了 {page.t} ms")

    # ③ 只发了旧页码那一个、第二个一直没来 → 报错（不能拿旧页码那屏当搜索结果）
    r, page, w = search_rig()

    def act3():
        send(page, w, LIST.format(p=2, q="123"), [123], 0, 100, 150)
    try:
        r._refresh(page, act3, "搜「123」", accept=want)
        ok("第二个请求没来 → 抛错", False, "没抛")
    except FillError as e:
        ok("第二个请求没来 → 抛错", "没有按预期刷新" in str(e), str(e))

    # ④ 点了没反应（一个请求都没发）→ 说「没点上」，而且不等满 45 秒
    r, page, w = search_rig()
    try:
        r._refresh(page, lambda: None, "点「我的实验」")
        ok("一个请求都没发 → 抛「没有去刷新」", False, "没抛")
    except FillError as e:
        ok("一个请求都没发 → 抛「没有去刷新」", "没有去刷新" in str(e) and page.t <= 6000,
           f"{e} / {page.t} ms")

    # ⑤ 慢接口（4.7 秒）也等得到：不是固定等 2.5 秒就读
    r, page, w = search_rig()

    def slow():
        send(page, w, LIST.format(p=1, q=""), [7, 8], 0, 4700, 4750)
    got = r._refresh(page, slow, "点「我的实验」")
    ok("接口 4.7 秒才回来 → 等到它，读到的是新那一批", page.dom == ["7", "8"]
       and [i["id"] for i in got["items"]] == [7, 8])


def test_search_same_keyword():
    print("\n[同一个词再搜]")
    r, page, w = search_rig()
    req = Req(Resp(200, body([123])), url=LIST.format(p=1, q="123"))
    w._on_request(req)
    w._on_finished(req)
    w.consumed = w.finished
    page.dom = ["123"]

    class Box:
        def input_value(self):
            return "123"
    r._search_box = lambda p: Box()
    r._rows_of = lambda p: [{"id": "123", "name": "x", "status": "实验中", "end_date": ""}]
    called = []
    r._refresh = lambda *a, **k: called.append(a)
    hit = r._search_for(page, "123")
    ok("眼前就是这个词的结果 → 直接用，不重新请求（页面也不会发）",
       hit and hit["id"] == "123" and not called)


# ------------------------------------------------------------ 翻页
class Btn:
    def click(self):
        pass


class Pager:
    """假的列表：13 页；fail_at = {页码: 还要失败几次}。"""

    def __init__(self, r, pages=13, fail_at=None, total_known=True, next_grey=False):
        self.r, self.pages, self.cur = r, pages, 1
        self.fail_at = dict(fail_at or {})
        self.total_known, self.next_grey = total_known, next_grey
        self.reopened = 0
        r._idle = lambda page: None
        r._pager_button = lambda page, fwd: (
            None if (fwd and self.cur >= self.pages) or (not fwd and self.cur <= 1) else Btn())
        r._next_disabled = lambda page: self.next_grey
        r._refresh = self._refresh
        r._reopen_list = self._reopen
        self._set_meta()

    def _set_meta(self):
        self.r._meta = ({"currentSize": self.cur, "perPageSize": 15, "totalPageSize": self.pages,
                         "totalSize": self.pages * 15} if self.total_known
                        else {"currentSize": self.cur})

    def _refresh(self, page, action, what, accept=None):
        nxt = self.cur + 1
        if self.fail_at.get(nxt, 0) > 0:
            self.fail_at[nxt] -= 1
            self.r._meta = {}
            raise FillError("实验列表接口返回异常（HTTP 504）")
        self.cur = nxt
        self._set_meta()

    def _reopen(self, page):
        self.reopened += 1
        self.cur = 1
        self._set_meta()


def test_goto_page():
    print("\n[_goto_page 翻页]")
    r = runner()
    Pager(r)
    ok("第 13 页之后 → False（接口说总共 13 页）", r._goto_page(None, 14) is False)

    r = runner()
    p = Pager(r)
    ok("正常翻到第 7 页", r._goto_page(None, 7) and p.cur == 7)

    r = runner()
    p = Pager(r, fail_at={7: 1})
    got = r._goto_page(None, 7)
    ok("翻到第 7 页时接口出错一次 → 重开列表再翻过去，不当成到底",
       got is True and p.cur == 7 and p.reopened == 1, f"got={got} cur={p.cur} reopened={p.reopened}")

    r = runner()
    p = Pager(r, fail_at={7: 99})
    p.cur = 6
    p._set_meta()
    try:
        r._goto_page(None, 7)
        ok("第 7 页一直出错 → 重试用完后抛错（不静默收工）", False, "没抛")
    except FillError:
        ok("第 7 页一直出错 → 重试用完后抛错（不静默收工）", p.reopened == 2, f"reopened={p.reopened}")

    r = runner()
    p = Pager(r, total_known=False, next_grey=True)
    p.cur = 13
    p._set_meta()
    ok("接口没给总数、下一页是灰的 → False", r._goto_page(None, 14) is False)

    r = runner()
    p = Pager(r, total_known=False, next_grey=False)
    p.cur = 13
    p._set_meta()
    r.f["page_retries"] = 0
    try:
        r._goto_page(None, 14)
        ok("接口没给总数、分页器不见了 → 抛错（不是到底）", False, "没抛")
    except FillError:
        ok("接口没给总数、分页器不见了 → 抛错（不是到底）", True)

    r = runner()
    p = Pager(r)
    p.cur = 1          # 续期后被刷回了第 1 页
    p._set_meta()
    ok("列表被刷回第 1 页 → 能翻回第 4 页", r._goto_page(None, 4) and p.cur == 4)

    r = runner()
    r._meta = {"totalSize": 267, "perPageSize": 15}
    ok("没给 totalPageSize 时按 totalSize / perPageSize 算（267 条 → 18 页）", r._page_count() == 18)


# ------------------------------------------------------------ 读表格
ROWS = [
    {"cells": ["EP券拉新实验\nID:15937\n父子实验", "实验中", "50 %", "2026-09-04\n2026-11-28"], "shown": True},
    {"cells": ["名字里带实验中的实验\nID:15866", "实验中", "50 %", "2026-09-01\n2026-11-29"], "shown": True},
    {"cells": ["藏起来的行\nID:1", "实验中", "", ""], "shown": False},
    {"cells": ["早就结束了\nID:15529", "已结束", "50 %", "2026-01-01\n2026-03-01"], "shown": True},
    {"cells": ["又一个实验中\nID:15500", "实验中", "", "2026-01-01\n2026-12-01"], "shown": True},
]


def test_rows():
    print("\n[读表格]")
    r = runner()
    r._rows_of = lambda page: r._parse_rows(ROWS)
    parsed = r._parse_rows(ROWS)
    ok("ID 从「实验名称」那一格里抓", [x["id"] for x in parsed] == ["15937", "15866", "15529", "15500"])
    ok("名称只取第一行", parsed[0]["name"] == "EP券拉新实验")
    ok("到期日取「开始/结束时间」的最后一行", parsed[0]["end_date"] == "2026-11-28")

    found, hit = r._scan_page(None)
    ok("扫到第一个非「实验中」就停，后面的「实验中」不再算",
       [t["id"] for t in found] == ["15937", "15866"] and hit, str([t["id"] for t in found]))

    r._items = {"15937": {"expirationTime": "2026-12-09 23:59:59"}}
    found, _ = r._scan_page(None)
    ok("接口给了 expirationTime 就用接口的（表格那列偶尔渲染不全）", found[0]["end_date"] == "2026-12-09")

    t = r._find_id(None, "15529")
    ok("搜到了但不是「实验中」→ 带上 issues 说清楚", t and t["issues"] and "已结束" in t["issues"][0])
    ok("搜不到 → None", r._find_id(None, "404") is None)
    ok("1593 不会认成 15937（整格 ID 比对）", r._find_id(None, "1593") is None)


def test_collect_keeps_issues():
    print("\n[清单模式：搜到的已结束实验]")
    r = runner("id_list")
    r._search_for = lambda page, kw: {"key": kw, "id": kw, "name": "x", "status": "已结束",
                                      "end_date": "", "issues": ["这个实验现在是「已结束」"]}
    out = r._collect_by_search(None, [{"id": "15529", "name": "", "row": 2, "date": None,
                                      "date_raw": "", "issues": []}])
    ok("issues 不能被清掉（1.1.19 之前会清成 [] 然后照样去跑）", out[0]["issues"], str(out[0]))


# ------------------------------------------------------------ 兜底：没走到的记成失败
def test_unreached():
    print("\n[没走到的实验]")
    r = runner()
    r._targets = [{"key": str(i), "id": str(i), "name": f"实验{i}", "end_date": "", "issues": []}
                  for i in range(1, 30)]
    ctx = {"stats": {"ok": 6, "failed": 0, "skipped": 0, "dry": 0}, "results": [],
           "i": 6, "seen": {str(i) for i in range(1, 7)}}
    r._account_unreached(None, ctx)
    ok("29 个只走到 6 个 → 另外 23 个记成失败",
       ctx["stats"]["failed"] == 23 and len(ctx["results"]) == 23, str(ctx["stats"]))
    ok("并且在界面上报一条 error", any(lv == "error" and "没走到" in m for lv, m in r.ui.lines))

    r = runner()
    r._targets = [{"key": "1", "id": "1", "name": "a", "issues": []},
                  {"key": "2", "id": "2", "name": "b", "issues": ["已结束"]}]
    ctx = {"stats": {"ok": 1, "failed": 0, "skipped": 0, "dry": 0}, "results": [],
           "i": 1, "seen": {"1"}}
    r._account_unreached(None, ctx)
    ok("预检就标红的不算没走到", ctx["stats"]["failed"] == 0)

    r = runner()
    r._targets = [{"key": str(i), "id": str(i), "name": f"实验{i}", "issues": []} for i in (1, 2, 3)]
    ctx = {"stats": {"ok": 0, "failed": 0, "skipped": 0, "dry": 0}, "results": [],
           "i": 0, "seen": {"1"}}
    r._account_unreached({"1": "", "2": ""}, ctx)
    ok("只算预检界面上勾着的（3 没勾 → 不算）", ctx["stats"]["failed"] == 1)

    r = runner()        # 没走过预检，只有 records
    ctx = {"stats": {"ok": 0, "failed": 0, "skipped": 0, "dry": 0}, "results": [],
           "i": 0, "seen": set()}
    r._account_unreached({"15937": "EP券"}, ctx)
    ok("没走过预检也按 records 里的实验 ID 兜底", ctx["stats"]["failed"] == 1
       and ctx["results"][0]["实验名称"] == "EP券")


# ------------------------------------------------------------ 日期面板（纯判断）
def test_panel():
    print("\n[日期面板]")
    days = [[str(d), d <= 28] for d in range(1, 31)]
    snap = {"label": "2026 年 11 月", "days": days}
    ok("标题「2026 年 11 月」→ (2026, 11)", A.AbRunner._snap_ym(snap) == (2026, 11))
    ok("格子齐了才算渲染完", A.AbRunner._rendered(snap)
       and not A.AbRunner._rendered({"label": "2026 年 11 月", "days": days[:5]})
       and not A.AbRunner._rendered({"label": "", "days": days}))
    avail, last_open = A.AbRunner._month_state(snap)
    ok("可选 1~28、月末是灰的 → 上限就在本月", avail[-1] == 28 and not last_open)
    all_open = {"label": "2026 年 11 月", "days": [[str(d), True] for d in range(1, 31)]}
    ok("整月可选 → 还要往后翻", A.AbRunner._month_state(all_open)[1])
    ok("月初的灰（今天之前）不算到顶",
       A.AbRunner._month_state({"days": [[str(d), d >= 10] for d in range(1, 31)]})[1])


def test_misc():
    print("\n[杂项]")
    ok("已是最晚：选出来 = 现在的到期日 → 不提交", A.AbRunner._no_gain("2026-11-28", "2026-11-28"))
    ok("选出来更早 → 不提交（不缩短）", A.AbRunner._no_gain("2026-11-28", "2026-11-01"))
    ok("选出来更晚 → 提交", not A.AbRunner._no_gain("2026-11-28", "2026-12-09"))
    ok("格式认不出 → 宁可提交", not A.AbRunner._no_gain("", "2026-12-09"))

    ok("URL 参数：userId", A._param(LIST.format(p=2, q=""), "userId") == "55798")
    ok("URL 参数：空的 queryParam 读成 ''", A._param(LIST.format(p=2, q=""), "queryParam") == "")
    ok("# 路由：只差 # 后面 → 同一个文档",
       A._same_doc("http://abtest.bilibili.co/#/abtest/list/test?testId=1",
                   "http://abtest.bilibili.co/#/abtest/list?space=abtest"))
    ok("换了站点 → 不是同一个文档",
       not A._same_doc("https://manager.bilibili.co/v3/#/x", "http://abtest.bilibili.co/#/abtest/list"))
    ok("「续期」按钮文字两边带空格也认", bool(A._spaced("续期").match(" 续期 ")))
    ok("　但不会点到标题「实验续期」", not A._spaced("续期").match("实验续期"))

    m = A._nav_error("u", Exception(": net::ERR_NAME_NOT_RESOLVED at https://x Call log: …"))
    ok("域名解析失败 → 说人话", "内网/VPN" in m and "Call log" not in m, m)

    r = runner()
    r._watch = A._ListWatch(FakePage(), r"/ab/v3/experiment/list\?")
    r._watch._on_request(Req(None, url=LIST.format(p=1, q="")))
    called = []
    r._refresh = lambda *a, **k: called.append(a)
    r._select_my_experiments(r._watch.page)
    ok("列表请求已经带 userId → 不再点「我的实验」", not called)

    src = (ROOT / "src" / "ab_runner.py").read_text(encoding="utf-8")
    n = len(re.findall(r"wait_for_timeout\(", src))
    ok("ab_runner 里没有写死的 wait_for_timeout（硬约定第 1 条）", n == 0, f"还有 {n} 处")


if __name__ == "__main__":
    test_read()
    test_search_race()
    test_search_same_keyword()
    test_goto_page()
    test_rows()
    test_collect_keeps_issues()
    test_unreached()
    test_panel()
    test_misc()
    print(f"\n{len(PASS)} 项通过，{len(FAIL)} 项失败")
    sys.exit(1 if FAIL else 0)
