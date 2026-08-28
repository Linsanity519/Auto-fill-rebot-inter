"""价格策略「本工具配置过的」台账。

## 干什么

`价格策略配置`（通用 Runner，yaml 里写了 `ledger: 价格策略开关`）每跑完一轮，
把这一批**成功配好的人群名称**连同**所在策略**记成一条：

    {"at": "2026-08-28 15:20:03", "strategy_id": "186",
     "strategy_name": "子凡测试", "strategy_url": "...://.../edit/186",
     "run_id": "...", "names": ["111", "222", "333"]}

`价格策略批量开启 / 关闭` 选「本工具配置过的」范围时，读这个文件、按当前打开的
策略筛出人群名称，去页面上挨行点开关。

## 约定

- 一次 run 记**一条**（一批），不按行记 —— 用户要的就是「开/关 昨天那批」。
- `strategy_id` 存**路由数字ID**（URL 尾 `/edit/186` 的 186），不是页面上显示的
  业务ID（0713…）。跨策略要用它对得上，见 docs/价格策略批量开关-配置项抓取.md §六。
- 读写都不抛异常：台账坏了顶多少一个「按配置过的」范围，不能挡住开关本身。
- 只增不删，也不回写开关状态（要审计再说）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .paths import user_path

log = logging.getLogger(__name__)


def _path(ledger_name: str) -> Path:
    safe = "".join(c for c in str(ledger_name) if c not in r':\/?*[]<>|"').strip() or "价格策略开关"
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


def append(ledger_name: str, *, strategy_id: str, strategy_name: str,
           strategy_url: str, run_id: str, names: list[str]) -> None:
    """记一批。names 为空就不记（跑了但一条都没成功）。"""
    names = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not names:
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
    doc["batches"].append({
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "strategy_id": str(strategy_id or ""),
        "strategy_name": str(strategy_name or ""),
        "strategy_url": str(strategy_url or ""),
        "run_id": str(run_id or ""),
        "names": names,
    })
    try:
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        log.warning("台账写不进去：%s", p, exc_info=True)


def batches_for(ledger_name: str, strategy_id: str) -> list[dict]:
    """某条策略下的批次，新的在前。strategy_id 空 = 不过滤。"""
    sid = str(strategy_id or "")
    out = load(ledger_name)
    if not sid:
        return out
    return [b for b in out if str(b.get("strategy_id") or "") == sid]


def strategies(ledger_name: str) -> list[dict]:
    """台账里出现过的不同策略，**最近配过的在前**。

    → [{"id", "name", "url", "last_at", "batches"}]
    「本工具配置过的」范围 + 策略框留空 时，就是逐个开/关这几条。
    """
    order: list[str] = []
    agg: dict[str, dict] = {}
    for b in load(ledger_name):          # 已经是新→旧
        sid = str(b.get("strategy_id") or "")
        if not sid:
            continue
        if sid not in agg:
            order.append(sid)
            agg[sid] = {"id": sid, "name": b.get("strategy_name") or "",
                        "url": b.get("strategy_url") or "",
                        "last_at": b.get("at") or "", "batches": 0}
        cur = agg[sid]
        cur["batches"] += 1
        if not cur["name"] and b.get("strategy_name"):
            cur["name"] = b["strategy_name"]
        if not cur["url"] and b.get("strategy_url"):
            cur["url"] = b["strategy_url"]
    return [agg[s] for s in order]


def names_for(ledger_name: str, strategy_id: str, since: str | None = None,
              until: str | None = None) -> list[str]:
    """某条策略下、本工具配过的全部人群名称，去重，新的在前。

    since / until：'YYYY-MM-DD'，只要这个日期区间里配的那几批（含端点）。
    """
    seen: set = set()
    out: list[str] = []
    for b in batches_for(ledger_name, strategy_id):
        at = str(b.get("at", ""))
        if since and at[:10] < since:
            continue
        if until and at[:10] > until:
            continue
        for n in b.get("names") or []:
            n = str(n).strip()
            if n and n not in seen:
                seen.add(n)
                out.append(n)
    return out
