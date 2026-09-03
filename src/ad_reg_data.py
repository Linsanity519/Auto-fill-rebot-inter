"""常规商广的数据：准备页给「视频数量 N / 跳过前几个 K / 目的·内容·转化 / 出价…」，
Excel 给每条创意的「素材标题 / 素材描述 / 落地页」。

一次投放 = N 个视频（从「我的视频」跳过前 K 个往下顺延取），Excel N 行，
第 i 行配第 i 个视频。每 10 个视频归一个单元，每个视频一条创意。

⚠ 只服务 mode: ad_regular。原生商广走 ad_data，两边互不影响。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

# 读 xlsx 的活儿和原生商广一样，直接复用（隐藏行列处理、float→int 收敛都在里面）
from .ad_data import _rows as _read_rows
from .wizard_data import DataError

log = logging.getLogger(__name__)

TITLE_COL, DESC_COL, LANDING_COL = "素材标题", "素材描述", "落地页"
MAX_PER_UNIT = 10          # 页面写死：一个单元最多 10 条创意
MAX_TITLES = 6             # 页面写死：一条创意最多 6 条标题


def _grouping(cfg: dict) -> dict:
    return cfg.get("grouping") or {}


def _per_unit(cfg: dict) -> int:
    return int(_grouping(cfg).get("max_creatives", MAX_PER_UNIT)) or MAX_PER_UNIT


def columns(cfg: dict) -> list[dict]:
    return [dict(c) for c in (cfg.get("columns") or [])]


def unit_name(cfg: dict, seq: int, today: str) -> str:
    tpl = _grouping(cfg).get("name_template", "常规商广_{日期}_{序号}")
    return tpl.format(**{"日期": today, "序号": seq})


def _int(prep: dict, key: str, default: int = 0) -> int:
    try:
        return int(float(str(prep.get(key, "")).strip() or default))
    except (TypeError, ValueError):
        return default


def _split_titles(cell: str) -> list[str]:
    """一个单元格里换行写的多条标题 → 列表。空行丢掉。"""
    return [ln.strip() for ln in str(cell or "").replace("\r\n", "\n").split("\n") if ln.strip()]


def load(data_file: str, cfg: dict, settings: dict | None = None) -> dict:
    """读成 {"units": [...], "skip": K, "wanted": N}。

    每个单元 {seq, name, creatives}；每条创意
      {video_index, 素材标题(原文), titles(拆好的list), 素材描述, 落地页, _row}
    """
    prep = (settings or {}).get("ad_prep") or {}
    n = max(0, _int(prep, "视频数量", 0))
    k = max(0, _int(prep, "跳过前几个", 0))
    per = _per_unit(cfg)
    today = datetime.now().strftime("%Y%m%d")

    if not data_file:
        raise DataError("还没选数据文件")
    rows, idx = _read_rows(data_file)
    name = Path(data_file).name
    if not rows:
        hint = ("　这份是生成出来的空模板（只有表头），请选你自己填过数据的那份表。"
                if "模板" in name else "　第 1 行是表头，数据从第 2 行开始。")
        raise DataError(f"「{name}」里一行数据都没有。\n{hint}")

    need = [c["name"] for c in columns(cfg) if c.get("required") and c["name"] not in idx]
    if need:
        raise DataError(f"「{name}」表头缺必填列：{'、'.join(need)}。现有列：{'、'.join(idx) or '（空）'}")

    creatives = []
    for i, rec in enumerate(rows):
        titles = _split_titles(rec.get(TITLE_COL, ""))
        creatives.append({
            "video_index": k + i,
            TITLE_COL: rec.get(TITLE_COL, ""),
            "titles": titles,
            DESC_COL: str(rec.get(DESC_COL, "")).strip(),
            LANDING_COL: str(rec.get(LANDING_COL, "")).strip(),
            "_row": rec.get("_row", i + 2),
        })

    units = []
    for seq, start in enumerate(range(0, len(creatives), per), 1):
        chunk = creatives[start:start + per]
        units.append({"seq": seq, "name": unit_name(cfg, seq, today), "creatives": chunk})

    return {"units": units, "skip": k, "wanted": n}


def validate(cfg: dict, data: dict, prep: dict) -> list[str]:
    """跑之前的体检。"""
    issues = []
    n = _int(prep, "视频数量", 0)
    total = sum(len(u["creatives"]) for u in data["units"])

    if not data["units"]:
        issues.append("Excel 一行数据都没有，一个单元都建不出来")
    if n and total != n:
        issues.append(f"准备页「视频数量」填的是 {n}，但 Excel 有 {total} 行 —— 对不上，"
                      f"按 Excel 的 {total} 行算（{len(data['units'])} 个单元）")

    for u in data["units"]:
        for c in u["creatives"]:
            r = c["_row"]
            head = f"第{r}行"
            if not c["titles"]:
                issues.append(f"{head}「素材标题」是空的")
            if len(c["titles"]) > MAX_TITLES:
                issues.append(f"{head}「素材标题」{len(c['titles'])} 条，页面最多 {MAX_TITLES} 条")
            for t in c["titles"]:
                if not 2 <= len(t) <= 40:
                    issues.append(f"{head} 标题「{t}」{len(t)} 字，要 2~40 字")
            d = c[DESC_COL]
            if not d:
                issues.append(f"{head}「素材描述」是空的")
            elif not 2 <= len(d) <= 10:
                issues.append(f"{head}「素材描述」{len(d)} 字，要 2~10 字：{d}")
            lp = c[LANDING_COL]
            if not lp:
                issues.append(f"{head}「落地页」是空的")
            elif not lp.lower().startswith("https://"):
                issues.append(f"{head}「落地页」要 https:// 开头：{lp}")
    return issues
