"""策略中心：把「每次都一样」的投放规则从 Excel 里搬出来，配一次、全局套用。

⚠ 只服务 mode: wizard 的资源位投放。别的配置类型完全走不到这里。

一套策略由三块组成：

  rules       通用规则。生效平台/流量池/频次/赛马…… 按 yaml 的 strategy_groups
              分组，界面上一组一张卡。一个字段一个值，全批通用。

  groups      方案组。一组字段打包成若干套命名方案，整套选用：
                人群      新客 / 过期 / 即期 / 在期……
                内容限制  版本限制 + 生效内容 + 内容类型 + ep付费状态……
              每组两种用法（2026-08-21 起就这两种）：
                fixed    全部单元用同一套方案
                keyword  按单元名称匹配 —— 一张有序的「关键词 → 方案」表，
                         从上往下第一条命中的生效，都没命中就用 fallback
              ⚠ 原来还有第三种 excel（模板里出列、逐个单元填），已经去掉：
                这些字段一个单元一个单元填纯属重复劳动，而且模板列多到看不过来。
                老策略文件里存的 excel 会在读的时候被当成 fixed。
              方案库 schemes 是用户自己的（首次从 yaml 的 presets 灌一次，
              之后改 yaml 不会覆盖已存的策略），可增删改名。

  exceptions  例外清单。[{positions: [...], field: 字段名, value: 值}]
              field 用某一组的 exception_field（「人群方案」「内容限制方案」）时，
              值是方案名，表示这几个资源位整组换一套 —— push 类只吃 DMP 人群包
              就靠这条。

优先级：例外 > 方案组 > 通用规则。取值入口只有 resolve() 一个。

存盘 config/strategies/<配置类型>.json：
    {"active": "默认策略",
     "items": {"默认策略": {rules, groups: {audience: {...}, content: {...}},
                            exceptions, updated_at}}}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from . import wizard_schema as W
from .paths import user_path

log = logging.getLogger(__name__)

DEFAULT_NAME = "默认策略"
MODE_FIXED, MODE_KEYWORD = "fixed", "keyword"
MODES = (MODE_FIXED, MODE_KEYWORD)
BAD_CHARS = r':\/?*[]<>|' + '"'

# 迁移用：老结构里人群那一组存在顶层 "audience" 键上
LEGACY_GROUP_KEY = "audience"


def _safe_stem(name: str) -> str:
    return "".join(ch for ch in str(name) if ch not in BAD_CHARS).strip() or "策略"


def path_for(cfg: dict):
    """策略文件路径。按配置类型的 name 存，一个配置类型一个文件。"""
    return user_path("config", "strategies", f"{_safe_stem(cfg.get('name', '策略'))}.json")


def exception_fields(cfg: dict) -> dict[str, str]:
    """例外清单里「整组换一套」用的字段名 → 组的存盘键。"""
    return {spec["exception_field"]: spec["key"] for spec in W.scheme_groups(cfg).values()}


# ---------------------------------------------------------------- 结构
def _seed_group(spec: dict) -> dict:
    """首次打开时的一组：把 yaml 的 presets 灌成用户自己的方案库。"""
    schemes, rules = {}, []
    for ps in spec.get("presets") or []:
        name = str(ps.get("name") or "").strip()
        if not name:
            continue
        schemes[name] = {k: str(v) for k, v in (ps.get("values") or {}).items() if str(v).strip()}
        words = [str(w) for w in (ps.get("keywords") or []) if str(w).strip()]
        if words:
            rules.append({"keywords": words, "schemes": [name]})
    return {
        "mode": MODE_FIXED,
        "scheme": next(iter(schemes), ""),
        "rules": rules,
        "fallback": [],
        "schemes": schemes,
    }


def _blank_item(cfg: dict) -> dict:
    return {"rules": {},
            "groups": {spec["key"]: _seed_group(spec)
                       for spec in W.scheme_groups(cfg).values()},
            "exceptions": [], "updated_at": ""}


def _clean_values(d) -> dict:
    return {str(k): str(v) for k, v in (d or {}).items() if str(v).strip()}


def _norm_group(spec: dict, grp) -> dict:
    """一组的结构归一。任何来源（文件、前端、迁移）都先过这里。"""
    if not isinstance(grp, dict):
        return _seed_group(spec)
    schemes = {str(n): _clean_values(v) for n, v in (grp.get("schemes") or {}).items()}
    if not schemes:
        schemes = _seed_group(spec)["schemes"]

    rules = []
    for r in grp.get("rules") or []:
        words = [str(w).strip() for w in (r.get("keywords") or []) if str(w).strip()]
        # 老结构一条规则只有一个 scheme，新结构是 schemes 列表，两种都认
        picked = r.get("schemes")
        if picked is None:
            picked = [r.get("scheme")] if r.get("scheme") else []
        picked = [str(s) for s in picked if str(s) in schemes]
        if words and picked:
            rules.append({"keywords": words, "schemes": picked})

    # ⚠ 老策略里可能存着已经取消的 excel，一律当 fixed —— 那些字段现在不出列了，
    #   还按 excel 走会变成「谁都没给值」，跑起来才发现必填为空
    mode = str(grp.get("mode") or MODE_FIXED)
    if mode not in MODES:
        mode = MODE_FIXED
    scheme = str(grp.get("scheme") or "")
    if scheme not in schemes:
        scheme = next(iter(schemes), "")
    # 兜底也可以是多套（和规则同构）；老结构存的是单个字符串
    fb = grp.get("fallback")
    fb = fb if isinstance(fb, list) else ([fb] if fb else [])
    fallback = [str(s) for s in fb if str(s) in schemes]
    return {"mode": mode, "scheme": scheme, "rules": rules,
            "fallback": fallback, "schemes": schemes}


def _norm_exceptions(exc) -> list[dict]:
    out = []
    for e in exc or []:
        if not isinstance(e, dict):
            continue
        positions = [str(p) for p in (e.get("positions") or []) if str(p).strip()]
        field = str(e.get("field") or "").strip()
        value = str(e.get("value") or "").strip()
        if positions and field and value:
            out.append({"positions": positions, "field": field, "value": value})
    return out


def _migrate(cfg: dict, item: dict) -> dict:
    """认一下旧结构，免得已经配过的白配。

    ① 最早那版：{"global": {字段: 值}, "positions": {...}, "auto_preset": bool}
    ② 上一版：  顶层 "audience" 是人群那一组，内容限制那几个字段混在 rules 里
    """
    item = dict(item)

    # ① global / positions
    if "global" in item or "positions" in item:
        g = _clean_values(item.get("global"))
        aud_names = set(W.group_field_names(cfg, LEGACY_GROUP_KEY))
        new = _blank_item(cfg)
        new["rules"] = {k: v for k, v in g.items() if k not in aud_names}
        old_aud = {k: v for k, v in g.items() if k in aud_names}
        if old_aud and LEGACY_GROUP_KEY in new["groups"]:
            new["groups"][LEGACY_GROUP_KEY]["schemes"]["原有人群"] = old_aud
            new["groups"][LEGACY_GROUP_KEY]["scheme"] = "原有人群"
            new["groups"][LEGACY_GROUP_KEY]["mode"] = (
                MODE_KEYWORD if item.get("auto_preset") else MODE_FIXED)
        for pos, vals in (item.get("positions") or {}).items():
            for field, value in _clean_values(vals).items():
                new["exceptions"].append({"positions": [str(pos)], "field": field, "value": value})
        new["updated_at"] = str(item.get("updated_at") or "")
        log.info("策略「%s」已从最早的结构迁移", item.get("_name", ""))
        item = new

    groups = dict(item.get("groups") or {})

    # ② 顶层 audience → groups.audience
    if LEGACY_GROUP_KEY not in groups and isinstance(item.get(LEGACY_GROUP_KEY), dict):
        groups[LEGACY_GROUP_KEY] = item[LEGACY_GROUP_KEY]
        log.info("策略「%s」的人群已并进方案组", item.get("_name", ""))

    # ③ 内容限制这类新拆出来的组：值原来躺在 rules 里，捞出来存成一套方案。
    #    ⚠ 不做这一步的话，已经配好的内容限制会在这次升级后凭空消失。
    rules = _clean_values(item.get("rules"))
    for gname, spec in W.scheme_groups(cfg).items():
        key = spec["key"]
        mine = {n: rules[n] for n in spec["fields"] if n in rules}
        if not mine:
            continue
        for n in mine:
            rules.pop(n, None)
        if isinstance(groups.get(key), dict) and (groups[key].get("schemes") or {}):
            continue                    # 这一组已经有方案库了，rules 里那份是残留
        seeded = _seed_group(spec)
        name = f"原有{gname}"
        seeded["schemes"][name] = mine
        seeded["scheme"] = name
        seeded["mode"] = MODE_FIXED
        groups[key] = seeded
        log.info("策略「%s」的「%s」已从通用规则搬进方案组", item.get("_name", ""), gname)

    item["rules"] = rules
    item["groups"] = groups
    return item


def _normalize(cfg: dict, doc) -> dict:
    """任何来源（文件、前端）的策略文档都先过这里，保证结构完整。"""
    if not isinstance(doc, dict):
        doc = {}
    items = doc.get("items")
    if not isinstance(items, dict) or not items:
        items = {DEFAULT_NAME: _blank_item(cfg)}

    specs = W.scheme_groups(cfg)
    scheme_fields = set(W.scheme_field_names(cfg))

    clean: dict[str, dict] = {}
    for name, item in items.items():
        if not isinstance(item, dict):
            item = {}
        item = _migrate(cfg, dict(item, _name=name))
        got = item.get("groups") or {}
        clean[str(name)] = {
            # 方案组接管的字段永远不留在 rules 里，留着会和方案库打架
            "rules": {k: v for k, v in _clean_values(item.get("rules")).items()
                      if k not in scheme_fields},
            "groups": {spec["key"]: _norm_group(spec, got.get(spec["key"]))
                       for spec in specs.values()},
            "exceptions": _norm_exceptions(item.get("exceptions")),
            "updated_at": str(item.get("updated_at") or ""),
        }

    active = str(doc.get("active") or "")
    if active not in clean:
        active = next(iter(clean))
    return {"active": active, "items": clean}


# ---------------------------------------------------------------- 读写
def load(cfg: dict) -> dict:
    """读整份策略文档。文件不存在 / 读坏了都退回一份出厂默认，不抛异常。"""
    path = path_for(cfg)
    if not path.exists():
        return _normalize(cfg, None)
    try:
        return _normalize(cfg, json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        log.warning("策略文件读不了，按空策略处理：%s", path, exc_info=True)
        return _normalize(cfg, None)


def save(cfg: dict, doc: dict) -> str:
    doc = _normalize(cfg, doc)
    doc["items"][doc["active"]]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path = path_for(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def active_payload(cfg: dict) -> dict:
    """当前启用的那套策略。"""
    doc = load(cfg)
    return doc["items"][doc["active"]]


# ---------------------------------------------------------------- 取值
def group_of(payload: dict | None, key: str) -> dict:
    return ((payload or {}).get("groups") or {}).get(key) or {}


def scheme_values(payload: dict | None, key: str, name: str) -> dict:
    return dict((group_of(payload, key).get("schemes") or {}).get(name) or {})


def match_rules(payload: dict | None, key: str, unit_name: str) -> list[dict]:
    """这一组的「关键词 → 方案」表里，单元名称命中的行（保持表里的顺序）。"""
    name = str(unit_name or "")
    if not name:
        return []
    return [r for r in (group_of(payload, key).get("rules") or [])
            if any(w and w in name for w in (r.get("keywords") or []))]


def schemes_for(cfg: dict, payload: dict | None, key: str, position: str,
                unit_name: str | None = None) -> tuple[list[str], str]:
    """这个单元在这一组里该用哪几套方案，返回 (方案名列表, 怎么定的)。

    一条关键词规则可以指多套 —— 多值字段（人群标签、内容类型这些多选）取并集，
    不走页面上的「添加人群配置」。

    例外里给这个资源位整组指定了方案 → 用它（push 类只吃 DMP 人群包就靠这个）。
    否则按 mode：fixed 用固定那套；keyword 从上往下取第一条命中的，没命中用兜底。
    """
    grp = group_of(payload, key)
    gname, spec = W.group_spec(cfg, key)
    forced = exception_value(payload, position, spec["exception_field"])
    if forced and forced in (grp.get("schemes") or {}):
        return [forced], f"例外：{position} 的{gname}指定用「{forced}」"

    if grp.get("mode") == MODE_KEYWORD:
        hits = match_rules(payload, key, unit_name or "")
        if hits:
            picked = list(hits[0].get("schemes") or [])
            note = f"{gname}按单元名称命中「" + "＋".join(picked) + "」"
            if len(picked) > 1:
                note += "（几套合并成一组投）"
            if len(hits) > 1:
                other = "、".join("＋".join(h.get("schemes") or []) for h in hits[1:])
                note += f"（{other} 也匹配，按表里顺序取了上面这条）"
            return picked, note
        fb = list(grp.get("fallback") or [])
        return fb, (f"{gname}没命中任何关键词，用兜底「{'＋'.join(fb)}」" if fb else "")
    one = str(grp.get("scheme") or "")
    return ([one] if one else []), ""


def exceptions_for(cfg: dict, payload: dict | None, position: str) -> dict:
    """这个资源位的例外 {字段: 值}（不含「整组换一套方案」那几条）。"""
    swaps = set(exception_fields(cfg))
    out = {}
    for e in (payload or {}).get("exceptions") or []:
        if position in (e.get("positions") or []) and e.get("field") not in swaps:
            out[e["field"]] = e["value"]
    return out


def exception_value(payload: dict | None, position: str, field: str) -> str:
    for e in (payload or {}).get("exceptions") or []:
        if e.get("field") == field and position in (e.get("positions") or []):
            return str(e.get("value") or "")
    return ""


def resolve(cfg: dict, payload: dict | None, position: str,
            unit_name: str | None = None) -> dict:
    """这个单元最终该套用的策略值 {字段名: 值}。取值只走这一个口子。

    优先级：例外 > 方案组 > 通用规则。
    两边都没配的字段不出现在结果里 —— 由校验去报「策略中心没配」，这里不猜默认值。
    """
    payload = payload or {}
    have = W.unit_field_names(cfg, position)      # 这个资源位页面上真有的字段
    rules = payload.get("rules") or {}
    exc = exceptions_for(cfg, payload, position)

    from_groups: dict[str, str] = {}
    for key in W.group_keys(cfg):
        picked, _ = schemes_for(cfg, payload, key, position, unit_name)
        vals, _ = merge_schemes(cfg, payload, key, picked)
        from_groups.update(vals)

    out: dict[str, str] = {}
    for name in W.strategy_names(cfg):
        if name not in have:
            continue                              # 这个资源位没这个字段，别塞
        val = (str(exc.get(name, "") or "").strip()
               or str(from_groups.get(name, "") or "").strip()
               or str(rules.get(name, "") or "").strip())
        if val:
            out[name] = val
    return out


def merge_schemes(cfg: dict, payload: dict | None, key: str,
                  picked: list[str]) -> tuple[dict, list[str]]:
    """把这一组里选中的几套方案合成一套，返回 (值, 说不通的地方)。

    多选字段（人群标签、内容类型这些）取并集 —— 页面上本来就是多选，
    所以「新客 + 在期」= 把两边的标签都勾上。

    单值字段（人群类型、天数区间、生效内容…）几套之间必须一致，不一致就说不通：
    比如一套是「基本人群」另一套是「DMP 人群包」，同一块里只能选一个类型。
    这种直接报出来让人自己决定，不猜。
    """
    if not picked:
        return {}, []
    multi = W.multi_value_names(cfg)
    out: dict[str, str] = {}
    bad: list[str] = []

    for name in W.group_field_names(cfg, key):
        got = [(s, str(scheme_values(payload, key, s).get(name, "") or "").strip())
               for s in picked]
        got = [(s, v) for s, v in got if v]
        if not got:
            continue
        if name in multi:
            merged: list[str] = []
            for _, v in got:
                for one in v.replace("，", ",").split(","):
                    one = one.strip()
                    if one and one not in merged:
                        merged.append(one)
            out[name] = ",".join(merged)
        else:
            out[name] = got[0][1]
            uniq = {v for _, v in got}
            if len(uniq) > 1:
                who = "、".join(f"{s}={v}" for s, v in got)
                bad.append(f"「{name}」这几套配得不一样（{who}），"
                           f"同一块里只能有一个值，合不到一起")
    return out, bad


def merge_conflicts(cfg: dict, payload: dict | None, position: str,
                    unit_name: str | None = None) -> list[str]:
    """多套方案合不到一起的地方，给校验用。"""
    out: list[str] = []
    for key in W.group_keys(cfg):
        picked, _ = schemes_for(cfg, payload, key, position, unit_name)
        if len(picked) < 2:
            continue
        out += merge_schemes(cfg, payload, key, picked)[1]
    return out


def notes(cfg: dict, payload: dict | None, position: str, unit_name: str | None) -> str:
    """跑的时候写进日志的一句话：这个单元的人群/内容限制是怎么定的。"""
    bits = []
    for key in W.group_keys(cfg):
        _, note = schemes_for(cfg, payload, key, position, unit_name)
        if note:
            bits.append(note)
    return "；".join(bits)


def per_unit_names(cfg: dict, payload: dict | None) -> set[str]:
    """哪些字段是「逐个单元可能不一样」的 —— 处在关键词匹配模式的那几组。

    这些字段不能按资源位一次性校验，得跟着行走。
    """
    out: set[str] = set()
    for key in W.group_keys(cfg):
        if group_of(payload, key).get("mode") == MODE_KEYWORD:
            out |= set(W.group_field_names(cfg, key))
    return out


def unmatched_hint(cfg: dict, payload: dict | None, unit_name: str) -> list[str]:
    """名字没命中、组里又没兜底的那几组，各说一句人话。

    ⚠ 不这么区分的话，只会看到一句「策略中心没配人群选组」——
      而实际上策略配得好好的，是这个单元的名字一个关键词都不含。
    """
    out = []
    for gname, spec in W.scheme_groups(cfg).items():
        key = spec["key"]
        grp = group_of(payload, key)
        if grp.get("mode") != MODE_KEYWORD:
            continue
        if match_rules(payload, key, unit_name) or grp.get("fallback"):
            continue
        words = "、".join("/".join(r["keywords"]) for r in grp.get("rules") or [])
        out.append(f"单元名称「{unit_name}」没命中任何{gname}关键词（{words}），"
                   f"策略里也没设兜底方案 —— 改名带上关键词，"
                   f"或者在策略中心给这套策略设一个兜底方案")
    return out


# ---------------------------------------------------------------- 给界面
def field_defs_for_ui(cfg: dict) -> list[dict]:
    """策略中心要渲染的字段清单。

    kind：multi = 多选勾选框（逗号分隔存）、single = 单选下拉、text = 自由填。
    when：(触发字段, 触发值) —— 界面按它做级联，父字段没选中就不显示这一项。
    scheme_group：属于哪个方案组的存盘键（空 = 通用规则字段）。
    """
    out = []
    for f in W.strategy_field_defs(cfg):
        t = f.get("type", "")
        opts = list(f.get("options") or [])
        if t in ("checkbox_sync_formily", "multiselect_vue", "multiselect_antd"):
            kind = "multi"
        elif t == "number_range_by_label":
            kind = "range"          # 页面上是「n 天至 m 天」两个数字框，界面要对齐
        elif opts:
            kind = "single"
        else:
            kind = "text"
        when = f.get("_when")
        gkey = W.group_key_of_field(cfg, f["name"])
        out.append({
            "name": f["name"],
            "kind": kind,
            "options": opts,
            "required": bool(f.get("required")),
            "when": list(when) if when else None,
            "positions": list(f.get("_positions") or []),
            "scheme_group": gkey,
            "group": W.group_of(cfg, f["name"]),
            "note": W.describe(f),
        })
    return out


def group_defs_for_ui(cfg: dict) -> list[dict]:
    """方案组的静态信息，界面按它渲染卡片（一组一张）。"""
    return [{"key": spec["key"], "name": name,
             "exception_field": spec["exception_field"],
             "fields": list(spec["fields"])}
            for name, spec in W.scheme_groups(cfg).items()]


def summary(cfg: dict, payload: dict | None) -> list[tuple[str, str, str]]:
    """[(字段, 值, 备注)]，给模板的说明页用。"""
    payload = payload or {}
    rules = payload.get("rules") or {}
    rows = []
    for name in W.rule_names(cfg):
        exc = [e for e in payload.get("exceptions") or [] if e.get("field") == name]
        note = ""
        if exc:
            note = "；".join(f"{'、'.join(e['positions'])} = {e['value']}" for e in exc)
            note = f"例外：{note}"
        rows.append((name, str(rules.get(name, "") or "") or "（未配置）", note))

    for gname, spec in W.scheme_groups(cfg).items():
        key = spec["key"]
        grp = group_of(payload, key)
        if grp.get("mode") == MODE_KEYWORD:
            pairs = "；".join(f"{'/'.join(r['keywords'])} → {'＋'.join(r.get('schemes') or [])}"
                             for r in grp.get("rules") or [])
            fb = "＋".join(grp.get("fallback") or [])
            rows.append((gname, "按单元名称匹配",
                         pairs + (f"；都没命中用「{fb}」" if fb else "")))
        else:
            rows.append((gname, grp.get("scheme") or "（未配置）", "全部单元用同一套"))
        for e in payload.get("exceptions") or []:
            if e.get("field") == spec["exception_field"]:
                rows.append((f"　{gname}例外", e["value"], "、".join(e["positions"])))
    return rows
