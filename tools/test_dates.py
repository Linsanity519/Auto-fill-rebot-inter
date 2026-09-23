"""日期统一（datasource.norm_time + 准备页日历字段）的场景测试。

    python tools\test_dates.py

不联网、不碰浏览器。背景见 datasource.norm_time 的注释：校验宽（strptime 不挑
补没补 0）、页面严，两套尺子各活各的，结果就是「校验全过、一跑全炸」——
1.1.21 原生商广/常规商广整批失败就是这么来的。这里守的就是「只有一把尺子」。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src import ad_prep, formcfg, webapp        # noqa: E402
from src.datasource import norm_time            # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("　" + detail) if detail and not cond else ""))


def eq(name, got, want):
    ok(name, got == want, f"got={got!r} want={want!r}")


# ------------------------------------------------------------ norm_time
def test_norm_time():
    print("\n[norm_time：补 0 / 统一分隔符]")
    eq("月日补 0", norm_time("2026-9-4"), "2026-09-04")
    eq("斜杠换成横杠", norm_time("2026/9/4"), "2026-09-04")
    eq("已经标准的不动", norm_time("2026-09-24"), "2026-09-24")
    eq("带时分：补秒", norm_time("2026-9-4 9:00"), "2026-09-04 09:00:00")
    eq("带时分秒：小时补 0", norm_time("2026/9/4 9:00:00"), "2026-09-04 09:00:00")
    eq("T 分隔也认", norm_time("2026-9-4T09:00"), "2026-09-04 09:00:00")

    print("\n[norm_time：认不出来的原样退回，不替校验判对错]")
    eq("不是日期", norm_time("长期投放"), "长期投放")
    eq("空值", norm_time(""), "")
    eq("None", norm_time(None), "")
    eq("前后空格去掉", norm_time("  2026-9-4  "), "2026-09-04")
    eq("月份越界也原样退（由校验去报）", norm_time("2026-13-01"), "2026-13-01")

    print("\n[norm_time：⚠ 这条是 1.1.21 整批失败的原型]")
    ok("填之前收敛一次，回读的值就对得上了",
       norm_time("2026-9-24 - 2026-10-11".split(" - ")[0]) == "2026-09-24")


# ------------------------------------------------------------ 准备页日历字段
DATE_FORMS = ("原生商广", "原生商广新", "常规商广")


def test_prep_daterange():
    print("\n[准备页：三个商广的「投放起止时间」都是日历控件]")
    for n in DATE_FORMS:
        cfg = formcfg.load(n)
        f = [x for x in ad_prep.field_defs(cfg) if x["name"] == "投放起止时间"]
        if not f:
            ok(f"{n} 有「投放起止时间」", False)
            continue
        eq(f"{n} 是 daterange", f[0].get("type"), "daterange")
        eq(f"{n} 界面上长成日历", webapp._prep_kind(f[0]), "daterange")
        ok(f"{n} 默认值保留 {{今天}}", str(f[0].get("default", "")).startswith("{今天}"))


def _issue(cfg, val):
    got = ad_prep.validate(cfg, {"投放时间": "设置起止时间", "投放起止时间": val})
    return [i for i in got if "投放起止时间" in i]


def test_prep_validate():
    print("\n[准备页：存下来的坏值要在「载入并检查」时拦住]")
    cfg = formcfg.load("常规商广")
    ok("正常值放行", not _issue(cfg, "2026-09-24 - 2026-10-11"))
    ok("⚠ 老的手打值（没补 0）必须放行，不然存量准备页全报错",
       not _issue(cfg, "2026-9-24 - 2026-10-11"))
    ok("{今天} 放行", not _issue(cfg, "{今天} - 2026-12-31"))
    ok("结束早于开始：拦", _issue(cfg, "2026-10-11 - 2026-09-24"))
    ok("不是日期：拦", _issue(cfg, "下周一 - 2026-10-11"))
    ok("少一头：拦", _issue(cfg, "2026-09-24"))
    ok("月份越界：拦", _issue(cfg, "2026-13-01 - 2026-10-11"))


# ------------------------------------------------------------ Excel 侧
def test_excel_dates():
    print("\n[Excel：价格面板 / 资源位投放的日期列也走同一把尺子]")
    from src import pp_data, wizard_data
    ok("pp_data 用的是 norm_time", "DS.norm_time" in Path("src/pp_data.py").read_text(encoding="utf-8"))
    ok("wizard_data 用的是 norm_time",
       "DS.norm_time" in Path("src/wizard_data.py").read_text(encoding="utf-8"))
    ok("wizard 的时间校验先收敛再判",
       "val = DS.norm_time(val)" in Path("src/wizard_data.py").read_text(encoding="utf-8"))
    ok("_looks_like_time 认标准写法", wizard_data._looks_like_time("2026-09-04 09:00:00"))
    ok("pp 的 _TIME_RE 认标准写法", bool(pp_data._TIME_RE.match(norm_time("2026-9-4 9:00"))))


# ------------------------------------------------------------ 只有一把尺子
def test_single_source():
    print("\n[⚠ 别再各写各的：日期收敛只能有一份实现]")
    src = Path("src")
    copies = [p.name for p in src.glob("*.py")
              if "def _pad_dates" in p.read_text(encoding="utf-8")]
    ok("没有第二份 _pad_dates", not copies, f"还有：{copies}")
    ok("ad_filler 用的是共用的那份",
       "from .datasource import norm_time" in (src / "ad_filler.py").read_text(encoding="utf-8"))


def main():
    print("=" * 56)
    print("日期统一 场景测试")
    print("=" * 56)
    for fn in (test_norm_time, test_prep_daterange, test_prep_validate,
               test_excel_dates, test_single_source):
        fn()
    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗ " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
