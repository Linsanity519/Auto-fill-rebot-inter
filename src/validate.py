"""离线校验：不碰浏览器，先把数据里能查出来的错全查出来。

跑到第 15 条才发现卡种写错，前面 14 条已经提交了——这种事必须在开跑前拦住。
"""
from .filler import split_multi


def _field_issues(f, value: str, prefix: str = "") -> list[str]:
    name = prefix + f["name"]
    issues = []

    if not value:
        if f.get("required"):
            issues.append(f"{name}：必填但为空")
        return issues

    opts = f.get("options")
    if opts and f.get("match") != "contains":
        vals = split_multi(value) if f.get("type") == "checkbox_sync" else [value]
        bad = [v for v in vals if v not in opts]
        if bad:
            hint = "、".join(opts[:6]) + ("…" if len(opts) > 6 else "")
            issues.append(f"{name}：「{'、'.join(bad)}」不是有效值（可选：{hint}）")
    return issues


def validate_record(form_cfg: dict, record: dict) -> list[str]:
    """返回这条记录的所有问题；空列表 = 通过。"""
    issues = []
    header = record["header"]

    for f in form_cfg["fields"]:
        value = str(header.get(f["name"], "")).strip()
        issues += _field_issues(f, value)
        for sub in (f.get("reveals") or {}).get(value, []):
            issues += _field_issues(sub, str(header.get(sub["name"], "")).strip())

    list_cfg = form_cfg.get("list")
    if not list_cfg:
        return issues

    items = record.get("items") or []
    if not items:
        issues.append("没有任何明细项")
        return issues

    if "variants" in list_cfg:
        key = list_cfg["variants_by"]
        val = str(header.get(key, "")).strip()
        if val not in list_cfg["variants"]:
            issues.append(f"{key}：「{val}」没有对应的明细字段配置")
            return issues
        fields = list_cfg["variants"][val]
    else:
        fields = list_cfg["fields"]

    limit = list_cfg.get("max_rows", 20)
    if len(items) > limit:
        issues.append(f"明细 {len(items)} 项，超过上限 {limit}")

    for i, item in enumerate(items, 1):
        for f in fields:
            issues += _field_issues(f, str(item.get(f["name"], "")).strip(), prefix=f"第{i}项-")

    return issues


def validate_all(form_cfg: dict, records: list[dict]) -> list[list[str]]:
    return [validate_record(form_cfg, r) for r in records]


def summarize(record: dict, form_cfg: dict) -> dict:
    """给界面表格用的一行摘要。"""
    h = record["header"]
    first = form_cfg["fields"][0]["name"]
    vkey = (form_cfg.get("list") or {}).get("variants_by")
    return {
        "名称": h.get(first, "") or "(未命名)",
        "类型": h.get(vkey, "") if vkey else "",
        "明细": len(record.get("items") or []),
    }
