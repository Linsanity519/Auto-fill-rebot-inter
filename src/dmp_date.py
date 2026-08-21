"""DMP 人群延期弹窗里的日期面板操作。

单独一个文件，不被别的执行器引用 —— 加这套逻辑不会影响价格配置和资源位投放。

要解决的两件事：
  1. 「延到某个具体日期」——面板默认只开在当前月，目标日期在下个月就得先翻页
  2. 「超过系统最大可选日期就取最大」——最大日期不在当前月，必须一个月一个月
     往后翻，直到翻不动或连着几个月都没有可选日期为止

⚠ 不能只看当前打开的那一个月：老实现取「当前面板里最后一个可选格子」，
  如果可选范围跨月（常见：今天到 90 天后），拿到的是本月最后一天，不是真正的上限。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime

log = logging.getLogger(__name__)

# 面板月份标题，如 antd 的「2026年8月」/ Element 的「2026 年 8 月」
_HEADER_RE = re.compile(r"(\d{4})\s*[年\-/.]\s*(\d{1,2})")


class DateError(Exception):
    """日期面板相关的失败。由调用方包成 FillError 抛出去。"""


def parse_date(value) -> date | None:
    """把用户填的日期解析成 date。认不出来返回 None。

    支持：date/datetime 对象、2026-08-20、2026/8/20、2026.8.20、20260820、
    以及 Excel 读出来的 '2026-08-20 00:00:00'。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    s = str(value).strip()
    if not s:
        return None
    s = s.split()[0]                      # 砍掉 '00:00:00'

    m = re.match(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    elif re.match(r"^\d{8}$", s):
        y, mo, d = int(s[:4]), int(s[4:6]), int(s[6:])
    else:
        return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def fmt(d: date) -> str:
    return d.strftime("%Y-%m-%d")


class DatePanel:
    """一个打开着的日期选择面板。

    cfg 是 config/forms/DMP延期.yaml 里那几个 selector 列表，页面改版只改 yaml。
    """

    def __init__(self, page, cfg: dict):
        self.page = page
        self.c = cfg
        self.max_forward = int(cfg.get("max_forward_months", 24))

    # ---------------- 打开 ----------------
    def open(self, label: str | None = None):
        """点开有效期输入框，等面板出现。

        先按「人群有效期至」这类字段文字找它后面的输入框；找不到再退回
        yaml 里的 date_input_selectors。

        ⚠ 点击是「切换」不是「打开」：面板已经开着时再点会把它关掉。
          所以先判状态，开着就直接用。
        """
        if self.is_open():
            return None

        inp = self._input_by_label(label) if label else None
        if inp is None:
            inp = self._input_by_selectors()
        if inp is None:
            raise DateError("人群延期弹窗里找不到有效期输入框（检查 yaml 的 date_field_label / date_input_selectors）")

        inp.click()
        self.page.wait_for_timeout(int(self.c.get("panel_open_wait", 500)))
        if not self.is_open():
            # 有的组件第一次点只聚焦不展开，再点一次
            inp.click()
            self.page.wait_for_timeout(500)
        if not self.is_open():
            raise DateError("点了有效期输入框但日期面板没出来（检查 yaml 的 panel_selectors / latest_date_selectors）")
        return inp

    PANEL_FALLBACK = ("[class*=picker-dropdown]:not([class*=picker-dropdown-hidden])",
                      ".el-picker-panel")

    def is_open(self) -> bool:
        """日期浮层是不是开着。

        ⚠ 不能拿「有没有可选格子」当判据：可选范围的最后一个月再往后翻，
          整月都是禁用的，一个可选格子都没有 —— 那时面板明明开着，
          却会被判成关着，再点一次就真关了。所以只认浮层容器本身。
        """
        for sel in (self.c.get("panel_selectors") or self.PANEL_FALLBACK):
            try:
                loc = self.page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    return True
            except Exception:
                continue
        return self._cell_selector() is not None

    def _input_by_label(self, label: str):
        pat = re.compile(rf"^\s*\*?\s*{re.escape(label)}\s*[:：]?\s*$")
        node = self.page.locator("label, span, div, td").filter(has_text=pat).last
        try:
            if not node.count():
                return None
            after = node.locator("xpath=following::input[not(@type='hidden')][1]")
            if after.count() and after.first.is_visible():
                return after.first
        except Exception:
            return None
        return None

    def _input_by_selectors(self):
        for selector in self.c.get("date_input_selectors") or ["input"]:
            loc = self.page.locator(selector)
            for i in range(loc.count()):
                cand = loc.nth(i)
                try:
                    if cand.is_visible() and cand.is_enabled():
                        return cand
                except Exception:
                    continue
        return None

    # ---------------- 面板内容 ----------------
    def _cell_selector(self) -> str | None:
        """当前面板里「可选日期格子」用哪个 selector 能命中。

        ⚠ 必须判可见，不能只判 count：antd 关掉面板时只是给浮层加个
          picker-dropdown-hidden，格子还留在 DOM 里。只数个数的话，
          面板明明关着也会被当成开着，后面所有点击全部落空。
        """
        for selector in self.c.get("latest_date_selectors") or []:
            try:
                loc = self.page.locator(selector)
                if loc.count() and loc.first.is_visible():
                    return selector
            except Exception:
                continue
        return None

    def header_month(self) -> tuple[int, int] | None:
        """面板当前显示的年月。读不出来返回 None（退化成只按格子文字算）。"""
        for selector in self.c.get("panel_header_selectors") or []:
            try:
                loc = self.page.locator(selector)
                for i in range(loc.count()):
                    if not loc.nth(i).is_visible():
                        continue
                    m = _HEADER_RE.search(loc.nth(i).inner_text() or "")
                    if m:
                        return int(m.group(1)), int(m.group(2))
            except Exception:
                continue
        return None

    def cells(self) -> list[tuple[int, date]]:
        """当前月所有可选格子，返回 [(下标, 日期)]。

        ⚠ 一次 evaluate_all 把 title/文字全捞回来，别逐个 get_attribute：
          翻 24 个月 × 30 格 = 700 多次跨进程调用，会慢到用户以为卡死。
        """
        selector = self._cell_selector()
        if not selector:
            return []
        loc = self.page.locator(selector)
        try:
            raw = loc.evaluate_all(
                "els => els.map(e => ({t: e.getAttribute('title') || '',"
                " x: (e.innerText || '').trim()}))")
        except Exception:
            return []

        ym = self.header_month()
        out = []
        for i, item in enumerate(raw):
            d = parse_date(item.get("t"))
            if d is None and ym and item.get("x", "").isdigit():
                try:
                    d = date(ym[0], ym[1], int(item["x"]))
                except ValueError:
                    d = None
            if d is not None:
                out.append((i, d))
        return out

    # ---------------- 翻月 ----------------
    def _click_arrow(self, key: str) -> bool:
        for selector in self.c.get(key) or []:
            loc = self.page.locator(selector)
            for i in range(loc.count()):
                btn = loc.nth(i)
                try:
                    if not btn.is_visible():
                        continue
                    cls = btn.get_attribute("class") or ""
                    if "disabled" in cls or btn.get_attribute("disabled") is not None:
                        continue
                    btn.click()
                    self.page.wait_for_timeout(int(self.c.get("month_wait", 260)))
                    return True
                except Exception:
                    continue
        return False

    def _step(self, forward: bool) -> bool:
        """翻一个月。翻不动（到边界 / 按钮禁用）返回 False。"""
        before = self.header_month()
        key = "next_month_selectors" if forward else "prev_month_selectors"
        if not self._click_arrow(key):
            return False
        after = self.header_month()
        if before and after and before == after:
            return False        # 点了但没动，当成到头了
        return True

    def goto_month(self, target: date) -> bool:
        """把面板翻到目标日期所在的月份。"""
        return self._goto_ym(target.year, target.month)

    def _goto_ym(self, year: int, month: int) -> bool:
        want = year * 12 + month
        for _ in range(self.max_forward * 2 + 2):
            ym = self.header_month()
            if ym is None:
                return True     # 读不到月份就别瞎翻，交给调用方按格子碰运气
            cur = ym[0] * 12 + ym[1]
            if cur == want:
                return True
            if not self._step(forward=want > cur):
                return False
        return False

    # ---------------- 对外 ----------------
    def max_date(self) -> date | None:
        """系统允许选到的最晚日期。

        从当前月往后翻，记下每个月最大的可选日期；连着 2 个月一个可选日期都没有，
        或者翻不动了，就认为到头了。

        ⚠ 走完必须把面板翻回起始月：不还原的话，面板停在「全是禁用日期」的月份上，
          下一次再算就从空月份起步，直接得出「没有任何可选日期」。
        """
        start = self.header_month()
        best = None
        empty_streak = 0
        for _ in range(self.max_forward):
            got = self.cells()
            if got:
                empty_streak = 0
                month_max = max(d for _, d in got)
                if best is None or month_max > best:
                    best = month_max
            else:
                empty_streak += 1
                if empty_streak >= 2:
                    break
            if not self._step(forward=True):
                break

        if start:
            self._goto_ym(*start)
        return best

    def pick(self, target: date) -> date:
        """点选目标日期。不在可选范围内时抛 DateError。"""
        if not self.goto_month(target):
            raise DateError(f"日期面板翻不到 {fmt(target)} 所在的月份")

        selector = self._cell_selector()
        if not selector:
            raise DateError("日期面板里没有可选的日期格子")

        for idx, d in self.cells():
            if d == target:
                self.page.locator(selector).nth(idx).click()
                self.page.wait_for_timeout(int(self.c.get("after_pick_wait", 300)))
                return d
        raise DateError(f"{fmt(target)} 在面板里不可选")

    def pick_capped(self, target: date | None,
                    limit: date | None = None) -> tuple[date, bool, date]:
        """按需求选日期，返回 (实际选中的日期, 是否被上限截断, 用到的上限)。

        target 为空          → 直接选系统最大可选日期
        target 超过系统上限  → 选系统最大可选日期（需求里明确要求这么兜底）

        limit 可以由调用方传进来。系统上限是「今天 + N 天」，一次批量运行里
        对所有人群都一样，没必要每个人群都重新翻六个月的面板 —— 传上次算出来的
        值进来能省掉绝大部分翻页。传错了也不会写错数据：pick() 选不中会抛
        DateError，调用方据此重算即可。
        """
        if limit is None:
            limit = self.max_date()
        if limit is None:
            raise DateError("日期面板里找不到任何可选日期；对照截图更新 yaml 的 latest_date_selectors")

        if target is None or target > limit:
            return self.pick(limit), target is not None, limit
        return self.pick(target), False, limit
