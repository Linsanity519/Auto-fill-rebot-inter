"""wizard 模式的配置解析：把 profile 展开成「每个资源位实际要填哪些字段」。

⚠ 只服务 mode: wizard 的 profile。老的单弹窗配置（价格配置）不会走到这里。

profile 里字段是「公共 + 增量」写法，这里负责合并成完整清单：
  单元层 = unit_common（或 unit_push） + 该资源位的 unit_extra
  创意层 = 该资源位的 creative
再把 reveals 里的条件字段一并摊平，供生成模板和校验使用。
"""
from __future__ import annotations

STEP_ACTIVITY = "activity"
STEP_UNIT = "unit"
STEP_CREATIVE = "creative"


def is_wizard(cfg: dict) -> bool:
    return cfg.get("mode") == "wizard"


def position_names(cfg: dict) -> list[str]:
    """可选资源位清单，保持 yaml 里的书写顺序。"""
    return list((cfg.get("positions") or {}).keys())


def position_meta(cfg: dict, name: str) -> dict:
    pos = (cfg.get("positions") or {}).get(name)
    if pos is None:
        raise KeyError(f"配置里没有资源位「{name}」。可选：{position_names(cfg)}")
    return pos


def real_position_name(cfg: dict, name: str) -> str:
    """页面上资源位表格里的真实名称（部分和我们给的简称不一样）。"""
    return position_meta(cfg, name).get("real_name") or name


def step_by_key(cfg: dict, key: str) -> dict:
    for s in cfg["steps"]:
        if s["key"] == key:
            return s
    raise KeyError(f"没有步骤 {key}")


# ---------------------------------------------------------------- 字段展开
def flatten(fields: list[dict]) -> list[dict]:
    """把 reveals 里的条件字段摊平成一维清单（保序、去重）。

    展开后每个字段带上 _when = (触发字段名, 触发值)，
    模板和校验靠它告诉用户「这列只在某某=某值时才要填」。
    """
    out: list[dict] = []
    seen: set[str] = set()

    def walk(fs, when):
        for f in fs:
            f = dict(f)
            if when:
                f.setdefault("_when", when)
            if f["name"] not in seen:
                seen.add(f["name"])
                out.append(f)
            for val, subs in (f.get("reveals") or {}).items():
                walk(subs, (f["name"], val))

    walk(fields, None)
    return out


def unit_fields(cfg: dict, position: str) -> list[dict]:
    """该资源位单元层的完整字段（未展开 reveals）。"""
    meta = position_meta(cfg, position)
    base_key = meta.get("unit_template", "unit_common")
    base = cfg.get(base_key) or []
    return list(base) + list(meta.get("unit_extra") or [])


def creative_fields(cfg: dict, position: str) -> list[dict]:
    return list(position_meta(cfg, position).get("creative") or [])


# ---------------------------------------------------------------- 策略中心
def strategy_groups(cfg: dict) -> dict[str, list[str]]:
    """策略中心的分组：{组名: [字段名]}，顺序就是界面上的顺序。"""
    return {k: list(v or []) for k, v in (cfg.get("strategy_groups") or {}).items()}


def rule_names(cfg: dict) -> list[str]:
    """通用规则字段（不含人群），按分组顺序拍平。"""
    out: list[str] = []
    for names in strategy_groups(cfg).values():
        out += names
    return out


def group_of(cfg: dict, name: str) -> str:
    """这个策略字段归哪一组。方案组（人群/内容限制）的字段归到方案组自己那一组。"""
    for g, names in strategy_groups(cfg).items():
        if name in names:
            return g
    for g, spec in scheme_groups(cfg).items():
        if name in (spec.get("fields") or []):
            return g
    return "其他"


def strategy_names(cfg: dict) -> list[str]:
    """策略中心能供值的全部单元层字段 = 通用规则 + 各方案组的字段。

    这些字段一律不进 Excel 模板，执行时由 wizard_strategy.resolve() 统一补。
    """
    return rule_names(cfg) + scheme_field_names(cfg)


