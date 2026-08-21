"""独立更新器：等主程序退出，再把新版本换上去。

它会被单独打成「配置助手更新器.exe」。不能放进主 EXE：Windows 不允许一个正在
运行的可执行文件把自己替换掉，正在被 import 的 src/ 也不该在自己脚下换掉。

三种活儿：
  --payload   换代码包（日常，~300KB）：备份 src/ assets/ main.py，解压覆盖，重启
  --installer 跑完整安装包（运行时变了才需要，~45MB）
  --rollback  代码包换坏了的时候，把备份还原回去

⚠ 代码包这条路是「整目录替换」而不是「逐个文件覆盖」：新版本删掉某个模块时，
  逐个覆盖会把旧的 .py 留在原地，下次 import 到的是一个本该消失的模块 ——
  这种问题极难查。所以先整个搬到 .backup，再解压出全新的。
"""
from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

APP_EXE = "配置助手.exe"
BACKUP_DIR = ".backup"
# payload.json 是代码包自带的说明（版本 + 要求的运行时代号），一起换掉。
PAYLOAD_MEMBERS = ("main.py", "src", "assets", "payload.json")
# 解压完必须存在的东西。少了任何一个说明包是坏的，立刻回滚。
SENTINELS = ("main.py", "src/__init__.py", "assets/webui/index.html")


def _log(path: Path, message: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as output:
            output.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


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


def _launch_app(target: Path, log: Path) -> None:
    exe = target / APP_EXE
    if exe.is_file():
        subprocess.Popen([str(exe)], cwd=str(target))
    else:
        _log(log, f"找不到主程序，无法重启：{exe}")


# ---------------- 代码包 ----------------
def _safe_members(zf: zipfile.ZipFile) -> list[str]:
    """挡住 zip slip：只收 main.py / src/ / assets/ 下的相对路径。"""
    names = []
    for name in zf.namelist():
        norm = name.replace("\\", "/").lstrip("/")
        if not norm or norm.endswith("/"):
            continue
        if ".." in Path(norm).parts or Path(norm).is_absolute():
            raise ValueError(f"更新包里有非法路径：{name}")
        top = Path(norm).parts[0]
        if top not in PAYLOAD_MEMBERS:
            raise ValueError(f"更新包里有预期之外的内容：{name}")
        names.append(name)
    if not names:
        raise ValueError("更新包是空的")
    return names


def _stash(target: Path, backup: Path, log: Path) -> None:
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    backup.mkdir(parents=True, exist_ok=True)
    for member in PAYLOAD_MEMBERS:
        src = target / member
        if src.exists():
            shutil.move(str(src), str(backup / member))
            _log(log, f"已备份 {member}")


def _restore(target: Path, backup: Path, log: Path) -> None:
    for member in PAYLOAD_MEMBERS:
        saved = backup / member
        if not saved.exists():
            continue
        live = target / member
        if live.exists():
            shutil.rmtree(live, ignore_errors=True) if live.is_dir() else live.unlink()
        shutil.move(str(saved), str(live))
        _log(log, f"已还原 {member}")


def apply_payload(payload: Path, target: Path, log: Path) -> int:
    backup = target / BACKUP_DIR
    try:
        with zipfile.ZipFile(payload) as zf:
            names = _safe_members(zf)
            _stash(target, backup, log)
            zf.extractall(target, members=names)
        missing = [s for s in SENTINELS if not (target / s).is_file()]
        if missing:
            raise ValueError(f"更新包缺少关键文件：{'、'.join(missing)}")
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        _log(log, f"代码包应用失败，正在回滚：{exc}")
        try:
            _restore(target, backup, log)
            _log(log, "回滚完成，已恢复到更新前的版本")
            _error(f"更新失败，已自动恢复到原来的版本。\n\n原因：{exc}\n详情：{log}")
        except OSError as restore_exc:
            _log(log, f"回滚也失败了：{restore_exc}")
            _error("更新失败，且自动恢复没能完成。\n\n"
                   f"请手动把 {backup} 里的 main.py / src / assets 移回上一层目录。\n\n"
                   f"详情：{log}")
        return 4

    _log(log, f"代码包已应用：{payload.name}")
    shutil.rmtree(backup, ignore_errors=True)
    _launch_app(target, log)
    return 0


# ---------------- 完整安装包 ----------------
def run_installer(installer: Path, log: Path) -> int:
    # ⚠ Inno 的 /LOG= 会自己创建并覆盖这个文件，所以绝不能和更新器自己的日志同名 ——
    #   否则最需要的那几行（等进程、启动安装包）正好会被冲掉。
    inno_log = log.with_name("update-install-inno.log")
    _log(log, f"启动安装包：{installer}（Inno 日志：{inno_log.name}）")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([
        str(installer), "/VERYSILENT", "/NORESTART", "/SP-", f"/LOG={inno_log}",
    ], cwd=str(installer.parent), creationflags=flags)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, default=0)
    parser.add_argument("--installer")
    parser.add_argument("--payload")
    parser.add_argument("--target", default="")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--log", required=True)
    args = parser.parse_args()

    log = Path(args.log)
    target = Path(args.target).resolve() if args.target else Path(sys.executable).resolve().parent

    if args.rollback:
        backup = target / BACKUP_DIR
        if not backup.is_dir():
            _error(f"没有可还原的备份（{backup} 不存在）。")
            return 5
        try:
            _restore(target, backup, log)
        except OSError as exc:
            _log(log, f"手动回滚失败：{exc}")
            _error(f"还原失败：{exc}\n\n详情：{log}")
            return 5
        shutil.rmtree(backup, ignore_errors=True)
        _launch_app(target, log)
        return 0

    package = Path(args.payload or args.installer or "")
    if not package.is_file():
        _log(log, f"更新包不存在：{package}")
        _error(f"找不到已下载的更新包。\n\n详情：{log}")
        return 2
    if args.pid and not _wait_process(args.pid, timeout=90):
        _log(log, f"等待主程序退出超时，PID={args.pid}")
        _error(f"程序未能在 90 秒内退出，请关闭程序后再试。\n\n详情：{log}")
        return 3

    if args.payload:
        return apply_payload(package, target, log)
    return run_installer(package, log)


if __name__ == "__main__":
    sys.exit(main())
