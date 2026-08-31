"""src/health.py 的离线测试：不开浏览器，用一个假 page 喂给 probe()。

选择器体检的价值全在「label 文字 / css 选择器 分得清、命中数判断对」——
这些逻辑不碰真实 DOM 也能验。真页面那层由 tools/test_filler_locate.py（需要
playwright 二进制，没有就整体 skip）补。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src import health  # noqa: E402

_p = _f = 0


def ok(cond, msg):
    global _p, _f
    if cond:
        _p += 1
        print(f"  ✓ {msg}")
    else:
        _f += 1
        print(f"  ✗ {msg}")


# ---------------- 假 page ----------------
class _Loc:
    def __init__(self, n, visible=None):
        self._n = n
        self._vis = [True] * n if visible is None else list(visible)

    def count(self):
        return self._n

    def nth(self, i):
        return self

    def is_visible(self):
        # nth(i) 返回 self，用一个游标模拟逐个可见性
        v = self._vis[self._cursor] if getattr(self, "_cursor", 0) < len(self._vis) else False
        self._cursor = getattr(self, "_cursor", 0) + 1
        return v


class FakePage:
    """按预设返回命中数。by_text[文字] / by_sel[选择器] -> (count, [visible...])。"""
    def __init__(self, by_text=None, by_sel=None):
        self.by_text = by_text or {}
        self.by_sel = by_sel or {}

    def set_default_timeout(self, _):
        pass

    def get_by_text(self, text, exact=True):
        n, vis = self.by_text.get(text, (0, []))
        loc = _Loc(n, vis or None)
        loc._cursor = 0
        return loc

    def locator(self, sel):
        n, vis = self.by_sel.get(sel, (0, []))
        loc = _Loc(n, vis or None)
        loc._cursor = 0
        return loc


# ---------------- 测：label vs 选择器 分类 ----------------
print("\n[label 还是选择器]")
ok(health._looks_like_label("平台"), "纯中文 → label")
ok(health._looks_like_label("推广形式"), "纯中文 → label")
ok(not health._looks_like_label("#crowd_name"), "带 # → 选择器")
ok(not health._looks_like_label('role=button[name="新建"]'), "带 =[\" → 选择器")
ok(not health._looks_like_label(".ant-modal-footer .ant-btn-primary"), "带 . 和空格 → 选择器")
ok(not health._looks_like_label('button:has-text("新建人群")'), "带 :() → 选择器")

# ---------------- 测：probe 命中判断 ----------------
print("\n[probe 命中判断]")
cfg = {
    "fields": [
        {"name": "人群名称", "selector": "#crowd_name"},
        {"name": "平台", "selector": "平台", "type": "checkbox_sync"},
        {"name": "限制类型", "selector": "#intervene_type",
         "reveals": {"兜底定价": [{"name": "价格", "selector": "#sku_price"}]}},
    ],
    "ready_selector": ".ant-modal-wrap",
    "submit_selector": ".ant-modal-footer .ant-btn-primary",
}
page = FakePage(
    by_text={"平台": (1, [True])},
    by_sel={
        "#crowd_name": (1, [True]),
        "#intervene_type": (1, [True]),
        "#sku_price": (0, []),
        ".ant-modal-wrap": (1, [True]),
        ".ant-modal-footer .ant-btn-primary": (1, [True]),
    },
)
res = health.probe(cfg, page)
by_name = {r["name"]: r for r in res["rows"]}
ok(by_name["人群名称"]["status"] == "ok", "#crowd_name 命中 1 → ok")
ok(by_name["平台"]["status"] == "ok", "label「平台」命中 1 → ok")
ok(by_name["价格"]["status"] == "missing", "reveals 里 #sku_price 命中 0 → missing")
ok(by_name["ready_selector"]["status"] == "ok", "顶层 ready_selector 命中 → ok")
ok(res["checked"] == len(res["rows"]) and res["checked"] >= 5, "checked 计数对得上")
ok(res["ok"] is False, "有一个 missing → 整体 ok=False")

# 弹窗判据当前没打开 → closed 而不是 missing
page2 = FakePage(by_text={"平台": (1, [True])}, by_sel={
    "#crowd_name": (1, [True]), "#intervene_type": (1, [True]), "#sku_price": (1, [True]),
    ".ant-modal-wrap": (0, []), ".ant-modal-footer .ant-btn-primary": (1, [True]),
})
res2 = health.probe(cfg, page2)
bn2 = {r["name"]: r for r in res2["rows"]}
ok(bn2["ready_selector"]["status"] == "closed", "ready_selector 命中 0 → closed（不算硬失败）")
ok(res2["ok"] is True, "只是弹窗没开 → 整体 ok=True")

# ambiguous：命中多个又没 label_index
print("\n[ambiguous]")
cfg3 = {"fields": [{"name": "人群标签", "selector": "人群标签"}]}
page3 = FakePage(by_text={"人群标签": (3, [True, True, True])})
r3 = health.probe(cfg3, page3)["rows"][0]
ok(r3["status"] == "ambiguous" and r3["count"] == 3, "命中 3 个、没 label_index → ambiguous")

cfg4 = {"fields": [{"name": "人群标签", "selector": "人群标签", "label_index": 2}]}
r4 = health.probe(cfg4, FakePage(by_text={"人群标签": (3, [True, True, True])}))["rows"][0]
ok(r4["status"] == "ok", "label_index=2 期望命中 3 个 → ok")

print("\n" + "=" * 48)
print(f"通过 {_p} 项，失败 {_f} 项")
sys.exit(1 if _f else 0)
