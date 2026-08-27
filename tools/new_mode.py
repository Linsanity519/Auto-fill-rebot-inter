r"""新增一个配置类型：把「接线」那部分一条命令铺好。

    python tools\new_mode.py 优惠券配置 --prefix coupon
    python tools\new_mode.py 优惠券配置 --prefix coupon --dry-run    # 只看要动哪些文件

## 它做什么、不做什么

**做**（这些是机械活，忘一个就静默出问题）：

  config/forms/<名>.yaml        骨架：name / description / nav / mode / url / ready_selector
  docs/<名>-配置项抓取.md        占位，写清下一步跑 capture.py
  docs/README.md                 加一行索引
  src/registry.py                MODES 加一条 + 两个 lazy 工厂
  src/formcfg.py                 BY_MODE 加一格
  src/<前缀>_runner.py           主流程骨架（StateMixin + preview + run）
  src/<前缀>_filler.py           控件填法骨架（建在 fill_core 上）

**不做**：真正的业务 —— 页面上有哪些字段、控件怎么点、Excel 出哪些列。
那些得先把页面抓明白（`tools\capture.py`），抓完再往骨架里填。

⚠ 生成完立刻跑一次 `python tools\check_mode.py <名>`，应该全绿。
  不绿说明这个脚本和当前架构脱节了 —— 修脚本，别手动绕过去。

## 顺序

    1. python tools\new_mode.py 新类型名 --prefix xx
    2. python tools\capture.py --out docs\新类型名-配置项抓取.md      抓页面
    3. 人工核对抓取记录（控件真实类型、选项全集、联动）
    4. 往 yaml 里填字段，往 _filler / _runner 里填业务
    5. python tools\check_mode.py 新类型名                          随时自检
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent

YAML_TPL = '''# ============================================================
# {name}
# 结构抓取：见 docs/{name}-配置项抓取.md ——【改这个文件之前先看那份】
# ============================================================
# ⚠ 定位一律走「label 文字 → 字段块 → 块内按选项文字」。
#   不要用编译哈希类名（tw-xxxxxx / css-1a75fj6 / emotion 类），发版即失效。
# ⚠ 往这里加新的**顶层**键时，回 src/formcfg.py 的 BY_MODE["{mode}"] 里登记一下，
#   不然 tools\\check_mode.py 会提示「不认识这个键」。纯 YAML 锚点用 _ 开头。

name: {name}
description: TODO 一句话说清这个配置类型是干嘛的（首页当功能导航用的就是这句）

# 侧栏归类
nav:
  group: TODO 归到哪个主 Tab
  group_order: 99
  label: {name}
  order: 1

mode: {mode}

# ---------------- 页面 ----------------
form_url: 'TODO 要填表的那一页'

# 「页面可以开始填了」的判据。⚠ 挑一个**最晚出现**的元素 ——
# 挑早了会在后面报「找不到字段 xxx」，而真实原因是页面还没渲染完。
ready_selector: 'TODO'

# ---------------- 字段 ----------------
# 抓完页面再填。每个字段至少要有：name（Excel 列名）/ label（页面原文）/ type。
# ⚠ label 用页面上的原文，不要用运营口径表里的叫法 —— 翻过车，见 docs/README.md「教训」。
fields: []
'''

DOC_TPL = '''# {name} — 配置项抓取

**这份还没抓。** 下一步：

```bash
python tools\\capture.py --open "要抓的页面URL"      # 开浏览器，人工登录并点到那一屏
python tools\\capture.py --out docs\\{name}-配置项抓取.md
```

抓完把上面这段删掉，按下面几节人工核对补全。
草稿里控件类型是**推断**的，下拉的选项读不到，联动完全没体现 —— 这三样必须人眼过。

## 定位要点（踩坑清单）

- TODO

## 一、字段全表

TODO（capture.py 会生成）

## 二、联动

**选了什么之后哪些字段出现 / 消失 / 变必填。**
这一段直接决定 Excel 模板出哪些列，而模板列在跑之前就定死 —— 漏一条整个模板重做。

```bash
python tools\\capture.py --snap a
#   （人工在页面上改那个值）
python tools\\capture.py --snap b --diff a
```

TODO

## 三、提交的判据

- 成功：TODO（URL 跳转？弹窗消失？绿色提示条？）
- 失败：TODO（错误显示在哪）

## 四、有没有能直接取数的接口

能拿接口就别翻 DOM。TODO
'''

RUNNER_TPL = '''"""{name} 主流程。

⚠ 只服务 mode: {mode}。和别的配置类型的 runner 互不调用 ——
  各家后台的 DOM 栈完全不同，共用只会互相踩。

