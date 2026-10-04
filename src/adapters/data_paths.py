"""任务数据独立于安装目录；旧业务文件仍通过旧路径读取。"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from .locking import FileLock
from ..domain.identity import stable_id


def task_root(settings: dict | None = None) -> Path:
    configured = (settings or {}).get("task_data_dir") or os.environ.get("CONFIG_ASSISTANT_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "ConfigAssistant"


def browser_lock(settings: dict) -> FileLock:
    url = urlsplit(settings.get("cdp_url", "http://127.0.0.1:9222"))
    hostname = "loopback" if url.hostname in ("localhost", "127.0.0.1", "::1") else url.hostname
    key = stable_id("browser", [hostname, url.port or (443 if url.scheme == "https" else 80)])
    # Shared across installation/data directories for this OS user.
    root = Path(settings.get("task_lock_dir") or os.environ.get("CONFIG_ASSISTANT_LOCK_HOME") or
                Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "ConfigAssistant" / "locks")
    return FileLock(root / (key + ".lock"))
