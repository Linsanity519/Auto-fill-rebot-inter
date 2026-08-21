"""读 Excel / CSV，并按「分组」列把多行合并成「一条记录 + 多个明细项」。

长表格式（推荐）——一条配置有几项就写几行，用「分组」列归并：

  分组 | 价格人群名称 | 人群选组 | 平台          | 优先级 | 限制类型 | 会员卡种 | 价格均值 | 价格区间下限 | 价格区间上限
  1    | 新客低价     | 不限     | iPhone,Android| 10     | 常规均价 | 连续包年 | 88       | 80           | 100
  1    |              |          |               |        |          | 连续包月 | 15       | 12           | 20
  2    | 老客召回     | 不限     | pc            | 20     | 常规均价 | 年度大会员| 148     | 140          | 160

同一分组里，主表字段只需在第一行填，后续行留空即可。
没有「分组」列时，退化成一行一条记录、每条一个明细项。
"""
from pathlib import Path
import pandas as pd


def load_table(path: str, sheet_name=None) -> list[dict]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"数据文件不存在：{p.resolve()}")

    if p.suffix.lower() in (".xlsx", ".xlsm"):
        df = pd.read_excel(p, sheet_name=sheet_name if sheet_name else 0, dtype=str)
    elif p.suffix.lower() == ".csv":
        df = pd.read_csv(p, dtype=str, encoding="utf-8-sig")
    else:
        raise ValueError(f"不支持的文件类型：{p.suffix}")

    df = df.fillna("")
    df.columns = [str(c).strip() for c in df.columns]
    return df.to_dict(orient="records")


def item_field_names(list_cfg: dict) -> list[str]:
    """明细字段名；有 variants 时取所有变体的并集（保持出现顺序）。"""
    if not list_cfg:
        return []
    groups = list(list_cfg["variants"].values()) if "variants" in list_cfg else [list_cfg["fields"]]
    names = []
    for g in groups:
        for f in g:
            if f["name"] not in names:
                names.append(f["name"])
    return names


def header_field_names(form_cfg: dict) -> list[str]:
    """主表字段名，含 reveals 里的条件字段。"""
    names = []
    for f in form_cfg["fields"]:
        names.append(f["name"])
        for subs in (f.get("reveals") or {}).values():
            for s in subs:
                if s["name"] not in names:
                    names.append(s["name"])
    return names


def build_records(rows: list[dict], form_cfg: dict, group_key: str = "分组") -> list[dict]:
    """把扁平的表格行组装成 [{header: {...}, items: [{...}], source_rows: [i,...]}]"""
    list_cfg = form_cfg.get("list")
    header_names = header_field_names(form_cfg)
    item_names = item_field_names(list_cfg)

    # 没配重复行，或表里没有分组列 → 一行一条
    if not list_cfg or not rows or group_key not in rows[0]:
        return [
            {"header": r, "items": [r] if item_names else [], "source_rows": [i]}
            for i, r in enumerate(rows)
        ]

    records, index = [], {}
    for i, r in enumerate(rows):
        gid = str(r.get(group_key, "")).strip() or f"__row{i}"
        if gid not in index:
            index[gid] = {
                "header": {k: r.get(k, "") for k in header_names},
                "items": [],
                "source_rows": [],
            }
            records.append(index[gid])
        rec = index[gid]
        # 后续行里主表字段若非空则覆盖（允许中途修正）
        for k in header_names:
            if str(r.get(k, "")).strip():
                rec["header"][k] = r[k]
        if any(str(r.get(k, "")).strip() for k in item_names):
            rec["items"].append({k: r.get(k, "") for k in item_names})
        rec["source_rows"].append(i)

    return records


def check_columns(rows: list[dict], form_cfg: dict) -> list[str]:
    """返回缺失的必填列名。

    只校验无条件必填的主表字段。条件字段（reveals）和明细字段随变体变化，
    在实际填写时按行校验——这里一刀切会误报。
    """
    if not rows:
        return []
    cols = set(rows[0].keys())
    need = [f["name"] for f in form_cfg["fields"] if f.get("required")]
    return [n for n in need if n not in cols]
