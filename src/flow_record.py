"""录制器：挂到已登录的 Chrome，把运营的操作拼成 flow_data 的步骤图。

## 怎么工作

Playwright 的 `expose_binding` + `add_init_script` 走的是 CDP，**绕过页面 CSP** ——
所以内网后台再严的 script-src 也能注进去。往页面装：

  · 捕获阶段的 click / change / keydown 监听，每个元素即时算出**多套选择器候选**
    （见 flow_data 顶部：text / role / label / attr / css，稳→脆）
  · 一个右下角浮动工具条（暂停 / 在这停一下 / 完成），SPA 重渲染会自动补回来

事件回传给 Python，这里去抖 + 拼步骤。导航（framenavigated）单独记成 goto。

## v1 不做的

  · 自定义下拉的「选了哪一项」只在点到 role=option 时能认出来；点开+点选会录成
    两个 click，留给整理页人工并成一个 select
  · 不自动插 wait —— 整理页有「插等待」块
"""
from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

_SUBMIT_WORDS = re.compile(r"保\s*存|提\s*交|确\s*定|下一步|发\s*布|完\s*成|保存并")

_INJECT = r"""
(() => {
  if (window.__flowRecInstalled) return;
  window.__flowRecInstalled = true;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const looksAuto = id => !id || /[0-9a-f]{6,}|^[a-z]+-\d+$|(^| )(css-|tw-|sc-|jsx-|emotion-)/.test(id);

  function visibleText(el){
    let n = el;
    for (let i = 0; i < 3 && n; i++){
      const t = clean(n.textContent);
      if (t && t.length <= 24 && !n.querySelector('input,textarea,select')) return t;
      n = n.parentElement;
    }
    return '';
  }
  function labelFor(el){
    if (el.id){
      try { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return clean(l.textContent); } catch(e){}
    }
    let s = el.parentElement;
    for (let i = 0; i < 4 && s; i++){
      const l = s.querySelector(':scope > label, :scope > .ant-form-item-label label, :scope label');
      if (l && !l.contains(el)) return clean(l.textContent);
      s = s.parentElement;
    }
    return '';
  }
  function cssPath(el){
    const parts = []; let n = el;
    while (n && n.nodeType === 1 && parts.length < 5){
      if (n.id && !looksAuto(n.id)){ parts.unshift('#' + CSS.escape(n.id)); break; }
      let sel = n.tagName.toLowerCase();
      const p = n.parentElement;
      if (p){
        const same = [...p.children].filter(c => c.tagName === n.tagName);
        if (same.length > 1) sel += ':nth-of-type(' + (same.indexOf(n) + 1) + ')';
      }
      parts.unshift(sel);
      n = p;
    }
    return parts.join(' > ');
  }
  function pickFor(el){
    const out = [];
    const txt = visibleText(el);
    if (txt) out.push({ text: txt });
    const role = el.getAttribute('role') || ({ BUTTON: 'button', A: 'link' })[el.tagName];
    const aria = el.getAttribute('aria-label');
    if (role && (aria || txt)) out.push({ role: role, name: aria || txt });
    if (el.matches && el.matches("input,select,textarea,[contenteditable='true']")){
      const lab = labelFor(el);
      if (lab) out.push({ label: lab });
    }
    for (const a of ['data-testid', 'name', 'aria-label']){
      const v = el.getAttribute && el.getAttribute(a);
      if (v) out.push({ attr: a + '=' + v });
    }
    if (el.id && !looksAuto(el.id)) out.push({ attr: 'id=' + el.id });
    out.push({ css: cssPath(el) });
    const seen = new Set();
    return out.filter(c => { const k = JSON.stringify(c); if (seen.has(k)) return false; seen.add(k); return true; });
  }
  function emit(kind, el, extra){
    try {
      window.__flowRec(Object.assign({
        kind: kind, pick: pickFor(el),
        seen: visibleText(el) || clean((el.getAttribute && el.getAttribute('placeholder')) || '')
      }, extra || {}));
    } catch (e){}
  }

  document.addEventListener('click', e => {
    if (window.__flowRecPaused) return;
    let el = e.target;
    if (el.closest && el.closest('#__flowToolbar')) return;
    el = (el.closest && el.closest("a,button,[role='button'],[role='tab'],[role='menuitem'],[role='option'],label,input[type='checkbox'],input[type='radio']")) || el;
    if (el.getAttribute && el.getAttribute('role') === 'option') emit('select', el, { value: visibleText(el) });
    else emit('click', el);
  }, true);

  document.addEventListener('change', e => {
    if (window.__flowRecPaused) return;
    const el = e.target;
    if (!el || (el.closest && el.closest('#__flowToolbar'))) return;
    if (el.tagName === 'SELECT'){
      const o = el.options[el.selectedIndex];
      emit('select', el, { value: clean(o && o.text) });
    } else if (el.matches && el.matches('input,textarea')){
      if (el.type === 'checkbox' || el.type === 'radio') return;
      emit('fill', el, { value: el.value });
    }
  }, true);

  document.addEventListener('keydown', e => {
    if (window.__flowRecPaused) return;
    if (e.key === 'Enter' || e.key === 'Escape'){
      const el = e.target;
      if (el && el.closest && el.closest('#__flowToolbar')) return;
      emit('press', el || document.body, { key: e.key });
    }
  }, true);

  function toolbar(){
    if (document.getElementById('__flowToolbar') || !document.body) return;
    const bar = document.createElement('div');
    bar.id = '__flowToolbar';
    bar.style.cssText = 'position:fixed;z-index:2147483647;right:16px;bottom:16px;background:#211c1e;color:#fff;font:13px/1.4 system-ui,sans-serif;border-radius:10px;padding:8px 10px;box-shadow:0 8px 30px rgba(0,0,0,.4);display:flex;gap:8px;align-items:center';
    bar.innerHTML =
      '<span id="__flowDot" style="width:8px;height:8px;border-radius:50%;background:#e34d82;display:inline-block"></span>' +
      '<span id="__flowStat">录制中</span>' +
      '<button data-a="pause" style="all:unset;cursor:pointer;padding:2px 8px;border-radius:6px;background:#3a3438">暂停</button>' +
      '<button data-a="confirm" style="all:unset;cursor:pointer;padding:2px 8px;border-radius:6px;background:#3a3438">在这停一下</button>' +
      '<button data-a="done" style="all:unset;cursor:pointer;padding:2px 10px;border-radius:6px;background:#e34d82">完成</button>';
    bar.addEventListener('click', ev => {
      const b = ev.target.closest('button'); if (!b) return;
      const a = b.dataset.a;
      if (a === 'pause'){
        window.__flowRecPaused = !window.__flowRecPaused;
        document.getElementById('__flowStat').textContent = window.__flowRecPaused ? '已暂停' : '录制中';
        document.getElementById('__flowDot').style.background = window.__flowRecPaused ? '#948a90' : '#e34d82';
        b.textContent = window.__flowRecPaused ? '继续' : '暂停';
      } else if (a === 'confirm'){
        const note = prompt('这一步停下让人核对什么？', '核对一眼再继续') || '核对一眼';
        try { window.__flowCtl('confirm_here', note); } catch (e){}
      } else if (a === 'done'){
        try { window.__flowCtl('done', ''); } catch (e){}
        bar.remove();
      }
    }, true);
    document.body.appendChild(bar);
  }
  toolbar();
  try {
    new MutationObserver(() => { if (!document.getElementById('__flowToolbar')) toolbar(); })
      .observe(document.documentElement, { childList: true, subtree: true });
  } catch (e){}
})();
"""


