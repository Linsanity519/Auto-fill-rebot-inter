"""录制器：挂到已登录的 Chrome，把运营的操作拼成 flow_data 的步骤图。

## 怎么工作

Playwright 的 `expose_binding` + `add_init_script` 走的是 CDP，**绕过页面 CSP** ——
所以内网后台再严的 script-src 也能注进去。往页面装：

  · 捕获阶段的 click / change / keydown 监听
  · 一个右下角浮动工具条（暂停 / 在这停一下 / 完成），SPA 重渲染会自动补回来

事件回传给 Python，这里去抖 + 拼步骤。导航（framenavigated）单独记成 goto。

## 「记意图」不是「记 DOM 位置」（对齐 testRigor / Stagehand / UiPath 语义选择器）

每一步除了选择器候选，还记 **field**（这一步在哪个字段 / 区块下 —— 「投放展示位置」
「人群选组」「人群分组ID」）和**选中项的可见文字**。重放时先按 field 定位到那一块，
再在块里按文字挑；css 只当命中最快的缓存。这样后台改版、列表顺序变、要滚动才可见，
都不影响。识别出的语义步骤：

  · select {field, value}        —— 原生 <select> 或「点开下拉 + 点浮层里的某项」
  · search_pick {field, query, value} —— 搜索框打字 + 从结果里挑（Python 侧把 fill+select 并起来）
  · pick_item {value}            —— 点结果表格 / 列表里「文字是 xxx 的那一行」
  · fill / click / press         —— 和以前一样，fill/click 也带上 field

## v1 不做的

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
  // 勾选框 / 单选「这一项」的文字（「Android」「指定人群」）—— 不是它所在组的名字
  function checkLabel(el){
    if (el.id){
      try { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return clean(l.textContent); } catch(e){}
    }
    const wrap = el.closest('label');
    if (wrap){ const t = clean(wrap.textContent); if (t) return t; }
    for (const dir of ['nextSibling', 'previousSibling']){
      let sib = el[dir];
      while (sib){
        if (sib.nodeType === 3){ const t = clean(sib.textContent); if (t) return t; }
        else if (sib.nodeType === 1){ const t = clean(sib.textContent); if (t && t.length <= 24) return t; break; }
        sib = sib[dir];
      }
    }
    const p = el.parentElement;
    if (p){ const t = clean(p.textContent); if (t && t.length <= 24) return t; }
    return '';
  }
  // 这个元素属于哪个「字段 / 区块」—— 「投放展示位置」「人群选组」「人群分组ID」这种。
  // 重放靠它先定位到对的那一块，再在块里按文字挑，比死记 DOM 位置稳。
  const LABELISH_PARTS = ["label", "legend", ".ant-form-item-label",
    "[class*='form-item-label']", "[class*='FormItem-label']",
    "[class*='label']", "[class*='Label']", "dt", "th"];
  const LABELISH = LABELISH_PARTS.join(",");
  // ⚠ ":scope > a,b,c" 里只有第一个带 :scope > 前缀，b/c 变成全文档选择器 —— 每一段都要自己加
  const SCOPED_LABEL = LABELISH_PARTS.map(p => ":scope > " + p + ", :scope > * > " + p).join(",");
  function tidy(t){ return clean(t).replace(/[:：*\s]+$/, '').replace(/^[*\s]+/, ''); }
  function fieldOf(el){
    let n = el;
    for (let i = 0; i < 9 && n; i++){
      try {
        const l = n.querySelector && n.querySelector(SCOPED_LABEL);
        if (l && !l.contains(el)){
          const t = tidy(l.textContent);
          if (t && t.length >= 2 && t.length <= 20) return t;
        }
      } catch(e){}
      let sib = n.previousElementSibling, hop = 0;
      while (sib && hop++ < 3){
        // 兄弟里找「像标签的短文本」——但别把下拉触发器 / 控件自身的显示文字当成 label
        const ok = sib.matches && sib.matches(LABELISH + ",span,div,p,strong,b,dt")
          && !sib.matches(TRIGGER)
          && !sib.querySelector("input,select,textarea,button,a,[role='button'],[role='combobox'],[role='option']");
        if (ok){
          const t = tidy(sib.textContent);
          if (t && t.length >= 2 && t.length <= 20) return t;
        }
        sib = sib.previousElementSibling;
      }
      n = n.parentElement;
    }
    const h = el.closest && el.closest('section,fieldset,[class*="card"],[class*="panel"],[class*="block"]');
    const head = h && h.querySelector('h1,h2,h3,h4,h5,legend,[class*="title"]');
    return head ? tidy(head.textContent).slice(0, 20) : '';
  }
  // 浮层：下拉 / 级联 / 搜索结果这类
  const POPUP = "[role='listbox'],[role='menu'],[class*='dropdown'],[class*='Dropdown'],"
    + "[class*='select-menu'],[class*='select__menu'],[class*='option-list'],[class*='cascader'],"
    + "[class*='autocomplete'],[class*='popover'],[class*='pull-down'],[class*='select-dropdown']";
  function inPopup(el){ return !!(el.closest && el.closest(POPUP)); }
  const OPTIONISH = "[role='option'],[class*='option'],[class*='Option'],li,[class*='menu-item'],[class*='item']";
  // 触发下拉的那个控件（重放先点它，再在浮层里挑）。刻意不含 *-dropdown / *-menu /
  // *-popup —— 那些是浮层容器，不是触发器。
  const TRIGGER = "[role='combobox'],select,[class*='selector'],[class*='select-selection'],"
    + "[class*='select__control'],[class*='ivu-select-selection'],[class*='cascader-picker'],"
    + "[class*='-picker']:not([class*='dropdown'])";
  function triggerFor(el){
    if (!el.closest) return null;
    const t = el.closest(TRIGGER);
    return (t && !inPopup(t)) ? t : null;
  }
  // —— 参照 Automa 用的 @medv/finder / Chrome Recorder：生成一个**当场验证过唯一**
  //    的 css，而不是从 body 一路 nth-of-type 猜。做法：从叶子往根，每加一层就
  //    querySelectorAll 数一下，命中 1 个就停；一层内自己不唯一才补 :nth-child。
  const BAD_CLASS = /^(is-|has-|js-|ng-|v-|el-|van-|ant-|arco-|active$|selected$|open$|show$|hide$|hidden$|current$|disabled$|focus|hover|checked$)/;
  function goodClass(c){
    return c && c.length <= 24 && !/[0-9a-f]{6,}|[0-9]{3,}|--|__/.test(c)
      && !/(^| )(css-|tw-|sc-|jsx-|emotion-|_)/.test(c) && !BAD_CLASS.test(c);
  }
  function uniqCount(sel){ try { return document.querySelectorAll(sel).length; } catch(e){ return 99; } }
  function segFor(n){
    if (n.id && !looksAuto(n.id)) return { s: '#' + CSS.escape(n.id), strong: true };
    const tag = n.tagName.toLowerCase();
    for (const a of ['data-testid', 'data-test', 'data-cy', 'data-id', 'name', 'role', 'type', 'placeholder', 'aria-label']){
      const v = n.getAttribute && n.getAttribute(a);
      if (v && v.length <= 40 && !looksAuto(v) && /^[\w :.\/#-]+$/.test(v))
        return { s: tag + '[' + a + '="' + v + '"]', strong: a.indexOf('data-') === 0 || a === 'name' };
    }
    const cls = [...(n.classList || [])].filter(goodClass).slice(0, 3);
    if (cls.length) return { s: tag + '.' + cls.map(c => CSS.escape(c)).join('.'), strong: false };
    return { s: tag, strong: false };
  }
  function nthChild(n){
    const p = n.parentElement; if (!p) return '';
    return ':nth-child(' + ([...p.children].indexOf(n) + 1) + ')';
  }
  function finder(el){
    // 走到「当场唯一」就收 —— 一条唯一的 css 就是一条能用的选择器（Automa / DevTools
    // Recorder 也是这么干的），不因为它长就算「脆」。只有走到头都定位不唯一才算没抓准。
    let n = el, parts = [], guard = 0;
    while (n && n.nodeType === 1 && guard++ < 9){
      const seg = segFor(n);
      let piece = seg.s;
      const p = n.parentElement;
      if (p){
        let same = 2;
        try { same = [...p.children].filter(c => c.matches(piece)).length; } catch(e){}
        if (same !== 1) piece += nthChild(n);
      }
      parts.unshift(piece);
      const sel = parts.join(' > ');
      if (uniqCount(sel) === 1) return { css: sel, anchored: true };
      if (piece[0] === '#') return { css: sel, anchored: true };
      n = p;
    }
    const sel = parts.join(' > ') || el.tagName.toLowerCase();
    return { css: sel, anchored: uniqCount(sel) === 1 };
  }
  function pickFor(el){
    const out = [];
    // 1) 主选择器：当场验证唯一的 css
    const uq = finder(el);
    out.push({ css: uq.css, anchored: !!uq.anchored });
    // 2) 人可读的辅助 + DOM 改了之后的兜底
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
    const icon = iconToken(el);
    if (icon) out.push({ attr: 'class~=' + icon });
    const seen = new Set();
    return out.filter(x => { const k = JSON.stringify(x); if (seen.has(k)) return false; seen.add(k); return true; });
  }
  function emit(kind, el, extra){
    try {
      window.__flowRec(Object.assign({
        kind: kind, pick: pickFor(el), field: fieldOf(el) || '',
        seen: visibleText(el) || clean((el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder'))) || '')
      }, extra || {}));
    } catch (e){}
  }
  // 语义步：主键是「选了什么」，pick 用触发控件（重放先点它、再在浮层里按文字挑）
  function emitPick(kind, trig, value, extra){
    try {
      window.__flowRec(Object.assign({
        kind: kind, pick: trig ? pickFor(trig) : [], field: (trig && fieldOf(trig)) || '',
        value: value, seen: value
      }, extra || {}));
    } catch (e){}
  }
  function now(){ return (window.performance && performance.now()) || +new Date(); }

  const CLICKABLE = "a,button,[role='button'],[role='tab'],[role='menuitem'],[role='option'],"
    + "[role='switch'],[role='checkbox'],[role='radio'],label,summary,tr,"
    + "input[type='checkbox'],input[type='radio'],input[type='button'],input[type='submit'],[onclick],[tabindex]";
  function interactive(el){
    if (!el || !el.closest) return null;
    return el.closest(CLICKABLE) || (inPopup(el) ? (el.closest(OPTIONISH) || el) : null);
  }
  document.addEventListener('click', e => {
    if (window.__flowRecPaused) return;
    let el = e.target;
    if (el.closest && el.closest('#__flowToolbar')) return;

    // 每次点击先看：这是不是个下拉触发器？是就记下来（在 A/B/C 之前，因为触发器
    // 本身往往不落在 CLICKABLE 里、会被下面的 gate 拦掉）
    const trg0 = triggerFor(el);
    if (trg0) window.__flowTrig = { el: trg0, ts: now() };

    // A) 点在下拉 / 级联 / 搜索结果的浮层里 —— 记「选了『xxx』」，不是「点了这个 li」
    if (inPopup(el)){
      const opt = el.closest(OPTIONISH) || el;
      const val = visibleText(opt);
      if (val){
        const trig = (window.__flowTrig && now() - window.__flowTrig.ts < 8000)
          ? window.__flowTrig.el : (triggerFor(el) || opt);
        emitPick('select', trig, val);
      }
      return;
    }
    const it = interactive(el);
    if (!it) return;
    if (it.getAttribute && it.getAttribute('role') === 'option'){ emitPick('select', triggerFor(it) || it, visibleText(it)); return; }

    // 勾选框 / 单选：交给 change 事件记成语义化的 check（记「勾了 Android」而不是「点了这个 span」）
    const box = (it.matches && it.matches("input[type='checkbox'],input[type='radio']")) ? it
      : (it.querySelector && it.querySelector("input[type='checkbox'],input[type='radio']"))
      || (it.tagName === 'LABEL' && it.control && /^(checkbox|radio)$/.test(it.control.type) ? it.control : null);
    if (box) return;

    // B) 点在结果表格 / 列表的某一行 —— 记「在这里选文字是『xxx』的那行」
    const tr = it.closest && it.closest('table tr, [role="row"], ul>li, [class*="list"]>[class*="item"]');
    if (tr && !inPopup(tr) && (tr.querySelector('td,[role="cell"]') || tr.tagName === 'LI')){
      const cells = [...tr.querySelectorAll('td,[role="cell"]')].map(c => clean(c.textContent)).filter(Boolean);
      const key = (cells[0] || clean(tr.textContent) || '').slice(0, 40);
      if (key && key.length >= 2){
        const box = tr.closest('table') || tr.parentElement;
        window.__flowRec({ kind: 'pick_item', pick: pickFor(box), field: fieldOf(box) || '',
          value: key, seen: key });
        return;
      }
    }

    // C) 普通点击。顺手记一下这是不是个「下拉触发器」，给下一次浮层点击用
    if (triggerFor(it) || (it.matches && it.matches(TRIGGER)))
      window.__flowTrig = { el: triggerFor(it) || it, ts: now() };
    emit('click', it);
  }, true);

  document.addEventListener('change', e => {
    if (window.__flowRecPaused) return;
    const el = e.target;
    if (!el || (el.closest && el.closest('#__flowToolbar'))) return;
    if (el.tagName === 'SELECT'){
      const o = el.options[el.selectedIndex];
      emitPick('select', el, clean(o && o.text));
    } else if (el.matches && el.matches("input[type='checkbox'],input[type='radio']")){
      // 记「勾了 / 取消了『Android』」，不是「点了这个 input」
      const lbl = clean(checkLabel(el));
      window.__flowRec({ kind: 'check', pick: pickFor(el), field: fieldOf(el) || '',
        value: lbl, checked: !!el.checked, seen: lbl });
    } else if (el.matches && el.matches('input,textarea')){
      // 搜索框：记一下，Python 侧若紧跟着一次浮层选择，会并成一步 search_pick
      if (triggerFor(el) || /search|autocomplete|combobox/i.test((el.getAttribute && el.getAttribute('class')) || '')
          || el.getAttribute('role') === 'searchbox')
        window.__flowTrig = { el: triggerFor(el) || el, ts: now() };
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
      window.__flowRecStopped = true;      // 立刻叫停「浮条自愈」，别让它 1 秒后又冒出来
      ctl('done', '');
      setTimeout(() => { const el = document.getElementById('__flowToolbar'); if (el) el.remove(); }, 400);
    });

    [dot, stat, btnPause, btnConfirm, noteWrap, btnDone].forEach(n => bar.appendChild(n));
    document.body.appendChild(bar);
  }
  // 只负责「浮条在不在」，不碰监听器（重复挂监听器会让每个操作记两遍）。
  function ensureBar(){
    if (window.__flowRecStopped || window.__flowRecDone) return;   // 已经完成 / 停了，别再冒
    if (!document.getElementById('__flowToolbar')) toolbar();
  }
  window.__flowEnsureBar = ensureBar;
  toolbar();
  // 录制结束时 Python 侧 evaluate 调它：停掉自愈定时器 + observer + 移除浮条，
  // 不然这几个 orphan 定时器会在会话结束后把浮条再画出来（就是「完成后又弹一个框」）。
  window.__flowRecTeardown = function(){
    window.__flowRecStopped = true;
    try { (window.__flowRecTimers || []).forEach(clearInterval); } catch(e){}
    try { if (window.__flowRecObs) window.__flowRecObs.disconnect(); } catch(e){}
    const b = document.getElementById('__flowToolbar'); if (b) b.remove();
  };
  window.__flowRecTimers = window.__flowRecTimers || [];
  try {
    window.__flowRecObs = new MutationObserver(ensureBar);
    window.__flowRecObs.observe(document.documentElement, { childList: true, subtree: true });
  } catch (e){}
  try { window.__flowRecTimers.push(setInterval(ensureBar, 1000)); } catch (e){}
})();
"""


