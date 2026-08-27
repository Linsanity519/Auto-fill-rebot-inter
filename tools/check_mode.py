"""配置类型的离线自检：不开浏览器，把「接线接错了」这一类问题当场查出来。

    python tools\\check_mode.py                  查全部
    python tools\\check_mode.py 价格面板配置       只查一个
    python tools\\check_mode.py 价格面板配置 --data data\\xxx.xlsx   连数据一起查

## 为什么值得有这个

新接一个配置类型，最贵的循环不是写代码，是**跑一轮才发现接错了**：
开浏览器 → 登录 → 点到那一页 → 跑 → 看截图 → 发现是 yaml 少写了一个键。
一轮几分钟起步。而这一类问题（yaml 键名写错、registry 忘了加、caps 和预期不符、
策略/准备页的字段定义解析不出来）**全都不需要浏览器就能查**。

⚠ 它查的是**接线**，查不了 DOM 对不对 —— 选择器准不准只有实跑能验。
  所以它的定位是「实跑之前先过一遍」，不是「过了就不用跑」。

## 退出码

全过 0；有 ✗ 返回 1（可以挂到 CI 上）。⚠ 只提示，不影响退出码。
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import yaml                                     # noqa: E402

from src import registry                        # noqa: E402
from src.paths import user_path                 # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FORMS = user_path("config", "forms")
DOCS = ROOT / "docs"

OK, WARN, BAD = "✓", "⚠", "✗"


class Report:
    def __init__(self, title: str):
        self.title = title
        self.rows: list[tuple[str, str]] = []

    def ok(self, msg):
        self.rows.append((OK, msg))

    def warn(self, msg):
        self.rows.append((WARN, msg))

    def bad(self, msg):
        self.rows.append((BAD, msg))

    def check(self, label: str, fn, fatal: bool = True):
        """跑一段可能抛异常的检查。抛了就按 label 记一条失败，不让整个脚本崩。"""
        try:
            extra = fn()
            self.ok(label + (f"　{extra}" if extra else ""))
            return True
        except Exception as e:
            line = f"{label} —— {type(e).__name__}: {e}"
            self.bad(line) if fatal else self.warn(line)
            if "-v" in sys.argv:
                traceback.print_exc()
            return False

    @property
    def failed(self) -> int:
        return sum(1 for m, _ in self.rows if m == BAD)

    def print(self):
        print(f"\n{'─' * 60}\n{self.title}\n{'─' * 60}")
        for mark, msg in self.rows:
            print(f"  {mark} {msg}")


def _known_keys(exclude: Path) -> set:
    """别的 yaml 用过的顶层键，当作「已知词汇表」。"""
    known = set()
    for q in FORMS.glob("*.yaml"):
        if q == exclude:
            continue
        try:
            d = yaml.safe_load(q.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        known |= set(d.keys())
    return known


def _edit1(a: str, b: str) -> bool:
    """a 和 b 差一步以内（改一个字符 / 多一个 / 少一个）。"""
    if a == b:
        return False
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    lo, hi = (a, b) if len(a) < len(b) else (b, a)
    for i in range(len(hi)):
        if hi[:i] + hi[i + 1:] == lo:
            return True
    return False


def _check_typos(r: Report, cfg: dict, path: Path):
    """顶层键名打错一个字母是**静默**的：yaml 照样解析，功能悄悄少一半。

    ⚠ 这一条是拿一个故意写坏的 yaml 试出来的：把 strategy_groups 打成
      strategy_group，策略中心的字段数从 24 掉到 6，其它检查全绿。

    ⚠ **这是启发式，有已知误报，所以默认不跑，要加 --typos。**
      词汇表是「别的 yaml 用过的顶层键」，于是只在一份 yaml 里出现的键会被当成可疑：
        DMP延期 的 search_input_selector ⇄ AB实验延期 的 search_input_selectors
        价格面板配置 的 position ⇄ positions
      这三对都是**故意的**，不是打错。光看名字分不出「打错了」和「就是个新键」——
      真正的解法是给 form yaml 定一份 schema（架构优化方向 ②），那时这个函数就该退休。
      在**写一份新 yaml** 的时候手动跑一次最划算：那会儿所有键都还热乎，误报好认。
    """
    known = _known_keys(path)
    suspects = []
    for k in cfg:
        if k in known:
            continue
        near = [x for x in known if _edit1(str(k), str(x))]
        if near:
            suspects.append(f"「{k}」是不是想写 {near[:2]}")
    if suspects:
        r.warn("顶层键名可能打错了：" + "；".join(suspects)
               + "　← yaml 照样解析，功能会悄悄少一半，没有报错")
    else:
        r.ok("顶层键名没有和已知键差一个字母的")


# ------------------------------------------------------------ 单个配置类型
def check_form(path: Path, data_file: str | None = None) -> Report:
    r = Report(f"{path.stem}　（{path.name}）")

    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        r.bad(f"yaml 解析不了：{e}")
        return r
    r.ok("yaml 能解析")

    # --- 基本键 ---
    if cfg.get("name") != path.stem:
        r.bad(f"yaml 里的 name 是「{cfg.get('name')}」，和文件名「{path.stem}」对不上"
              "　← 界面按文件名选，runner 按 name 报，两边必须一致")
    else:
        r.ok("name 和文件名一致")

    if not cfg.get("description"):
        r.warn("没写 description　← 首页当功能导航用的就是这句话")
    if not cfg.get("nav"):
        r.warn("没写 nav　← 侧栏会把它扔进「其他」组")

    if "--typos" in sys.argv:
        _check_typos(r, cfg, path)

    # --- registry 接线 ---
    mode = cfg.get("mode")
    spec = registry.spec_for(mode)
    if mode is None:
        r.warn("没声明 mode　← 会走默认的 Runner + 通用模板（老配置就是这样，新接的一般要写）")
    elif mode not in registry.MODES:
        r.bad(f"mode「{mode}」不在 registry.MODES 里　← 加一条 MODES[\"{mode}\"]，"
              "不然会静默落到默认 Runner 上")
    else:
        r.ok(f"mode = {mode}，registry 里有")

    r.check("执行器造得出来", lambda: type(spec.make_runner(_fake_settings(), cfg, None)).__name__)

    # --- Excel 模板出口 ---
    if spec.build_template is None and mode in registry.MODES:
        hint = spec.no_template_hint or ""
        if hint:
            r.ok("不出 Excel 模板，界面上有话说清楚（no_template_hint）")
        else:
            r.warn("build_template 是 None，也没写 no_template_hint"
                   "　← 要么由 webapp/main 的特例分支接管了（资源位投放就是），"
                   "要么用户点「生成模板」会看到一句干巴巴的默认提示。确认一下是哪种")
    elif spec.build_template is not None:
        r.ok("有 Excel 模板生成器")
    else:
        r.ok("走通用模板生成器")

    # --- 界面能力（这是新接类型最容易和预期不符的一块）---
    from src.webapp import Api
    caps = ui = None

    def _caps():
        nonlocal caps, ui
        caps = Api._caps(cfg)
        ui = Api._ui_text(cfg, caps)
        on = [k for k, v in caps.items() if v] or ["（一个都没有）"]
        return "开着的：" + "、".join(on)

    r.check("界面能力算得出来", _caps)
    if caps:
        r.ok(f"跑法 run_kind = {ui['run_kind']}　投放页 Tab 叫「{ui['deliver_label']}」")

        # 声明了能力，就把对应的定义真解析一遍 —— 光有键、解析不了照样白搭
        if caps["strategy"]:
            from src import wizard_strategy as S
            r.check("策略中心的字段定义解析得了",
                    lambda: f"{len(S.field_defs_for_ui(cfg))} 个字段、"
                            f"{len(S.group_defs_for_ui(cfg))} 个方案组")
        if caps["prep"]:
            from src import ad_prep as P
            r.check("准备页的字段定义解析得了", lambda: f"{len(P.field_defs(cfg))} 项")
        if caps["positions"]:
            from src import wizard_schema as W
            r.check("资源位清单解析得了", lambda: f"{len(W.position_names(cfg))} 个资源位")

    # --- 抓取记录 ---
    # 抓取记录的文件名没有强制规范（DMP延期 那份就叫「DMP人群延期-页面结构.md」），
    # 所以按文件名和 README 索引两头找，都找不到才提示 —— 而且只提示，不算失败。
    idx = DOCS / "README.md"
    idx_text = idx.read_text(encoding="utf-8") if idx.exists() else ""
    hits = sorted(DOCS.glob(f"{path.stem}*.md"))
    if hits:
        r.ok("有抓取记录：" + "、".join(h.name for h in hits))
        if idx_text and path.stem not in idx_text:
            r.warn("抓取记录没进 docs/README.md 的索引")
    elif path.stem in idx_text:
        r.ok("docs/README.md 索引里提到了它（文件名和配置类型名不一样）")
    else:
        r.warn(f"docs/ 下找不到明显对应的抓取记录（找的是 {path.stem}*.md 和 README 索引）"
               "　← 抓取记录是改 yaml 的依据，凭截图猜字段翻过车，见 docs/README.md 的「教训」")

    # --- 可选：连数据一起查（走 runner.preview()，和界面上「载入并检查」同一条路）---
    if data_file:
        _check_data(r, cfg, spec, data_file)

    return r


def _check_data(r: Report, cfg: dict, spec, data_file: str):
    s = _fake_settings()
    s["data_file"] = data_file
    if not Path(data_file).exists():
        r.bad(f"数据文件不存在：{data_file}")
        return
    try:
        rows = spec.make_runner(s, cfg, None).preview()
    except Exception as e:
        txt = str(e)
        # dmp / ab 的「数据」本来就在网页上，preview() 必须连浏览器 —— 那不是错
        if "连不上 Chrome" in txt or "cdp" in txt.lower():
            r.warn("这个配置类型的预检要连浏览器（数据在网页上，不在 Excel 里），离线查不了")
        else:
            r.bad(f"载入并检查跑不通 —— {type(e).__name__}: {e}")
            if "-v" in sys.argv:
                traceback.print_exc()
        return
    bad = [x for x in rows if x.issues]
    r.ok(f"载入并检查跑通了：{len(rows)} 条，其中 {len(bad)} 条有问题")
    for x in bad[:5]:
        r.warn(f"　　第 {x.index} 条「{x.name}」：{'；'.join(x.issues[:2])}")
    if len(bad) > 5:
        r.warn(f"　　……另有 {len(bad) - 5} 条有问题，跑一次界面上的「载入并检查」看全")


def _fake_settings() -> dict:
    """够 runner 造出来就行。这里一律不碰网络、不写用户目录。"""
    return {
        "cdp_url": "http://127.0.0.1:9222",
        "timeout": 15000,
        "screenshot_dir": str(user_path("output", "screenshots")),
        "state_file": str(user_path("output", "state.json")),
        "result_file": str(user_path("output", "result.csv")),
        "data_file": "",
        "dry_run": True,
        "resume": True,
    }


# ------------------------------------------------------------ 全局检查
def check_global() -> Report:
    r = Report("全局")

    def stub_fresh():
        from tools import gen_stub_forms as G
        want = G.render(G.build_items())
        text = G.APP_JS.read_text(encoding="utf-8")
        start = text.index(G.BEGIN) + len(G.BEGIN)
        end = text.index(G.END, start)
        if text[start:end].strip() != want.strip():
            raise AssertionError(
                "app.js 里的 STUB_FORMS 过期了，跑一次 python tools\\gen_stub_forms.py"
                "　← 它是「不启动 pywebview 也能核对布局」的依据，走样了这件事就不成立")
        return f"{len(G.build_items())} 个配置类型"

    r.check("前端假数据（STUB_FORMS）是最新的", stub_fresh)

    def no_mode_names():
        text = (ROOT / "assets" / "webui" / "app.js").read_text(encoding="utf-8")
        import re
        hits = [m.group(0) for m in re.finditer(r'modeIs\(\s*"[^"]+"\s*\)', text)
                if "//" not in text[max(0, text.rindex("\n", 0, m.start())):m.start()]]
        if hits:
            raise AssertionError(
                f"app.js 里又出现了按 mode 名判断：{hits[:3]}"
                "　← 界面能力一律走 caps（见 webapp.Api._caps），别再列 mode 清单")
        return "没有 modeIs(...)"

    r.check("app.js 没有按 mode 名硬编码的能力判断", no_mode_names)

    def caps_no_mode_names():
        import inspect

        from src.webapp import Api
        src = inspect.getsource(Api._caps)
        bad = [m for m in registry.MODES if f'"{m}"' in src or f"'{m}'" in src]
        if bad:
            raise AssertionError(f"_caps() 里出现了 mode 名：{bad}"
                                 "　← 该在 yaml 里补一个声明，不是在这里加分支")
        return "干净"

    r.check("_caps() 里没有 mode 名", caps_no_mode_names)
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description="配置类型的离线自检（不开浏览器）")
    ap.add_argument("form", nargs="?", help="只查这一个；不给就查全部")
    ap.add_argument("--data", help="连数据文件一起查（走和「载入并检查」同一条路）")
    ap.add_argument("--typos", action="store_true",
                    help="顺带查顶层键名是不是打错了（启发式，有已知误报，写新 yaml 时用）")
    ap.add_argument("-v", action="store_true", help="出错时打完整调用栈")
    args = ap.parse_args()

    paths = sorted(FORMS.glob("*.yaml"))
    if args.form:
        paths = [p for p in paths if p.stem == args.form]
        if not paths:
            print(f"没有这个配置类型：{args.form}\n有的是：" +
                  "、".join(p.stem for p in sorted(FORMS.glob("*.yaml"))))
            return 1

    reports = [check_form(p, args.data) for p in paths]
    if not args.form:
        reports.append(check_global())

    for rep in reports:
        rep.print()

    failed = sum(rep.failed for rep in reports)
    warns = sum(1 for rep in reports for m, _ in rep.rows if m == WARN)
    print(f"\n{'═' * 60}")
    print(f"查了 {len(paths)} 个配置类型：失败 {failed} 项，提示 {warns} 项")
    if failed:
        print("失败项必须修 —— 这些是跑起来一定会出问题的接线错误。")
    print("⚠ 这里全过 ≠ 能跑通：选择器准不准只有实跑能验。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
