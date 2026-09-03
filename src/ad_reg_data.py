"""常规商广的「数据」：没有 Excel，一切从准备页参数推出来。

一次投放 = N 个单元，每个单元一个视频、6 条共用文案。
「视频从哪来」不进这里 —— runner 跑的时候现从「我的视频」列表按位置取
（跳过前 K 个，往下顺延 N 个）。这里只负责：
  · 把准备页的 N / K / 6 条文案 读成结构
  · 拼每个单元的名字
  · 跑之前的离线体检（validate）

⚠ 只服务 mode: ad_regular。原生商广走 ad_data，两边互不影响。
"""
from __future__ import annotations

from datetime import datetime

TITLE_KEYS = [f"文案{i}" for i in range(1, 7)]


def _grouping(cfg: dict) -> dict:
    return cfg.get("grouping") or {}


def unit_name(cfg: dict, seq: int, today: str) -> str:
    """单元名。seq 从 1 起。{日期}=YYYYMMDD，{序号}=seq。"""
    tpl = _grouping(cfg).get("name_template", "常规商广_{日期}_{序号}")
    return tpl.format(**{"日期": today, "序号": seq, "标题": ""})


def titles_of(prep: dict) -> list[str]:
    """准备页那 6 个「文案N」里非空的，按顺序。"""
    return [str(prep.get(k, "")).strip() for k in TITLE_KEYS if str(prep.get(k, "")).strip()]


def _int(prep: dict, key: str, default: int = 0) -> int:
    try:
        return int(float(str(prep.get(key, "")).strip() or default))
    except (TypeError, ValueError):
        return default


def build(cfg: dict, prep: dict) -> dict:
    """读成 {"units": [...], "skip": K, "titles": [...]}。

    每个单元 {seq, name, video_index}。video_index 是「我的视频」列表里的
    绝对下标（0 起）：跳过前 K 个之后，第 seq 个单元用的是第 K+seq-1 个视频。
    """
    n = max(0, _int(prep, "视频数量", 0))
    k = max(0, _int(prep, "跳过前几个", 0))
    today = datetime.now().strftime("%Y%m%d")
    units = []
    for i in range(n):
        seq = i + 1
        units.append({
            "seq": seq,
            "name": unit_name(cfg, seq, today),
            "video_index": k + i,
        })
    return {"units": units, "skip": k, "titles": titles_of(prep)}


def validate(cfg: dict, data: dict, prep: dict) -> list[str]:
    """跑之前的体检。返回人话的问题清单。"""
    issues = []
    if not data["units"]:
        issues.append("「视频数量」是 0 或没填，一个单元都建不出来")
    if _int(prep, "视频数量", 0) > 50:
        issues.append(f"「视频数量」{_int(prep, '视频数量', 0)} 太多了，一次先别超过 50")
    titles = data["titles"]
    if not titles:
        issues.append("6 条文案一条都没填（至少要有「文案1」）")
    mx = int(_grouping(cfg).get("titles_max", 6))
    if len(titles) > mx:
        issues.append(f"文案填了 {len(titles)} 条，页面一个创意最多 {mx} 条")
    for i, t in enumerate(titles, 1):
        if not 2 <= len(t) <= 40:
            issues.append(f"「文案{i}」{len(t)} 字，素材标题要 2~40 字：{t}")
    desc = str(prep.get("素材描述", "")).strip()
    if desc and not 2 <= len(desc) <= 10:
        issues.append(f"「素材描述」{len(desc)} 字，页面要求 2~10 字：{desc}")
    return issues