TODO 写清这套和别人的关键差别（几层？成功判据是什么？有没有必须的先后顺序？）
"""
from __future__ import annotations

import logging
from pathlib import Path

from .browser import Browser
from .fill_core import FillError
from .preview import PreviewRow
from .runstate import StateMixin
from .ui import BaseUI, ConsoleUI, Stopped
from .{prefix}_filler import {cls}Filler

log = logging.getLogger(__name__)


def _key(item: dict) -> str:
    """断点的 key：**重跑时还认得出是同一条**的东西。

    ⚠ 别用列表下标 —— 用户在 Excel 中间插一行就全错位了。
      能带上名字就带上（见 wizard_runner / pp_runner 里的 _key）。
    """
    return str(item.get("name") or "")


class {cls}Runner(StateMixin):
    def __init__(self, settings: dict, form_cfg: dict, ui: BaseUI | None = None):
        self.s = settings
        self.f = form_cfg
        self.ui = ui or ConsoleUI()
        self.shot_dir = Path(settings["screenshot_dir"])
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self.auto = False
        self.created = []
        self._init_state()          # 断点，clear_state 也一起有了

    # ---------------- 预检（不碰浏览器）----------------
    def preview(self) -> list[PreviewRow]:
        """跑之前把数据解析 + 校验一遍。界面上「载入并检查」调的就是它。

        ⚠ 尽量别在这里开浏览器：离线的话 tools\\check_mode.py --data 能直接验。
        """
        items = []          # TODO 读 Excel / 读界面参数
        return [
            PreviewRow(index=i + 1, name=str(it.get("name", "")), kind="",
                       detail_count=0, issues=[],
                       done=self.state.is_done(_key(it)), payload=it)
            for i, it in enumerate(items)
        ]

    # ---------------- 主流程 ----------------
    def run(self, items: list[dict] | None = None):
        items = items if items is not None else [r.payload for r in self.preview()]
        dry = self.s.get("dry_run")
        total = len(items)
        stats = {{"ok": 0, "failed": 0, "skipped": 0, "dry": 0}}
        results = []

        self.ui.log(f"「{{self.f['name']}}」共 {{total}} 条" + ("（试跑：只填不提交）" if dry else ""))
        self.ui.progress(0, total, stats)

        try:
            with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
                filler = {cls}Filler(b.page, self.s["timeout"],
                                     on_note=lambda m: self.ui.log(f"    {{m}}", "warn"))

                for i, it in enumerate(items):
                    self.ui.checkpoint()        # 暂停在这儿阻塞，停止抛 Stopped
                    label = f"[{{i + 1}}/{{total}}]"
                    name = str(it.get("name", ""))

                    if self.state.is_done(_key(it)):
                        self.ui.log(f"{{label}} {{name}} 已完成过，跳过")
                        continue

                    try:
                        b.front()               # ⚠ 后台标签会被 Chrome 降频 15 倍
                        # TODO 打开页面、填、截图
                        _ = filler
                        self._shot(b.page, i + 1, "filled")

                        if dry:
                            stats["dry"] += 1
                            self.ui.progress(i + 1, total, stats)
                            continue

                        # ⚠ 提交前一定停下来给人看截图，除非用户选了全自动
                        action = "submit" if self.auto else self.ui.confirm(label, name)
                        if action == "auto":
                            self.auto, action = True, "submit"
                        if action == "stop":
                            break
                        if action == "skip":
                            stats["skipped"] += 1
                            self.ui.progress(i + 1, total, stats)
                            continue

                        # TODO 提交 + 确认真的成功了（别只看"点了保存"）
                        stats["ok"] += 1
                        self.state.mark_done(_key(it))
                        self.ui.log(f"{{label}} 完成", "ok")

                    except Stopped:
                        raise
                    except Exception as e:
                        msg = str(e)
                        log.exception("%s 失败", label)
                        shot = self._shot(b.page, i + 1, "error")
                        stats["failed"] += 1
                        self.state.mark_failed(_key(it), name, msg)
                        self.ui.log(f"{{label}} 失败：{{msg}}", "error")
                        self.ui.log(f"    截图：{{shot}}")
                        if not self.ui.ask_continue(msg):
                            break

                    self.ui.progress(i + 1, total, stats)

        except Stopped:
            self.ui.log("已停止", "warn")

        ok = stats["failed"] == 0
        self.ui.finished("跑完了" if ok else "跑完了，有失败",
                         f"成功 {{stats['ok']}}　失败 {{stats['failed']}}　"
                         f"跳过 {{stats['skipped']}}　试跑 {{stats['dry']}}", ok)
        return results

    def _shot(self, page, idx: int, tag: str) -> str:
        p = self.shot_dir / f"{{self.f['name']}}_{{idx}}_{{tag}}.png"
        try:
            page.screenshot(path=str(p))
        except Exception:
            log.warning("截图失败", exc_info=True)
        return str(p)
'''

FILLER_TPL = '''"""{name} 这套 DOM 的控件填法。

