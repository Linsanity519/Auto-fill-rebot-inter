"""每个配置类型（mode）要用的执行器 / 模板生成器 / 可选范围，集中声明在这里。

原来这四份东西散落在四个 if/elif 里（src/gui.py 的 _make_runner 和生成模板分支、
main.py 的 --make-template 和 --cli 两处），四处要同时改、还很容易漏一处。
新增一个 mode 现在只用在这个文件里加一条 MODES[...]。

⚠ 保持函数体内 lazy import：build.bat 打包时给 PyInstaller 加了
  --hidden-import src.dmp_data / src.ab_runner 之类的显式声明，就是因为这些模块
  只在用到的分支里 import，静态扫描找不到，所以才需要显式声明。这里继续沿用
  「lazy import + build.bat 里显式 hidden-import」这个组合，不要把 import 挪到模块顶部
  （挪了的话 PyInstaller 会自动收进去，但那是巧合，不是这个组合设计上的保证）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class ModeSpec:
    make_runner: Callable                     # (settings, cfg, ui) -> Runner-like 对象
    scopes: list = field(default_factory=list)          # [(显示文字, 值), ...]；没有就不显示「延期范围」这一行
    build_template: Optional[Callable] = None            # (form_name) -> 生成的文件路径；wizard 不走这个，各自处理见 gui.py/main.py
    no_template_hint: str = ""                            # 界面上：当前范围不需要 Excel 模板时的提示
    no_template_hint_cli: str = ""                         # 命令行下同一件事的措辞（引导用 --scope，不是点界面）


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


def _runner_meeting(settings, cfg, ui):
    from .meeting_runner import MeetingRunner
    return MeetingRunner(settings, cfg, ui)


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
    import yaml

    from . import ad_template
    from .paths import user_path

    cfg = yaml.safe_load(user_path("config", "forms", f"{name}.yaml").read_text(encoding="utf-8"))
    return ad_template.build(cfg)


def _template_default(name: str) -> str:
    from . import template
    return template.build(name)


# 「延期范围」按配置类型给不同选项，每种类型的第一项就是默认值。
# 没列在这里的 mode（价格配置、资源位投放）不会显示这一行。
# ⚠ AB 故意不给「全部实验中」：全站几千条实验，全扫既慢又会动到别人的实验。
MODES: dict[str, ModeSpec] = {
    "wizard": ModeSpec(
        make_runner=_runner_wizard,
    ),
    "dmp_extension": ModeSpec(
        make_runner=_runner_dmp,
        scopes=[
            ("全部生效中 → 最晚日期", "active"),
            ("我创建的 → 最晚日期", "mine"),
            ("按清单指定人群ID", "id_list"),
        ],
        build_template=_template_dmp,
        no_template_hint=(
            "当前「延期范围」直接读取网页里的人群，不需要 Excel 模板。\n\n"
            "要按人群ID指定的话，先把范围切到「按清单指定人群ID」。"),
        no_template_hint_cli="直接读取网页中的人群，不需要 Excel 模板（要按人群ID指定请加 --scope id_list）",
    ),
    "ad_native": ModeSpec(
        make_runner=_runner_ad,
        build_template=_template_ad,
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
    "ab_extension": ModeSpec(
        make_runner=_runner_ab,
        scopes=[
            ("我的实验 → 最晚日期", "mine"),
            ("按清单指定实验ID", "id_list"),
        ],
        build_template=_template_ab,
        no_template_hint=(
            "当前「延期范围」直接读取网页里「我的实验」下的实验，不需要 Excel 模板。\n\n"
            "要按实验ID指定的话，先把范围切到「按清单指定实验ID」。"),
        no_template_hint_cli="直接读取网页中「我的实验」下的实验，不需要 Excel 模板（要按实验ID指定请加 --scope id_list）",
    ),
}

DEFAULT_SPEC = ModeSpec(make_runner=_runner_default, build_template=_template_default)


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
