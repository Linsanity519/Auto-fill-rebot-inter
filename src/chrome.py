"""找到并启动带调试端口的 Chrome。用户在这个窗口里自己登录，脚本不碰凭据。"""
import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

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
