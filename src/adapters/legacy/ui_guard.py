"""旧桌面入口的互斥过渡，防止更换正在运行的 runner。"""
from __future__ import annotations

from functools import wraps


def guarded(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._command_lock:
            if self.worker is not None and self.worker.is_alive():
                return {"ok": False, "error": "已有任务正在运行，请先完成或停止当前任务"}
            return method(self, *args, **kwargs)
    return call