⚠ 独立于别的 filler。各家后台的 DOM 栈完全不同（Formily / Vue+tw- / iView /
  Arco / antd），选择器一行都不能互抄 —— 这个隔离是刻意的，别去合并。

**和 DOM 无关的部分一律从 src/fill_core.py 拿**（等待、按文字挑一条、
等渲染稳定、报错措辞），别再抄第五遍。这个文件只负责三件 DOM 特有的事：

  1. 怎么按 label 找到字段块        _block()
  2. 怎么把下拉浮层点开             _open()
  3. 怎么读出浮层里的选项文字       _options()

定位策略：TODO（抓完页面填。⚠ 不用编译哈希类名）
"""
from __future__ import annotations

import logging

from .fill_core import (FillError, field_error, js_click, note, option_error,
                        pick, verify_error, wait_until)

log = logging.getLogger(__name__)


class {cls}Filler:
    def __init__(self, page, timeout: int = 15000, on_note=None):
        self.page = page
        self.timeout = timeout
        self._on_note = on_note

    def _note(self, msg: str):
        note(self._on_note, msg)

    # ------------------------------------------------ DOM 特有的三件事
    def _block(self, label: str, required: bool = True):
        """按 label 文字拿到字段块。找不到时 required=False 返回 None。"""
        # TODO 换成这套 DOM 的找法
        raise FillError(f"TODO 还没实现 _block（要找的是「{{label}}」）")

    def _open(self, blk):
        """把这个字段的下拉浮层点开。"""
        # TODO。⚠ 先收掉上一个浮层，不然点击会被它吃掉、还会读到上一格的选项
        raise FillError("TODO 还没实现 _open")

    def _options(self, blk) -> list:
        """读出**当前展开着的**浮层里的选项文字。"""
        # TODO。⚠ 浮层关掉常常不从 DOM 里删，只是 display:none，要挑显示着的那个
        return []

    # ------------------------------------------------ 下面的都是通用逻辑
    def text(self, label: str, value: str):
        blk = self._block(label)
        if blk is None:
            raise field_error(label)
        # TODO 填进去
        got = ""          # TODO 回读
        if got != value:
            raise verify_error(label, got, value)

    def select(self, label: str, value: str, contains: bool = False):
        """按文字挑一条。⚠ 每次都回读核对 —— 这类后台大量存在「点了没选上还不报错」。"""
        blk = self._block(label)
        if blk is None:
            raise field_error(label)
        self._open(blk)
        if not wait_until(self.page, lambda: self._options(blk), self.timeout):
            raise option_error(label, value, [])
        texts = self._options(blk)
        hit = pick(texts, value, contains)
        if hit is None:
            raise option_error(label, value, texts)
        # TODO 点中 hit 那一条，然后回读核对
        _ = js_click
'''


def _valid(name: str) -> str | None:
    if not name or "/" in name or "\\\\" in name:
        return "配置类型名不能为空、不能带斜杠"
    return None


def build(name: str, prefix: str, mode: str, cls: str, dry: bool) -> int:
    plan = []

    forms = ROOT / "config" / "forms" / f"{name}.yaml"
    doc = ROOT / "docs" / f"{name}-配置项抓取.md"
    runner = ROOT / "src" / f"{prefix}_runner.py"
    filler = ROOT / "src" / f"{prefix}_filler.py"

    for p, text in ((forms, YAML_TPL.format(name=name, mode=mode)),
                    (doc, DOC_TPL.format(name=name)),
                    (runner, RUNNER_TPL.format(name=name, mode=mode, prefix=prefix, cls=cls)),
                    (filler, FILLER_TPL.format(name=name, cls=cls))):
        if p.exists():
            print(f"✗ 已经存在，不覆盖：{p.relative_to(ROOT)}")
            return 1
        plan.append((p, text))

    # registry：MODES 加一条 + 一个 lazy 工厂
    reg = ROOT / "src" / "registry.py"
    reg_text = reg.read_text(encoding="utf-8")
    if f'"{mode}"' in reg_text:
        print(f"✗ registry.py 里已经有 mode「{mode}」了")
        return 1
    factory = (f'\n\ndef _runner_{prefix}(settings, cfg, ui):\n'
               f'    from .{prefix}_runner import {cls}Runner\n'
               f'    return {cls}Runner(settings, cfg, ui)\n')
    reg_new = reg_text.replace("\n\ndef _runner_default(", factory + "\n\ndef _runner_default(", 1)
    entry = (f'    "{mode}": ModeSpec(\n'
             f'        make_runner=_runner_{prefix},\n'
             f'        # TODO 要 Excel 模板就补 build_template=_template_{prefix}\n'
             f'    ),\n')
    reg_new = reg_new.replace("}\n\nDEFAULT_SPEC", entry + "}\n\nDEFAULT_SPEC", 1)
    if reg_new == reg_text:
        print("✗ registry.py 的结构和这个脚本对不上了，请手动加，并回来修脚本")
        return 1
    plan.append((reg, reg_new))

    # formcfg：BY_MODE 加一格
    fc = ROOT / "src" / "formcfg.py"
    fc_text = fc.read_text(encoding="utf-8")
    if f'"{mode}": {{' in fc_text:
        print(f"✗ formcfg.py 的 BY_MODE 里已经有「{mode}」了")
        return 1
    fc_new = fc_text.replace(
        "BY_MODE = {",
        'BY_MODE = {\n'
        f'    # 往 {name}.yaml 加了新顶层键，就登记进这一格\n'
        f'    "{mode}": {{\n'
        f'        "fields",\n'
        f'    }},', 1)
    if fc_new == fc_text:
        print("✗ formcfg.py 的结构和这个脚本对不上了")
        return 1
    plan.append((fc, fc_new))

    # docs/README.md 索引
    idx = ROOT / "docs" / "README.md"
    idx_text = idx.read_text(encoding="utf-8")
    marker = "## 其它配置类型\n\n| 文档 | 抓的是什么 |\n|---|---|\n"
    if marker in idx_text:
        plan.append((idx, idx_text.replace(
            marker,
            marker + f"| [{name}-配置项抓取.md]({name}-配置项抓取.md) | TODO 还没抓 |\n", 1)))
    else:
        print("⚠ docs/README.md 的「其它配置类型」表没找到，索引这一行请手动加")

    for p, _ in plan:
        print(("[试运行] 会写 " if dry else "已写 ") + str(p.relative_to(ROOT)))
    if dry:
        print("\n（--dry-run，什么都没动）")
        return 0
    for p, text in plan:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    print(f"""
