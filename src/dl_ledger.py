"""「常规资源位批量开关」自己动过哪些单元 / 创意的台账。

## 干什么

每跑完一轮，把**这一批真的翻转成功的**行记成一条：

    {"id": "20260904200157-3", "at": "2026-09-04 20:01:57",
     "level": "unit", "level_label": "单元",
     "activity": "708", "direction": "off", "verb": "暂停",
     "items": [{"id": "134222", "name": "会员弹窗-兜底-62686-0513"}, ...]}

`id` 是这一批的身份证：界面上「本工具操作过的」列出的每一批都能勾选，勾了哪几批就
只翻哪几批（`batches_for(..., ids=...)`）。老记录没有 id，用 `at` 兜底 —— 见 `bid()`。

下一次把「选哪些行」切到**本工具操作过的**，就能直接把这批**反向**再点一遍 ——
「我刚暂停的那批，现在恢复投放」「刚开起来的那批，再关掉」是这个配置类型最常见的
第二步，靠人肉记 ID 清单太容易漏。

## 为什么不复用 src/pt_ledger.py

那份记的是「策略 + 人群名称」（价格策略那套的坐标系），这边的坐标系是
「层级 + 活动 + ID」，一个字段都对不上；而且 pt 那套在线上跑着，改它的结构
等于动两个配置类型。两边都只有百来行，各记各的更省事。

## 约定

- 一次 run 记**一条**（一批），只记 `status == "ok"` 的行 —— 跳过的、失败的不进台账，
  否则"把这批翻回去"会翻到一堆根本没动过的行。
- `direction` 记的是**当时做了什么**（on=启动 / off=暂停）。取"要翻回去的那批"时
  按**反方向**挑：现在要启动，就找上次被暂停的那些。
- 读写都不抛异常：台账坏了顶多少一个范围可选，不能挡住开关本身。
- 只增不删。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .paths import user_path

log = logging.getLogger(__name__)

KEEP = 200          # 台账最多留多少批，多了从最旧的丢


def _path(ledger_name: str) -> Path:
    safe = "".join(c for c in str(ledger_name) if c not in r':\/?*[]<>|"').strip() or "常规资源位开关"
    return user_path("output", f"{safe}台账.json")


def path(ledger_name: str) -> str:
    """台账文件的绝对路径（界面上「打开」按钮用）。"""
    return str(_path(ledger_name))


def load(ledger_name: str) -> list[dict]:
    """全部批次，**新的在前**。读不了返回空。"""
    p = _path(ledger_name)
    if not p.exists():
        return []
    try:
        doc = json.loads(p.read_text(encoding="utf-8")) or {}
        batches = list(doc.get("batches") or [])
    except (OSError, ValueError):
        log.warning("台账读不了，当作空：%s", p, exc_info=True)
        return []
    return list(reversed(batches))


def append(ledger_name: str, *, level: str, level_label: str, activity: str,
           direction: str, verb: str, items: list[dict]) -> None:
    """记一批。items 为空就不记（跑了但一条都没成功）。"""
    items = [{"id": str(i.get("id") or ""), "name": str(i.get("name") or "")}
             for i in (items or []) if str(i.get("id") or "").strip()]
    if not items:
        return
    p = _path(ledger_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"batches": []}
    if p.exists():
        try:
            doc = json.loads(p.read_text(encoding="utf-8")) or {"batches": []}
        except (OSError, ValueError):
            log.warning("台账坏了，重写一份：%s", p, exc_info=True)
            doc = {"batches": []}
    if not isinstance(doc.get("batches"), list):
        doc["batches"] = []
    now = datetime.now()
    doc["batches"].append({
        # 同一秒内跑完两批也不会撞（后面接的是这份台账里的序号）
        "id": f"{now:%Y%m%d%H%M%S}-{len(doc['batches']) + 1}",
        "at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "level": str(level or ""), "level_label": str(level_label or ""),
        "activity": str(activity or ""),
        "direction": str(direction or ""), "verb": str(verb or ""),
        "items": items,
    })
    doc["batches"] = doc["batches"][-KEEP:]
    try:
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        log.warning("台账写不进去：%s", p, exc_info=True)


def bid(batch: dict) -> str:
    """这一批的身份证。老记录没写 id，退回用时间戳当 key。"""
    return str(batch.get("id") or batch.get("at") or "")


def batches_for(ledger_name: str, *, level: str = "", direction: str = "",
                activity: str = "", since: str | None = None,
                until: str | None = None, ids=None) -> list[dict]:
    """按层级 / 方向 / 活动 / 日期区间 / 批次id 挑批次，新的在前。

    空参数 = 这一项不过滤。`ids` 是界面上勾了哪几批（None / 空 = 不按批次挑）。
    """
    want = {str(i) for i in (ids or []) if str(i).strip()}
    out = []
    for b in load(ledger_name):
        if want and bid(b) not in want:
            continue
        if level and str(b.get("level") or "") != level:
            continue
        if direction and str(b.get("direction") or "") != direction:
            continue
        if activity and str(b.get("activity") or "") != activity:
            continue
        at = str(b.get("at", ""))
        if since and at[:10] < since:
            continue
        if until and at[:10] > until:
            continue
        out.append(b)
    return out


def items_for(ledger_name: str, **kw) -> list[dict]:
    """挑出来那些批次里的行，按 ID 去重，新的在前 → [{id, name}]。"""
    seen: set = set()
    out: list[dict] = []
    for b in batches_for(ledger_name, **kw):
        for it in b.get("items") or []:
            i = str(it.get("id") or "")
            if i and i not in seen:
                seen.add(i)
                out.append({"id": i, "name": str(it.get("name") or "")})
    return out
