"""选择器体检：把 form yaml 里声明的定位目标,挨个拿到当前页面上点一遍名,
看还找不找得到。发版后、批量跑之前先过一遍,别等跑到一半才发现后台改版了。

## 能查什么、不能查什么

**能**：yaml 里每个字段的 `selector`(CSS / role= / 纯 label 文字)、以及顶层的
`ready_selector` / `open_dialog` / `open_steps` / `submit_selector` 等,在当前页面上
命中几个元素、可不可见。命中 0 → 后台多半把这个字段/按钮的文字或结构改了。

**不能**：选项点不点得中、填进去生不生效、联动对不对 —— 这些只有实跑能验。
和 `tools/check_mode.py` 一样,它的定位是「实跑之前先过一遍」,不是「过了就不用跑」。

## 为什么不复用各 filler 的定位逻辑

四套 filler 的选择器一行都不能互抄(DOM 栈完全不同,见 CLAUDE.md)。体检要的是
一个**跨 mode 通用**的浅探针:label 文字在不在、按钮结构变没变 —— 这类最常见的
崩法不需要 filler 内部那套「缩到字段块再按选项文字点」的私货。
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# 一个「纯 label 文字」长这样:全是中文/英数/常见标点,不带任何选择器元字符。
# 命中这个的,当 label 用 get_by_text 找;否则当 CSS / Playwright 引擎选择器找。
_PLAIN_LABEL = re.compile(r'^[一-鿿0-9A-Za-z（）()·、\-_/ ]+$')
# 选择器元字符:出现任一个就肯定不是纯文字
_SELECTOR_CHARS = set('#.[]>=:"\'*~')


def _looks_like_label(target: str) -> bool:
    t = (target or "").strip()
    if not t:
        return False
    if any(c in _SELECTOR_CHARS for c in t):
        return False
    return bool(_PLAIN_LABEL.match(t))


def _probe_one(page, target: str, want_count: int | None = None) -> dict:
    """在当前页面上找 target,返回一条体检结果。

    want_count  期望命中几个(同名 label 会多次出现,yaml 里 label_index 暗示了这点);
                None = 只要 >=1 就算 ok,>1 记 ambiguous 但不算错。
    """
    target = (target or "").strip()
    kind = "label" if _looks_like_label(target) else "selector"
    out = {"target": target, "kind": kind, "count": 0, "visible": 0,
           "status": "error", "note": ""}
    try:
        loc = page.get_by_text(target, exact=True) if kind == "label" \
            else page.locator(target)
        n = loc.count()
        out["count"] = n
        vis = 0
        for i in range(min(n, 8)):          # 逐个查可见性,最多看前 8 个
            try:
                if loc.nth(i).is_visible():
                    vis += 1
            except Exception:
                pass
        out["visible"] = vis

        if n == 0:
            out["status"] = "missing"
            out["note"] = ("页面上没有这段文字 —— 多半是后台把它改了"
                           if kind == "label" else "选择器一个都没命中 —— 结构可能变了")
        elif want_count is not None and n != want_count:
            out["status"] = "ambiguous"
            out["note"] = f"命中 {n} 个,yaml 期望 {want_count} 个"
        elif vis == 0:
            out["status"] = "hidden"
            out["note"] = f"命中 {n} 个,但都不可见(可能还没展开/在别的 tab)"
        elif n > 1 and want_count is None:
            out["status"] = "ambiguous"
            out["note"] = f"命中 {n} 个(没写 label_index,填表时可能挑错)"
        else:
            out["status"] = "ok"
    except Exception as e:
        out["note"] = f"探测出错:{e}"
        log.debug("probe 出错 target=%r", target, exc_info=True)
    return out


def _iter_fields(cfg: dict):
    """yaml 的 fields + reveals 展开出来的子字段,一并吐出 (作用域, 字段)。"""
    for f in cfg.get("fields") or []:
        yield "主表", f
        for val, subs in (f.get("reveals") or {}).items():
            for s in subs or []:
                yield f"主表·选「{val}」后", s
    lst = cfg.get("list") or {}
    groups = list((lst.get("variants") or {}).values()) or [lst.get("fields") or []]
    for g in groups:
        for f in g:
            yield "明细", f


# 顶层那些「定位目标」键:值是单个选择器
_TOP_SINGLE = ("ready_selector", "open_dialog", "submit_selector", "cancel_selector",
               "success_selector", "open_button", "add_button")
# 值是选择器列表
_TOP_LIST = ("open_steps",)


def probe(cfg: dict, page, timeout_ms: int = 4000) -> dict:
    """对当前页面跑一遍体检。返回 {ok, checked, bad, rows:[...]}。

    ⚠ 不写死 sleep:命中计数 / 可见性判断都是即时的,不等渲染。真要等页面稳,
      调用方(webapp / CLI)自己在调 probe 之前把页面点到位。
    """
    try:
        page.set_default_timeout(min(timeout_ms, 4000))
    except Exception:
        pass

    rows: list[dict] = []

    # 1) 字段
    seen_labels: dict[str, int] = {}
    for scope, f in _iter_fields(cfg):
        target = str(f.get("selector") or f.get("label") or f.get("name") or "").strip()
        if not target:
            continue
        want = f.get("label_index")
        want_count = (want + 1) if isinstance(want, int) else None
        r = _probe_one(page, target, want_count)
        r["where"] = scope
        r["name"] = f.get("name") or target
        rows.append(r)

    # 2) 顶层定位键(按钮 / 弹窗判据)
    for key in _TOP_SINGLE:
        val = cfg.get(key)
        if isinstance(val, str) and val.strip():
            r = _probe_one(page, val)
            r["where"] = "顶层"
            r["name"] = key
            # 弹窗类判据:没打开弹窗时本来就该是 0,降级成提示不算错
            if r["status"] == "missing" and key in ("ready_selector", "success_selector"):
                r["status"] = "closed"
                r["note"] = "现在没命中 —— 若弹窗/提示当前没打开,这是正常的"
            rows.append(r)
    for key in _TOP_LIST:
        for i, val in enumerate(cfg.get(key) or []):
            if isinstance(val, str) and val.strip():
                r = _probe_one(page, val)
                r["where"] = "顶层"
                r["name"] = f"{key}[{i}]"
                rows.append(r)

    bad = [r for r in rows if r["status"] in ("missing", "ambiguous", "error")]
    return {
        "ok": not any(r["status"] in ("missing", "error") for r in rows),
        "checked": len(rows),
        "bad": len(bad),
        "rows": rows,
    }
