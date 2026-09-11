"""src/dmp_runner.py 的场景测试（翻页 / 列表接口 / 兜底）。改那个文件之后跑一遍：

    python tools\\test_dmp_runner.py

不联网、不碰浏览器。这里测的都是 1.1.18 线上真出过的事：
  · 翻页时接口卡了 35 秒、回来的不是列表 → 旧代码当成「最后一页」，29 个只做 6 个就收工
  · 预检列出来的人群没走到 → 必须记成失败，不能「成功 6、失败 0」
选择器准不准只有实跑能验，这里不管。
"""
import logging
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src import dmp_runner as D   # noqa: E402
from src.filler import FillError   # noqa: E402
from src.ui import BaseUI          # noqa: E402

logging.disable(logging.CRITICAL)
PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("　" + detail) if detail and not cond else ""))


class FakePage:
    def __init__(self):
        self.slept = 0

    def wait_for_timeout(self, ms):
        self.slept += ms

    def on(self, *a):
        pass


class UI(BaseUI):
    def __init__(self):
        self.lines = []

    def log(self, msg, level="info"):
        self.lines.append((level, msg))


def runner(scope="active"):
    tmp = Path(tempfile.mkdtemp())
    s = {"screenshot_dir": str(tmp), "state_file": str(tmp / "state.json"),
         "result_file": str(tmp / "r.csv"), "timeout": 1000, "dmp_scope": scope}
    f = {"name": "DMP延期", "list_ready_timeout": 1000, "page_retries": 2}
    return D.DmpRunner(s, f, UI())


# ------------------------------------------------------------ 接口响应的判读
class Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Req:
    def __init__(self, resp, method="GET", url="https://x/dmp/admin/crowd/list/page?pageNum=1"):
        self._resp, self.method, self.url = resp, method, url

    def response(self):
        return self._resp


def watch_with(resp):
    w = D._ListWatch(FakePage(), "/crowd/list/page")
    w._on_request(Req(resp))
    w._on_finished(Req(resp))
    return w


def test_read():
    print("\n[_ListWatch 判读接口响应]")
    good = {"code": 0, "message": "OK", "data": {"crowd_list": [{"crowd_id": 1}],
                                                  "total_num": 122, "curr_page_num": 3}}
    ok("正常响应 → 拿到 data", watch_with(Resp(200, good))._read()["curr_page_num"] == 3)

    for name, resp in [
        ("HTTP 502", Resp(502, {"code": 0, "data": {"crowd_list": []}})),
        ("code≠0（后台报错）", Resp(200, {"code": -500, "message": "服务繁忙", "data": None})),
        ("data 里没有 crowd_list", Resp(200, {"code": 0, "data": {}})),
    ]:
        try:
            watch_with(resp)._read()
            ok(f"{name} → 抛错（不能当成空列表）", False, "没抛")
        except FillError as e:
            ok(f"{name} → 抛错（不能当成空列表）", "返回异常" in str(e), str(e))

    ok("响应体读不到 → {}（退回看页面，不抛）",
       watch_with(Resp(200, ValueError("No resource")))._read() == {})

    w = D._ListWatch(FakePage(), "/crowd/list/page")
    w._on_request(Req(None, method="OPTIONS"))
    ok("OPTIONS 预检不算列表请求", w.sent == 0 and w.pending == 0)

    w = D._ListWatch(FakePage(), "/crowd/list/page")
    r = Req(None)
    r.failure = "net::ERR_CONNECTION_RESET"
    w._on_request(r)
    w._on_failed(r)
    try:
        w._read()
        ok("请求失败 → 抛错并说出原因", False, "没抛")
    except FillError as e:
        ok("请求失败 → 抛错并说出原因", "ERR_CONNECTION_RESET" in str(e), str(e))


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
        self.r._meta = ({"curr_page_num": self.cur, "curr_page_size": 10,
                         "total_num": self.pages * 10} if self.total_known
                        else {"curr_page_num": self.cur})

    def _refresh(self, page, action, what):
        nxt = self.cur + 1
        if self.fail_at.get(nxt, 0) > 0:
            self.fail_at[nxt] -= 1
            self.r._meta = {}
            raise FillError("人群列表接口返回异常（HTTP 504）")
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
    ok("第 13 页之后 → False（真的到底了）", r._goto_page(None, 14) is False)

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
    p.cur = 1          # 保存后被刷回了第 1 页
    p._set_meta()
    ok("列表被刷回第 1 页 → 能翻回第 4 页", r._goto_page(None, 4) and p.cur == 4)


# ------------------------------------------------------------ 兜底：没走到的记成失败
def test_unreached():
    print("\n[没走到的人群]")
    r = runner()
    r._targets = [{"key": str(i), "id": str(i), "name": f"人群{i}", "issues": []}
                  for i in range(1, 30)]
    ctx = {"stats": {"ok": 6, "failed": 0, "skipped": 0, "dry": 0}, "results": [],
           "i": 6, "seen": {str(i) for i in range(1, 7)}}
    r._account_unreached(None, ctx)
    ok("29 个只走到 6 个 → 另外 23 个记成失败",
       ctx["stats"]["failed"] == 23 and len(ctx["results"]) == 23, str(ctx["stats"]))
    ok("并且在界面上报一条 error", any(lv == "error" and "没走到" in m for lv, m in r.ui.lines))

    r = runner()
    r._targets = [{"key": "1", "id": "1", "name": "a", "issues": []},
                  {"key": "2", "id": "2", "name": "b", "issues": ["已失效"]}]
    ctx = {"stats": {"ok": 1, "failed": 0, "skipped": 0, "dry": 0}, "results": [],
           "i": 1, "seen": {"1"}}
    r._account_unreached(None, ctx)
    ok("预检就标红的不算没走到", ctx["stats"]["failed"] == 0)


def test_already_max():
    print("\n[已是最晚日期]")
    r = runner()
    ok("上限还不知道 → 不跳（得先开一次弹窗算出来）",
       not r._already_max({"expire": date(2027, 3, 10)}))
    r._max_limit = date(2027, 3, 10)
    ok("失效时间 = 上限 → 跳过", r._already_max({"expire": date(2027, 3, 10)}))
    ok("失效时间早于上限 → 要延", not r._already_max({"expire": date(2027, 2, 16)}))
    ok("清单点名了日期 → 不走这条捷径",
       not r._already_max({"expire": date(2027, 3, 10), "want_date": date(2027, 1, 1)}))


def test_nav_error():
    print("\n[打不开页面时的提示]")
    m = D._nav_error("u", Exception(": net::ERR_NAME_NOT_RESOLVED at https://x Call log: …"))
    ok("域名解析失败 → 说人话", "内网/VPN" in m and "Call log" not in m, m)
    m = D._nav_error("u", Exception("Target page, context or browser has been closed"))
    ok("标签页被关 → 说人话", "标签页被关" in m, m)


if __name__ == "__main__":
    test_read()
    test_goto_page()
    test_unreached()
    test_already_max()
    test_nav_error()
    print(f"\n{len(PASS)} 项通过，{len(FAIL)} 项失败")
    sys.exit(1 if FAIL else 0)
