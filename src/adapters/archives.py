"""Versioned complete archives with content-addressed attachments and conflict handling."""
from __future__ import annotations

import base64
from datetime import date, datetime
from decimal import Decimal
import hashlib
import io
import json
import math
import re
from pathlib import Path, PurePosixPath
import zipfile
from uuid import uuid4

from .task_store import TaskStore, encode, now

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
SECRET_KEYS = {"password", "passwd", "cookie", "cookies", "authorization", "access_token",
               "refresh_token", "secret", "webhook", "sheet_webhook", "storage_state"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def scrub(value):
    if isinstance(value, dict):
        return {str(k): ("[excluded credential]" if str(k).lower() in SECRET_KEYS else scrub(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"(?i)([?&](?:key|access_token|refresh_token|token|scode)=)[^&\s]+",
                      r"\1[excluded]", value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Decimal, Path)):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return {"_type": "nonfinite_float", "value": str(value)}
    return value


class Archives:
    def __init__(self, store: TaskStore):
        self.store = store
        self.root = store.root / "attachments"
        self.root.mkdir(exist_ok=True)

    def _save_blob(self, checksum: str, data: bytes) -> None:
        from .locking import FileLock, atomic_bytes
        destination = self.root / checksum
        with FileLock(self.root / (checksum + ".lock"), timeout=3):
            if destination.exists():
                if digest(destination.read_bytes()) != checksum:
                    raise ValueError("已有同名损坏附件，请先恢复备份")
            else:
                atomic_bytes(destination, data)

    def attach(self, path: Path) -> dict:
        path = Path(path)
        if not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("附件不存在或超过 64 MiB 限制：" + path.name)
        data = path.read_bytes()
        value = digest(data)
        self._save_blob(value, data)
        return {"name": path.name, "sha256": value, "size": len(data),
                "path": "attachments/" + value}

    def bundle(self, task_id: str) -> tuple[dict, bytes]:
        task = self.store.task(task_id)
        if task["status"] in ("queued", "running", "awaiting_confirmation"):
            raise ValueError("运行中的任务暂不能导出完整档案，请等待结束或停止")
        with self.store.connect() as db:
            row = db.execute("SELECT body FROM plans WHERE id=?", (task["plan_id"],)).fetchone()
            plan = json.loads(row[0])
        body = scrub({"schema_version": 1, "task": task, "plan": plan,
                      "items": self.store.results(task_id), "events": self.store.events(task_id),
                      "attachments": task.get("attachments", []),
                      "completeness": task.get("completeness", "complete")})
        checksum = digest(encode(body).encode("utf-8"))
        document = {"archive_id": task_id + ":" + checksum, "sha256": checksum, "body": body}
        stream = io.BytesIO()
        if len(encode(document).encode("utf-8")) + sum(x["size"] for x in body["attachments"]) > MAX_ARCHIVE_BYTES:
            raise ValueError("档案解压后的内容超过 64 MiB，请拆分或使用受控文件通道")
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            def write(name: str, content):
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
            write("manifest.json", encode(document))
            written = set()
            for ref in body["attachments"]:
                if ref["path"] in written:
                    continue
                data = (self.root / ref["sha256"]).read_bytes()
                if digest(data) != ref["sha256"] or len(data) != ref["size"]:
                    raise ValueError("本机附件损坏：" + ref["name"])
                write(ref["path"], data)
                written.add(ref["path"])
        if stream.tell() > MAX_ARCHIVE_BYTES:
            raise ValueError("档案超过 64 MiB，请使用受控文件通道处理")
        return document, stream.getvalue()

    def export(self, task_id: str, destination: Path) -> dict:
        document, data = self.bundle(task_id)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Never silently overwrite the user's existing file.
        with destination.open("xb") as stream:
            stream.write(data)
        return {"archive_id": document["archive_id"], "sha256": digest(data),
                "path": str(destination), "bytes": len(data)}

    def import_bytes(self, data: bytes, expected_id: str | None = None) -> dict:
        if len(data) > MAX_ARCHIVE_BYTES:
            raise ValueError("档案包超过大小限制")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            names = [x.filename for x in infos]
            if len(names) != len(set(names)) or len(names) > 2001:
                raise ValueError("重复文件名或附件过多")
            if sum(x.file_size for x in infos) > MAX_ARCHIVE_BYTES:
                raise ValueError("解压后档案超过大小限制")
            for name in names:
                if "\\" in name or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
                    raise ValueError("非法包内路径")
            document = json.loads(archive.read("manifest.json"))
            if expected_id is not None and document.get("archive_id") != expected_id:
                raise ValueError("分块标识与档案标识不一致")
            body = document["body"]
            if body.get("schema_version") != 1:
                raise ValueError("不支持的档案版本")
            checksum = digest(encode(body).encode("utf-8"))
            if checksum != document["sha256"]:
                raise ValueError("档案内容校验失败")
            attachments = []
            expected = {"manifest.json"}
            for ref in body.get("attachments", []):
                if ref["path"] != "attachments/" + ref["sha256"] or len(ref["sha256"]) != 64:
                    raise ValueError("非法附件路径")
                part = archive.read(ref["path"])
                if len(part) != ref["size"] or digest(part) != ref["sha256"]:
                    raise ValueError("附件校验失败")
                expected.add(ref["path"])
                attachments.append((ref, part))
            if set(names) != expected:
                raise ValueError("存在未声明的文件")
        archive_id = str(document["archive_id"])
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT digest FROM imports WHERE id=?", (archive_id,)).fetchone()
            if prior and prior[0] != checksum:
                db.execute("INSERT INTO conflicts VALUES(?,?,?,?)",
                           ("conflict_" + uuid4().hex, archive_id, checksum, encode(document)))
                return {"status": "conflict", "archive_id": archive_id}
            if not prior:
                # Immutable files first, then one transaction makes the archive visible.
                # A failed import can leave unreferenced blobs, never a partial visible archive.
                for ref, part in attachments:
                    self._save_blob(ref["sha256"], part)
                db.execute("INSERT INTO imports VALUES(?,?,?)", (archive_id, checksum, encode(document)))
        return {"status": "duplicate" if prior else "received_complete", "archive_id": archive_id,
                "sha256": checksum, "package_sha256": digest(data),
                "attachments": len(attachments), "received_at": now()}

    def imported(self, limit: int = 100) -> list[dict]:
        with self.store.connect() as db:
            return [json.loads(x[0]) for x in db.execute("SELECT body FROM imports ORDER BY rowid DESC LIMIT ?",
                                                        (max(1, min(limit, 500)),))]

    def import_legacy(self, path: Path) -> dict:
        """Import a v1 manifest as read-only, explicitly incomplete history."""
        path = Path(path)
        if path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("旧清单过大")
        legacy = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(legacy, dict) or not isinstance(legacy.get("items"), list) or not legacy.get("run_id"):
            raise ValueError("不是受支持的旧版运行清单")
        body = {"schema_version": 1, "completeness": "legacy_partial",
                "legacy_manifest": scrub(legacy),
                "limitations": ["旧清单缺失的原始输入、逐步事件或附件无法事后恢复",
                                "导入仅供查阅，不生成可执行任务或已核验成功记录"], "attachments": []}
        checksum = digest(encode(body).encode("utf-8"))
        archive_id = "legacy:" + str(legacy["run_id"])
        document = {"archive_id": archive_id, "sha256": checksum, "body": body}
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as package:
            package.writestr("manifest.json", encode(document))
        return self.import_bytes(data.getvalue())


def archive_chunks(document: dict, data: bytes, chunk_chars: int) -> list[dict]:
    if not 128 <= chunk_chars <= 16000:
        raise ValueError("分块容量必须在 128–16000 字符内，并以实际表格限制为准")
    encoded = base64.b64encode(data).decode("ascii")
    parts = [encoded[i:i + chunk_chars] for i in range(0, len(encoded), chunk_chars)]
    return [{"schema_version": 1, "archive_id": document["archive_id"], "index": i,
             "count": len(parts), "package_sha256": digest(data), "content": part}
            for i, part in enumerate(parts)]


def collect_chunks(chunks: list[dict]) -> bytes:
    if not chunks:
        raise ValueError("没有档案分块")
    first, seen = chunks[0], {}
    count = first["count"]
    if not isinstance(count, int) or count < 1 or count > 700000:
        raise ValueError("非法分块总数")
    for chunk in chunks:
        if any(chunk[k] != first[k] for k in ("archive_id", "count", "package_sha256", "schema_version")):
            raise ValueError("分块档案或版本冲突")
        index = chunk["index"]
        if not isinstance(index, int) or not 0 <= index < count:
            raise ValueError("非法分块序号")
        if index in seen and seen[index] != chunk["content"]:
            raise ValueError("同一分块内容冲突")
        seen[index] = chunk["content"]
    if len(seen) != count:
        raise ValueError("档案分块不完整")
    content = "".join(seen[i] for i in range(count))
    if len(content) > MAX_ARCHIVE_BYTES * 4 // 3 + 4:
        raise ValueError("档案内容过大")
    data = base64.b64decode(content, validate=True)
    if digest(data) != first["package_sha256"]:
        raise ValueError("档案整体校验失败")
    return data
