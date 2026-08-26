"""挂载到用户已登录的 Chrome。脚本永远不处理账号密码。"""
import logging

from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)


class Browser:
    def __init__(self, cdp_url: str, timeout: int = 15000):
        self.cdp_url = cdp_url
        self.timeout = timeout
        self._pw = None
        self.browser = None
        self.page = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        try:
            self.browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        except Exception as e:
            self._pw.stop()
            raise RuntimeError(
                f"连不上 Chrome ({self.cdp_url})。\n"
                f"请先双击 start_chrome.bat 启动带调试端口的 Chrome，并在里面登录好内网系统。\n"
                f"原始错误：{e}"
            ) from e

        ctx = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        self.page = ctx.pages[0] if ctx.pages else ctx.new_page()
        self.front()
        self.page.set_default_timeout(self.timeout)
        # ⚠ 页面跳转比「找一个控件」慢得多：内网抖一下、或者刚连上 VPN 的第一次
        #   加载，15 秒根本不够（实测整轮 6 条全死在 Page.goto 超时上）。
        #   导航单独给 3 倍时间，找控件仍然按原来的超时，快速失败。
        self.page.set_default_navigation_timeout(max(self.timeout * 3, 45000))
        return self

    def front(self):
        """把我们操作的这个标签页切到最前。

        ⚠ 这是整套流程里最大的一个性能开关，不是「顺手美化一下」：
          我们驱动的是 ctx.pages[0]，也就是用户 Chrome 里的第一个标签页。
          它一旦不是当前正在看的那个，Chrome 就把这个渲染进程降频，
          Playwright 每次点击的可见性/稳定性判定都要等好几帧 ——
          实测同一个创意页，前台 0.65 秒填完一条，后台 10.0 秒，差 15 倍。
          （注意 document.visibilityState 这时仍然是 visible，光看它看不出来。）
          所以每个单元、每条创意开填前都拨一次；已经在前台时是 0ms 的空操作。
        """
        try:
            self.page.bring_to_front()
        except Exception:
            pass                      # 拨不到前台顶多是慢，不该让整轮跑挂掉

    def __exit__(self, *exc):
        # 只断开连接，不关用户的浏览器
        #
        # ⚠ 收尾失败一律吞掉，绝不能往外抛。这里抛出去的异常会盖过
        #   with 块里真正的结果 —— 实测过一次：整轮跑完、弹完「成功 2 失败 0」，
        #   退出 with 时 close() 抛「Connection closed while reading from the driver」，
        #   被 Runner 最外层的 except 抓住，又弹了一个「没能开始」，
        #   两个自相矛盾的结论。断不开连接对用户没有任何影响，记日志就够了。
        for step, fn in (("断开浏览器", getattr(self.browser, "close", None)),
                         ("停止 playwright", getattr(self._pw, "stop", None))):
            if fn is None:
                continue
            try:
                fn()
            except Exception:
                log.warning("%s失败，忽略", step, exc_info=True)
        return False
