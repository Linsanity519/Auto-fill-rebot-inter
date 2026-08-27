"""src/formcfg.py 的场景测试。改那个文件（或往 yaml 里加了新顶层键）之后跑：

    python tools\\test_formcfg.py

不联网、不开浏览器。会读仓库里真实的 config/forms/*.yaml。
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import yaml   # noqa: E402

from src import formcfg as F   # noqa: E402

import logging  # noqa: E402
logging.disable(logging.CRITICAL)

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("　" + detail) if detail and not cond else ""))


def test_existing_all_clean():
    print("\n[存量 yaml 必须全过]")
    # ⚠ 词汇表是从这些 yaml 扫出来的，所以它们本来就该全过。
    #   这条测试真正防的是：以后往 yaml 里加了新顶层键、却忘了登记进 BY_MODE。
    for name, cfg in F.load_all():
        errs, warns = F.validate(cfg, name)
        bad = [w for w in warns if "不是" in w or "不在" in w]     # 只看键名那类提示
        ok(f"{name}　没有认不得的顶层键", not errs and not bad,
           f"{errs + bad}")


def test_typo():
    print("\n[打错一个字母]")
    cfg = dict(F.load("价格面板配置"))
    cfg["strategy_group"] = cfg.pop("strategy_groups")
    _, warns = F.validate(cfg, "价格面板配置")
    hit = [w for w in warns if "strategy_group" in w]
    ok("⚠ 抓得出 strategy_groups → strategy_group", bool(hit))
    ok("　　而且猜得出想写的是哪个", bool(hit) and "strategy_groups" in hit[0])


def test_no_false_positive():
    print("\n[不能误报]")
    # 这三对是**故意的**同名单复数，早先那版启发式对它们全误报过
    pairs = [("dmp_extension", "search_input_selector"),
             ("ab_extension", "search_input_selectors"),
             ("price_panel", "position"),
             ("price_panel", "positions")]
    for mode, key in pairs:
        ok(f"{mode} 认得 {key}", key in F.known_keys(mode))


def test_unknown_key():
    print("\n[压根没见过的键]")
    cfg = dict(F.load("DMP延期"))
    cfg["随便编一个"] = 1
    _, warns = F.validate(cfg, "DMP延期")
    ok("会提示要去 BY_MODE 登记", any("BY_MODE" in w for w in warns))


def test_anchor_keys():
    print("\n[YAML 锚点存放处]")
    cfg = dict(F.load("DMP延期"))
    cfg["_我随便放的锚点"] = ["a", "b"]
    _, warns = F.validate(cfg, "DMP延期")
    ok("_ 开头的一律放行", not any("锚点" in w and "不在" in w for w in warns))
    ok("老的中文名锚点已登记（改名要连着改引用，所以没动）",
       "搭售类型选项" in F.known_keys("price_panel"))


def test_name_mismatch():
    print("\n[name 和文件名对不上]")
    cfg = dict(F.load("DMP延期"))
    errs, _ = F.validate(cfg, "别的名字")
    ok("算错误不算提示（这个一定会串）", len(errs) == 1 and "对不上" in errs[0])


def test_cache():
    print("\n[缓存]")
    a = F.load("DMP延期")
    b = F.load("DMP延期")
    ok("同一次运行里读两次是同一个对象（不重复解析）", a is b)

    d = Path(tempfile.mkdtemp())
    try:
        f = d / "x.yaml"
        f.write_text("name: x\nmode: null\n", encoding="utf-8")
        F._cache.pop(str(f), None)

        # 直接打桩 path_for，验「改了文件就重读」这条
        orig = F.path_for
        F.path_for = lambda n: f
        try:
            first = F.load("x")
            time.sleep(0.01)
            f.write_text("name: x\nmode: null\ndescription: 改过了\n", encoding="utf-8")
            second = F.load("x")
            ok("⚠ 文件改了要重读（抓页面调选择器时一直靠这个）",
               second.get("description") == "改过了", f"读回来 {second}")
            ok("　　确实是新对象", first is not second)
        finally:
            F.path_for = orig
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_callers_use_it():
    print("\n[调用方都收敛到这儿了]")
    root = Path(__file__).resolve().parent.parent
    offenders = []
    for f in [root / "main.py", *(root / "src").glob("*.py")]:
        if f.name in ("formcfg.py", "settings.py", "usage.py"):
            continue          # 这几个读的是 settings.yaml，不是 form yaml
        text = f.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "yaml.safe_load" in line and "forms" in line:
                offenders.append(f"{f.name}: {line.strip()[:60]}")
    ok("没有绕过 formcfg 直接读 form yaml 的地方", not offenders, str(offenders))


def main():
    print("=" * 56)
    print("formcfg 场景测试")
    print("=" * 56)
    for fn in (test_existing_all_clean, test_typo, test_no_false_positive,
               test_unknown_key, test_anchor_keys, test_name_mismatch,
               test_cache, test_callers_use_it):
        fn()
    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗ " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
