"""Reliable full-archive outbox, independent of public aggregate telemetry."""
from __future__ import annotations

import json
from .archives import Archives, archive_chunks, collect_chunks, digest
from .task_store import TaskStore, encode
from .locking import FileLock


class Collection:
    def __init__(self, store: TaskStore):
        self.store = store
        self.archives = Archives(store)

    def enqueue(self, task_id: str, chunk_chars: int = 2000) -> int:
        document, data = self.archives.bundle(task_id)
        chunks = archive_chunks(document, data, chunk_chars)
        with self.store.connect() as db:
            for chunk in chunks:
                part_id = digest((chunk["archive_id"] + ":" + str(chunk["index"])).encode())
                previous = db.execute("SELECT body FROM outbox WHERE id=?", (part_id,)).fetchone()
                if previous and previous[0] != encode(chunk):
                    raise ValueError("现有分块配置不同，请完成原批次归集后再更改分块容量")
                db.execute("INSERT OR IGNORE INTO outbox(id,task_id,body,status) VALUES(?,?,?,?)",
                           (part_id, task_id, encode(chunk), "pending"))
        return len(chunks)

    def pending(self, task_id: str | None = None) -> list[dict]:
        with self.store.connect() as db:
            query = "SELECT * FROM outbox"
            args = ()
            if task_id:
                query += " WHERE task_id=?"
                args = (task_id,)
            return [{**dict(row), "body": json.loads(row["body"])} for row in db.execute(query, args)]

    def push(self, transport, limit: int = 100) -> dict:
        accepted = rejected = 0
        with FileLock(self.store.root / "collection.lock", timeout=0):
            rows = [x for x in self.pending() if x["status"] in ("pending", "rejected", "retry")]
            for row in rows[:limit]:
                try:
                    # One chunk per request: partial acceptance cannot drop another row.
                    ok = transport.send(row["body"])
                    status, detail = ("accepted", "接口已接收，仍待完整收集核验") if ok else ("rejected", "平台拒收；保留待处理")
                except Exception:
                    status, detail = "retry", "网络或响应异常；可能已送达，收集端按档案与分块 ID 去重"
                with self.store.connect() as db:
                    db.execute("UPDATE outbox SET status=?,detail=?,attempts=attempts+1 WHERE id=?",
                               (status, detail, row["id"]))
                accepted += status == "accepted"
                rejected += status != "accepted"
        return {"accepted": accepted, "pending_or_rejected": rejected,
                "note": "接口接收不等于完整归集"}

    def receive(self, chunks: list[dict]) -> dict:
        data = collect_chunks(chunks)
        receipt = self.archives.import_bytes(data, expected_id=chunks[0]["archive_id"])
        if receipt["status"] == "conflict":
            return receipt
        return receipt

    def acknowledge(self, receipt: dict) -> int:
        if receipt.get("status") not in ("received_complete", "duplicate"):
            raise ValueError("回执没有确认完整接收")
        matched = [x for x in self.pending() if x["body"]["archive_id"] == receipt.get("archive_id")
                   and x["body"]["package_sha256"] == receipt.get("package_sha256")]
        if not matched:
            raise ValueError("回执不匹配本机的档案版本")
        data = collect_chunks([x["body"] for x in matched])
        # Validate the receipt against our own manifest, not only a caller's success flag.
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            doc = json.loads(archive.read("manifest.json"))
        if receipt.get("sha256") != doc["sha256"] or receipt.get("attachments") != len(doc["body"]["attachments"]):
            raise ValueError("回执内容或附件数量不匹配")
        with self.store.connect() as db:
            db.executemany("UPDATE outbox SET status='verified_complete',detail=? WHERE id=?",
                           [(encode(receipt), x["id"]) for x in matched])
        return len(matched)


def read_all_pages(reader, page_size: int = 1000, max_pages: int = 10000) -> list[dict]:
    """Reader returns (rows, next_cursor); a cap never silently means complete."""
    cursor, seen, rows = None, set(), []
    for _ in range(max_pages):
        batch, next_cursor = reader(cursor, page_size)
        rows.extend(batch)
        if next_cursor is None:
            return rows
        if next_cursor in seen:
            raise ValueError("收集分页游标循环，结果不完整")
        seen.add(next_cursor)
        cursor = next_cursor
    raise ValueError("超过收集页数上限，不能把部分结果标为完整")
