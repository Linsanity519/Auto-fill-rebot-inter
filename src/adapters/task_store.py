"""SQLite local archive and persistent command queue; one connection per call."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from ..domain.pricing import PricePlan, check_selection, plan_fingerprint
from .locking import FileLock, ResourceBusy


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class TaskStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "tasks.sqlite3"
        with FileLock(self.root / "migration.lock", timeout=10):
            with self.connect() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version > 1:
                    raise RuntimeError("任务库来自较新版本，请使用匹配版本；不会降级覆盖")
                if version == 0:
                    if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                        self.backup(self.root / "before-v1.sqlite3")
                        raise RuntimeError("未识别的旧任务库已备份，拒绝覆盖")
                    db.executescript("""
                    CREATE TABLE plans(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                    CREATE TABLE tasks(id TEXT PRIMARY KEY, plan_id TEXT NOT NULL,
                      status TEXT NOT NULL, body TEXT NOT NULL, created TEXT NOT NULL);
                    CREATE TABLE items(task_id TEXT, item_id TEXT, status TEXT NOT NULL,
                      detail TEXT NOT NULL, PRIMARY KEY(task_id,item_id));
                    CREATE TABLE events(task_id TEXT, seq INTEGER, event_id TEXT UNIQUE,
                      body TEXT NOT NULL, PRIMARY KEY(task_id,seq));
                    CREATE TABLE commands(id TEXT PRIMARY KEY, operation TEXT, body TEXT,
                      status TEXT, response TEXT, created TEXT);
                    CREATE TABLE controls(task_id TEXT PRIMARY KEY, paused INTEGER DEFAULT 0,
                      cancelled INTEGER DEFAULT 0, decision TEXT DEFAULT '');
                    CREATE TABLE imports(id TEXT PRIMARY KEY, digest TEXT, body TEXT);
                    CREATE TABLE conflicts(id TEXT PRIMARY KEY, archive_id TEXT, digest TEXT, body TEXT);
                    CREATE TABLE outbox(id TEXT PRIMARY KEY, task_id TEXT, body TEXT,
                      status TEXT, attempts INTEGER DEFAULT 0, detail TEXT DEFAULT '');
                    PRAGMA user_version=1;
                    """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def backup(self, destination: Path) -> None:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.resolve() == self.path.resolve():
            raise ValueError("备份目标不能是正在使用的数据库")
        with self.connect() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()

    def save_plan(self, plan: PricePlan) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO plans VALUES(?,?)", (plan.plan_id, encode(plan.to_dict())))

    def plan(self, plan_id: str) -> PricePlan:
        return PricePlan.from_dict(self.plan_document(plan_id))

    def plan_document(self, plan_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT body FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise ValueError("找不到指定计划")
        return json.loads(row[0])

    def create_task(self, plan_id: str, selected: list[str], mode: str, entry: str) -> str:
        if mode not in ("auto", "confirm", "sample"):
            raise ValueError("执行模式无效")
        plan = self.plan(plan_id)
        check_selection(plan, selected)
        approval = {"fingerprint": plan_fingerprint(plan), "selected": selected,
                    "mode": mode, "entry": entry, "approved_at": now(),
                    "identity_kind": "local_user_action"}
        key = plan_fingerprint(plan) + encode(sorted(selected))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # A plan/selection can be submitted only once, even from two clients.
            for row in db.execute("SELECT id,body FROM tasks WHERE plan_id=?", (plan_id,)):
                if json.loads(row["body"]).get("execution_key") == key:
                    return row["id"]
            for row in db.execute("""
                SELECT i.item_id,p.body FROM items i
                JOIN tasks t ON t.id=i.task_id JOIN plans p ON p.id=t.plan_id
                WHERE i.status IN ('submitted','unknown')"""):
                previous = json.loads(row["body"])
                if row["item_id"] in selected and previous.get("context") == plan.context:
                    raise ValueError("所选对象存在结果未知的历史提交，请先核验原任务")
            task_id = "task_" + uuid4().hex
            body = {"approval": approval, "execution_key": key}
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?)",
                       (task_id, plan_id, "queued", encode(body), now()))
            db.execute("INSERT INTO controls(task_id) VALUES(?)", (task_id,))
            self._event(db, task_id, {"type": "approved", **approval})
            # Task and dispatch are one transaction: no orphaned queued task.
            db.execute("INSERT INTO commands VALUES(?,?,?,?,?,?)",
                       ("cmd_" + uuid4().hex, "execute", encode({"task_id": task_id}),
                        "queued", "{}", now()))
        return task_id

    def task(self, task_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("找不到指定任务")
        return {"task_id": row["id"], "plan_id": row["plan_id"], "status": row["status"],
                "created_at": row["created"], **json.loads(row["body"])}

    def tasks(self, limit: int = 100) -> list[dict]:
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM tasks ORDER BY created DESC LIMIT ?",
                                          (max(1, min(limit, 500)),))]
        return [self.task(x) for x in ids]

    def _event(self, db, task_id: str, event: dict) -> None:
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE task_id=?",
                         (task_id,)).fetchone()[0]
        db.execute("INSERT INTO events VALUES(?,?,?,?)",
                   (task_id, seq, "event_" + uuid4().hex, encode({"time": now(), **event})))

    def record(self, task_id: str, item_id: str, status: str, detail: dict) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone():
                raise ValueError("任务不存在")
            db.execute("INSERT OR REPLACE INTO items VALUES(?,?,?,?)",
                       (task_id, item_id, status, encode(detail)))
            self._event(db, task_id, {"type": "item", "item_id": item_id, "status": status, **detail})

    def finish(self, task_id: str, status: str, detail: dict) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("任务不存在")
            body = json.loads(row[0])
            body["detail"] = detail
            db.execute("UPDATE tasks SET status=?,body=? WHERE id=?", (status, encode(body), task_id))
            self._event(db, task_id, {"type": "task", "status": status, **detail})

    def results(self, task_id: str) -> list[dict]:
        with self.connect() as db:
            return [{"item_id": r["item_id"], "status": r["status"], "detail": json.loads(r["detail"])}
                    for r in db.execute("SELECT * FROM items WHERE task_id=? ORDER BY rowid", (task_id,))]

    def events(self, task_id: str, after: int = 0) -> list[dict]:
        with self.connect() as db:
            return [{"seq": r["seq"], "event_id": r["event_id"], **json.loads(r["body"])}
                    for r in db.execute("SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq",
                                        (task_id, after))]

    def command(self, operation: str, body: dict) -> str:
        if operation not in ("prepare", "reverse", "reconcile"):
            raise ValueError("不支持的宿主命令")
        command_id = "cmd_" + uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO commands VALUES(?,?,?,?,?,?)",
                       (command_id, operation, encode(body), "queued", "{}", now()))
        return command_id

    def claim_command(self) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM commands WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE commands SET status='running' WHERE id=?", (row["id"],))
            return {**dict(row), "body": json.loads(row["body"])}

    def command_result(self, command_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT status,response FROM commands WHERE id=?", (command_id,)).fetchone()
        if row is None:
            raise ValueError("命令不存在")
        return {"status": row["status"], **json.loads(row["response"])}

    def complete_command(self, command_id: str, response: dict, success: bool) -> None:
        with self.connect() as db:
            db.execute("UPDATE commands SET status=?,response=? WHERE id=?",
                       ("completed" if success else "failed", encode(response), command_id))

    def control(self, task_id: str, **changes) -> dict:
        if any(k not in ("paused", "cancelled", "decision") for k in changes):
            raise ValueError("非法控制字段")
        if "decision" in changes and changes["decision"] not in ("submit", "auto", "skip", "stop", ""):
            raise ValueError("非法确认动作")
        with self.connect() as db:
            for key, value in changes.items():
                db.execute(f"UPDATE controls SET {key}=? WHERE task_id=?", (value, task_id))
            row = db.execute("SELECT * FROM controls WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("任务控制记录不存在")
        return dict(row)

    def recover_host(self) -> None:
        # Only the holder of host.lock calls this; never assume a crashed write failed.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("""SELECT t.id,p.body FROM tasks t JOIN plans p ON p.id=t.plan_id
                                     WHERE t.status IN ('running','awaiting_confirmation')""").fetchall():
                if json.loads(row["body"]).get("capability") != "pricing.toggle":
                    continue
                db.execute("UPDATE tasks SET status='interrupted' WHERE id=?", (row["id"],))
                self._event(db, row["id"], {"type": "host_recovery", "status": "interrupted"})
            db.execute("UPDATE commands SET status='failed',response=? WHERE status='running'",
                       (encode({"error": "宿主中断，请查询原任务并只读核验，命令不会自动重放"}),))

    def answer(self, task_id: str, item_id: str, decision: str) -> None:
        if decision not in ("submit", "auto", "skip", "stop"):
            raise ValueError("非法确认动作")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,body FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["status"] != "awaiting_confirmation":
                raise ValueError("此任务当前没有等待确认")
            if json.loads(row["body"]).get("detail", {}).get("item_id") != item_id:
                raise ValueError("确认条目已变化")
            db.execute("UPDATE controls SET decision=? WHERE task_id=?", (decision, task_id))
            self._event(db, task_id, {"type": "decision", "item_id": item_id, "decision": decision})

    def recover_legacy(self) -> None:
        """An OS lease, rather than a guessed PID, distinguishes live old runners."""
        with self.connect() as db:
            rows = db.execute("""SELECT t.id,p.body FROM tasks t JOIN plans p ON p.id=t.plan_id
                                 WHERE t.status='running'""").fetchall()
        for row in rows:
            if not str(json.loads(row["body"]).get("capability", "")).startswith("legacy."):
                continue
            try:
                with FileLock(self.root / "legacy-locks" / (row["id"] + ".lock")):
                    if self.task(row["id"])["status"] == "running":
                        self.finish(row["id"], "unknown",
                                    {"message": "原执行进程已离开，保留输入与已记录日志；平台结果须人工核查"})
            except ResourceBusy:
                continue
