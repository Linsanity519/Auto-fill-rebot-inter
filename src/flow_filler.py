"""自制工作流（mode: flow）的 DOM 操作。建在 src/fill_core.py 上。

和别的 filler 不同：它不认识任何一套具体后台的 DOM，只按 flow json 里录下来的
**选择器候选（pick）** 去定位。候选按顺序试，命中哪个记哪个 —— 这是自制配置
唯一的「自愈」手段，也是它比手写 filler 脆的原因。

候选类型见 src/flow_data.py 顶部。
"""
from __future__ import annotations

import logging
import re

from .fill_core import FillError, norm, note, wait_until

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
    def _candidate_locator(self, cand: dict):
        """一个候选 → 一个 Playwright Locator（不保证存在）。认不出返回 None。"""
        if "text" in cand:
            v = str(cand["text"])
            # 可点的、文字等于 v 的元素（穿透子 span）
            return self.page.locator(
                "a,button,[role='button'],[role='link'],[role='tab'],[role='menuitem'],"
                "label,li,span,div"
            ).filter(has_text=re.compile(rf"^\s*{re.escape(v)}\s*$"))
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
                return true;
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
            loc = self._candidate_locator(cand)
            if loc is None:
                tried.append(f"{how}(认不出)")
                continue
            try:
                el = loc.first
                if want_visible:
                    if wait_until(self.page, lambda e=el: e.is_visible(), 2500):
                        return Resolved(el, how)
                elif el.count():
                    return Resolved(el, how)
            except Exception:
                pass
            tried.append(how)
        raise FillError(f"这一步的选择器都没命中（试过：{tried}）—— 页面可能变了，回录制页重录这步")

    # ------------------------------------------------ 动作
    def goto(self, url: str):
        self.page.goto(url, wait_until="domcontentloaded")

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

    def select(self, pick: list, value: str) -> str:
        r = self.resolve(pick)
        el = r.locator
        try:
            el.select_option(label=str(value))
            return r.how
        except Exception:
            pass
        # 不是原生 <select>：点开、按文字挑
        el.click()
        opt = self.page.get_by_text(re.compile(rf"^\s*{re.escape(str(value))}\s*$")).last
        if not wait_until(self.page, lambda: opt.count() > 0, self.timeout):
            raise FillError(f"下拉里没有「{value}」")
        opt.click()
        return r.how

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
