"""Runner 和界面之间的接口。控制台和图形界面各实现一份，Runner 不关心是哪个。"""
import sys
import time


class Stopped(Exception):
    """用户点了停止。"""


class BaseUI:
    def log(self, msg: str, level: str = "info"):
        raise NotImplementedError

    def progress(self, done: int, total: int, stats: dict):
        pass

    def confirm(self, label: str, summary: str) -> str:
        """返回 submit / skip / auto / stop"""
        return "submit"

    def ask_continue(self, error: str) -> bool:
        """某条失败后，还继续跑下一条吗"""
        return False

    def checkpoint(self):
        """每条记录开始前调用；暂停时在这里阻塞，停止时抛 Stopped"""
        pass

    def finished(self, title: str, body: str, ok: bool):
        pass


def _out(msg: str):
    """安全打印。打包成 --windowed 的 exe 后 sys.stdout 是 None，print 会直接炸。"""
    try:
        if sys.stdout:
            print(msg)
    except Exception:
        pass


def _ask(prompt: str) -> str | None:
    """安全读取输入。读不到返回 None。

    ⚠ 无控制台时 input() 抛的是 RuntimeError('lost sys.stdin')，不是 EOFError，
      只兜 EOFError 会让整个程序在最后一步崩掉。
    """
    if not sys.stdin:
        return None
    try:
        return input(prompt).strip().lower()
    except Exception:
        return None


class ConsoleUI(BaseUI):
    def __init__(self, auto: bool = False):
        self.auto = auto
        # 等人敲键盘的总时长。埋点要拿它把「机器在干活」和「机器在等人」分开，
        # 口径和图形界面那边的 WebUI.wait_seconds 保持一致 —— 不然同一件事
        # 命令行跑出来的「机器代劳」会凭空多出思考时间。
        self.wait_seconds = 0.0

    def log(self, msg, level="info"):
        mark = {"error": "✗", "ok": "✓", "warn": "!"}.get(level, " ")
        _out(f"{mark} {msg}")

    def _timed_ask(self, prompt: str):
        t0 = time.monotonic()
        try:
            return _ask(prompt)
        finally:
            self.wait_seconds += time.monotonic() - t0

    def confirm(self, label, summary):
        if self.auto:
            return "submit"
        while True:
            ans = self._timed_ask(f"{label} 提交？[y=提交 / n=跳过 / a=以后全部自动 / q=退出] ")
            if ans is None:          # 没法问，保守起见不提交
                return "skip"
            if ans in ("y", ""):
                return "submit"
            if ans == "n":
                return "skip"
            if ans == "a":
                self.auto = True
                return "submit"
            if ans == "q":
                return "stop"

    def ask_continue(self, error):
        return self._timed_ask("这条失败了，继续下一条？[y/n] ") == "y"

    def finished(self, title, body, ok):
        from . import notify

        notify.beep(ok=ok)
        notify.popup(title, body, warn=not ok)
