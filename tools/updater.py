"""独立更新器：等待主程序退出，再启动 Inno Setup 安装包。

它会被单独打成「配置助手更新器.exe」。不能放进主 EXE：Windows 不允许一个
正在运行的可执行文件把自己替换掉。
"""
from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path


def _log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def _wait_process(pid: int, timeout: float) -> bool:
    # SYNCHRONIZE = 0x00100000；OpenProcess 返回 0 说明目标已经不存在。
    handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return True
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _error(message: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, message, "配置助手更新失败", 0x10)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--installer", required=True)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()

    installer, log = Path(args.installer), Path(args.log)
    if not installer.is_file():
        _log(log, f"安装包不存在：{installer}")
        _error(f"找不到已下载的安装包。\n\n详情：{log}")
        return 2
    if not _wait_process(args.pid, timeout=90):
        _log(log, f"等待主程序退出超时，PID={args.pid}")
        _error(f"程序未能在 90 秒内退出，请关闭程序后再试。\n\n详情：{log}")
        return 3

    _log(log, f"启动安装包：{installer}")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([
        str(installer), "/VERYSILENT", "/NORESTART", "/SP-", f"/LOG={log}",
    ], cwd=str(installer.parent), creationflags=flags)
    return 0


if __name__ == "__main__":
    sys.exit(main())