# ---------------------------------------------------------------- 方案组
# 「方案组」= 一组字段打包成若干套命名方案，整套选用：
#   人群     —— 新客 / 过期 / 即期 / 在期……
#   内容限制 —— 版本限制 + 生效内容 + 内容类型 + ep付费状态……
# 两组的用法完全一样：要么全部单元用同一套，要么按单元名称匹配。
# ⚠ 加一组只要往 yaml 的 scheme_groups 里加一段，Python 和界面都不用动。
def scheme_groups(cfg: dict) -> dict[str, dict]:
    """{组名: {key, exception_field, fields, presets}}，保持 yaml 里的顺序。

    key 是存盘用的英文键（audience / content），改了会丢已存的策略，别动。
    """
    out: dict[str, dict] = {}
    for name, spec in (cfg.get("scheme_groups") or {}).items():
        spec = spec or {}
        out[str(name)] = {
            "key": str(spec.get("key") or name),
            "exception_field": str(spec.get("exception_field") or f"{name}方案"),
            "fields": [str(f) for f in (spec.get("fields") or [])],
            "presets": list(spec.get("presets") or []),
        }
    return out


def group_keys(cfg: dict) -> list[str]:
    return [spec["key"] for spec in scheme_groups(cfg).values()]


def group_spec(cfg: dict, key: str) -> tuple[str, dict]:
    """按存盘键找一组，返回 (组名, 定义)。找不到抛 KeyError。"""
    for name, spec in scheme_groups(cfg).items():
        if spec["key"] == key:
            return name, spec
    raise KeyError(f"配置里没有方案组 {key}")


def group_field_names(cfg: dict, key: str) -> list[str]:
    for _, spec in scheme_groups(cfg).items():
        if spec["key"] == key:
            return list(spec["fields"])
    return []


def scheme_field_names(cfg: dict) -> list[str]:
    """所有方案组的字段拍平（保序、去重）。"""
    out: list[str] = []
    for spec in scheme_groups(cfg).values():
        for n in spec["fields"]:
            if n not in out:
                out.append(n)
    return out


def group_key_of_field(cfg: dict, name: str) -> str:
    for spec in scheme_groups(cfg).values():
        if name in spec["fields"]:
            return spec["key"]
    return ""


def unit_field_names(cfg: dict, position: str) -> set[str]:
    """该资源位单元层实际有哪些字段（含 reveals 展开出来的条件字段）。"""
    return {f["name"] for f in flatten(unit_fields(cfg, position))}


def strategy_fields_for(cfg: dict, position: str) -> list[dict]:
    """策略中心里对这个资源位生效的那部分字段（保持 strategy_fields 的顺序）。"""
    names = strategy_names(cfg)
    have = unit_field_names(cfg, position)
    by_name = {f["name"]: f for f in flatten(unit_fields(cfg, position))}
    return [by_name[n] for n in names if n in have and n in by_name]


def strategy_field_defs(cfg: dict) -> list[dict]:
    """策略中心界面用：每个策略字段的定义 + 它对哪些资源位生效。

    同一个字段名可能在多个资源位里重复声明（unit_extra 里的 &f_xxx 片段），
    定义取第一次遇到的那份，_positions 记全部命中的资源位。
    """
    order = strategy_names(cfg)
    found: dict[str, dict] = {}
    for pos in position_names(cfg):
        for f in strategy_fields_for(cfg, pos):
            d = found.setdefault(f["name"], dict(f, _positions=[]))
            d["_positions"].append(pos)
    return [found[n] for n in order if n in found]


def unit_columns_for_template(cfg: dict, position: str) -> list[dict]:
    """模板里单元层要出的列 = 单元层字段 - 策略中心接管的那些。

    ⚠ 人群和内容限制也在「策略中心接管」里，恒定不出列 —— 2026-08-21 起
      取消了「Excel 里逐单元填」这个选项，两组都只能「同一套 / 按单元名称匹配」。
      老模板里要是还留着这些列，读数据时以表为准（见 wizard_data._units_of），
      不会打架。
    """
    skip = set(strategy_names(cfg))
    return [f for f in columns_for(cfg, position, STEP_UNIT) if f["name"] not in skip]


