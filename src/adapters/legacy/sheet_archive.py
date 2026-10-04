"""Reuse existing release-injected webhook configuration, with explicit archive fields."""
from __future__ import annotations

from ... import report
from ..collection import Collection

FIELDS = ("archive_id", "index", "count", "package_sha256", "content", "schema_version")


class SheetArchiveTransport:
    def __init__(self, settings: dict):
        config = settings.get("archive_collection") or {}
        if config.get("enabled") is not True or config.get("schema_verified") is not True:
            raise ValueError("完整档案通道尚未启用：须核实智能表格字段和单元格容量")
        self.mapping = config.get("fields") or {}
        if set(self.mapping) != set(FIELDS) or len(set(self.mapping.values())) != len(FIELDS):
            raise ValueError("档案字段映射必须完整且不能复用同一列")
        self.url = report.sheet_webhook_url(settings)
        if not self.url:
            raise ValueError("缺少现有智能表格回传配置")
        self.max_chars = int(config.get("chunk_chars", 2000))

    def send(self, chunk: dict) -> bool:
        if len(chunk["content"]) > self.max_chars:
            raise ValueError("分块超出已确认字段容量")
        result = report._post_json(self.url, {"add_records": [
            {"values": {self.mapping[k]: chunk[k] for k in FIELDS}}]}, report.SHEET_TIMEOUT)
        # Missing status is NOT positive acknowledgement for complete archives.
        return result.get("errcode") == 0


def sync_archives(store, settings: dict) -> dict:
    store.recover_legacy()
    config = settings.get("archive_collection") or {}
    collection = Collection(store)
    for task in store.tasks(500):
        if task["status"] not in ("queued", "running", "awaiting_confirmation"):
            collection.enqueue(task["task_id"], int(config.get("chunk_chars", 2000)))
    try:
        transport = SheetArchiveTransport(settings)
    except ValueError as exc:
        return {"status": "configuration_required", "message": str(exc)}
    return {"status": "attempted", **collection.push(transport)}
