"""找到并启动带调试端口的 Chrome。用户在这个窗口里自己登录，脚本不碰凭据。"""
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)


def raise_window(title_hint: str = "") -> bool:
    """把 Chrome / Edge 窗口拽到 OS 前台（Windows）。非 Windows / 失败都静默 False。

    ⚠ `page.bring_to_front()`（CDP）只把**标签页**在 Chrome 里激活，
      不保证把 Chrome **窗口**顶到别的程序前面 —— 尤其我们的 pywebview 窗口正拿着
      焦点时，Windows 的「前台锁」会挡住后台进程抢焦点。这里按一下 ALT 解锁再抢。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        u = ctypes.windll.user32
        hint = (title_hint or "").strip().lower()
        found: list[tuple[int, str]] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _):
            if not u.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(hwnd, cls, 256)
            if cls.value != "Chrome_WidgetWin_1":
                return True
            buf = ctypes.create_unicode_buffer(512)
            u.GetWindowTextW(hwnd, buf, 512)
            if buf.value:
                found.append((hwnd, buf.value))
            return True

        u.EnumWindows(_cb, 0)
        if not found:
            return False
        target = next((h for h, t in found if hint and hint in t.lower()), found[0][0])
        u.ShowWindow(target, 9)                       # SW_RESTORE
        u.keybd_event(0x12, 0, 0, 0)                  # ALT down（解前台锁）
        u.keybd_event(0x12, 0, 2, 0)                  # ALT up
        u.SetForegroundWindow(target)
        u.BringWindowToTop(target)
        return True
    except Exception:
        log.debug("raise_window 失败", exc_info=True)
        return False

CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]


def find_browser() -> str | None:
    for p in CANDIDATES:
        if Path(p).exists():
            return p
    # 注册表兜底
    try:
        import winreg

        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(
                    root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
                ) as k:
                    path = winreg.QueryValue(k, None)
                    if path and Path(path).exists():
                        return path
            except OSError:
                continue
    except ImportError:
        pass
    return None


LOGIN_MARKERS = ("login.html", "/login", "passport", "sso")


def list_pages(cdp_url: str, timeout: float = 1.5) -> list[dict]:
    """列出当前所有标签页（走 CDP 的 HTTP 接口，比起 Playwright 轻量得多）。"""
    try:
        with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/list", timeout=timeout) as r:
            data = json.load(r)
        return [t for t in data if t.get("type") == "page"]
    except (urllib.error.URLError, OSError, ValueError):
        return []


def on_login_page(cdp_url: str) -> bool | None:
    """True=还停在登录页，False=已经登录，None=读不到（浏览器没开）。"""
    pages = list_pages(cdp_url)
    if not pages:
        return None
    urls = [p.get("url", "") for p in pages]
    real = [u for u in urls if u.startswith("http")]
    if not real:
        return None
    return all(any(m in u.lower() for m in LOGIN_MARKERS) for u in real)


def is_connected(cdp_url: str, timeout: float = 1.5) -> bool:
    """调试端口通不通。"""
    try:
        with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=timeout) as r:
            json.load(r)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def diagnose(cdp_url: str, want_host: str | None = None) -> dict:
    """「浏览器为什么连不上」的一句人话。

    ⚠ 界面上原来到处硬编码「浏览器没连上，请先启动浏览器并登录」—— 但连不上有好几种,
      每种的下一步动作都不一样(没装 / 装了没带调试端口 / 开着但停在登录页 / 都好了)。
      这里把现成的几个探针(is_connected / find_browser / list_pages / on_login_page)
      合起来,只多产出一个 `hint` 字段:该跟用户说的那一句。

    want_host  传了就顺带看一眼「有没有打开过这个域名的页面」(host_seen);
               None 时 host_seen 恒为 None(没问就不答)。
    """
    port_open = is_connected(cdp_url, timeout=1.0)
    exe = find_browser()
    pages = list_pages(cdp_url) if port_open else []
    real = [p.get("url", "") for p in pages if p.get("url", "").startswith("http")]
    on_login = on_login_page(cdp_url) if port_open else None

    host_seen = None
    if want_host:
        h = want_host.lower().lstrip("*.")
        host_seen = any(h in u.lower() for u in real)

    if not port_open:
        if not exe:
            hint = ("没找到 Chrome / Edge。装一个,或手动指定路径。"
                    "找过这些位置：" + "、".join(CANDIDATES))
        else:
            hint = ("调试端口不通。多半是 Chrome 开着、但没带调试端口启动 —— "
                    "点「启动浏览器并登录」用带端口的方式重开一个(不影响你平时那个 Chrome)。")
    elif on_login is True:
        hint = "浏览器连上了,但还停在登录页 —— 先在弹出的窗口里扫码登录内网系统。"
    elif want_host and host_seen is False:
        hint = (f"浏览器连上了、也登录了,但没有打开 {want_host} 的页面 —— "
                "点「启动浏览器并登录」直达对应的配置页,或自己在浏览器里打开它。")
    elif not real:
        hint = "浏览器连上了,但一个正经页面都没开 —— 点「启动浏览器并登录」打开配置页。"
    else:
        hint = ""            # 一切正常

    return {
        "port_open": port_open,
        "exe_found": bool(exe),
        "exe_path": exe or "",
        "page_count": len(real),
        "on_login_page": on_login,
        "host_seen": host_seen,
        "hint": hint,
    }


def launch(cdp_url: str, profile_dir: str | Path, start_url: str | None = None) -> str:
    """启动带调试端口的浏览器，并直接打开目标页面。

    已经在跑的话，用同一个 user-data-dir 再调一次 chrome.exe——
    Chrome 会把 URL 转交给已有实例开新标签页，而不是起第二个进程。
    """
    exe = find_browser()
    if not exe:
        raise RuntimeError(
            "没找到 Chrome 或 Edge。请手动指定路径，或确认已安装。\n"
            "找过这些位置：\n  " + "\n  ".join(CANDIDATES)
        )

    port = cdp_url.rsplit(":", 1)[-1].strip("/")
    profile = Path(profile_dir).resolve()
    profile.mkdir(parents=True, exist_ok=True)

    running = is_connected(cdp_url)
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if start_url:
        args.append(start_url)

    if running and not start_url:
        return "浏览器已在运行，直接复用"

    subprocess.Popen(
        args,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        close_fds=True,
        env=os.environ.copy(),
    )

    if running:
        return f"已在浏览器里打开：{start_url}"
    where = f"，并打开 {start_url}" if start_url else ""
    return f"已启动 {Path(exe).name}（调试端口 {port}）{where}"