def activity_fields(cfg: dict) -> list[dict]:
    return list(step_by_key(cfg, STEP_ACTIVITY)["fields"])


def columns_for(cfg: dict, position: str, layer: str) -> list[dict]:
    """生成模板用：某资源位某层要出现的列（已展开条件字段）。"""
    if layer == STEP_ACTIVITY:
        return flatten(activity_fields(cfg))
    if layer == STEP_UNIT:
        return flatten(unit_fields(cfg, position))
    if layer == STEP_CREATIVE:
        return flatten(creative_fields(cfg, position))
    raise ValueError(layer)


# ---------------------------------------------------------------- 固定值
# yaml 里给某个字段写了 default，就是「这一列基本不会变，别让人每次都填」。
# 模板里这一列照常出，但留空 = 用这个默认值；要换别的填进去就是了。
def defaults_for(cfg: dict, position: str, layer: str) -> dict[str, str]:
    """这一层里带默认值的字段 {字段名: 默认值}。"""
    return {f["name"]: str(f["default"])
            for f in columns_for(cfg, position, layer)
            if str(f.get("default", "")).strip()}


def apply_defaults(row: dict, defaults: dict) -> list[str]:
    """把默认值补进这一行的空位，返回补了哪几个字段（给日志用）。"""
    filled = []
    for name, val in (defaults or {}).items():
        if not str(row.get(name, "") or "").strip():
            row[name] = val
            filled.append(name)
    return filled


def creative_system(cfg: dict, position: str) -> str:
    return position_meta(cfg, position).get("system", "v1")


DATE_TYPES = ("date_by_label", "date_range_start", "date_range_end")

MULTI_TYPES = ("checkbox_sync_formily", "multiselect_vue", "multiselect_antd")


def multi_value_names(cfg: dict) -> set:
    """值是「逗号分隔的多个」的字段名。合并多套人群方案时这些取并集。"""
    out = set()
    for pos in position_names(cfg):
        for f in flatten(unit_fields(cfg, pos)):
            if f.get("type") in MULTI_TYPES:
                out.add(f["name"])
    return out


def parse_range(value: str):
    """「小-大」解析成 (小, 大)，解不开返回 None。

    ⚠ 上界允许填 -1 表示不限（后台自己的约定，页面上别的数字框默认值就是 -1），
      所以不能简单按「-」切 —— "8--1" 得切成 8 和 -1，切成 8 和 1 就填错了。
    """
    import re as _re
    m = _re.match(r"^\s*(-?\d+)\s*(?:-|~|到|至|,|，)\s*(-?\d+)\s*$", str(value or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


UNLIMITED = -1        # 天数区间上界填 -1 = 不限


def describe(f: dict) -> str:
    """一列的填写说明，给模板的说明页用。"""
    if f.get("_note"):
        return f["_note"]
    bits = []
    if str(f.get("default", "")).strip():
        bits.append(f"不用填，留空就用固定值：{f['default']}")
    opts = f.get("options")
    if opts:
        bits.append("可选：" + "、".join(opts))
    if f.get("match") == "contains":
        bits.append("填 ID 即可，页面自动匹配")
    if f.get("max"):
        bits.append(f"最多 {f['max']} 字")
    if f.get("size"):
        bits.append(f"尺寸 {f['size']}")
    if f.get("type", "") in DATE_TYPES:
        bits.append("时间格式：2026-09-24 10:00:00（年-月-日 时:分:秒，年份不能省）")
    if f.get("type", "").startswith("upload"):
        bits.append("填图片路径，或直接把图片贴进这个单元格")
    if f.get("max_pick"):
        bits.append(f"多选，最多 {f['max_pick']} 个，用逗号分隔")
    elif f.get("type") in ("checkbox_sync_formily", "multiselect_vue", "multiselect_antd"):
        bits.append("多选，用英文逗号分隔")
    when = f.get("_when")
    if when:
        bits.append(f"仅当「{when[0]}」=「{when[1]}」时需要填")
    return "；".join(bits) or "自由填写"
