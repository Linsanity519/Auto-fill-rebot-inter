"""大会员投放系统「投放列表」页（单元 / 创意两张表）的读 + 开关。

⚠ 独立于别的 filler。这页是 Ant Design 5（满页 css-mncuj7 / css-var-«r26» 编译哈希，
  **一个都不能用**）。定位一律走语义 class + 文字，见
  docs/常规资源位批量开关-配置项抓取.md。

这套 DOM 特有的四件事：

  1. 找表    —— 全页一张主表；挑 thead 里同时含「<层级>ID」和「操作」的那张
  2. 筛      —— 顶部筛选表单（.ant-form-item，按 label 文字找输入框）+ 「查 询」按钮
  3. 读行    —— 一次 page.evaluate 把 ID / 名称 / 状态 读回来，行 key = data-row-key
  4. 开关    —— 操作列的「更多」**是 hover 触发的下拉**，菜单项按状态变：
                投放中 → 暂停投放 / 终止投放
                已暂停 → 启动投放 / 终止投放
                未开始 / 已完成 / 已终止 → 一个都没有（只剩查看/复制）
                点完**有二次确认弹窗**（和价格策略那套不一样），要点「确 定」

⚠ 「终止投放」不可逆，这个模块**永远不点它** —— 只认「暂停投放」「启动投放」两条。

翻页：antd 分页，默认 20 条/页，进页先拨到 100 条/页（选项有 10/20/50/100）。
"""
from __future__ import annotations

import logging
from urllib.parse import unquote

from .fill_core import FillError, norm, wait_until

log = logging.getLogger(__name__)

BASE = "https://rich-vip.bilibili.co/manage/v/delivery-manage/delivery-list/delivery"

# 两个层级只差「哪张表 / 哪几列 / 哪个 URL」，别的完全一样
LEVELS = {
    "unit": {
        "label": "单元",
        "url": f"{BASE}/unit",
        "id_col": "单元ID",
        "name_col": "单元名称",
        "state_col": "单元状态",
        "act_col": "活动ID",
        "act_name_col": "活动名称",
        "act_filter": "活动ID",          # 这张表能直接按活动ID筛
    },
    "creative": {
        "label": "创意",
        "url": f"{BASE}/originality",
        "id_col": "创意ID",
        "name_col": "单元名称",           # 创意本身没有名字，列表里认的是所属单元名
        "state_col": "创意状态",
        "act_col": "",                    # ⚠ 创意表**没有活动ID列**，只有活动名称
        "act_name_col": "活动名称",
        "act_filter": "活动名称",          # 所以按活动筛只能用名称，见 dl_runner._collect
    },
}

# 目标状态 → 该点的菜单项 / 已达标时的状态文字
MENU_ON = "启动投放"
MENU_OFF = "暂停投放"
STATE_ON = "投放中"
STATE_OFF = "已暂停"
# 这几个状态下菜单里既没有「启动投放」也没有「暂停投放」，跳过就是了
DEAD_STATES = ("未开始", "已完成", "已终止")

