"""策略编辑页底部「价格配置」表的读 + 点开关。

⚠ 独立于别的 filler。这页是 Ant Design + ProComponents，满页 css-1a75fj6 /
  css-var-«ra1» 编译哈希，**一个都不能用**。定位靠语义 class + 文字，见
  docs/价格策略批量开关-配置项抓取.md。

这套 DOM 特有的三件事都很轻：

  1. 找表        —— 全页两张 table，挑 thead 同时含「状态」「操作」的那张
  2. 读行        —— 一次 page.evaluate 把每行的 名称/人群选组/状态/操作链接文字读回来
  3. 点开关      —— 行内那个裸 <a>开启</a> / <a>关闭</a>，点一下直接生效、没有确认弹窗

翻页：表格自带 antd 分页，默认每页 10 条，超了逐页 next。
"""
from __future__ import annotations

import logging
import re

from .fill_core import FillError, norm, note, wait_until

log = logging.getLogger(__name__)

# 一次读回整页的行 + 分页信息。判据全用结构和文字，不碰 css-hash。
_READ_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const tables = [...document.querySelectorAll('table')];
  const tbl = tables.find(t => {
    const h = [...t.querySelectorAll('thead th')].map(x => x.innerText.replace(/\s+/g, ''));
    return h.includes('状态') && h.includes('操作');
  });
  if (!tbl) return { found: false, empty: false, loading: false, rows: [], page: 1, pages: 1, total: 0 };

  // 表骨架在、但数据还没回来的两种样子：antd 的 loading 遮罩 / 「暂无数据」占位
  const wrap = tbl.closest('.ant-table-wrapper') || tbl.parentElement;
  const loading = !!(wrap && wrap.querySelector('.ant-spin-spinning'));
  const empty = !!(tbl.querySelector('.ant-table-placeholder')
                   || (tbl.querySelector('.ant-empty') && !tbl.querySelector('tbody tr.ant-table-row')));

  const H = [...tbl.querySelectorAll('thead th')].map(x => x.innerText.replace(/\s+/g, ''));
  const ni = 0;
  const gi = H.indexOf('人群选组');
  const si = H.indexOf('状态');
  const oi = H.indexOf('操作');

  const rows = [...tbl.querySelectorAll('tbody tr.ant-table-row')].map(tr => {
    const td = [...tr.querySelectorAll('td')];
    const opCell = td[oi];
    let link = '';
    if (opCell) {
      for (const a of opCell.querySelectorAll('a')) {
        const t = clean(a.textContent);
        if (t === '开启' || t === '关闭') { link = t; break; }
      }
    }
    return {
      key: tr.getAttribute('data-row-key') || '',
      name: clean(td[ni] ? td[ni].innerText : ''),
      group: gi >= 0 && td[gi] ? clean(td[gi].innerText) : '',
      state: si >= 0 && td[si] ? clean(td[si].innerText) : '',
      link: link,
    };
  });

  // 分页：找这张表底下的 antd 分页条
  let pager = null, n = tbl;
  for (let i = 0; i < 6 && n; i++) {
    const p = n.parentElement;
    if (p) { const pg = p.querySelector('.ant-pagination'); if (pg) { pager = pg; break; } }
    n = p;
  }
  let page = 1, pages = 1, total = rows.length;
  if (pager) {
    const totalText = pager.querySelector('.ant-pagination-total-text');
    if (totalText) {
      const m = totalText.textContent.match(/(\d+)\s*条/g);
      if (m && m.length) total = parseInt(m[m.length - 1]) || total;
    }
    const active = pager.querySelector('.ant-pagination-item-active');
    if (active) page = parseInt(active.getAttribute('title') || active.textContent) || 1;
    const items = [...pager.querySelectorAll('.ant-pagination-item')]
      .map(li => parseInt(li.getAttribute('title') || li.textContent) || 0);
    if (items.length) pages = Math.max(...items, page);
  }
  return { found: true, empty, loading, rows, page, pages, total };
}
"""


class PtToggleFiller:
    def __init__(self, page, timeout: int = 15000, on_note=None):
        self.page = page
        self.timeout = timeout
        self._on_note = on_note

    def _note(self, msg: str):
        note(self._on_note, msg)

    # ------------------------------------------------ 读
    def snapshot(self) -> dict:
        """当前页：{found, rows:[{key,name,group,state,link}], page, pages, total}。"""
        try:
            data = self.page.evaluate(_READ_JS)
        except Exception as e:
            raise FillError(f"读「价格配置」表失败：{e}")
        return data or {"found": False, "rows": [], "page": 1, "pages": 1, "total": 0}

    @staticmethod
    def _settled(snap: dict) -> bool:
        """表骨架在、loading 遮罩没了、要么有行要么明确「暂无数据」。"""
        return bool(snap.get("found") and not snap.get("loading")
                    and (snap.get("rows") or snap.get("empty")))

    def wait_table(self, timeout: int | None = None) -> dict:
        t = timeout or self.timeout
        if not wait_until(self.page, lambda: self._settled(self.snapshot()), t):
            snap = self.snapshot()
            if not snap.get("found"):
                raise FillError(
                    "页面上没找到「价格配置」表（表头要同时有「状态」「操作」两列）。"
                    "是不是没停在策略编辑页？")
            raise FillError("「价格配置」表一直在加载中，等超时了 —— 网络太慢或页面没就绪")
        return self.snapshot()

    def open_strategy(self, route_id: str):
        """跳到某条策略的编辑页并等「价格配置」表加载完（跨策略用）。

        ⚠ 直接 goto 策略编辑页，有的时候那张「价格配置」表的数据不回来（表头在、
          一行不加载，也不发请求）—— 实测冷跳转 36 秒都等不到。这条路是尽力而为：
          等不到就把话说清楚，让用户自己在浏览器里打开那条策略页、改用
          「当前打开的策略页」跑。
        """
        from .pt_strategy import EDIT_URL
        url = EDIT_URL.format(route_id=route_id)
        if not self.page.url.startswith(url.split("#")[0]):
            self.page.goto(url, wait_until="domcontentloaded")
        try:
            self.wait_table(max(self.timeout * 3, 45000))
        except FillError as e:
            raise FillError(
                f"跳转到策略 {route_id} 的编辑页后，「价格配置」表没加载出来（{e}）。"
                f"这条后台对直接跳转支持不稳 —— 请在浏览器里手动打开这条策略页，"
                f"再用「策略范围 = 当前打开的策略页」跑。")

    # ------------------------------------------------ 翻页
    #
    # ⚠ 全页有两张表（价格配置 + 操作记录），各带一个 antd 分页条。翻页动作必须
    #   点**价格配置那张表自己**的分页 —— 所以用一段 JS 定位到目标表、再找它最近的
    #   .ant-pagination，点里面的 上一页/下一页。page 级的 .ant-pagination-next
    #   会撞上另一张表。
    _PAGER_CLICK_JS = r"""
    (dir) => {
      const tables = [...document.querySelectorAll('table')];
      const tbl = tables.find(t => {
        const h = [...t.querySelectorAll('thead th')].map(x => x.innerText.replace(/\s+/g, ''));
        return h.includes('状态') && h.includes('操作');
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
      const btn = li.querySelector('button') || li;
      btn.click();
      return 'clicked';
    }
    """

    def _page_move(self, direction: str) -> bool:
        before = [r["key"] for r in self.snapshot().get("rows", [])]
        try:
            res = self.page.evaluate(self._PAGER_CLICK_JS, direction)
        except Exception:
            return False
        if res != "clicked":
            return False
        wait_until(self.page,
                   lambda: [r["key"] for r in self.snapshot().get("rows", [])] != before,
                   self.timeout)
        self.page.wait_for_timeout(200)
        return True

    def next_page(self) -> bool:
        """翻到下一页；已经是最后一页返回 False。翻完等表格重新渲染。"""
        return self._page_move("next")

    def first_page(self):
        """回到第 1 页（翻过页之后要复位）。"""
        for _ in range(50):
            if self.snapshot().get("page", 1) <= 1:
                return
            if not self._page_move("prev"):
                return

    # ------------------------------------------------ 点开关
    def toggle(self, name: str, want: str) -> str:
        """把当前页上叫 name 的行切到 want（'on' / 'off'）。

        返回：
          'ok'    点了、且行内文字翻转成功
          'skip'  已经是目标态
        其它情况抛 FillError。
        ⚠ 只在当前页找；调用方负责翻到对的页。
        """
        want_link = "关闭" if want == "on" else "开启"     # 目标态下这行该显示的操作文字
        cur_link = "开启" if want == "on" else "关闭"       # 现在（未达标时）该点的那个字

        rows = [r for r in self.snapshot().get("rows", []) if norm(r["name"]) == norm(name)]
        if not rows:
            raise FillError(f"当前页没有叫「{name}」的行")
        if len(rows) > 1:
            self._note(f"「{name}」在当前页有 {len(rows)} 行同名，操作第 1 行")
        row = rows[0]

        if row["link"] == want_link:
            return "skip"
        if row["link"] != cur_link:
            raise FillError(
                f"「{name}」操作列的文字是「{row['link'] or '(空)'}」，"
                f"既不是「开启」也不是「关闭」，不敢动")

        tr = self._row_locator(name)
        link = tr.locator("td").last.locator("a").filter(
            has_text=re.compile(rf"^\s*{cur_link}\s*$")).first
        if not link.count():
            raise FillError(f"「{name}」行里找不到「{cur_link}」链接")
        link.click()

        ok = wait_until(
            self.page,
            lambda: next((norm(r["link"]) == want_link
                          for r in self.snapshot().get("rows", [])
                          if norm(r["name"]) == norm(name)), False),
            self.timeout)
        if not ok:
            now = next((r["link"] for r in self.snapshot().get("rows", [])
                        if norm(r["name"]) == norm(name)), "?")
            raise FillError(
                f"点了「{name}」的「{cur_link}」，但等了 {self.timeout // 1000}s "
                f"操作列还是「{now}」，没生效")
        return "ok"

    def _row_locator(self, name: str):
        exact = re.compile(rf"^\s*{re.escape(name)}\s*$")
        return self.page.locator("tr.ant-table-row").filter(
            has=self.page.locator("td").first.filter(has_text=exact)).first
