"""自制工作流（mode: flow）的 DOM 操作。建在 src/fill_core.py 上。

和别的 filler 不同：它不认识任何一套具体后台的 DOM，只按 flow json 里录下来的
**选择器候选（pick）** 去定位。候选按顺序试，命中哪个记哪个 —— 这是自制配置
唯一的「自愈」手段，也是它比手写 filler 脆的原因。

候选类型见 src/flow_data.py 顶部。
"""
from __future__ import annotations

import logging
import re

from .fill_core import FillError, norm, note, wait_stable, wait_until

log = logging.getLogger(__name__)


class Resolved:
    __slots__ = ("locator", "how")

    def __init__(self, locator, how: str):
        self.locator = locator
        self.how = how          # 命中的是哪种候选，写进结果里方便 review


class FlowFiller:
    def __init__(self, page, timeout: int = 15000, on_note=None):
        self.page = page
        self.timeout = timeout
        self._on_note = on_note

    def _note(self, msg: str):
        note(self._on_note, msg)

    # ------------------------------------------------ 定位
    def _candidate_locators(self, cand: dict) -> list:
        """一个候选 → 一串 Playwright Locator，按「越准越靠前」排。认不出返回 []。

        text 候选拆成两条：先试**可点角色**（a/button/[role=button|tab|menuitem|option]）里
        文字精确相等的，再退到宽标签（label/li/span/div）。忙页面上 span/div 会把
        「文字在后代里」的祖先也算命中，可点角色优先能挡掉大半。
        """
        if "text" in cand:
            rx = re.compile(rf"^\s*{re.escape(str(cand['text']))}\s*$")
            return [
                self.page.locator(
                    "a,button,[role='button'],[role='link'],[role='tab'],"
                    "[role='menuitem'],[role='option'],[role='checkbox'],[role='radio']"
                ).filter(has_text=rx),
                self.page.locator("label,li,span,div,td,th").filter(has_text=rx),
            ]
        loc = self._one_locator(cand)
        return [loc] if loc is not None else []

    def _one_locator(self, cand: dict):
        """非 text 候选 → 单个 Locator（不保证存在）。认不出返回 None。"""
        if "role" in cand:
            try:
                return self.page.get_by_role(cand["role"], name=cand.get("name"))
            except Exception:
                return None
        if "label" in cand:
            return self._by_label(str(cand["label"]))
        if "attr" in cand:
            raw = str(cand["attr"])
            if "=" in raw:
                k, val = raw.split("=", 1)
                return self.page.locator(f"[{k.strip()}='{val.strip()}']")
            return self.page.locator(f"[{raw.strip()}]")
        if "css" in cand:
            try:
                return self.page.locator(str(cand["css"]))
            except Exception:
                return None
        return None

    def _by_label(self, text: str):
        """label 文字 → 它管的字段块里的可交互控件。

        和 tools/capture.py 一个路子：找到文字等于 text 的 label / 首子节点，
        往上一两层拿到字段块，取块里第一个 input/select/textarea/[contenteditable]。
        """
        js = r"""
        (labelText) => {
          const clean = s => (s || '').replace(/\s+/g, ' ').trim();
          // ⚠ 先把上一步留下的标记全清掉 —— 不清的话它会越攒越多，
          //   [data-flow-hit='1'] 就同时命中好几个（就是日志里「label 匹配到 3 个」的成因）。
          document.querySelectorAll("[data-flow-hit]").forEach(e => e.removeAttribute('data-flow-hit'));
          const nodes = [...document.querySelectorAll('label, .ant-form-item-label, span, div, p')];
          for (const lb of nodes) {
            if (clean(lb.textContent) !== labelText) continue;
            if (lb.querySelector('input,select,textarea')) continue;   // 它自己不是块
            let scope = lb.parentElement;
            for (let up = 0; up < 3 && scope; up++) {
              const ctl = scope.querySelector(
                "input:not([type=hidden]),select,textarea,[contenteditable='true']," +
                "[role='combobox'],[role='textbox'],[role='switch']");
              if (ctl) {
                ctl.setAttribute('data-flow-hit', '1');
                return true;   // 只标第一个匹配的 label 下的第一个控件
              }
              scope = scope.parentElement;
            }
          }
          return false;
        }
        """
        try:
            if self.page.evaluate(js, text):
                loc = self.page.locator("[data-flow-hit='1']")
                return loc
        except Exception:
            pass
        return self.page.locator("__flow_nomatch__")   # 空定位器

    def resolve(self, pick: list, want_visible: bool = True) -> Resolved:
        """按顺序试候选，第一个能定位到（且可见）的算数。"""
        if not pick:
            raise FillError("这一步没有选择器")
        tried = []
        for cand in pick:
            how = next((k for k in ("text", "role", "label", "attr", "css") if k in cand), "?")
            locs = self._candidate_locators(cand)
            if not locs:
                tried.append(f"{how}(认不出)")
                continue
            for li, loc in enumerate(locs):
                try:
                    el = loc.first
                    if want_visible:
                        if not wait_until(self.page, lambda e=el: e.is_visible(), 2500):
                            continue
                    elif not el.count():
                        continue
                except Exception:
                    continue
                # 命中多个：取第一个，但要让用户知道 —— 忙页面上「文字相等」很容易撞。
                try:
                    n = loc.count()
                    if n > 1:
                        self._note(f"选择器（{how}）在页面上匹配到 {n} 个，用的是第 1 个；"
                                   f"跑错了就回录制页给这步补个更准的选择器")
                except Exception:
                    pass
                return Resolved(el, how if li == 0 else how + "*")
            tried.append(how)
        raise FillError(f"这一步的选择器都没命中（试过：{tried}）—— 页面可能变了，回录制页重录这步")

    # ------------------------------------------------ 逐步试跑：给要操作的元素描一圈
    def highlight(self, pick: list, on: bool = True):
        """逐步试跑时在页面上标出这一步要碰的元素。best-effort，任何异常都不抛。"""
        if not pick:
            return
        try:
            r = self.resolve(pick, want_visible=False)
        except FillError:
            return
        js_on = ("e => { e.__fo = e.style.outline; e.__oo = e.style.outlineOffset;"
                 " e.style.outline = '3px solid #e34d82'; e.style.outlineOffset = '2px';"
                 " try { e.scrollIntoView({block:'center'}); } catch(_){} }")
        js_off = ("e => { if (e.__fo !== undefined) e.style.outline = e.__fo;"
                  " if (e.__oo !== undefined) e.style.outlineOffset = e.__oo; }")
        try:
            r.locator.evaluate(js_on if on else js_off)
        except Exception:
            pass

    # ------------------------------------------------ 动作
    def settle(self, hard: bool = False):
        """一个动作之后：等页面别再动。录制是「点了就下一步」，重放得等渲染跟上，
        不然下一步的选择器十有八九「还没出现」。全都有上限，超时就往下走。
        """
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        if hard:
            try:
                self.page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass
        try:
            wait_stable(self.page,
                        lambda: self.page.evaluate(
                            "document.body ? document.body.getElementsByTagName('*').length : 0"),
                        quiet_ms=400, timeout=2500)
        except Exception:
            pass

    def goto(self, url: str):
        self.page.goto(url, wait_until="domcontentloaded")
        self.settle(hard=True)

    def click(self, pick: list) -> str:
        r = self.resolve(pick)
        try:
            r.locator.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        r.locator.click()
        return r.how

    def fill(self, pick: list, value: str) -> str:
        r = self.resolve(pick)
        el = r.locator
        try:
            el.fill("")
            el.fill(str(value))
        except Exception:
            el.click()
            el.type(str(value), delay=15)
        got = ""
        try:
            got = norm(el.input_value())
        except Exception:
            got = norm(value)      # contenteditable 之类读不到 input_value，别卡
        if got and norm(value) not in got and got not in norm(value):
            self._note(f"填「{value}」之后读到的是「{got}」")
        return r.how

    # ------------------------------------------------ 语义定位：先 pick，不行再按 field 文字
    def _control(self, pick: list, field: str) -> Resolved:
        """定位一个「控件」。pick 命中就用 pick；没命中就按 field（那个字段/区块的
        label 文字）去找它管的控件。重放时后台改版，pick 常失效，field 文字一般还在。"""
        if pick:
            try:
                return self.resolve(pick, want_visible=True)
            except FillError:
                pass
        if field:
            loc = self._by_label(str(field))
            if loc.count():
                return Resolved(loc.first, "field")
            loc2 = self._field_control(str(field))
            if loc2 is not None and loc2.count():
                return Resolved(loc2.first, "field")
        raise FillError(f"这一步既定位不到选择器，也找不到字段「{field or '(未记)'}」—— 回录制页重录这步")

    def _field_control(self, text: str):
        """label 文字 → 它那一块里「能点开的控件」（下拉触发器 / combobox / 按钮 / 输入框）。"""
        js = r"""
        (labelText) => {
          const clean = s => (s || '').replace(/\s+/g, ' ').trim();
          document.querySelectorAll('[data-flow-ctl]').forEach(e => e.removeAttribute('data-flow-ctl'));
          const nodes = [...document.querySelectorAll("label,.ant-form-item-label,[class*='label'],[class*='Label'],dt,th,span,div,p")];
          for (const lb of nodes) {
            if (clean(lb.textContent) !== labelText) continue;
            if (lb.querySelector("input,select,textarea,[role='combobox']")) continue;
            let scope = lb.parentElement;
            for (let up = 0; up < 4 && scope; up++) {
              const c = scope.querySelector(
                "select,[role='combobox'],[class*='selector'],[class*='select-selection']," +
                "[class*='picker'],input:not([type=hidden]),[role='button'],button");
              if (c) { c.setAttribute('data-flow-ctl', '1'); return true; }
              scope = scope.parentElement;
            }
          }
          return false;
        }
        """
        try:
            if self.page.evaluate(js, text):
                return self.page.locator("[data-flow-ctl='1']")
        except Exception:
            pass
        return None

    def _pick_option(self, value: str, where=None):
        """在浮层 / 页面里点文字匹配 value 的那一项。先精确、再退到包含（远程搜索结果常带后缀）。"""
        v = str(value)
        root = where or self.page
        exact = root.get_by_text(re.compile(rf"^\s*{re.escape(v)}\s*$"))
        if wait_until(self.page, lambda: exact.count() > 0, self.timeout):
            exact.last.click()
            return
        loose = root.get_by_text(v, exact=False)
        if wait_until(self.page, lambda: loose.count() > 0, 3000):
            loose.first.click()
            return
        raise FillError(f"列表里没有「{value}」")

    def select(self, pick: list, value: str, field: str = "") -> str:
        r = self._control(pick, field)
        el = r.locator
        try:
            el.select_option(label=str(value))      # 原生 <select>
            return r.how
        except Exception:
            pass
        try:
            el.click()
        except Exception:
            try:
                el.evaluate("e => e.click()")
            except Exception:
                pass
        self.page.wait_for_timeout(150)
        self._pick_option(value)
        return r.how

    def search_pick(self, pick: list, query: str, value: str, field: str = "") -> str:
        """搜索框打字 + 从结果里挑。query 只为触发（远程）搜索，真正的目标是 value。"""
        r = self._control(pick, field)
        el = r.locator
        try:
            el.click()
        except Exception:
            pass
        box = el
        try:
            if el.evaluate("e => e.tagName") not in ("INPUT", "TEXTAREA"):
                inner = el.locator("input, textarea").first
                if inner.count():
                    box = inner
        except Exception:
            pass
        typed = str(query or value)
        try:
            box.fill("")
            box.type(typed, delay=20)
        except Exception:
            box.click()
            self.page.keyboard.type(typed, delay=20)
        self.page.wait_for_timeout(450)
        self._pick_option(value)
        return r.how

    def pick_item(self, pick: list, value: str, field: str = "") -> str:
        """点结果表格 / 列表里「文字是 value 的那一行」。"""
        v = str(value)
        scope = self.page
        try:
            scope = self._control(pick, field).locator
        except FillError:
            pass
        row = scope.locator("tr, li, [role='row'], [class*='item']").filter(
            has_text=re.compile(re.escape(v)))
        if not wait_until(self.page, lambda: row.count() > 0, self.timeout):
            raise FillError(f"列表里没有「{v}」这一行")
        target = row.first
        try:
            target.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        target.click()
        return "pick_item"

    def press(self, key: str, pick: list | None = None):
        if pick:
            self.resolve(pick).locator.press(key)
        else:
            self.page.keyboard.press(key)

    def wait_for(self, pick: list, timeout: int | None = None):
        t = timeout or self.timeout
        try:
            self.resolve(pick)
            return
        except FillError:
            pass
        if not wait_until(self.page,
                          lambda: self._safe_resolve(pick), t):
            raise FillError("等的元素一直没出现")

    def _safe_resolve(self, pick):
        try:
            self.resolve(pick)
            return True
        except FillError:
            return False

    def wait_text(self, text: str, timeout: int | None = None):
        t = timeout or self.timeout
        loc = self.page.get_by_text(str(text), exact=False)
        if not wait_until(self.page, lambda: loc.count() > 0, t):
            raise FillError(f"等「{text}」出现，等了 {t // 1000}s 没等到")

    def do_assert(self, step: dict):
        if step.get("text"):
            loc = self.page.get_by_text(str(step["text"]), exact=False)
            if not wait_until(self.page, lambda: loc.count() > 0, self.timeout):
                raise FillError(f"校验失败：页面上没有「{step['text']}」")
        if step.get("gone"):
            if not wait_until(self.page, lambda: not self._safe_resolve([step["gone"]]),
                              self.timeout):
                raise FillError("校验失败：本该消失的元素还在")
        if step.get("url_matches"):
            pat = str(step["url_matches"])
            if not wait_until(self.page, lambda: re.search(pat, self.page.url), self.timeout):
                raise FillError(f"校验失败：URL 不匹配 {pat}（当前 {self.page.url}）")
