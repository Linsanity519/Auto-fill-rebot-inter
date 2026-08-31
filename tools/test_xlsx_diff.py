"""src/xlsx_diff.py + registry.expected_columns 的离线测试。不开浏览器。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from openpyxl import Workbook  # noqa: E402

from src import formcfg, registry, xlsx_diff  # noqa: E402

_p = _f = 0


def ok(cond, msg):
    global _p, _f
    _p += cond
    _f += not cond
    print(("  ✓ " if cond else "  ✗ ") + msg)


def _xlsx(sheet_cols: dict) -> str:
    wb = Workbook()
    wb.remove(wb.active)
    for name, cols in sheet_cols.items():
        ws = wb.create_sheet(name)
        ws.append(list(cols))
    p = os.path.join(tempfile.mkdtemp(), "t.xlsx")
    wb.save(p)
    return p


print("\n[expected_columns：每个 mode 都算得出（或明确 {}）]")
EXPECT_KEYS = {
    "价格配置": {"数据"}, "DMP延期": {"人群清单"}, "AB实验延期": {"实验清单"},
    "DMP人群新建": {"数据"}, "原生商广": {"素材"},
}
for name, keys in EXPECT_KEYS.items():
    cols = registry.expected_columns(formcfg.load(name), {})
    ok(set(cols) == keys and all(cols.values()), f"{name} → {sorted(cols)}")
for name in ("价格策略批量开关", "预定会议室"):
    ok(registry.expected_columns(formcfg.load(name), {}) == {}, f"{name} 不吃 Excel → {{}}")

print("\n[compare：缺列 / 多列 / 完全一致]")
exp = registry.expected_columns(formcfg.load("DMP延期"), {})   # {人群清单: [人群ID, 延期至, 人群名称]}
good = _xlsx({"人群清单": ["人群ID", "延期至", "人群名称"]})
d = xlsx_diff.compare(exp, good)
ok(d["ok"] and not xlsx_diff.summarize(d), "表头一模一样 → ok，summarize 为空")

bad = _xlsx({"人群清单": ["人群ID", "备注"]})
d = xlsx_diff.compare(exp, bad)
sec = d["sheets"]["人群清单"]
ok(not d["ok"], "缺列 → ok=False")
ok(set(sec["missing"]) == {"延期至", "人群名称"}, "missing 列对")
ok(sec["extra"] == ["备注"], "extra 列对")
ok("延期至" in xlsx_diff.summarize(d), "summarize 提到缺的列")

print("\n[compare：sheet 名对不上时按第一个 sheet 兜底]")
renamed = _xlsx({"Sheet1": ["人群ID", "延期至", "人群名称"]})
d = xlsx_diff.compare(exp, renamed)
ok(d["ok"], "sheet 名从「人群清单」改成「Sheet1」，仍按第一个 sheet 比 → ok")

print("\n[compare：空白 / 全角空格归一]")
sp = _xlsx({"人群清单": [" 人群ID ", "延期至", "人群名称　"]})
ok(xlsx_diff.compare(exp, sp)["ok"], "列名带首尾空白 / 全角空格 → 归一后仍算命中")

print("\n[compare：读不到文件不报错]")
ok(xlsx_diff.compare(exp, "N:/nope/none.xlsx")["ok"], "文件不存在 → ok=True（跳过，不误报）")
ok(xlsx_diff.compare({}, good)["ok"], "expected 为空 → ok=True")

print("\n" + "=" * 48)
print(f"通过 {_p} 项，失败 {_f} 项")
sys.exit(1 if _f else 0)
