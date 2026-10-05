"""Versioned price-toggle contract, independent of browser and storage."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from .identity import stable_id

CAPABILITY = "pricing.toggle"
RULE_VERSION = 1


@dataclass(frozen=True)
class PriceItem:
    item_id: str
    strategy_id: str
    object_id: str
    name: str
    before: str
    after: str
    group: str = ""
    issue: str = ""

    @classmethod
    def from_row(cls, row: dict, direction: str) -> PriceItem:
        if direction not in ("on", "off"):
            raise ValueError("目标状态必须是 on 或 off")
        sid, oid = str(row.get("strategy_id") or ""), str(row.get("key") or "")
        before = {"开启": "off", "关闭": "on"}.get(row.get("link"), "unknown")
        issue = str(row.get("issue") or "")
        if not sid or not oid:
            issue = "缺少策略或对象 ID，不能按名称猜测执行对象"
        elif before == "unknown":
            issue = "平台当前状态无法识别"
        elif direction == "on" and row.get("group") == "不限" and before != direction:
            issue = "人群选组为不限，不可开启"
        return cls(stable_id("item", [sid, oid]), sid, oid,
                   str(row.get("name") or ""), before, direction,
                   str(row.get("group") or ""), issue)


@dataclass(frozen=True)
class PricePlan:
    plan_id: str
    created_at: str
    context: dict
    inputs: dict
    items: tuple[PriceItem, ...]
    source_task: str = ""
    capability: str = CAPABILITY
    rule_version: int = RULE_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> PricePlan:
        if value.get("capability") != CAPABILITY or value.get("rule_version") != RULE_VERSION:
            raise ValueError("计划契约版本不兼容，请重新预检")
        return cls(**{**value, "items": tuple(PriceItem(**x) for x in value["items"])})


def plan_fingerprint(plan: PricePlan) -> str:
    return stable_id("plan_content", plan.to_dict())


def check_selection(plan: PricePlan, selected: list[str]) -> None:
    if len(selected) != len(set(selected)) or not selected:
        raise ValueError("执行清单为空或包含重复条目")
    allowed = {x.item_id: x for x in plan.items}
    for item_id in selected:
        if item_id not in allowed or allowed[item_id].issue:
            raise ValueError("执行条目不属于此计划或尚未通过校验")


def describe(plan: PricePlan) -> str:
    changed = sum(not x.issue and x.before != x.after for x in plan.items)
    blocked = sum(bool(x.issue) for x in plan.items)
    return (f"环境：{plan.context.get('environment', '未知')}；"
            f"身份依据：{plan.context.get('identity_label', '未知')}；"
            f"计划变更 {changed} 项，阻断 {blocked} 项。"
            "执行前再次检查状态；取消不会撤销已完成的变更。")
