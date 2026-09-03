"""「准备阶段参数」：界面上填一次，本批所有单元共用。

字段清单不写死在代码里，读 yaml，两种写法：

  prep_fields     直接在这儿声明（原生商广的计划名称/出价…）
  prep_from_unit  只写名字，定义在 unit_common 里（价格面板配置的收银台类型/面板个数…）
                  —— 那些字段填页面时也要用，定义只该有一份。

⚠ 别拿它当「策略中心的简化版」用。这张卡**不滚动**（.content 是 overflow:hidden），
  塞十来项还行，上百项就会有一截点不到；也没有方案库 / 关键词匹配 / 多套切换。
  「配一次、全批套用」而且字段多、还要按单元分方案的，用策略中心（wizard_strategy）。
  判断标准：跟着「这次投放」走的放这儿，跟着「策略」走的放策略中心。

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


def _from_unit(cfg: dict) -> list[dict]:
    """prep_from_unit：字段定义在 unit_common 里，这里只按名字挑出来。

    ⚠ 不在这儿抄一份定义。同一个字段的 label / 选项 / 必填 / 级联只该有一处，
      否则页面改了选项，两边总有一边忘了跟。
    ⚠ 挑中的字段 reveals 出来的子字段自动跟过来（选中类型 → 面板1/2选中套餐，
      赠单片 → 可赠单片价格），不然它们会没人管。
    """
    names = [str(n) for n in (cfg.get("prep_from_unit") or [])]
    if not names:
        return []
    from . import wizard_schema as W

    pos = W.position_names(cfg)[0]
    flat = W.flatten(W.unit_fields(cfg, pos))
    keep, out = set(names), []
    for f in flat:                      # flatten 保序：父字段一定排在它的子字段前面
        when = f.get("_when")
        if f["name"] in keep or (when and when[0] in keep):
            keep.add(f["name"])         # 子字段也算进来，孙字段才跟得上
            out.append(f)
    return out


def field_defs(cfg: dict) -> list[dict]:
    """「投放配置/准备」页上要显示的字段，保持书写顺序。"""
    return _from_unit(cfg) + [dict(f) for f in (cfg.get("prep_fields") or [])]


def has_prep(cfg: dict) -> bool:
    """这个配置类型要不要在「投放配置」页显示那张共用参数表。"""
    return bool(cfg.get("prep_fields") or cfg.get("prep_from_unit"))


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


def resolve_options(f: dict, values: dict) -> list | None:
    """这一项当前的可选项。

    普通字段就是 yaml 里的 options；联动字段（写了 options_map）按 options_by
    指定的那些字段的当前值拼出 key，去 options_map 里查。查不到返回 []
    （上游还没选 / 选了个没有下游的值），界面上就是个空下拉。

    ⚠ 必须和 app.js 的 prepOptions() 一套算法，否则「界面能选、保存报不在选项里」。
    """
    omap = f.get("options_map")
    if not omap:
        return f.get("options")
    by = f.get("options_by") or []
    sep = f.get("options_join", " | ")
    key = sep.join(str(values.get(n, "")).strip() for n in by)
    return list(omap.get(key, []))


def shown(f: dict, values: dict) -> bool:
    """这一项当前该不该出现（when 没满足的字段界面上是隐藏的）。

    when: [依赖字段, 值] 或 [依赖字段, [值1, 值2]]，和界面上 prepShown 同一套规则。

    ⚠ 校验必须跟界面看到的一致：隐藏字段还按必填拦，会出现「界面上没这一项、
      却一直提示没填」，人完全没法处理。
    """
    when = f.get("when") or f.get("_when")
    if not when:
        return True
    cur = str(values.get(when[0], "")).strip()
    want = when[1]
    if not isinstance(want, list):
        want = [want]
    # 和 wizard_schema.when_active 同一套：全等 / 多选成员 / 前缀
    members = [x.strip() for x in cur.replace("，", ",").split(",") if x.strip()]
    return any(cur == str(v) or str(v) in members or (cur and cur.startswith(str(v)))
               for v in want)


def validate(cfg: dict, values: dict) -> list[str]:
    """必填项有没有填、下拉值在不在选项里。返回人话的问题清单。"""
    issues = []
    for f in field_defs(cfg):
        if not shown(f, values):
            continue
        name = f["name"]
        val = str(values.get(name, "")).strip()
        if not val:
            if f.get("required"):
                issues.append(f"[准备阶段] 「{name}」没填")
            continue
        opts = resolve_options(f, values)
        if f.get("options_map") and not opts:
            issues.append(f"[准备阶段] 「{name}」的上游还没选好，这一项没有可选项")
            continue
        if opts:
            # 多选字段存的是「A,B,C」，得逐个比；按整串比会把合法值判成非法
            t = str(f.get("type", ""))
            multi = "checkbox" in t or "multiselect" in t
            vals = ([x.strip() for x in val.replace("，", ",").split(",") if x.strip()]
                    if multi else [val])
            bad = [v for v in vals if v not in opts]
            if bad:
                issues.append(f"[准备阶段] 「{name}」这些不在可选项里：{'、'.join(bad)}。"
                              f"可选：{'/'.join(opts)}")
        # 原生商广写 number，价格面板那边是 pp_number（类型名同时给填写逻辑用）
        if "number" in str(f.get("type", "")):
            try:
                float(val)
            except ValueError:
                issues.append(f"[准备阶段] 「{name}」要填数字，现在是「{val}」")
    return issues


def summary(cfg: dict, values: dict) -> list[tuple[str, str]]:
    """给模板说明页/界面用的 [(字段名, 值)]，空值显示成「未填」。"""
    return [(f["name"], str(values.get(f["name"], "")).strip() or "未填")
            for f in field_defs(cfg)]
