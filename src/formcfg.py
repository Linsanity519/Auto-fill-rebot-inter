r"""form yaml 的唯一入口：读、缓存、校验。

## 为什么有这个

改这个文件之前的状况：
  · `yaml.safe_load` 散在 6 处（registry ×2、webapp ×2、gui ×2、template ×1），
    每处各读各的、没有缓存
  · **一个键名都没校验过**。8 份 yaml 只有 3 个键是所有类型共有的，
    100 个键只出现在一份里 —— 也就是说每接一个配置类型就发明一批新词，
    而打错一个字母是**完全静默**的

第二条才是真花钱的地方。实测过：把 `strategy_groups` 打成 `strategy_group`，
yaml 照样解析、界面照样显示、跑起来策略字段从 24 个悄悄变成 6 个，
一句报错都没有 —— 只能等实跑到那一步、发现单元配错了才回头查。
而实跑一轮要开浏览器、登录、点到那一页，几分钟起步。

所以这里做的事很朴素：**把「这个 mode 认得哪些顶层键」写下来**，
不认得的当场说出来，并猜一下是不是打错了。

## 词汇表怎么维护

`CORE` 是所有配置类型都能用的；`BY_MODE` 是各家自己的。
初版是从当时那 8 份 yaml 里**扫出来**的，所以存量一定全过 —— 它记录的是现状，
不是我拍脑袋定的规矩。

**往 yaml 里加一个新的顶层键时，记得回来把它加进对应的那一格。**
不加的话 `tools\check_mode.py` 会提示「不认识这个键」——
那正是它该干的事，别去关掉提示，回来登记一下就好。

⚠ 只管**顶层**键。字段级的结构（fields 里每一项长什么样）差异太大，
  管起来会变成给五套 DOM 各写一份 schema，得不偿失 —— 那部分交给
  `tools\check_mode.py` 里「把定义真解析一遍」那几条来兜。

⚠ 以 `_` 开头的键一律放行：那是**纯 YAML 锚点**的存放处（`_frag`），
  只为了让下面用 `*ref` 引用，程序永远不会读它。
  新加锚点请用 `_` 开头 —— 老的几个中文名锚点（搭售类型选项、买赠商品类型、
  商机权益选项）已经登记在下面，就不改了，改名要连着改引用它的地方。
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .paths import user_path

log = logging.getLogger(__name__)

ANCHOR_PREFIX = "_"


CORE = {
    "description", "form_url", "mode", "name", "nav", "ready_selector",
}

BY_MODE = {
    "_default": {
        "antd_prefix", "cancel_selector", "fields", "list", "open_dialog", "open_steps",
        # ledger：写了就在每轮跑完往 src/pt_ledger.py 的台账记一批（价格配置在用）
        "ledger",
        "reset_between_rows", "sku_types", "submit_selector", "success_selector",
    },
    "ab_extension": {
        "active_status", "after_confirm_wait", "after_filter_wait", "after_menu_wait",
        "after_open_wait", "after_submit_wait", "cancel_texts", "date_available_selector",
        "date_input_selectors", "dialog_close_wait", "dialog_selector", "dialog_title",
        "dialog_wait", "empty_read_retries", "empty_read_wait", "end_date_column",
        "error_selectors", "extension_menu_item", "id_pattern", "max_month_lookahead",
        "max_scan_pages", "menu_item_selector", "menu_wait", "month_cells_selector",
        "month_label_selector", "month_wait", "more_menu_selectors", "more_menu_text",
        "my_experiment_selectors", "my_experiment_text", "name_column",
        "next_month_selectors", "next_page_selectors", "page_wait", "panel_ready_selector",
        "panel_wait", "popconfirm_ok_selectors", "popconfirm_wait", "prev_month_selectors",
        "row_selector", "scope", "search_input_selectors", "search_wait", "status_column",
        "stop_after_empty_months", "submit_texts", "success_selectors", "table_selector",
    },
    "ad_native": {
        "columns", "create_url_marker", "creative", "grouping", "plan_fields", "prep_fields",
        "submit_button", "unit_fields", "urls",
    },
    # 原生商广新 / 常规商广投放：ad.bilibili.co 新版投放页（#/promote/auto-v2）。
    # 两层：project_fields（项目层 bd- 表单）+ material（素材聚合池）。
    "ad_v2": {
        "columns", "create_url_marker", "material", "prep_fields", "project_fields",
        "submit_button", "urls",
    },
    "dmp_extension": {
        "active_status", "after_each_wait", "after_filter_wait", "after_open_wait",
        "after_pick_wait", "after_save_wait", "cancel_texts", "confirm_texts",
        "creator_column", "date_field_label", "date_input_selectors", "empty_selector",
        "error_selectors", "extension_menu_text", "id_column", "known_status",
        "latest_date_selectors", "list_ready_timeout", "max_forward_months",
        "menu_item_selector", "menu_open_wait", "mine_creator", "mine_filter_texts",
        "mine_radio_selector", "mine_status", "month_wait", "name_column",
        "next_month_selectors", "next_page_selectors", "non_extendable_status",
        "op_trigger_selector", "page_change_timeout", "page_settle_wait",
        "panel_header_selectors", "panel_open_wait", "panel_selectors",
        "prev_month_selectors", "row_key_attribute", "row_selector", "save_texts", "scope",
        "search_attempts", "search_button_selector", "search_input_selector",
        "search_max_rows", "search_timeout",
    },
    "meeting_reserve": {
        "buildings", "data_source", "grab",
    },
    "pt_toggle": {
        "data_source", "direction", "ledger", "reversible", "scopes", "toggle", "ui",
    },
    # 自制配置类型的 cfg 是 flow_data.synthetic_cfg 拼出来的，不是手写的 yaml，
    # 但走同一套校验 —— _flow 是那份 config/flows/*.json 的原件。
    "flow": {
        "data_source", "flow", "ui", "_flow",
    },
    "price_panel": {
        "activity", "creative", "direct_drops", "excel_from_unit", "next_button",
        "pid_platform_alias", "pid_sheet", "position", "position_ready_selector",
        "positions", "prep_from_unit", "scheme_groups", "sku_map_skip", "sku_unit_fields",
        "skus", "strategy_groups", "ui", "unit_common", "unit_fields", "unit_url_template",
        "买赠商品类型", "搭售类型选项",
    },
    "wizard": {
        "_frag", "positions", "scheme_groups", "steps", "strategy_groups", "ui",
        "unit_common", "unit_push", "固化权益选项",
    },
}

# 打错一个字母最容易，所以除了「认不认得」还要猜一下想写的是哪个
def _edit1(a: str, b: str) -> bool:
    """a 和 b 差一步以内（改一个字符 / 多一个 / 少一个）。"""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    lo, hi = (a, b) if len(a) < len(b) else (b, a)
    return any(hi[:i] + hi[i + 1:] == lo for i in range(len(hi)))


def known_keys(mode) -> set:
    """这个 mode 认得的全部顶层键。

    ⚠ 用 set() 包一层：新加的 mode 那一格常常先是空的，而**空集合在 Python 里
      没法写成字面量** —— `{}` 是空 dict，`CORE | {}` 直接 TypeError。
      （tools/new_mode.py 生成骨架时真踩过。）
    """
    return CORE | set(BY_MODE.get(mode or "_default") or ())


# ---------------------------------------------------------------- 读
_cache: dict = {}          # {路径: (mtime, cfg)}


def path_for(name: str) -> Path:
    return user_path("config", "forms", f"{name}.yaml")


def load(name: str) -> dict:
    """读一个配置类型的 yaml。按 mtime 缓存，同一次运行里反复读不重复解析。

    ⚠ 缓存按 mtime 失效，所以在程序跑着的时候手改 yaml 也能立刻生效
      （抓页面调选择器的时候一直是这么干的）。
    """
    p = path_for(name)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = None
    hit = _cache.get(str(p))
    if hit and mtime is not None and hit[0] == mtime:
        return hit[1]
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if mtime is not None:
        _cache[str(p)] = (mtime, cfg)
    return cfg


def load_all() -> list[tuple[str, dict]]:
    """[(配置类型名, cfg), ...]，按文件名排序。读不了的跳过并记日志。"""
    out = []
    for p in sorted(user_path("config", "forms").glob("*.yaml")):
        try:
            out.append((p.stem, load(p.stem)))
        except Exception:
            log.warning("配置读取失败：%s", p, exc_info=True)
    return out


# ---------------------------------------------------------------- 校验
def validate(cfg: dict, name: str) -> tuple[list[str], list[str]]:
    """返回 (必须修的, 提示)。不抛异常 —— 校验失败绝不能挡住程序启动。"""
    errors, warns = [], []

    if cfg.get("name") != name:
        errors.append(f"yaml 里的 name 是「{cfg.get('name')}」，和文件名「{name}」对不上"
                      "　← 界面按文件名选、runner 按 name 报，两边必须一致")
    if not cfg.get("description"):
        warns.append("没写 description　← 首页当功能导航用的就是这句话")
    if not cfg.get("nav"):
        warns.append("没写 nav　← 侧栏会把它扔进「其他」组")

    mode = cfg.get("mode")
    known = known_keys(mode)
    for k in cfg:
        key = str(k)
        if key.startswith(ANCHOR_PREFIX) or key in known:
            continue
        near = [x for x in known if _edit1(key, x)]
        if near:
            warns.append(f"「{key}」不是 {mode or '默认'} 认得的键，是不是想写 {near[:2]}？"
                         "　← 打错一个字母是静默的，yaml 照样解析、功能悄悄少一半")
        else:
            warns.append(f"「{key}」不在 {mode or '默认'} 的已知键里。"
                         "确实是新加的就去 src/formcfg.py 的 BY_MODE 里登记一下；"
                         "纯 YAML 锚点请用 _ 开头")
    return errors, warns