_READ_JS = r"""
(cols) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const [idCol, nameCol, stateCol, actCol, actNameCol] = cols;
  const tables = [...document.querySelectorAll('table')];
  const tbl = tables.find(t => {
    const h = [...t.querySelectorAll('thead th')].map(x => x.innerText.replace(/\s+/g, ''));
    return h.includes(idCol) && h.includes('操作');
  });
  if (!tbl) return { found: false, empty: false, loading: false, rows: [], page: 1, pages: 1, total: 0 };

  const wrap = tbl.closest('.ant-table-wrapper') || tbl.parentElement;
  const loading = !!(wrap && wrap.querySelector('.ant-spin-spinning'));
  const empty = !!(tbl.querySelector('.ant-table-placeholder')
                   || (tbl.querySelector('.ant-empty') && !tbl.querySelector('tbody tr.ant-table-row')));

  const H = [...tbl.querySelectorAll('thead th')].map(x => x.innerText.replace(/\s+/g, ''));
  const at = name => (name ? H.indexOf(name) : -1);
  const ii = at(idCol), ni = at(nameCol), si = at(stateCol), ai = at(actCol), an = at(actNameCol);
  const ui = H.indexOf('单元ID');

  const rows = [...tbl.querySelectorAll('tbody tr.ant-table-row')].map(tr => {
    const td = [...tr.querySelectorAll('td')];
    const cell = i => (i >= 0 && td[i] ? clean(td[i].innerText) : '');
    return {
      key: tr.getAttribute('data-row-key') || '',
      id: cell(ii),
      name: cell(ni),
      state: cell(si),
      act: cell(ai),
      act_name: cell(an),
      unit_id: cell(ui),
    };
  });

  let pager = null, n = tbl;
  for (let i = 0; i < 6 && n; i++) {
    const p = n.parentElement;
    if (p) { const pg = p.querySelector('.ant-pagination'); if (pg) { pager = pg; break; } }
    n = p;
  }
  let page = 1, pages = 1, total = rows.length;
  if (pager) {
    const tt = pager.querySelector('.ant-pagination-total-text');
    if (tt) { const m = tt.textContent.match(/(\d+)\s*条/g);
              if (m && m.length) total = parseInt(m[m.length - 1]) || total; }
    const active = pager.querySelector('.ant-pagination-item-active');
    if (active) page = parseInt(active.getAttribute('title') || active.textContent) || 1;
    const items = [...pager.querySelectorAll('.ant-pagination-item')]
      .map(li => parseInt(li.getAttribute('title') || li.textContent) || 0);
    if (items.length) pages = Math.max(...items, page);
  }
  return { found: true, empty, loading, rows, page, pages, total };
}
"""

# 分页按钮：全页只有一张主表，但仍然从表往上找它自己的 .ant-pagination（别的页可能加表）
_PAGER_JS = r"""
(a) => {
  const [idCol, dir] = a;
  const tbl = [...document.querySelectorAll('table')].find(t => {
    const h = [...t.querySelectorAll('thead th')].map(x => x.innerText.replace(/\s+/g, ''));
    return h.includes(idCol) && h.includes('操作');
  });
  if (!tbl) return 'no-table';
  let pager = null, n = tbl;
  for (let i = 0; i < 6 && n; i++) {
    const p = n.parentElement;
    if (p) { const pg = p.querySelector('.ant-pagination'); if (pg) { pager = pg; break; } }
    n = p;
  }
  if (!pager) return 'no-pager';
  const li = pager.querySelector(dir === 'next' ? '.ant-pagination-next' : '.ant-pagination-prev');
  if (!li) return 'no-btn';
  const cls = typeof li.className === 'string' ? li.className : '';
  if (cls.includes('ant-pagination-disabled') || li.getAttribute('aria-disabled') === 'true')
    return 'disabled';
  (li.querySelector('button') || li).click();
  return 'clicked';
}
"""

_SIZE_OPEN_JS = r"""
() => {
  const sel = document.querySelector('.ant-pagination .ant-pagination-options .ant-select');
  if (!sel) return 'no-size';
  const cur = sel.querySelector('.ant-select-selection-item');
  if (cur && cur.innerText.replace(/\s+/g, '').startsWith('100')) return 'already';
  sel.querySelector('.ant-select-selector').dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
  return 'opened';
}
"""

# 一个函数管两种浮层：菜单项（.ant-dropdown 里的 li）和下拉选项（.ant-select-dropdown）
_PICK_JS = r"""
(args) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const root = args[0], want = args.slice(1);
  const sel = root === '.ant-dropdown' ? 'li.ant-dropdown-menu-item' : '.ant-select-item-option';
  for (const d of document.querySelectorAll(root)) {
    const cls = typeof d.className === 'string' ? d.className : '';
    if (cls.includes('hidden')) continue;
    for (const it of d.querySelectorAll(sel)) {
      const t = clean(it.innerText);
      if (want.includes(t)) { it.click(); return 'clicked:' + t; }
    }
  }
  return 'not-found';
}
"""

