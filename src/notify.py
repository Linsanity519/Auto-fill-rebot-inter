"""完成提醒：Windows 原生弹窗 + 提示音。用 ctypes 而非 tkinter，打包体积小很多。"""
import logging
import sys

log = logging.getLogger(__name__)

MB_OK = 0x0
MB_ICONINFO = 0x40
MB_ICONWARN = 0x30
MB_TOPMOST = 0x40000


def popup(title: str, message: str, warn: bool = False):
    """弹一个置顶消息框。非 Windows 或调用失败时退化成打印。"""
    # --windowed 打包后 sys.stdout 是 None，print 会抛异常
    try:
        if sys.stdout:
            print(f"\n{'=' * 46}\n{title}\n{message}\n{'=' * 46}")
    except Exception:
        pass
    if sys.platform != "win32":
        return
    try:
        import ctypes

        icon = MB_ICONWARN if warn else MB_ICONINFO
        ctypes.windll.user32.MessageBoxW(0, message, title, MB_OK | icon | MB_TOPMOST)
    except Exception:
        log.warning("弹窗失败", exc_info=True)


def beep(ok: bool = True):
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBeep(0x40 if ok else 0x30)
    except Exception:
        pass
