"""跨策略：把用户填的「策略」清单解析成一批可 goto 的编辑页。

用户在「批量开关」卡的第二个文本框里一行填一条，三种写法混填都行：

  · 编辑页 URL   .../strategy-center/list/edit/186   → 取 186
  · 路由数字ID   186                                → 直接用
  · 业务ID       07135930239440（14 位那种）        → 去策略列表页查出路由ID

判据（`classify`）：带 `/edit/<n>` 的当 URL；纯数字且 ≤7 位当路由ID；
纯数字且 ≥8 位当业务ID。别的报错。

业务ID → 路由ID：扫策略列表页那张表，`data-row-key` 是路由ID、`td[1]` 是业务ID，
一次把全部（实测 192 条 / 10 页）读成 `{业务ID: (路由ID, 名称)}`。
详见 docs/价格策略批量开关-配置项抓取.md §七之二。
"""
from __future__ import annotations

import logging
import re

from .fill_core import wait_until

log = logging.getLogger(__name__)

LIST_URL = ("https://rich-vip.bilibili.co/manage/v/experiment-manage/"
            "strategy-center/list")
EDIT_URL = LIST_URL + "/edit/{route_id}"

_EDIT_RE = re.compile(r"/edit/(\d+)")

_LIST_SCAN_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const tables = [...document.querySelectorAll('table')];
  // 列表页那张表：thead 有「策略id」和「策略名称」
  const tbl = tables.find(t => {
    const h = [...t.querySelectorAll('thead th')].map(x => x.innerText.replace(/\s+/g, ''));
    return h.includes('策略id') && h.includes('策略名称');
  });
  if (!tbl) return { found: false, rows: [], page: 1, pages: 1 };
  const H = [...tbl.querySelectorAll('thead th')].map(x => x.innerText.replace(/\s+/g, ''));
  const bi = H.indexOf('策略id'), ni = H.indexOf('策略名称');
  const rows = [...tbl.querySelectorAll('tbody tr.ant-table-row')].map(tr => {
    const td = [...tr.querySelectorAll('td')];
    return {
      route: tr.getAttribute('data-row-key') || '',
      biz: bi >= 0 && td[bi] ? clean(td[bi].innerText) : '',
      name: ni >= 0 && td[ni] ? clean(td[ni].innerText) : '',
    };
  });
  let pager = null, n = tbl;
  for (let i = 0; i < 6 && n; i++) {
    const p = n.parentElement;
    if (p) { const pg = p.querySelector('.ant-pagination'); if (pg) { pager = pg; break; } }
    n = p;
  }
  let page = 1, pages = 1;
  if (pager) {
    const a = pager.querySelector('.ant-pagination-item-active');
    if (a) page = parseInt(a.getAttribute('title') || a.textContent) || 1;
    const items = [...pager.querySelectorAll('.ant-pagination-item')]
      .map(li => parseInt(li.getAttribute('title') || li.textContent) || 0);
    if (items.length) pages = Math.max(...items, page);
  }
  return { found: true, rows, page, pages };
}
"""

_LIST_NEXT_JS = r"""
() => {
  const tables = [...document.querySelectorAll('table')];
  const tbl = tables.find(t => {
    const h = [...t.querySelectorAll('thead th')].map(x => x.innerText.replace(/\s+/g, ''));
    return h.includes('策略id') && h.includes('策略名称');
  });
  if (!tbl) return 'no-table';
  let pager = null, n = tbl;
  for (let i = 0; i < 6 && n; i++) {
    const p = n.parentElement;
    if (p) { const pg = p.querySelector('.ant-pagination'); if (pg) { pager = pg; break; } }
    n = p;
  }
  if (!pager) return 'no-pager';
  const li = pager.querySelector('.ant-pagination-next');
  if (!li) return 'no-btn';
  const cls = typeof li.className === 'string' ? li.className : '';
  if (cls.includes('ant-pagination-disabled') || li.getAttribute('aria-disabled') === 'true')
    return 'disabled';
  (li.querySelector('button') || li).click();
  return 'clicked';
}
"""


def classify(token: str) -> tuple[str, str]:
    """一行 → (kind, value)。kind ∈ {route, biz, bad}。"""
    t = (token or "").strip()
    if not t:
        return "bad", ""
    m = _EDIT_RE.search(t)
    if m:
        return "route", m.group(1)
    if t.isdigit():
        return ("biz", t) if len(t) >= 8 else ("route", t)
    return "bad", t


def parse_tokens(text: str) -> list[str]:
    out = []
    for line in str(text or "").replace("\r", "\n").split("\n"):
        for piece in re.split(r"[,，;；\s]+", line.strip()):
            if piece:
                out.append(piece)
    return out


class StrategyResolver:
    """一次运行里复用：业务ID→路由ID 的表只扫一遍。"""

    def __init__(self, page, timeout: int = 15000, on_note=None):
        self.page = page
        self.timeout = timeout
        self._on_note = on_note
        self._map: dict[str, tuple[str, str]] | None = None   # biz -> (route, name)

    def _note(self, msg: str):
        if self._on_note:
            try:
                self._on_note(msg)
            except Exception:
                pass

    def _scan_list(self) -> dict[str, tuple[str, str]]:
        if self._map is not None:
            return self._map
        self._note("打开策略列表页，读「业务ID → 路由ID」对照表…")
        self.page.goto(LIST_URL, wait_until="domcontentloaded")
        if not wait_until(self.page, lambda: self.page.evaluate(_LIST_SCAN_JS).get("found"),
                          max(self.timeout * 2, 30000)):
            self._note("策略列表页没加载出来，业务ID 没法换算")
            self._map = {}
            return self._map
        m: dict[str, tuple[str, str]] = {}
        for _ in range(40):
            snap = self.page.evaluate(_LIST_SCAN_JS)
            for r in snap.get("rows", []):
                if r.get("biz") and r.get("route"):
                    m.setdefault(r["biz"], (r["route"], r.get("name", "")))
            if snap.get("page", 1) >= snap.get("pages", 1):
                break
            before = [r.get("route") for r in snap.get("rows", [])]
            if self.page.evaluate(_LIST_NEXT_JS) != "clicked":
                break
            wait_until(self.page,
                       lambda: [x.get("route") for x in self.page.evaluate(_LIST_SCAN_JS).get("rows", [])] != before,
                       self.timeout)
            self.page.wait_for_timeout(150)
        self._note(f"对照表读到 {len(m)} 条策略")
        self._map = m
        return m

    def resolve(self, tokens: list[str]) -> list[dict]:
        """→ [{token, route_id, name, ok, error}]，顺序同输入、去重。"""
        out: list[dict] = []
        seen: set[str] = set()
        need_biz = [t for t in tokens if classify(t)[0] == "biz"]
        bizmap = self._scan_list() if need_biz else {}
        for tok in tokens:
            kind, val = classify(tok)
            if kind == "bad":
                out.append({"token": tok, "route_id": "", "name": "",
                            "ok": False, "error": f"认不出「{tok}」（要 编辑页URL / 路由ID / 业务ID）"})
                continue
            if kind == "route":
                rid, name = val, ""
            else:
                hit = bizmap.get(val)
                if not hit:
                    out.append({"token": tok, "route_id": "", "name": "", "ok": False,
                                "error": f"业务ID「{val}」在策略列表里没找到"})
                    continue
                rid, name = hit
            if rid in seen:
                continue
            seen.add(rid)
            out.append({"token": tok, "route_id": rid, "name": name, "ok": True, "error": ""})
        return out
