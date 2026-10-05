"""Collect private full archives separately from public team.json.

Use --input for a local JSON export. Live mode requires a verified --sql-template
containing {limit} and {offset}, field aliases matching the archive contract,
and the maintainer's existing wecom-cli authorization. This tool never pushes Git.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.adapters.collection import Collection, read_all_pages
from src.adapters.task_store import TaskStore
from tools.collect_usage import sheet_query, stats_docid


def collect(rows: list[dict], target: Collection) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        if not row.get("archive_id"):
            continue  # Existing summary rows may share the table.
        value = dict(row)
        for key in ("index", "count", "schema_version"):
            value[key] = int(value[key])
        groups[value["archive_id"]].append(value)
    receipts = []
    for archive_id, chunks in groups.items():
        try:
            receipts.append(target.receive(chunks))
        except (ValueError, KeyError) as exc:
            receipts.append({"status": "incomplete_or_conflict", "archive_id": archive_id, "error": str(exc)})
    return receipts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="列名为 archive_id/index/count/package_sha256/content/schema_version 的 JSON")
    parser.add_argument("--sql-template", help="经实际平台验证的分页 SQL 文件；只允许 SELECT")
    parser.add_argument("--docid")
    parser.add_argument("--database-dir", required=True, help="维护者私有档案位置，不要放进公共仓库")
    parser.add_argument("--receipts-dir", required=True)
    args = parser.parse_args()
    if bool(args.input) == bool(args.sql_template):
        parser.error("必须且只能选择 --input 或 --sql-template")
    if args.input:
        rows = json.loads(Path(args.input).read_text(encoding="utf-8"))
    else:
        sql = Path(args.sql_template).read_text(encoding="utf-8").strip()
        if not sql.upper().startswith("SELECT ") or ";" in sql or not all(x in sql for x in ("{limit}", "{offset}")):
            parser.error("需要包含 {limit}/{offset} 的单条只读 SELECT")
        docid = args.docid or stats_docid()
        if not docid:
            parser.error("未配置当前智能表格")
        def page(cursor, size):
            offset = int(cursor or 0)
            batch = sheet_query(docid, sql.format(limit=size, offset=offset))
            return batch, offset + size if len(batch) == size else None
        rows = read_all_pages(page)
    receipts = collect(rows, Collection(TaskStore(Path(args.database_dir))))
    destination = Path(args.receipts_dir)
    destination.mkdir(parents=True, exist_ok=True)
    from src.adapters.archives import digest
    for receipt in receipts:
        filename = digest(receipt["archive_id"].encode()) + ".json"
        (destination / filename).write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"archives": len(receipts), "complete": sum(x["status"] in
          ("received_complete", "duplicate") for x in receipts),
          "receipts": str(destination)}, ensure_ascii=False))
    return int(any(x["status"] not in ("received_complete", "duplicate") for x in receipts))


if __name__ == "__main__":
    raise SystemExit(main())
