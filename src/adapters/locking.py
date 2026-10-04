"""进程间排他文件锁；操作结束或进程退出后由操作系统释放。"""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time

_registry_guard = threading.Lock()
_registry: dict[str, threading.Lock] = {}


class ResourceBusy(RuntimeError):
    pass


class FileLock:
    def __init__(self, path: str | Path, timeout: float = 0):
        self.path = Path(path).resolve()
        self.timeout = timeout
        self._file = None
        with _registry_guard:
            self._thread_lock = _registry.setdefault(str(self.path), threading.Lock())

    def acquire(self):
        if not self._thread_lock.acquire(timeout=max(0, self.timeout)):
            raise ResourceBusy("已有任务占用此资源，请等待当前任务完成")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("a+b")
            self._file.seek(0, os.SEEK_END)
            if self._file.tell() == 0:
                self._file.write(b"\0")
                self._file.flush()
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    self._file.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise ResourceBusy("另一个助手进程正在使用此资源") from exc
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        except BaseException:
            if self._file:
                self._file.close()
                self._file = None
            self._thread_lock.release()
            raise

    def release(self):
        if self._file is None:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file, fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
            self._thread_lock.release()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()


def atomic_json(path: str | Path, value: object) -> None:
    import json
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    filename = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=path.parent, prefix=path.name + ".",
                                         suffix=".tmp", delete=False) as target:
            filename = target.name
            json.dump(value, target, ensure_ascii=False, indent=2)
            target.flush()
            os.fsync(target.fileno())
        os.replace(filename, path)
        filename = None
    finally:
        if filename:
            Path(filename).unlink(missing_ok=True)


def atomic_bytes(path: str | Path, data: bytes) -> None:
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    filename = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".",
                                         suffix=".tmp", delete=False) as target:
            filename = target.name
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(filename, path)
        filename = None
    finally:
        if filename:
            Path(filename).unlink(missing_ok=True)