_MENU_READ_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = [];
  for (const d of document.querySelectorAll('.ant-dropdown')) {
    const cls = typeof d.className === 'string' ? d.className : '';
    if (cls.includes('ant-dropdown-hidden')) continue;
    for (const li of d.querySelectorAll('li.ant-dropdown-menu-item')) out.push(clean(li.innerText));
  }
  return out;
}
"""

# 二次确认弹窗：只认**看得见**那个 .ant-modal-wrap
# （页面上留着几个 display:none 的空壳，playwright 的 locator 会撞上它们并一直等）
_MODAL_JS = r"""
(action) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  for (const w of document.querySelectorAll('.ant-modal-wrap')) {
    if (w.style.display === 'none') continue;
    const text = clean(w.innerText);
    if (action === 'read') return text;
    for (const b of w.querySelectorAll('button')) {
      const t = clean(b.innerText).replace(/\s+/g, '');
      if ((action === 'ok' && t === '确定') || (action === 'cancel' && t === '取消')) {
        b.click(); return 'clicked';
      }
    }
    return 'no-button';
  }
  return '';
}
"""

_TOAST_JS = r"""
() => [...document.querySelectorAll('.ant-message-notice, .ant-notification-notice')]
        .map(x => (x.innerText || '').replace(/\s+/g, ' ').trim())
