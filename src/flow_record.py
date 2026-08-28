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
    两个 click，留给整理页人工并成一个 select（整理页有「合并为下拉」按钮）
  · 不自动插 wait —— 整理页有「插步」菜单

## 浮条为什么这么写（踩过的坑）

  · 按钮**不用 `all:unset`**：`pointer-events` 是可继承属性，`all:unset` 会让按钮
    继承祖先的值，目标页某个加载态给 `body` 设了 `pointer-events:none` 时整条浮条
    就点不动了。这里每个按钮显式 `setProperty('pointer-events','auto','important')`。
  · 「完成」先写 `window.__flowRecDone=true` 这个哨兵、**再**调 `__flowCtl` 绑定，
    浮条最后才移除。绑定万一没注入成功（重连 CDP 时偶发），Python 侧还能靠轮询
    哨兵收尾，不会卡在「已记 N 步…」永远转圈。
  · 点击同时挂 capture / bubble / 直接 onclick 三处：个别 SPA 在 document 上抢
    capture 阶段 `stopImmediatePropagation`，只挂一处会被吃掉。
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
    // 元素自己的短文本最准；再往上找不含表单控件的短文本容器
    const own = clean(el.textContent || '');
    if (own && own.length <= 40 && !(el.querySelector && el.querySelector('input,textarea,select'))) return own;
    let n = el.parentElement;
    for (let i = 0; i < 4 && n; i++){
      const t = clean(n.textContent);
      if (t && t.length <= 32 && !n.querySelector('input,textarea,select')) return t;
      n = n.parentElement;
    }
    return '';
  }
  // 图标按钮：从 class / <use href> 里抠出语义词（anticon-edit / icon-delete / #icon-search）
  function iconToken(el){
    const scan = [el].concat([...(el.querySelectorAll ? el.querySelectorAll('[class],use,svg') : [])].slice(0, 4));
    for (const n of scan){
      const cls = (n.getAttribute && (n.getAttribute('class') || '')) || '';
      let m = cls.match(/(?:antcon-|anticon-|icon-|iconfont-|van-icon-|el-icon-)([a-z][a-z0-9-]{1,20})/i);
      if (m) return m[0];
      const href = (n.getAttribute && (n.getAttribute('href') || n.getAttribute('xlink:href') || '')) || '';
      m = href.match(/#(?:icon-)?([a-z][a-z0-9-]{1,20})/i);
      if (m) return 'icon-' + m[1];
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
  const stableAttr = n => {
    for (const a of ['data-testid', 'data-test', 'data-cy', 'data-id', 'data-key', 'data-name']){
      const v = n.getAttribute && n.getAttribute(a);
      if (v && /^[\w -]{1,40}$/.test(v) && !looksAuto(v)) return a + '=' + v;
    }
    if (n.id && !looksAuto(n.id)) return 'id=' + n.id;
    return '';
  };
  function cssPath(el){
    // 尽量挂到一个稳定的祖先（id / data-testid / role），后面只跟一小段，
    // 而不是从 body 一路 nth-of-type —— 那种改版必失效。
    const parts = []; let n = el; let anchored = false;
    while (n && n.nodeType === 1 && parts.length < 6){
      const sa = stableAttr(n);
      if (sa){
        const [k, v] = sa.split('=');
        parts.unshift(k === 'id' ? '#' + CSS.escape(v) : '[' + k + "='" + v + "']");
        anchored = true;
        break;
      }
      const role = n.getAttribute && n.getAttribute('role');
      let sel = n.tagName.toLowerCase();
      if (role && !parts.length){ sel += "[role='" + role + "']"; }
      const p = n.parentElement;
      if (p){
        const same = [...p.children].filter(c => c.tagName === n.tagName);
        if (same.length > 1) sel += ':nth-of-type(' + (same.indexOf(n) + 1) + ')';
      }
      parts.unshift(sel);
      n = p;
    }
    return { path: parts.join(' > '), anchored: anchored };
  }
  function pickFor(el){
    const out = [];
    const txt = visibleText(el);
    if (txt) out.push({ text: txt });
    const role = el.getAttribute('role') || ({ BUTTON: 'button', A: 'link' })[el.tagName];
    const aria = el.getAttribute('aria-label') || el.getAttribute('title');
    if (role && (aria || txt)) out.push({ role: role, name: aria || txt });
    if (el.matches && el.matches("input,select,textarea,[contenteditable='true']")){
      const lab = labelFor(el);
      if (lab) out.push({ label: lab });
    }
    for (const a of ['data-testid', 'data-test', 'data-cy', 'name', 'aria-label', 'title', 'alt', 'placeholder']){
      const v = el.getAttribute && el.getAttribute(a);
      if (v && /^[\w /:.-]{1,50}$/.test(v)) out.push({ attr: a + '=' + v });
    }
    if (el.id && !looksAuto(el.id)) out.push({ attr: 'id=' + el.id });
    // 图标按钮：拿图标名当锚
    const icon = iconToken(el);
    if (icon) out.push({ attr: 'class~=' + icon });
    // 稳定祖先的兜底
    const sa = el.closest && (() => { let n = el; for (let i = 0; i < 6 && n; i++){ const s = stableAttr(n); if (s && n !== el) return s; n = n.parentElement; } return ''; })();
    const c = cssPath(el);
    out.push({ css: c.path, anchored: !!c.anchored || !!sa });
    const seen = new Set();
    return out.filter(x => { const k = JSON.stringify(x); if (seen.has(k)) return false; seen.add(k); return true; });
  }
  function emit(kind, el, extra){
    try {
      window.__flowRec(Object.assign({
        kind: kind, pick: pickFor(el),
        seen: visibleText(el) || clean((el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder'))) || '')
      }, extra || {}));
    } catch (e){}
  }

  const CLICKABLE = "a,button,[role='button'],[role='tab'],[role='menuitem'],[role='option'],"
    + "[role='switch'],[role='checkbox'],[role='radio'],label,summary,"
    + "input[type='checkbox'],input[type='radio'],input[type='button'],input[type='submit'],[onclick],[tabindex]";
  function interactive(el){
    if (!el || !el.closest) return null;
    const hit = el.closest(CLICKABLE);
    if (hit) return hit;
    // 没落在任何可交互元素上：多半是点空白 / 滚动条 / 拖选，别记
    return null;
  }
  document.addEventListener('click', e => {
    if (window.__flowRecPaused) return;
    let el = e.target;
    if (el.closest && el.closest('#__flowToolbar')) return;
    const it = interactive(el);
    if (!it) return;                       // 只记真的点在可交互元素上的
    if (it.getAttribute && it.getAttribute('role') === 'option') emit('select', it, { value: visibleText(it) });
    else emit('click', it);
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
    if (e.key !== 'Enter' && e.key !== 'Escape') return;
    const el = e.target;
    if (el && el.closest && el.closest('#__flowToolbar')) return;
    // 只在输入框里按 Enter/Esc 才算一步（页面上到处按不该记）
    if (!el || !(el.matches && el.matches("input,textarea,[contenteditable='true']"))) return;
    emit('press', el, { key: e.key });
  }, true);

  function ctl(action, arg){
    // 绑定可能没注入成功（重连 CDP 时偶发）—— 关键状态先落到 window 上，
    // Python 侧轮询 __flowRecDone 兜底。
    if (action === 'done') window.__flowRecDone = true;
    try { window.__flowCtl(action, arg || ''); } catch (e){}
  }
  function mkBtn(label, bg, onClick){
    const b = document.createElement('button');
    b.textContent = label;
    b.type = 'button';
    b.style.cssText = 'cursor:pointer;padding:3px 10px;border-radius:6px;border:0;margin:0;'
      + 'font:13px/1.4 system-ui,sans-serif;color:#fff;background:' + bg;
    b.style.setProperty('pointer-events', 'auto', 'important');
    // capture 阶段一处 + onclick 一处：个别 SPA 在 document 抢 capture 阶段
    // stopImmediatePropagation，只挂 onclick 会被吃掉；两处都挂又会触发两次，
    // 300ms 锁一下去重（pause 之类切换类操作触发两次等于没反应）。
    let lock = 0;
    const run = ev => {
      if (ev){ ev.preventDefault(); ev.stopPropagation(); }
      const now = (window.performance && performance.now()) || +new Date();
      if (now - lock < 300) return;
      lock = now;
      onClick(b);
    };
    b.addEventListener('click', run, true);
    b.onclick = run;
    return b;
  }
  function toolbar(){
    if (document.getElementById('__flowToolbar') || !document.body) return;
    const bar = document.createElement('div');
    bar.id = '__flowToolbar';
    bar.style.cssText = 'position:fixed;z-index:2147483647;right:16px;bottom:16px;background:#211c1e;color:#fff;font:13px/1.4 system-ui,sans-serif;border-radius:10px;padding:8px 10px;box-shadow:0 8px 30px rgba(0,0,0,.4);display:flex;gap:8px;align-items:center;flex-wrap:wrap;max-width:min(92vw,520px)';
    bar.style.setProperty('pointer-events', 'auto', 'important');

    const dot = document.createElement('span');
    dot.id = '__flowDot';
    dot.style.cssText = 'width:8px;height:8px;border-radius:50%;background:#e34d82;display:inline-block;flex:none';
    const stat = document.createElement('span');
    stat.id = '__flowStat';
    stat.textContent = '录制中';

    const btnPause = mkBtn('暂停', '#3a3438', b => {
      window.__flowRecPaused = !window.__flowRecPaused;
      stat.textContent = window.__flowRecPaused ? '已暂停' : '录制中';
      dot.style.background = window.__flowRecPaused ? '#948a90' : '#e34d82';
      b.textContent = window.__flowRecPaused ? '继续' : '暂停';
    });
    // 「在这停一下」：不弹 prompt（内网 Chrome 里可能被拦 / 样子难看），
    // 就地在浮条里展开一个输入框。
    const noteWrap = document.createElement('span');
    noteWrap.style.cssText = 'display:none;gap:6px;align-items:center;flex:none';
    const noteInput = document.createElement('input');
    noteInput.placeholder = '停下来核对什么？';
    noteInput.style.cssText = 'font:13px system-ui,sans-serif;padding:3px 6px;border-radius:5px;border:1px solid #55494f;background:#2b2529;color:#fff;width:180px';
    noteInput.style.setProperty('pointer-events', 'auto', 'important');
    const submitNote = () => {
      const v = (noteInput.value || '').trim() || '核对一眼';
      ctl('confirm_here', v);
      noteInput.value = '';
      noteWrap.style.display = 'none';
      stat.textContent = window.__flowRecPaused ? '已暂停' : '录制中';
    };
    noteInput.addEventListener('keydown', e => {
      e.stopPropagation();
      if (e.key === 'Enter') submitNote();
      else if (e.key === 'Escape') noteWrap.style.display = 'none';
    }, true);
    const noteOk = mkBtn('加', '#3a3438', submitNote);
    noteWrap.appendChild(noteInput);
    noteWrap.appendChild(noteOk);

    const btnConfirm = mkBtn('在这停一下', '#3a3438', () => {
      const open = noteWrap.style.display !== 'none';
      noteWrap.style.display = open ? 'none' : 'flex';
      if (!open){ stat.textContent = '加「停一下」'; noteInput.focus(); }
    });
    const btnDone = mkBtn('完成', '#e34d82', () => {
      stat.textContent = '正在收尾…';
      ctl('done', '');
      setTimeout(() => { const el = document.getElementById('__flowToolbar'); if (el) el.remove(); }, 400);
    });

    [dot, stat, btnPause, btnConfirm, noteWrap, btnDone].forEach(n => bar.appendChild(n));
    document.body.appendChild(bar);
  }
  // 只负责「浮条在不在」，不碰监听器（重复挂监听器会让每个操作记两遍）。
  // Python 侧录制循环每隔一两秒也会 evaluate 一次这个函数兜底。
  window.__flowEnsureBar = toolbar;
  toolbar();
  try {
    new MutationObserver(() => { if (!document.getElementById('__flowToolbar')) toolbar(); })
      .observe(document.documentElement, { childList: true, subtree: true });
  } catch (e){}
  // SPA 整片重渲染时 MutationObserver 偶尔跟不上，再加一个定时兜底
  try { setInterval(() => { if (!window.__flowRecDone && !document.getElementById('__flowToolbar')) toolbar(); }, 1000); } catch (e){}
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
        # 两个 binding 各自 try：重连 CDP 后偶发「__flowRec 已注册」，
        # 一起兜的话第一个抛出就把 __flowCtl 也漏了 —— 那正是「完成」点不动的成因之一。
        for name, fn in (("__flowRec", self._on_event), ("__flowCtl", self._on_ctl)):
            try:
                self.page.expose_binding(name, fn)
            except Exception:
                log.debug("binding %s 已存在，忽略", name)
        self.page.add_init_script(_INJECT)
        self.page.on("framenavigated", self._on_nav)
        try:
            self.page.evaluate("() => { window.__flowRecInstalled = false; window.__flowRecDone = false; }")
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
                               "window.__flowRecInstalled = false; window.__flowRecDone = false; }")
        except Exception:
            pass
        return self.build()

    @property
    def done(self) -> bool:
        return self._done

    def pump(self) -> dict:
        """轮询用。sync 版 Playwright 只在调用 API 时才处理挂起的 binding 回调，
        录制中界面若只读 self.steps 而不碰页面，计数会一直停在 0、done 也收不到。
        这里 evaluate 一下顺带把 __flowRecDone 哨兵读回来（binding 万一没注入的兜底）。
        """
        try:
            v = self.page.evaluate("() => !!window.__flowRecDone")
            if v:
                self._done = True
        except Exception:
            pass
        return {"steps": len(self.steps), "done": self._done}

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
        if fp == self._last[0] and now - self._last[1] < 0.8:
            return                         # 同一动作 0.8s 内重复，丢（双击 / 冒泡多次）
        self._last = (fp, now)

        if kind == "click":
            step = {"op": "click", "pick": pick, "seen": seen}
            if _SUBMIT_WORDS.search(seen):
                step["submit"] = True
            # 和上一条 click 落在同一个元素上（pick 一样）→ 覆盖，不叠一堆
            if self.steps and self.steps[-1].get("op") == "click" \
                    and self.steps[-1].get("pick") == pick:
                self.steps[-1] = step
            else:
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