class FlowRecorder:
    def __init__(self, page, timeout: int = 15000):
        self.page = page
        self.timeout = timeout
        self.steps: list[dict] = []
        self._done = False
        self._last = (None, 0.0)          # (指纹, 时刻) 去抖
        self._last_action = 0.0           # 上一次 click/fill/select/press 的时刻
        self._last_fill = 0.0             # 上一次 fill 的时刻（判 fill+select→search_pick）
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
            # 上一次录制留下的 teardown / 定时器先清掉，再重置标志，最后重新注入
            self.page.evaluate("() => { try { window.__flowRecTeardown && window.__flowRecTeardown(); } catch(e){} "
                               "window.__flowRecInstalled = false; window.__flowRecDone = false; "
                               "window.__flowRecStopped = false; }")
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
            # teardown 会停掉页面里的自愈定时器 + observer + 移除浮条。
            # ⚠ 不重置 __flowRecStopped —— 留着它，万一有 orphan 定时器也不会再画浮条。
            self.page.evaluate("() => { try { window.__flowRecTeardown && window.__flowRecTeardown(); } catch(e){} "
                               "window.__flowRecInstalled = false; }")
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
        self._last_action = now      # 给 _on_nav 判断「这次跳转是不是刚才点出来的」
        field = str(ev.get("field") or "")
        value = str(ev.get("value", ""))

        if kind == "click":
            step = {"op": "click", "pick": pick, "seen": seen, "field": field}
            if _SUBMIT_WORDS.search(seen):
                step["submit"] = True
            if self.steps and self.steps[-1].get("op") == "click" \
                    and self.steps[-1].get("pick") == pick:
                self.steps[-1] = step
            else:
                self.steps.append(step)
        elif kind == "fill":
            step = {"op": "fill", "pick": pick, "value": value, "seen": seen, "field": field}
            if self.steps and self.steps[-1].get("op") == "fill" \
                    and self.steps[-1].get("pick") == pick:
                self.steps[-1] = step
            else:
                self.steps.append(step)
        elif kind == "select":
            prev = self.steps[-1] if self.steps else None
            # 「搜索框打字」+「浮层里挑一个」紧挨着 → 并成一步 search_pick。
            #   query（打的字）留着是为了触发远程搜索；真正的目标是 value。
            if prev and prev.get("op") == "fill" and now - self._last_fill < 6.0 \
                    and (not field or not prev.get("field") or field == prev.get("field")):
                self.steps[-1] = {"op": "search_pick",
                                  "pick": prev.get("pick") or pick,
                                  "field": prev.get("field") or field,
                                  "query": prev.get("value", ""), "value": value, "seen": value}
            else:
                self.steps.append({"op": "select", "pick": pick, "value": value,
                                   "seen": seen, "field": field})
        elif kind == "pick_item":
            self.steps.append({"op": "pick_item", "pick": pick, "value": value,
                               "seen": seen, "field": field})
        elif kind == "press":
            self.steps.append({"op": "press", "key": ev.get("key", "Enter")})

        if kind == "fill":
            self._last_fill = now

    def _on_nav(self, frame):
        try:
            if frame != self.page.main_frame:
                return
        except Exception:
            return
        url = frame.url
        if url == self._start_url or url.startswith("about:"):
            return
        # ⚠ 关键：**刚点过东西**（2.5s 内）的跳转，是那次点击的后果，不记 goto。
        #   录成 goto 会把这次会话的 activityId=708 这种一次性参数焊死进流程，
        #   下次跑必然跳到一个过期的页面 —— 这正是「录完不能复刻」的头号原因。
        #   点击本身重放时会再触发同样的跳转，filler 里 settle() 会等它。
        if time.monotonic() - self._last_action < 2.5:
            return
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