"""

# 筛选表单：按 label 文字找到 .ant-form-item 里的那个 input，给它打个临时标记，
# 再让 playwright 用真键盘事件填 —— **不能用 JS 直接写 .value**：
# antd 的 Form 是受控组件，值存在它自己的 store 里，JS 塞进 DOM 的值它根本不看，
# 点「查 询」时带出去的还是空条件（实测：输入框里明明显示 708，查回来的还是全站 4 万条）。
_MARK_JS = r"""
(label) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  document.querySelectorAll('[data-cc-field]').forEach(e => e.removeAttribute('data-cc-field'));
  if (!label) return 'cleared';
  const it = [...document.querySelectorAll('.ant-form-item')].find(x => {
    const l = x.querySelector('.ant-form-item-label');
    return l && clean(l.innerText).replace(/[:：]\s*$/, '') === label;
  });
  if (!it) return 'no-item';
  const inp = it.querySelector('input');
  if (!inp) return 'no-input';
  inp.setAttribute('data-cc-field', '1');
  return 'ok';
}
"""

# ⚠ 「查 询」只能用**真鼠标点**（playwright 的 click），JS 的 el.click() 点了没反应：
#   按钮点着了、也没报错，但表单不提交，查回来还是全站 4 万条。所以这里只负责
#   把按钮标出来，点的动作交给 playwright。（菜单项和弹窗按钮倒是 JS 点得动。）
_MARK_BTN_JS = r"""
(label) => {
  const clean = s => (s || '').replace(/\s+/g, '').trim();
  document.querySelectorAll('[data-cc-btn]').forEach(e => e.removeAttribute('data-cc-btn'));
  if (!label) return 'cleared';
  const b = [...document.querySelectorAll('button')].find(x => clean(x.innerText) === label);
  if (!b) return 'no-btn';
  b.setAttribute('data-cc-btn', '1');
  return 'ok';
}
"""


class DlToggleFiller:
    def __init__(self, page, timeout: int = 15000, on_note=None):
        self.page = page
        self.timeout = timeout
        self._on_note = on_note
        self.level = "unit"
        self._last_keys: set = set()      # 上一次 query() 填的是哪几格（决定能不能原地重查）

    # ------------------------------------------------ 小工具
    @property
    def L(self) -> dict:
        return LEVELS[self.level]

    def _note(self, msg: str):
        if self._on_note:
            try:
                self._on_note(msg)
            except Exception:
                pass

    def _cols(self) -> list:
        L = self.L
        return [L["id_col"], L["name_col"], L["state_col"], L["act_col"], L["act_name_col"]]

    # ------------------------------------------------ 读
    def snapshot(self) -> dict:
        try:
            data = self.page.evaluate(_READ_JS, self._cols())
        except Exception as e:
            raise FillError(f"读「{self.L['label']}」列表失败：{e}")
        return data or {"found": False, "rows": [], "page": 1, "pages": 1, "total": 0}

    @staticmethod
    def _settled(snap: dict) -> bool:
        return bool(snap.get("found") and not snap.get("loading")
                    and (snap.get("rows") or snap.get("empty")))

    def wait_table(self, timeout: int | None = None) -> dict:
        t = timeout or max(self.timeout * 2, 30000)
        if not wait_until(self.page, lambda: self._settled(self.snapshot()), t):
            snap = self.snapshot()
            if not snap.get("found"):
                raise FillError(
                    f"页面上没找到「{self.L['label']}」那张表"
                    f"（表头要同时有「{self.L['id_col']}」和「操作」）。"
                    f"是不是没登录 / 页面还没加载完？")
            raise FillError(f"「{self.L['label']}」列表一直在加载中，等超时了")
        return self.snapshot()

    def row(self, key: str) -> dict | None:
        return next((r for r in self.snapshot().get("rows", []) if r.get("key") == str(key)), None)

    # ------------------------------------------------ 打开某个层级 + 筛
    def open_level(self, level: str):
        """切到「单元」/「创意」那张表（顶部 tab，直接 goto 就行）。"""
        if level not in LEVELS:
            raise FillError(f"认不出层级「{level}」（只有 unit / creative）")
        self.level = level
        self.page.goto(LEVELS[level]["url"], wait_until="domcontentloaded")
        self.wait_table()

    def filter_by(self, label: str, value: str):
        """按 label 文字往筛选表单里填一个值。填不进去就报错（选择器漂了）。"""
        res = self.page.evaluate(_MARK_JS, label)
        if res != "ok":
            raise FillError(f"筛选表单里没找到「{label}」这一格（{res}）—— 后台改版了？")
        try:
            self.page.locator('input[data-cc-field="1"]').first.fill(str(value))
        finally:
            self.page.evaluate(_MARK_JS, "")
        got = self.page.evaluate(
            "(l) => { const clean=s=>(s||'').replace(/\\s+/g,' ').trim();"
            " const it=[...document.querySelectorAll('.ant-form-item')].find(x=>{"
            "   const b=x.querySelector('.ant-form-item-label');"
            "   return b && clean(b.innerText).replace(/[:：]\\s*$/,'')===l;});"
            " const i=it&&it.querySelector('input'); return i?i.value:''; }", label)
        if norm(got) != norm(value):
            raise FillError(f"「{label}」填进去是「{got}」，和要填的「{value}」对不上")

    def search(self) -> dict:
        """点「查 询」并等表格重新加载完。"""
        before = [r.get("key") for r in self.snapshot().get("rows", [])]
        if self.page.evaluate(_MARK_BTN_JS, "查询") != "ok":
            raise FillError("筛选区找不到「查 询」按钮")
        try:
            self.page.locator('button[data-cc-btn="1"]').first.click()
        finally:
            self.page.evaluate(_MARK_BTN_JS, "")
        # 先等「loading 起来 / 行变了」，再等 settle —— 只等 settle 会读到点之前那一屏
        wait_until(self.page,
                   lambda: bool(self.snapshot().get("loading"))
                   or [r.get("key") for r in self.snapshot().get("rows", [])] != before,
                   min(self.timeout, 8000))
        return self.wait_table()

    def _applied(self, conditions: dict) -> bool:
        """这次查询到底生效了没：看条件有没有被推到地址栏。

        这个后台查完会把条件写进 URL（`?unit_act_id=708` /
        `?originality_id=134226` / `?originality_act_name=<百分号编码>`），
        所以「每个值都能在 URL 里找到」就是生效了 —— 比只看有没有 `?` 严，
        连着查好几个 ID 时不会被上一次留下的参数骗过去。
        """
        try:
            url = unquote(self.page.url)
        except Exception:
            url = self.page.url
        return all(str(v) in url for v in conditions.values())

    def query(self, level: str, conditions: dict) -> dict:
        """打开某个层级 + 按条件筛 + 确认筛真的生效了。列表页的唯一入口。

        ⚠ 为什么要确认：表格出来了 ≠ 筛选表单能用了。**刚 goto 完那几秒**里，
          往输入框里填值看得见（DOM 的 value 确实变了）、点「查 询」也点得动，
          但 antd Form 的 store 还没接上，带出去的条件是空的 —— 查回来还是全站
          四万条，**一句报错都没有**。

        ⚠ 所以重试**不能靠重新进页** —— 冷页正是失败的原因，重进等于每次都先撞一次墙。
          正确的做法是：页已经在这个层级、上次也是同一组条件，就**原地改值重查**
          （表单是热的，一次就成）；只有原地重来两次还不行才整页重进当兜底。
        """
        if level not in LEVELS:
            raise FillError(f"认不出层级「{level}」（只有 unit / creative）")
        # 能原地重查的条件：还停在这个层级的列表页，且上一次填的就是同样这几格
        # （格子对不上的话，旧值还留在表单里，会一起被带进查询）
        warm = (self.level == level
                and LEVELS[level]["url"].rsplit("/", 1)[-1]
                == self.page.url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
                and self._last_keys == set(conditions))
        last = ""
        for attempt in range(4):
            try:
                if not warm:
                    self.open_level(level)
                    # 只象征性等一下就够：多等换不来第一发命中（试过 1.5s，
                    # 冷页照样筛不上，纯亏时间），真正管用的是失败后原地重来
                    self.page.wait_for_timeout(700 + 600 * attempt)
                self.level = level
                for label, value in conditions.items():
                    self.filter_by(label, value)
                self._last_keys = set(conditions)
                snap = self.search()
            except FillError as e:
                # 原地重查时页面可能正好在重绘，填不进去 —— 退回整页重进再来
                if not warm and attempt >= 2:
                    raise
                last = str(e)
                self._note(f"这次查询没走通（{e}），重进页面再来…")
                warm = False
                continue
            if self._applied(conditions):
                return snap
            last = f"{snap.get('total')} 条"
            self._note(f"筛选没生效（还是 {last}），第 {attempt + 2} 次重试…")
            # ⚠ 失败后**原地再来一次**，别重进页面：失败基本都是「页面刚加载完、
            #   表单还没接上」，这会儿页已经热了，重填重查一次就成；重进页面等于
            #   把自己送回冷启动，实测会连着失败好几轮。
            #   原地也不行才整页重进（那说明是别的毛病），两种交替着来。
            if warm:
                warm = False
            else:
                warm = True
                self.page.wait_for_timeout(1200)
        raise FillError(
            f"填了 {'、'.join(f'{k}={v}' for k, v in conditions.items())} 点了查询，"
            f"但列表没被筛过（{last}）—— 页面加载太慢或后台改版了，"
            f"手动在浏览器里查一次看看")

    def big_page(self):
        """把每页条数拨到 100（选项 10/20/50/100），少翻几页。拨不动就算了。"""
        try:
            before = len(self.snapshot().get("rows", []))
            if self.page.evaluate(_SIZE_OPEN_JS) != "opened":
                return
            self.page.wait_for_timeout(400)
            picked = self.page.evaluate(
                _PICK_JS, [".ant-select-dropdown", "100 条/页", "100条/页", "100"])
            if not str(picked).startswith("clicked"):
                return
            # 换页长会重新拉数据，行数变了才算换成了
            wait_until(self.page,
                       lambda: len(self.snapshot().get("rows", [])) != before
                       or self.snapshot().get("total", 0) <= before,
                       max(self.timeout, 15000))
            self.wait_table()
        except Exception:
            log.debug("拨每页条数失败，用默认的", exc_info=True)

    # ------------------------------------------------ 翻页
    def _page_move(self, direction: str) -> bool:
        before = [r.get("key") for r in self.snapshot().get("rows", [])]
        try:
            if self.page.evaluate(_PAGER_JS, [self.L["id_col"], direction]) != "clicked":
                return False
        except Exception:
            return False
        wait_until(self.page,
                   lambda: [r.get("key") for r in self.snapshot().get("rows", [])] != before,
                   self.timeout)
        self.page.wait_for_timeout(200)
        return True

    def next_page(self) -> bool:
        return self._page_move("next")

    def first_page(self):
        for _ in range(60):
            if self.snapshot().get("page", 1) <= 1:
                return
            if not self._page_move("prev"):
                return

    # ------------------------------------------------ 「更多」菜单
    def menu_items(self, key: str) -> list:
        """把某一行的「更多」浮层拉开，读回菜单项文字。

        ⚠ 这个下拉是 **hover 触发**的，而且表格刚重绘完那一下经常拉不开
          （实测第一次 hover 读回来是空的，鼠标移开再来一次就有了）。所以这里
          「移开 → hover → 读」重试几轮，别改成单次 hover。
        """
        tr = self.page.locator(f'tr.ant-table-row[data-row-key="{key}"]')
        if not tr.count():
            raise FillError(f"当前页没有 {self.L['id_col']}={key} 这一行")
        more = tr.locator("td").last.locator("a", has_text="更多").first
        if not more.count():
            return []
        for i in range(4):
            try:
                self.page.mouse.move(4, 4)
                self.page.wait_for_timeout(250)
                more.scroll_into_view_if_needed()
                more.hover()
            except Exception:
                continue
            self.page.wait_for_timeout(500 + 300 * i)
            items = self.page.evaluate(_MENU_READ_JS) or []
            if items:
                return items
        return []

    def close_menu(self):
        try:
            self.page.mouse.move(4, 4)
            self.page.keyboard.press("Escape")
        except Exception:
            pass

    # ------------------------------------------------ 开 / 关
    def toggle(self, key: str, want: str) -> str:
        """把 key 这一行切到 want（'on' = 启动投放 / 'off' = 暂停投放）。

        返回 'ok'（点了并且状态翻过来了）/ 'skip'（本来就是目标态）。
        其它情况抛 FillError。⚠ 只在当前页找；翻页是调用方的事。
        ⚠ 永远不会点「终止投放」—— 那一步不可逆。
        """
        item = MENU_ON if want == "on" else MENU_OFF
        want_state = STATE_ON if want == "on" else STATE_OFF

        row = self.row(key)
        if not row:
            raise FillError(f"当前页没有 {self.L['id_col']}={key} 这一行")
        if norm(row.get("state")) == want_state:
            return "skip"

        items = self.menu_items(key)
        if item not in items:
            self.close_menu()
            raise FillError(
                f"「{row.get('name') or key}」（{self.L['id_col']}={key}，"
                f"状态「{row.get('state')}」）的「更多」里没有「{item}」"
                + (f"，只有：{'、'.join(items)}" if items else "，菜单一项都没拉开"))
        if self.page.evaluate(_PICK_JS, [".ant-dropdown", item]) == "not-found":
            self.close_menu()
            raise FillError(f"「{item}」这一项点不动（浮层刚好收了？）")

        # 二次确认弹窗：先读一眼，确认写的确实是这个动作，再点「确 定」
        if not wait_until(self.page, lambda: bool(self.page.evaluate(_MODAL_JS, "read")),
                          min(self.timeout, 8000)):
            raise FillError(f"点了「{item}」但没弹出确认框，不敢接着点")
        text = self.page.evaluate(_MODAL_JS, "read") or ""
        if item not in text:
            self.page.evaluate(_MODAL_JS, "cancel")
            raise FillError(f"确认框写的是「{text[:60]}」，和「{item}」对不上，已取消")
        if self.page.evaluate(_MODAL_JS, "ok") != "clicked":
            raise FillError(f"确认框里没找到「确 定」：{text[:60]}")

        limit = max(self.timeout, 15000)
        if not wait_until(self.page,
                          lambda: norm((self.row(key) or {}).get("state")) == want_state,
                          limit):
            now = (self.row(key) or {}).get("state", "(行没了)")
            toast = "；".join(self.page.evaluate(_TOAST_JS) or [])
            raise FillError(
                f"点了「{item}」，等了 {limit // 1000}s 状态还是「{now}」"
                + (f"（页面提示：{toast}）" if toast else ""))
        return "ok"
