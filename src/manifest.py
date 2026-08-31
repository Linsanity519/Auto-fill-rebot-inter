"""运行产物清单：一次批量跑完，落一份「这批到底动了哪些东西」。

## 为什么

工具会在后台**真的建出**活动 / 单元，或**真的翻转**策略开关。选错了方案跑了 40 条，
现在只能一条条回后台手动收拾 —— 没有一张「这次碰过谁」的清单。

这份 json 就是那张清单：逐条记名称 + 结果 + runner 能顺手给出的那点线索
（策略、方向、资源位…）。给人工清理当对照表；「价格策略批量开关」还能拿它一键翻回去
（见 webapp.Api.pt_rollback）。

## 口径

- 不碰埋点那套铁律 —— 这份是**本机 output/ 下的文件**，不上传、不汇总，写全名无妨。
- 只记 runner 结果里现成的字段，不额外去查后台。拿不到 id 就只有名称，依然够对照。
- 写失败绝不能挡住「跑完了」的提示：这里任何异常都吞掉、返回空。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from .paths import user_path

log = logging.getLogger(__name__)

# 结果字典里值得留进清单的键（其余的是给 result.csv / 前端详情用的大字段，不进）
_KEEP = ("策略", "方向", "计划动作", "资源位", "单元名称", "活动ID", "单元ID",
         "activity_id", "unit_id", "url", "延期至", "人群ID", "实验ID")


def _safe_name(s: str) -> str:
    return "".join(c for c in str(s or "") if c not in r':\/?*[]<>|"').strip() or "run"


def _one(row, res: dict) -> dict:
    d = res or {}
    item = {
        "name": (getattr(row, "name", None) or d.get("名称") or d.get("name") or ""),
        "status": str(d.get("状态") or d.get("status") or ""),
        "note": str(d.get("错误") or d.get("说明") or d.get("note") or ""),
    }
    for k in _KEEP:
        v = d.get(k)
        if v not in (None, "", [], {}):
            item[k] = v
    return item


def write(run_id: str, form: str, mode: str, kept_rows, results, extra: dict | None = None) -> str:
    """落一份清单，返回文件路径（写失败返回 ""）。

    kept_rows / results 位置一一对应时逐行配对；对不上就只按 results 走。
    """
    try:
        rows = list(kept_rows or [])
        reslist = list(results or [])
        if len(rows) == len(reslist):
            items = [_one(r, res) for r, res in zip(rows, reslist)]
        else:
            items = [_one(None, res) for res in reslist]

        counts = {"ok": 0, "failed": 0, "skipped": 0, "dry": 0}
        for it in items:
            st = it["status"]
            st = {"dry_run": "dry", "not_extendable": "skipped"}.get(st, st)
            if st in counts:
                counts[st] += 1

        doc = {
            "run_id": run_id,
            "form": form,
            "mode": mode,
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "counts": counts,
            "items": items,
        }
        if extra:
            doc.update(extra)

        d = user_path("output", "manifests")
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{_safe_name(form)}-{run_id}.json"
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(p)
    except Exception:
        log.warning("产物清单写入失败（不影响运行）", exc_info=True)
        return ""


def load(path: str) -> dict:
    try:
        import pathlib
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:
        log.warning("产物清单读不了：%s", path, exc_info=True)
        return {}


def latest_for(form: str) -> str:
    """这个配置类型最近一份清单的路径。没有返回 ""。"""
    try:
        d = user_path("output", "manifests")
        cands = sorted(d.glob(f"{_safe_name(form)}-*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        return str(cands[0]) if cands else ""
    except Exception:
        return ""
