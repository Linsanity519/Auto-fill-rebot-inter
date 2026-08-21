"""原生商广的「准备阶段参数」：界面上填一次，本批所有单元共用。

⚠ 只服务 mode: ad_native。资源位投放那套「策略中心」（wizard_strategy）
  结构复杂得多（方案库/关键词匹配/资源位例外），这里用不上，所以另起一份
  极简的：一个配置类型一个 json，就是「字段名 → 值」的平铺字典。

字段清单不写死在代码里，读 yaml 的 prep_fields —— 以后要加一项
（比如再来个「投放结束日期」），只改 yaml，界面和校验会自动跟上。

存盘 config/prep/<配置类型>.json：
    {"values": {"计划名称": "...", "出价": "200"}, "updated_at": "..."}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from .paths import user_path

log = logging.getLogger(__name__)

BAD_CHARS = r':\/?*[]<>|' + '"'


def _safe_stem(name: str) -> str:
    return "".join(ch for ch in str(name) if ch not in BAD_CHARS).strip() or "准备参数"


def path_for(cfg: dict):
    return user_path("config", "prep", f"{_safe_stem(cfg.get('name', '准备参数'))}.json")


def field_defs(cfg: dict) -> list[dict]:
    """yaml 里声明的准备阶段字段，保持书写顺序。"""
    return [dict(f) for f in (cfg.get("prep_fields") or [])]


def defaults(cfg: dict) -> dict:
    return {f["name"]: str(f.get("default", "")) for f in field_defs(cfg)}


def load(cfg: dict) -> dict:
    """读存盘的值；没存过就用 yaml 里的 default。

    ⚠ 用 defaults 打底再覆盖，而不是直接返回存盘内容 —— 这样以后往 yaml 里
      加字段时，老的存盘文件也能自动补上新字段的默认值，不用用户重填一遍。
    """
    values = defaults(cfg)
    p = path_for(cfg)
    if p.exists():
        try:
            doc = json.loads(p.read_text(encoding="utf-8")) or {}
            for k, v in (doc.get("values") or {}).items():
                values[k] = "" if v is None else str(v)
        except (OSError, ValueError):
            log.warning("准备参数读取失败，用默认值：%s", p, exc_info=True)
    return values


def save(cfg: dict, values: dict) -> str:
    p = path_for(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    keep = {f["name"] for f in field_defs(cfg)}
    doc = {
        "values": {k: ("" if v is None else str(v)) for k, v in (values or {}).items()
                   if k in keep},
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


def validate(cfg: dict, values: dict) -> list[str]:
    """必填项有没有填、下拉值在不在选项里。返回人话的问题清单。"""
    issues = []
    for f in field_defs(cfg):
        name = f["name"]
        val = str(values.get(name, "")).strip()
        if not val:
            if f.get("required"):
                issues.append(f"[准备阶段] 「{name}」没填")
            continue
        opts = f.get("options")
        if opts and val not in opts:
            issues.append(f"[准备阶段] 「{name}」只能填：{'/'.join(opts)}，现在是「{val}」")
        if f.get("type") == "number":
            try:
                float(val)
            except ValueError:
                issues.append(f"[准备阶段] 「{name}」要填数字，现在是「{val}」")
    return issues


def summary(cfg: dict, values: dict) -> list[tuple[str, str]]:
    """给模板说明页/界面用的 [(字段名, 值)]，空值显示成「未填」。"""
    return [(f["name"], str(values.get(f["name"], "")).strip() or "未填")
            for f in field_defs(cfg)]
