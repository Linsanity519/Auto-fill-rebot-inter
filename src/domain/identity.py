"""稳定业务标识；展示行号与执行标识分开。"""
from __future__ import annotations

import json
from uuid import NAMESPACE_URL, uuid5


def stable_id(kind: str, value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return f"{kind}_{uuid5(NAMESPACE_URL, kind + ':' + encoded).hex}"


def identify_records(records: list[dict], context: object) -> list[dict]:
    """相同内容重复行仍有不同标识；筛选后已分配的 ID 不再变化。"""
    counts: dict[str, int] = {}
    result = []
    for index, record in enumerate(records):
        item = dict(record)
        content = {k: v for k, v in item.items() if not k.startswith("_execution_")}
        signature = stable_id("record", [context, content])
        occurrence = counts.get(signature, 0)
        counts[signature] = occurrence + 1
        item.setdefault("_execution_id", stable_id("item", [signature, occurrence]))
        item.setdefault("_execution_row", index + 1)
        result.append(item)
    return result
