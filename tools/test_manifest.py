"""src/manifest.py 的离线测试。不开浏览器。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src import manifest  # noqa: E402

_p = _f = 0


def ok(cond, msg):
    global _p, _f
    _p += cond
    _f += not cond
    print(("  ✓ " if cond else "  ✗ ") + msg)


class Row:
    def __init__(self, name, index=0):
        self.name = name
        self.index = index


print("\n[write：逐行配对 + counts]")
rows = [Row("人群A"), Row("人群B"), Row("人群C")]
res = [
    {"名称": "人群A", "状态": "ok", "策略": "策略1", "方向": "开启"},
    {"名称": "人群B", "状态": "failed", "错误": "点不到开关", "策略": "策略1", "方向": "开启"},
    {"名称": "人群C", "状态": "skipped", "错误": "已是目标状态", "策略": "策略1", "方向": "开启"},
]
p = manifest.write("wf_test1", "价格策略批量开关", "confirm", rows, res,
                   extra={"scope": "keyword"})
ok(p and Path(p).exists(), "清单文件写出来了")
doc = manifest.load(p)
ok(doc["counts"] == {"ok": 1, "failed": 1, "skipped": 1, "dry": 0}, f"counts 对：{doc['counts']}")
ok(doc["items"][0]["name"] == "人群A" and doc["items"][0]["策略"] == "策略1", "带上了策略字段")
ok(doc["items"][1]["note"] == "点不到开关", "失败项带上了错误说明")
ok(doc["scope"] == "keyword", "extra 合并进去了")
ok(doc["run_id"] == "wf_test1" and doc["form"] == "价格策略批量开关", "元信息对")

print("\n[write：行数对不上 → 只按 results]")
p2 = manifest.write("wf_test2", "价格策略批量开关", "auto", [Row("X")], res)
d2 = manifest.load(p2)
ok(len(d2["items"]) == 3 and d2["items"][0]["name"] == "人群A", "kept 和 results 对不上时按 results 走，名字从结果取")

print("\n[latest_for]")
ok(manifest.latest_for("价格策略批量开关") in (p, p2), "latest_for 找到最近一份")
ok(manifest.latest_for("根本没跑过的类型") == "", "没有就返回空串")

print("\n[dry_run 归一到 dry]")
p3 = manifest.write("wf_test3", "DMP延期", "confirm", None,
                    [{"名称": "n", "状态": "dry_run"}])
ok(manifest.load(p3)["counts"]["dry"] == 1, "状态 dry_run 计进 dry")

# 清理
for x in (p, p2, p3):
    try:
        Path(x).unlink()
    except OSError:
        pass

print("\n" + "=" * 48)
print(f"通过 {_p} 项，失败 {_f} 项")
sys.exit(1 if _f else 0)
