"""Archive legacy runs honestly: full input/result snapshots, legacy step coverage labelled."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4
from ..archives import Archives, scrub
from ..data_paths import task_root
from ..task_store import TaskStore, encode, now
from ...domain.identity import identify_records


class LegacyCapture:
    def __init__(self, settings: dict, form: dict, records: list[dict], mode: str, entry: str):
        self.store = TaskStore(task_root(settings))
        self.task_id = "task_" + uuid4().hex
        self.records = identify_records(records, [form["name"], settings.get("execution_context")])
        self.archives = Archives(self.store)
        self.output_paths = [Path(settings["result_file"])] if settings.get("result_file") else []
        self.shot_dir = Path(settings["screenshot_dir"]) if settings.get("screenshot_dir") else None
        if self.shot_dir:
            self.output_paths.extend(self.shot_dir.glob("*.png"))
        self.before_files = {str(p): (p.stat().st_mtime_ns, p.stat().st_size)
                             for p in self.output_paths if p.is_file()}
        attachments, paths = [], set()
        # Explicit input plus actual media file references; no browser profile/log directory sweep.
        candidates = [settings.get("data_file")]
        def visit(value):
            if isinstance(value, dict):
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, str) and Path(value).suffix.lower() in (
                    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".xlsx", ".csv"):
                candidates.append(value)
        visit(self.records)
        for value in candidates:
            if not value:
                continue
            path = Path(value)
            if path.is_file() and str(path.resolve()) not in paths:
                paths.add(str(path.resolve()))
                attachments.append(self.archives.attach(path))
        keys = ("execution_context", "wizard_activity", "wizard_strategy", "ad_prep", "pp_prep",
                "meeting_tasks", "toggle_direction", "toggle_params", "toggle_strategies",
                "pt_scope", "dmp_scope", "ab_scope", "toggle_scope", "toggle_activity",
                "toggle_level", "resume", "dry_run", "allow_rehearsal")
        plan_id = "legacy_plan_" + uuid4().hex
        plan = scrub({"plan_id": plan_id, "capability": "legacy." + form.get("mode", "default"),
                      "schema_version": 1, "form": form,
                      "inputs": self.records, "options": {k: settings[k] for k in keys if k in settings}})
        body = {"entry": entry, "mode": mode, "attachments": attachments,
                "completeness": "legacy_snapshot",
                "limitations": ["保存输入、操作日志与结果；旧执行器尚无逐步骤意图和完整平台回读证据",
                                "旧任务不冒充已核验成功，也不能由导入档案重新执行"]}
        from ..locking import FileLock
        self.lease = FileLock(self.store.root / "legacy-locks" / (self.task_id + ".lock"))
        self.lease.acquire()
        try:
            with self.store.connect() as db:
                db.execute("INSERT INTO plans VALUES(?,?)", (plan_id, encode(plan)))
                db.execute("INSERT INTO tasks VALUES(?,?,?,?,?)",
                           (self.task_id, plan_id, "running", encode(body), now()))
                self.store._event(db, self.task_id, {"type": "legacy_start", "mode": mode, "entry": entry})
        except BaseException:
            self.lease.release()
            raise

    def log(self, message: str, level: str) -> None:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store._event(db, self.task_id, {"type": "operation_log", "message": scrub(message), "level": level})

    def finish(self, results: list[dict], error: str = "") -> None:
        try:
            changed = []
            candidates = list(self.output_paths)
            if self.shot_dir:
                candidates.extend(self.shot_dir.glob("*.png"))
            for path in dict.fromkeys(candidates):
                if path.is_file() and self.before_files.get(str(path)) != (path.stat().st_mtime_ns, path.stat().st_size):
                    changed.append(self.archives.attach(path))
            if changed:
                import json
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    body = json.loads(db.execute("SELECT body FROM tasks WHERE id=?", (self.task_id,)).fetchone()[0])
                    body["attachments"].extend(changed)
                    db.execute("UPDATE tasks SET body=? WHERE id=?", (encode(body), self.task_id))
            for index, record in enumerate(self.records):
                result = results[index] if index < len(results) else {"状态": "unknown", "错误": error or "结果未返回"}
                status = {"ok": "reported_success", "skipped": "skipped", "dry_run": "read_only",
                          "no_change": "skipped", "not_extendable": "skipped"}.get(
                    result.get("状态"), "unknown")
                self.store.record(self.task_id, record["_execution_id"], status, scrub(result))
            state = "unknown" if error or len(results) != len(self.records) or any(
                r.get("状态") not in ("ok", "skipped", "dry_run", "no_change", "not_extendable") for r in results) else "completed"
            self.store.finish(self.task_id, state, {"message": error, "verification": "legacy_reported"})
        finally:
            self.lease.release()


class ArchiveUI:
    """Delegate interactions, persisting operation logs before displaying them."""
    def __init__(self, wrapped, capture: LegacyCapture):
        object.__setattr__(self, "wrapped", wrapped)
        object.__setattr__(self, "capture", capture)

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    def __setattr__(self, name, value):
        setattr(self.wrapped, name, value)

    def log(self, message: str, level: str = "info"):
        self.capture.log(message, level)
        return self.wrapped.log(message, level)