接下来：

  1. python tools\\check_mode.py {name}
     现在应该全绿（只有几条 TODO 的提示）。不绿说明这个脚本和架构脱节了，
     修 tools\\new_mode.py，别手动绕过去。

  2. python tools\\capture.py --open "要抓的页面URL"
     人工登录、点到要抓的那一屏，然后：
     python tools\\capture.py --out docs\\{name}-配置项抓取.md

  3. 人工核对抓取记录 —— 控件真实类型、选项全集、联动，这三样脚本抓不准。
     联动用 --snap a / 改一个值 / --snap b --diff a，它决定 Excel 出哪些列。

  4. 往 yaml 填字段、往 src/{prefix}_filler.py 和 src/{prefix}_runner.py 填业务。
     ⚠ 先看一眼现有的 filler 能不能直接复用 —— DOM 栈相同就别新写。

  5. 要 Excel 模板的话，加 src/{prefix}_template.py（建在 src/xlsx_kit.py 上），
     并在 registry.py 里补 build_template。

  6. 发版前：CHANGELOG.md 加一节 + src/__init__.py 版本号。
""")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="新增一个配置类型：把接线部分铺好")
    ap.add_argument("name", help="配置类型名（会当成 config/forms/<名>.yaml 的文件名）")
    ap.add_argument("--prefix", required=True,
                    help="文件名前缀，小写英文（比如 coupon → src/coupon_runner.py）")
    ap.add_argument("--mode", help="mode 名，默认同 --prefix")
    ap.add_argument("--dry-run", action="store_true", help="只打印要动哪些文件")
    args = ap.parse_args()

    bad = _valid(args.name)
    if bad:
        print("✗ " + bad)
        return 1
    if not re.fullmatch(r"[a-z][a-z0-9_]*", args.prefix):
        print("✗ --prefix 只能是小写字母/数字/下划线，且以字母开头")
        return 1

    mode = args.mode or args.prefix
    cls = "".join(w.capitalize() for w in args.prefix.split("_"))
    return build(args.name, args.prefix, mode, cls, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
