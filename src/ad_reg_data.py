"""常规商广的数据：**全部来自准备页，不吃 Excel**。

  视频数量 N / 跳过前几个 K  → 规模：从「我的视频」跳过前 K 个往下取 N 个视频
  素材标题（换行写≤6条）/ 素材描述 / 落地页 → 这 N 条创意共用同一套文案

一次投放 = N 个视频 = N 条创意，每 10 条归一个单元 → ceil(N/10) 个单元。

⚠ 1.1.10 之前这三样在 Excel 里逐行填（一行 = 一条创意）。改掉是因为：
  要 15 个单元就得手填 150 行，而这 150 行内容一模一样；更要命的是单元数
  由「Excel 行数」决定、而不是准备页那个叫「视频数量」的框 —— 那个框当时
  什么都不管，只在对不上时给一句提醒。填 150 却只跑出 1 个单元就是这么来的。
  现在规模只有一个来源：视频数量。要恢复「每条创意各填各的」，见 git 历史。

⚠ 只服务 mode: ad_regular。原生商广走 ad_data，两边互不影响。
"""
from __future__ import annotations

import logging
from datetime import datetime

from .wizard_data import DataError

log = logging.getLogger(__name__)

TITLE_COL, DESC_COL, LANDING_COL = "素材标题", "素材描述", "落地页"
MAX_PER_UNIT = 10          # 页面写死：一个单元最多 10 条创意
MAX_TITLES = 6             # 页面写死：一条创意最多 6 条标题


def _grouping(cfg: dict) -> dict:
    return cfg.get("grouping") or {}


def _per_unit(cfg: dict) -> int:
    return int(_grouping(cfg).get("max_creatives", MAX_PER_UNIT)) or MAX_PER_UNIT


def unit_name(cfg: dict, seq: int, today: str) -> str:
    tpl = _grouping(cfg).get("name_template", "常规商广_{日期}_{序号}")
    return tpl.format(**{"日期": today, "序号": seq})


def _int(prep: dict, key: str, default: int = 0) -> int:
    try:
        return int(float(str(prep.get(key, "")).strip() or default))
    except (TypeError, ValueError):
        return default


def _split_titles(cell: str) -> list[str]:
    """换行写的多条标题 → 列表。空行丢掉。"""
    return [ln.strip() for ln in str(cell or "").replace("\r\n", "\n").split("\n") if ln.strip()]


def load(data_file: str, cfg: dict, settings: dict | None = None) -> dict:
    """读成 {"units": [...], "skip": K, "wanted": N}。

    ⚠ data_file 保留在签名里只为和别的 mode 一个形状，这里不读它（data_source: none）。

    每个单元 {seq, name, creatives}；每条创意
      {video_index, 素材标题(原文), titles(拆好的list), 素材描述, 落地页}
    """
    prep = (settings or {}).get("ad_prep") or {}
    n = max(0, _int(prep, "视频数量", 0))
    k = max(0, _int(prep, "跳过前几个", 0))
    per = _per_unit(cfg)
    today = datetime.now().strftime("%Y%m%d")

    if not n:
        raise DataError("准备页的「视频数量」是 0 —— 填几个视频就建几条创意，"
                        f"每 {per} 条归一个单元。")

    raw = str(prep.get(TITLE_COL, ""))
    titles = _split_titles(raw)
    desc = str(prep.get(DESC_COL, "")).strip()
    landing = str(prep.get(LANDING_COL, "")).strip()

    # N 条创意共用同一套文案，只有 video_index 不同
    creatives = [{
        "video_index": k + i,
        TITLE_COL: raw,
        "titles": list(titles),
        DESC_COL: desc,
        LANDING_COL: landing,
    } for i in range(n)]

    units = []
    for seq, start in enumerate(range(0, len(creatives), per), 1):
        chunk = creatives[start:start + per]
        units.append({"seq": seq, "name": unit_name(cfg, seq, today), "creatives": chunk})

    return {"units": units, "skip": k, "wanted": n}


def warnings(cfg: dict, data: dict, prep: dict) -> list[str]:
    """说得出口但不该拦住人的话。⚠ 别把这些挪回 validate() ——
    issues 非空的行会被 start_run 整行筛掉，只有一个单元时就是「整批跑不了」。"""
    out = []
    units = data["units"]
    per = _per_unit(cfg)
    if units and len(units[-1]["creatives"]) != per:
        out.append(f"最后一个单元只有 {len(units[-1]['creatives'])} 条创意"
                   f"（不满 {per} 条）—— 视频数量不是 {per} 的整数倍，不影响跑")
    return out


def validate(cfg: dict, data: dict, prep: dict) -> list[str]:
    """跑之前的体检。只放**真拦得住**的问题，提醒走 warnings()。"""
    issues = []
    if not data["units"]:
        issues.append("一条创意都建不出来 —— 准备页的「视频数量」填几个视频？")
        return issues

    # N 条创意共用一套文案，查一条就够，报错也不用带行号了
    c = data["units"][0]["creatives"][0]
    if not c["titles"]:
        issues.append("准备页「素材标题」是空的")
    if len(c["titles"]) > MAX_TITLES:
        issues.append(f"准备页「素材标题」{len(c['titles'])} 条，页面最多 {MAX_TITLES} 条")
    for t in c["titles"]:
        if not 2 <= len(t) <= 40:
            issues.append(f"标题「{t}」{len(t)} 字，要 2~40 字")
    d = c[DESC_COL]
    if not d:
        issues.append("准备页「素材描述」是空的")
    elif not 2 <= len(d) <= 10:
        issues.append(f"准备页「素材描述」{len(d)} 字，要 2~10 字：{d}")
    lp = c[LANDING_COL]
    if not lp:
        issues.append("准备页「落地页」是空的")
    elif not lp.lower().startswith("https://"):
        issues.append(f"准备页「落地页」要 https:// 开头：{lp}")
    return issues
