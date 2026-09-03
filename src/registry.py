"""每个配置类型（mode）要用的执行器 / 模板生成器 / 可选范围，集中声明在这里。

原来这四份东西散落在四个 if/elif 里（src/gui.py 的 _make_runner 和生成模板分支、
main.py 的 --make-template 和 --cli 两处），四处要同时改、还很容易漏一处。
新增一个 mode 现在只用在这个文件里加一条 MODES[...]。

⚠ 保持函数体内 lazy import。理由是**启动速度**，不是打包：
  每个 mode 的 runner 会顺带拖进 playwright、openpyxl 那一串（browser.py
  顶层 import sync_playwright，*_data.py 顶层 import openpyxl），
  挪到模块顶部就变成开界面时全都 import 一遍，而用户一次只会跑一个 mode。

  （2026-08-26 订正：这里原来写"lazy import 是为了配合 build.bat 里的
   --hidden-import"，已经不对了。src/ 现在以普通文件放在 exe 旁边、不冻进包，
   build_app.spec 的 scan_imports() 明确把 "src" 从 hiddenimports 里剔除，
   所以 lazy 与否对打包没有任何影响。见 build_app.spec 开头。）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class ModeSpec:
    make_runner: Callable                     # (settings, cfg, ui) -> Runner-like 对象
    scopes: list = field(default_factory=list)          # [(显示文字, 值), ...]；没有就不显示「延期范围」这一行
    build_template: Optional[Callable] = None            # (form_name) -> 生成的文件路径；wizard 不走这个，各自处理见 main.py --make-template
    no_template_hint: str = ""                            # 界面上：当前范围不需要 Excel 模板时的提示
    no_template_hint_cli: str = ""                         # 命令行下同一件事的措辞（引导用 --scope，不是点界面）
    # 「这个 mode 的模板该有哪几列」——一等公民，给「你的 Excel 缺列了吗」用。
    # 签名 (cfg, opts) -> {sheet名: [列名, ...]}；不吃 Excel 的 mode 留空。
    # ⚠ 各 *_template.py 生成模板时就该走同一个函数，别再各写一份列定义（会漂移）。
    template_columns: Optional[Callable] = None


def _runner_wizard(settings, cfg, ui):
    from .wizard_runner import WizardRunner
    return WizardRunner(settings, cfg, ui)


def _runner_dmp(settings, cfg, ui):
    from .dmp_runner import DmpRunner
    return DmpRunner(settings, cfg, ui)


def _runner_ab(settings, cfg, ui):
    from .ab_runner import AbRunner
    return AbRunner(settings, cfg, ui)


def _runner_ad(settings, cfg, ui):
    from .ad_runner import AdRunner
    return AdRunner(settings, cfg, ui)


def _runner_ad_regular(settings, cfg, ui):
    from .ad_reg_runner import AdRegRunner
    return AdRegRunner(settings, cfg, ui)


def _runner_meeting(settings, cfg, ui):
    from .meeting_runner import MeetingRunner
    return MeetingRunner(settings, cfg, ui)


def _runner_price_panel(settings, cfg, ui):
    from .pp_runner import PriceRunner
    return PriceRunner(settings, cfg, ui)


def _runner_pt(settings, cfg, ui):
    from .pt_runner import PtToggleRunner
    return PtToggleRunner(settings, cfg, ui)


def _runner_flow(settings, cfg, ui):
    from .flow_runner import FlowRunner
    return FlowRunner(settings, cfg, ui)


def _runner_default(settings, cfg, ui):
    from .runner import Runner
    return Runner(settings, cfg, ui)


def _template_dmp(name: str) -> str:
    from . import dmp_template
    return dmp_template.build(name)


def _template_ab(name: str) -> str:
    from . import ab_template
    return ab_template.build(name)


def _template_ad(name: str) -> str:
    from . import ad_template
    from . import formcfg

    return ad_template.build(formcfg.load(name))


def _template_price_panel(name: str) -> str:
    from . import formcfg
    from . import pp_template

    return pp_template.build(formcfg.load(name))


def _template_default(name: str) -> str:
    from . import template
    return template.build(name)


# ---------------------------------------------------------------- 模板预期列
# ⚠ 都返回 {sheet名: [列名, ...]}。复用各 *_data / *_template 里已有的「出哪几列」
#   函数,不新写一份 —— 那份一旦和真模板对不上,「缺列检查」就会误报。
def _columns_default(cfg: dict, opts: dict | None = None) -> dict:
    from .datasource import header_field_names, item_field_names
    grouped = bool(cfg.get("list"))
    cols = ((["分组"] if grouped else [])
            + header_field_names(cfg) + item_field_names(cfg.get("list")))
    return {"数据": cols}


def _columns_ad(cfg: dict, opts: dict | None = None) -> dict:
    from . import ad_data as D
    return {"素材": [c["name"] for c in D.columns(cfg)]}


def _columns_dmp(cfg: dict, opts: dict | None = None) -> dict:
    from . import dmp_template as T
    return {"人群清单": [c[0] for c in T.COLS]}


def _columns_ab(cfg: dict, opts: dict | None = None) -> dict:
    from . import ab_template as T
    return {"实验清单": [c[0] for c in T.COLS]}


def _columns_wizard(cfg: dict, opts: dict | None = None) -> dict:
    from . import wizard_schema as W
    from . import wizard_template as WT
    opts = opts or {}
    positions = opts.get("positions") or W.position_names(cfg)
    if not positions:
        return {}
    out = {}
    if not opts.get("existing_activity"):
        out["活动"] = [f["name"] for f in W.columns_for(cfg, positions[0], W.STEP_ACTIVITY)]
    for p in positions:
        out[WT.sheet_name(p)] = [c["title"] for c in WT.columns_for_sheet(cfg, p)]
    return out


def _columns_price_panel(cfg: dict, opts: dict | None = None) -> dict:
    from . import pp_data as D
    from . import wizard_strategy as S
    opts = opts or {}
    payload = S.active_payload(cfg)
    channel = D.channel_of(cfg)
    per_sku = D.sku_columns(cfg, payload)
    units = [f["name"] for f in (D.unit_fields(cfg, channel) + list(per_sku or []))]
    out = {"单元": units}
    if not opts.get("existing_activity"):
        out[D.activity_sheet(cfg)] = [f["name"] for f in D.activity_fields(cfg)]
    return out


def _columns_flow(cfg: dict, opts: dict | None = None) -> dict:
    from . import flow_data as FD
    cols = FD.columns(cfg.get("_flow") or cfg.get("flow") or {})
    return {"数据": cols} if cols else {}


# 「延期范围」按配置类型给不同选项，每种类型的第一项就是默认值。
# 没列在这里的 mode（价格配置、资源位投放）不会显示这一行。
# ⚠ AB 故意不给「全部实验中」：全站几千条实验，全扫既慢又会动到别人的实验。
MODES: dict[str, ModeSpec] = {
    "wizard": ModeSpec(
        make_runner=_runner_wizard,
        template_columns=_columns_wizard,
    ),
    "dmp_extension": ModeSpec(
        make_runner=_runner_dmp,
        scopes=[
            ("全部生效中 → 最晚日期", "active"),
            ("我创建的 → 最晚日期", "mine"),
            ("按清单指定人群ID", "id_list"),
        ],
        build_template=_template_dmp,
        template_columns=_columns_dmp,
        no_template_hint=(
            "当前「延期范围」直接读取网页里的人群，不需要 Excel 模板。\n\n"
            "要按人群ID指定的话，先把范围切到「按清单指定人群ID」。"),
        no_template_hint_cli="直接读取网页中的人群，不需要 Excel 模板（要按人群ID指定请加 --scope id_list）",
    ),
    "ad_native": ModeSpec(
        make_runner=_runner_ad,
        build_template=_template_ad,
        template_columns=_columns_ad,
    ),
    # 常规商广：和原生同一个页面，但不吃 Excel（视频数量/文案都在准备页填），
    # 所以没有 build_template，也没有「延期范围」。
    "ad_regular": ModeSpec(
        make_runner=_runner_ad_regular,
        no_template_hint=("常规商广不用 Excel 模板。\n\n"
                          "在「准备」页填「视频数量 / 跳过前几个 / 6 条文案」等参数，"
                          "点保存，再「载入并检查」。"),
        no_template_hint_cli="常规商广不用 Excel 模板，参数都在准备页填",
    ),
    # ⚠ 抢会议室不读 Excel：任务清单在界面上直接填，存 config/prep/预定会议室.json。
    #   build_template 留空，界面上「生成模板」会拿 no_template_hint 提示改去哪儿填。
    "meeting_reserve": ModeSpec(
        make_runner=_runner_meeting,
        no_template_hint=(
            "抢会议室不用 Excel 模板。\n\n"
            "直接在「准备」页的「抢占任务」里加几条（日期/时间段/人数/楼栋），"
            "填完点「载入并检查」。"),
        no_template_hint_cli="抢会议室不用 Excel 模板，任务清单在图形界面上填",
    ),
    # 价格面板配置：老后台（manager.bilibili.co）的收银台价格面板单元。
    # 不新建活动，也没有「延期范围」，所以 scopes 留空。
    "price_panel": ModeSpec(
        make_runner=_runner_price_panel,
        build_template=_template_price_panel,
        template_columns=_columns_price_panel,
    ),
    # 价格策略批量开启 / 关闭：翻转策略编辑页底部「价格配置」表里已有行的开关。
    # 不吃 Excel，也没有「延期范围」的默认表（选项写在两份 yaml 的 scopes: 里）。
    "pt_toggle": ModeSpec(
        make_runner=_runner_pt,
        no_template_hint=("「批量开启/关闭」不用 Excel 模板。\n\n"
                          "选好「范围」（按名称关键词 / 本工具配置过的 / 整页全部），"
                          "填上关键词，点「载入并检查」。"),
        no_template_hint_cli="「批量开启/关闭」不用 Excel 模板，用 --scope 选范围",
    ),
    # 自制配置类型：录一遍操作生成的步骤图，定义在 config/flows/<名>.json。
    # 模板不走 *_template.py —— 按 flow 的 data.columns 直接出一张空表（见 webapp.make_template）。
    "flow": ModeSpec(
        make_runner=_runner_flow,
        template_columns=_columns_flow,
    ),
    "ab_extension": ModeSpec(
        make_runner=_runner_ab,
        scopes=[
            ("我的实验 → 最晚日期", "mine"),
            ("按清单指定实验ID", "id_list"),
        ],
        build_template=_template_ab,
        template_columns=_columns_ab,
        no_template_hint=(
            "当前「延期范围」直接读取网页里「我的实验」下的实验，不需要 Excel 模板。\n\n"
            "要按实验ID指定的话，先把范围切到「按清单指定实验ID」。"),
        no_template_hint_cli="直接读取网页中「我的实验」下的实验，不需要 Excel 模板（要按实验ID指定请加 --scope id_list）",
    ),
}

DEFAULT_SPEC = ModeSpec(make_runner=_runner_default, build_template=_template_default,
                        template_columns=_columns_default)


def spec_for(mode: Optional[str]) -> ModeSpec:
    """老配置没有 mode 字段，永远落到 DEFAULT_SPEC（走 Runner + 通用模板）。"""
    return MODES.get(mode, DEFAULT_SPEC)


def scopes_for(cfg: dict) -> list:
    """「延期范围」的可选项：form yaml 里的 scopes: 优先，没写就用这里的默认表。

    不改 yaml 也要能跑 —— 老配置没有 scopes: 字段时用 spec.scopes 兜底。
    """
    own = cfg.get("scopes")
    if own:
        return [(item[0], item[1]) for item in own]
    return spec_for(cfg.get("mode")).scopes


def expected_columns(cfg: dict, opts: dict | None = None) -> dict:
    """这个配置类型的 Excel 模板该有哪几列。{sheet名: [列名]}；不吃 Excel 就是 {}。

    读不出来时（比如策略还没配、资源位没选）返回 {} —— 缺列检查跳过,不误报。
    """
    fn = spec_for(cfg.get("mode")).template_columns
    if not fn:
        return {}
    try:
        return fn(cfg, opts or {}) or {}
    except Exception:
        import logging
        logging.getLogger(__name__).warning("算模板预期列失败,跳过缺列检查", exc_info=True)
        return {}