class FlowRecorder:
    def __init__(self, page, timeout: int = 15000):
        self.page = page
        self.timeout = timeout
        self.steps: list[dict] = []
        self._done = False
        self._last = (None, 0.0)          # (指纹, 时刻) 去抖
        self._start_url = ""
        self._running = False

    # ---------------- 生命周期 ----------------
    def start(self):
        self._start_url = self.page.url
        try:
            self.page.expose_binding("__flowRec", self._on_event)
            self.page.expose_binding("__flowCtl", self._on_ctl)
        except Exception:
            pass                          # 重复 start 时 binding 已存在，忽略
        self.page.add_init_script(_INJECT)
        self.page.on("framenavigated", self._on_nav)
        try:
            self.page.evaluate(_INJECT)
        except Exception:
            log.warning("注入录制脚本失败（当前页），换页后会自动重试", exc_info=True)
        self._running = True

    def stop(self) -> list[dict]:
        self._running = False
        try:
            self.page.remove_listener("framenavigated", self._on_nav)
        except Exception:
            pass
        try:
            self.page.evaluate("() => { const b = document.getElementById('__flowToolbar'); if (b) b.remove(); "
                               "window.__flowRecInstalled = false; }")
        except Exception:
            pass
        return self.build()

    @property
    def done(self) -> bool:
        return self._done

    # ---------------- 回调 ----------------
    def _on_ctl(self, source, action, arg=""):
        if action == "done":
            self._done = True
        elif action == "confirm_here":
            self.steps.append({"op": "confirm", "note": str(arg) or "核对一眼"})
        elif action in ("pause", "resume"):
            pass

    def _on_event(self, source, ev: dict):
        if not self._running:
            return
        kind = ev.get("kind")
        pick = ev.get("pick") or []
        seen = str(ev.get("seen") or "")
        now = time.monotonic()
        fp = json.dumps([kind, pick, ev.get("value"), ev.get("key")], ensure_ascii=False)
        if fp == self._last[0] and now - self._last[1] < 0.45:
            return                         # 同一动作 0.45s 内重复，丢
        self._last = (fp, now)

        if kind == "click":
            step = {"op": "click", "pick": pick, "seen": seen}
            if _SUBMIT_WORDS.search(seen):
                step["submit"] = True
            self.steps.append(step)
        elif kind == "fill":
            step = {"op": "fill", "pick": pick, "value": str(ev.get("value", "")), "seen": seen}
            # 同一个字段连续 fill → 覆盖上一条
            if self.steps and self.steps[-1].get("op") == "fill" \
                    and self.steps[-1].get("pick") == pick:
                self.steps[-1] = step
            else:
                self.steps.append(step)
        elif kind == "select":
            self.steps.append({"op": "select", "pick": pick,
                               "value": str(ev.get("value", "")), "seen": seen})
        elif kind == "press":
            self.steps.append({"op": "press", "key": ev.get("key", "Enter")})

    def _on_nav(self, frame):
        try:
            if frame != self.page.main_frame:
                return
        except Exception:
            return
        url = frame.url
        if url == self._start_url or url.startswith("about:"):
            return
        # 只在「路径变了」时记 goto，query/hash 抖动不算
        if self.steps and self.steps[-1].get("op") == "goto":
            self.steps[-1]["url"] = url
            return
        prev = self._start_url
        for s in reversed(self.steps):
            if s.get("op") == "goto":
                prev = s["url"]
                break
        if urlsplit(url).path.split("#")[0] != urlsplit(prev).path.split("#")[0]:
            self.steps.append({"op": "goto", "url": url})

    # ---------------- 产出 ----------------
    def build(self) -> list[dict]:
        """把 steps 收尾成一份能进 flow_data 的列表：开头补一个 goto。"""
        out = list(self.steps)
        if not any(s.get("op") == "goto" for s in out[:1]):
            out.insert(0, {"op": "goto", "url": self._start_url})
        return out
