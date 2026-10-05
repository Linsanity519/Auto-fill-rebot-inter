"""Legacy checkpoints with atomic persistence and explicit uncertain submissions.

Old keyed records remain readable. Corrupt state must be repaired explicitly;
it must never be interpreted as permission to replay every external write.
"""
from __future__ import annotations

import json
from pathlib import Path

from .adapters.locking import FileLock, atomic_json
from .domain.identity import stable_id


class StateError(RuntimeError):
    """Durability or recovery failure: stop submitting new changes."""


class StateUncertain(StateError):
    pass


class RunState:
    def __init__(self, path: str | Path, form_name: str, resume: bool = True):
        self.path = Path(path)
        self.form_name = form_name
        self.done: list = []
        self.failed: list = []
        self.pending: list = []
        self._ignore_history = not resume
        self._poison = ""
        data = self._read().get(form_name) or {}
        if not isinstance(data, dict):
            raise StateError("断点分区损坏；请先备份并核查历史任务")
        self.pending = list(data.get("pending") or [])
        if resume:
            self.done = list(data.get("done") or [])
            self.failed = list(data.get("failed") or [])

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("expected object")
            for partition in data.values():
                if not isinstance(partition, dict) or any(
                        field in partition and not isinstance(partition[field], list)
                        for field in ("done", "failed", "pending")):
                    raise ValueError("invalid checkpoint partition")
            return data
        except (OSError, ValueError) as exc:
            raise StateError("断点文件无法可靠读取；已停止续跑，请保留文件并核查：" + str(self.path)) from exc

    def _change(self, mutate):
        if self._poison:
            raise StateError(self._poison)
        try:
            with FileLock(str(self.path) + ".lock", timeout=2):
                all_state = self._read()
                source = all_state.get(self.form_name) or {}
                if not isinstance(source, dict):
                    raise StateError("断点分区损坏")
                mine = {"done": list(self.done if self._ignore_history else source.get("done") or []),
                        "failed": list(self.failed if self._ignore_history else source.get("failed") or []),
                        "pending": list(source.get("pending") or [])}
                mutate(mine)
                all_state[self.form_name] = mine
                atomic_json(self.path, all_state)
                self.done, self.failed, self.pending = mine["done"], mine["failed"], mine["pending"]
        except Exception as exc:
            self._poison = "断点保存失败，已停止后续提交；已发生的操作需要核验：" + str(self.path)
            raise StateError(self._poison) from exc

    def save(self):
        snapshot = {"done": list(self.done), "failed": list(self.failed), "pending": list(self.pending)}
        self._change(lambda mine: mine.update(snapshot))

    def is_done(self, key):
        if self._poison:
            raise StateError(self._poison)
        if key in self.pending:
            raise StateUncertain("此条上次提交结果未知，不能直接重跑：" + str(key))
        return key in self.done

    def begin(self, key):
        if key in self.pending:
            raise StateUncertain("存在待核查的提交：" + str(key))
        def change(mine):
            if key in mine["pending"]:
                raise StateUncertain("另一轮提交仍待核查")
            mine["pending"].append(key)
        self._change(change)

    def mark_done(self, key):
        def change(mine):
            if key not in mine["done"]:
                mine["done"].append(key)
            mine["pending"] = [x for x in mine["pending"] if x != key]
        self._change(change)

    def mark_failed(self, key, name: str, error: str):
        self._change(lambda mine: mine["failed"].append({"key": key, "name": name, "error": error}))
        if key in self.pending:
            raise StateUncertain("提交结果待核查，已停止继续写入：" + str(name))

    def clear(self):
        if self.pending:
            raise StateUncertain("仍有结果未知的提交，不能用清除断点绕过核查")
        self._change(lambda mine: mine.update(done=[], failed=[]))

    def __len__(self):
        return len(self.done)


class StateMixin:
    """给 Runner 用的三个方法。混进去就有断点，`self.s` / `self.f` 是现成的。

    用法（构造函数里）：

        self._init_state()          # 之后 self.state 就是 RunState

    然后：

        if self.state.is_done(key): 跳过
        ...成功后... self.state.mark_done(key)
        ...失败后... self.state.mark_failed(key, name, msg)

    ⚠ `clear_state()` 由这里统一提供 —— webapp 的「清除断点」是无条件调它的，
      少一个 mode 没有就是一个 AttributeError。
    """

    def _init_state(self):
        context = self.s.get("execution_context")
        namespace = self.f["name"]
        if context:
            namespace += "@" + stable_id("context", context)
        self.state = RunState(self.s["state_file"], namespace,
                              resume=bool(self.s.get("resume")))

    def clear_state(self):
        self.state.clear()

    def readonly_run(self, records=None):
        """只读模式只校验输入/读取状态，不调用 filler 或业务提交。"""
        from .domain.identity import identify_records
        from .ui import ConsoleUI
        ui = getattr(self, "ui", None) or ConsoleUI()
        rows = self.preview() if records is None else None
        values = [r.payload for r in rows] if rows is not None else list(records)
        values = identify_records(values, [self.f.get("name"), self.s.get("execution_context")])
        results = []
        for i, item in enumerate(values):
            issues = rows[i].issues if rows is not None else []
            results.append({"序号": item.get("_execution_row", i + 1),
                            "item_id": item["_execution_id"],
                            "状态": "failed" if issues else "dry_run",
                            "错误": "；".join(issues),
                            "名称": rows[i].name if rows is not None else str((item.get("header") or {}).get("名称", i + 1))})
        ui.log("只读校验完成；没有调用填写、创建或提交操作")
        failed = sum(r["状态"] == "failed" for r in results)
        ui.progress(len(results), len(results), {"ok": 0, "failed": failed, "skipped": 0, "dry": len(results)-failed})
        if hasattr(self, "_write_results"):
            self._write_results(results)
        ui.finished("只读校验完成", f"共 {len(results)} 条，问题 {failed} 条；未修改平台", not failed)
        return results
