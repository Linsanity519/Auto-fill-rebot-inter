"""三连竞价推广 auto-v2 页面的控件填写（mode: ad_v2）。

⚠ 独立于 src/filler.py / src/wizard_filler.py / src/ad_filler.py。
  老的「原生商广」是 ad_filler（iView），这个页面是另一套 DOM。

这个页面混了三套：
  1. 项目层主表单 —— B 站自研 bd- 组件。单选是 label.bd-radio-button，
     选中态看 class 里有没有 **is-active**（不是老页面的 active）。
  2. 推广目的 —— 老的 .ppt-new-item 卡片（.ppt-title + active）。
  3. 两个抽屉 —— 还是 iView：
       编辑定向  .ivu-drawer（body 是 .ivu-drawer-body），人群包结构同「原生商广老」
       添加稿件  .product-select-drawer（打开时加 .open），搜索框是 .ivu-input

素材层是「聚合配置」：稿件池 / 标题池 / 封面池 + 一条描述，见 add_* 方法。
日期是纯日历面板（bd-date-editor，range-input 只读），只能点日子格 —— 见 _bd_date_range。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .ad_image import ImageError, shrink
from .filler import FillError

log = logging.getLogger(__name__)

BD_ITEM = ".bd-form-item"
BD_LABEL = ".bd-form-item__label"
CARD = (".bd-radio-button, .ppt-new-item, .launch-type-item-new, "
        ".radio-item, .bd-radio")
ACTIVE_RE = re.compile(r"(^|\s)(is-active|active|is-checked|bd-radio-button--checked|"
                       r"ivu-radio-wrapper-checked)(\s|$)")
DRAWER_TARGETING = ".ivu-drawer"
DRAWER_PICKER = ".product-select-drawer"
_MONTH_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")


class Adv2Filler:
    def __init__(self, page, timeout: int = 15000):
        self.page = page
        self.timeout = timeout

    # ============================================================ 项目层
    def fill(self, fields: list[dict], values: dict, scope: str = ""):
        for f in fields:
            if not self._applies(f, values):
                continue
            handler = self.HANDLERS.get(f.get("type"))
            if handler is None:
                raise FillError(f"字段「{f['name']}」的 type={f.get('type')} 不认识")

            value = self._value_of(f, values)
            if value == "" and f.get("type") != "audience":
                if f.get("required"):
                    raise FillError(f"{scope}必填字段「{f['name']}」没有值")
                continue
            try:
                handler(self, f, value, values)
            except FillError:
                raise
            except Exception as e:
                raise FillError(f"{scope}填「{f['name']}」失败：{e}") from e

    @staticmethod
    def _applies(f: dict, values: dict) -> bool:
        when = f.get("when")
        if not when:
            return True
        name, want = when[0], str(when[1])
        return str(values.get(name, "")).strip() == want

    @staticmethod
    def _value_of(f: dict, values: dict) -> str:
        if "value" in f:
            return str(f["value"])
        src = f.get("from_prep")
        if isinstance(src, str):
            got = str(values.get(src, "")).strip()
            return got or str(f.get("default", ""))
        got = values.get(f["name"], "")
        got = "" if got is None else str(got).strip()
        return got or str(f.get("default", ""))

    # ------------------------------------------------ 定位
    def _bd_item(self, label: str):
        exact = re.compile(rf"^\s*\*?\s*{re.escape(label)}\s*[:：]?\s*$")
        item = self.page.locator(BD_ITEM).filter(
            has=self.page.locator(BD_LABEL, has_text=exact))
        try:
            item.first.wait_for(state="attached", timeout=self.timeout)
            return item.first
        except Exception:
            item = self.page.locator(BD_ITEM).filter(
                has=self.page.locator(BD_LABEL, has_text=re.compile(re.escape(label))))
            if not item.count():
                raise FillError(f"按 label「{label}」找不到 bd-form-item")
            return item.first

    def _click_card(self, scope, value: str, label: str):
        # ⚠ 改了上游字段（比如推广目的）之后，这一项的卡片是随后才挂上来的，
        #   立刻取会是 0 个 —— 轮询到有卡片为止，再按文字挑。
        cards = scope.locator(CARD)
        waited = 0
        while cards.count() == 0 and waited < self.timeout:
            self.page.wait_for_timeout(400)
            waited += 400
        seen = []
        for i in range(cards.count()):
            c = cards.nth(i)
            text = (c.inner_text() or "").strip().split("\n")[0].strip()
            if not text:
                continue
            seen.append(text)
            if text != value:
                continue
            cls = c.get_attribute("class") or ""
            if "disabled" in cls or "is-disabled" in cls:
                raise FillError(f"「{label}」的选项「{value}」是禁用状态")
            if not ACTIVE_RE.search(cls):
                c.click()
                self.page.wait_for_timeout(300)
            return
        raise FillError(f"「{label}」下没有选项「{value}」。实际有：{seen}")

    # ------------------------------------------------ 控件
    def _bd_radio(self, f, value, values):
        # 上游字段联动后这一组会整组替换，可能读到上一套选项 —— 多试几次
        last = None
        for attempt in range(8):
            try:
                self._click_card(self._bd_item(f["label"]), value, f["label"])
                return
            except FillError as e:
                last = e
                if "没有选项" not in str(e) or attempt == 7:
                    raise
                self.page.wait_for_timeout(1200)
        if last:
            raise last

    def _ppt_card(self, f, value, values):
        cards = self.page.locator(".ppt-new-item")
        waited = 0
        while cards.count() == 0 and waited < self.timeout:
            self.page.wait_for_timeout(400)
            waited += 400
        seen, target = [], None
        for i in range(cards.count()):
            c = cards.nth(i)
            title = (c.locator(".ppt-title").first.inner_text() or "").strip()
            seen.append(title)
            if title == value:
                target = c
                break
        if target is None:
            raise FillError(f"「推广目的」下没有「{value}」。实际有：{seen}")

        # ⚠ 这张卡点一下常常不生效（Vue 重渲染吃掉事件）—— 点完必须回读它有没有
        #   变成 active，没变就再点。推广目的定错，下面「推广内容」整组就是另一套。
        for attempt in range(6):
            if ACTIVE_RE.search(target.get_attribute("class") or ""):
                if attempt:
                    self.page.wait_for_timeout(1500)   # 让推广内容重渲染
                return
            target.scroll_into_view_if_needed()
            try:
                target.click(timeout=4000)
            except Exception:
                target.evaluate("e => e.click()")
            self.page.wait_for_timeout(1400)
        raise FillError(f"点了「推广目的={value}」6 次，它还是没变成选中态")

    def _card_by_text(self, f, value, values):
        cards = self.page.locator(CARD).filter(
            has_text=re.compile(rf"^\s*{re.escape(value)}"))
        if not cards.count():
            raise FillError(f"页面上找不到选项「{value}」")
        card = cards.first
        if not ACTIVE_RE.search(card.get_attribute("class") or ""):
            card.click()
            self.page.wait_for_timeout(300)

    def _bd_fill(self, f, value, values):
        ph = f["ph"]
        if f.get("label"):
            scope = self._bd_item(f["label"])
            el = scope.locator(
                f'input[placeholder*="{ph}"], textarea[placeholder*="{ph}"]').first
            if not el.count():
                el = scope.locator("input, textarea").first
        else:
            loc = self.page.locator(
                f'input[placeholder*="{ph}"]:visible, textarea[placeholder*="{ph}"]:visible')
            if not loc.count():
                raise FillError(f"页面上没有 placeholder 含「{ph}」的输入框")
            el = loc.first
        if not el.count():
            raise FillError(f"找不到输入框（placeholder 含「{ph}」）")
        el.click()
        el.fill("")
        el.fill(value)
        self.page.wait_for_timeout(150)

    def _bd_select(self, f, value, values):
        item = self._bd_item(f["label"])
        wrap = item.locator(".bd-select__wrapper, .bd-select").first
        if not wrap.count():
            raise FillError(f"「{f['label']}」下没有 bd-select")
        cur = item.locator(".bd-select__selected-item, .bd-select__selection").first
        if cur.count() and (cur.inner_text() or "").strip() == value:
            return
        wrap.click()
        self.page.wait_for_timeout(600)
        opts = self.page.locator(
            ".bd-select-dropdown__list li, .bd-select-dropdown li, "
            ".bd-select__popper li, [class*='select-dropdown'] li")
        hit = opts.filter(has_text=re.compile(rf"^\s*{re.escape(value)}\s*$")).first
        if not hit.count():
            avail = opts.all_inner_texts()
            self.page.keyboard.press("Escape")
            raise FillError(f"「{f['label']}」没有选项「{value}」。实际有：{avail}")
        hit.click()
        self.page.wait_for_timeout(400)

    # ---- 日期区间：纯日历面板（bd-picker-panel.bd-date-range-picker），
    #      range-input 只读，只能点日子格 ----
    def _bd_date_range(self, f, value, values):
        start, end = self._parse_range(f, value)
        ed = self.page.locator(".bd-date-editor").first
        if not ed.count():
            raise FillError("找不到 .bd-date-editor 日期控件")
        ed.locator(".bd-range-input, input").first.click()
        self.page.wait_for_timeout(700)
        panel = self.page.locator(".bd-picker-panel.bd-date-range-picker").last
        try:
            panel.wait_for(state="visible", timeout=self.timeout)
        except Exception as e:
            raise FillError("日期面板没打开") from e

        self._pick_day(*start)
        self.page.wait_for_timeout(500)
        self._pick_day(*end)
        self.page.wait_for_timeout(500)
        # 选完两头面板一般自己收；点掉标题当兜底
        try:
            self.page.locator(".bd-form__label, .bd-form-item__label").first.click(timeout=1500)
        except Exception:
            self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(300)

        got = (ed.locator(".bd-range-input, input").first.input_value() or "").strip()
        want = f"{start[0]:04d}-{start[1]:02d}-{start[2]:02d}"
        if got and got.replace(".", "-").replace("/", "-") != want:
            log.warning("日期回读「%s」和预期「%s」对不上（可能格式差异，不中断）", got, want)

    @staticmethod
    def _parse_range(f: dict, value: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        parts = re.split(r"\s+[-~]\s+|\s*~\s*", value.strip())
        if len(parts) < 2:
            parts = [p for p in re.split(r"\s+", value.strip()) if p and p != "-"]
        if len(parts) < 2:
            raise FillError(f"日期区间「{value}」解析不出开始/结束（写成「开始 - 结束」）")
        def d(s):
            m = re.match(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", s.strip())
            if not m:
                raise FillError(f"日期「{s}」不是 YYYY-MM-DD")
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
        return d(parts[0]), d(parts[-1])

    def _pick_day(self, year: int, month: int, day: int):
        heads = self.page.locator(".bd-date-range-picker__header")
        tables = self.page.locator("table.bd-date-table")
        for _ in range(40):
            months = []
            for i in range(min(heads.count(), 2)):
                m = _MONTH_RE.search(heads.nth(i).inner_text() or "")
                months.append((int(m.group(1)), int(m.group(2))) if m else None)
            if (year, month) in months:
                table = tables.nth(months.index((year, month)))
                cell = table.locator(
                    "td:not(.prev-month):not(.next-month):not(.disabled)").filter(
                    has=self.page.locator(".bd-date-table-cell__text",
                                          has_text=re.compile(rf"^\s*{day}\s*$")))
                if not cell.count():
                    cell = table.locator(
                        "td:not(.prev-month):not(.next-month):not(.disabled)",
                        has_text=re.compile(rf"^\s*{day}\s*$"))
                if not cell.count():
                    raise FillError(f"{year}-{month} 的日历里点不到 {day} 号"
                                    f"（可能早于今天被禁用）")
                cell.first.click()
                return
            ref = next((m for m in months if m), None)
            if ref is None:
                raise FillError("日期面板读不到当前月份")
            forward = (year, month) > ref
            aria = "下个月" if forward else "上个月"
            btn = self.page.locator(f'[aria-label="{aria}"]').first
            if not btn.count():
                btn = self.page.locator(
                    ".bd-picker-panel__icon-btn.arrow-right" if forward
                    else ".bd-picker-panel__icon-btn.arrow-left").first
            if not btn.count():
                raise FillError(f"日期面板上找不到「{aria}」翻页按钮")
            btn.click()
            self.page.wait_for_timeout(300)
        raise FillError(f"翻了 40 次还没翻到 {year}-{month}")

    # ------------------------------------------------ 定向人群（iView 抽屉）
    def _audience(self, f, value, values):
        src = f.get("from_prep") or {}
        include = str(values.get(src.get("include", ""), "")).strip()
        exclude = str(values.get(src.get("exclude", ""), "")).strip()
        if not include and not exclude:
            return

        drawer = self._open_targeting(f.get("open_button", "编辑定向"))
        item = drawer.locator(".ivu-form-item").filter(
            has=self.page.locator(".ivu-form-item-label",
                                  has_text=re.compile(r"^\s*人群包\s*$"))).first
        if not item.count():
            raise FillError("编辑定向抽屉里找不到「人群包」")
        if include:
            self._pick_audience(item, f.get("include_option", "指定人群包"), include)
        if exclude:
            self._pick_audience(item, f.get("exclude_option", "排除人群包"), exclude)

        confirm = f.get("confirm_button", "确认")
        btn = drawer.get_by_text(re.compile(rf"^\s*{re.escape(confirm)}\s*$")).last
        if not btn.count():
            raise FillError(f"定向抽屉里没有「{confirm}」按钮")
        btn.click()
        try:
            drawer.wait_for(state="hidden", timeout=self.timeout)
        except Exception as e:
            raise FillError("点了确认但定向抽屉没关，后面字段会被挡住") from e
        self.page.wait_for_timeout(800)

    def _open_targeting(self, button_text: str):
        for attempt in (1, 2):
            self.click_button(button_text)
            drawer = self.page.locator(f"{DRAWER_TARGETING}:visible").last
            try:
                drawer.wait_for(state="visible",
                                timeout=4000 if attempt == 1 else self.timeout)
                self.page.wait_for_timeout(1000)
                return drawer
            except Exception:
                if attempt == 2:
                    raise FillError(f"点了「{button_text}」但定向抽屉没打开")
                self.page.wait_for_timeout(800)

    def _pick_audience(self, item, option: str, name: str):
        tab = item.locator(".radio-item").filter(
            has_text=re.compile(rf"^\s*{re.escape(option)}\s*$")).first
        if not tab.count():
            avail = item.locator(".radio-item").all_inner_texts()
            raise FillError(f"「人群包」下没有「{option}」。实际有：{avail}")
        if not ACTIVE_RE.search(tab.get_attribute("class") or ""):
            tab.click()
            self.page.wait_for_timeout(1200)

        rows = item.locator(".list-item:not(.checkbox-all)").filter(has_text=name)
        if not rows.count():
            avail = item.locator(".list-item").all_inner_texts()[:20]
            raise FillError(f"「{option}」里没有人群包「{name}」。前 20 个：{avail}")
        box = rows.first.locator(".ivu-checkbox-wrapper").first
        if not box.count():
            raise FillError(f"「{name}」这一行里没找到勾选框")
        if "ivu-checkbox-wrapper-checked" not in (box.get_attribute("class") or ""):
            box.click()
            self.page.wait_for_timeout(600)

    # ============================================================ 素材层（聚合）
    def add_archives(self, picker: dict, avids: list[str]):
        if not avids:
            return
        open_btn = picker.get("open_button", "添加稿件/视频")
        drawer_sel = picker.get("drawer_selector", DRAWER_PICKER)
        search_ph = picker.get("search_ph", "请输入稿件bvid或avid搜索")

        # 素材区在页面很靠下，先滚到「添加稿件/视频」再点
        try:
            self.page.get_by_text(re.compile(rf"^\s*{re.escape(open_btn)}\s*$")).last \
                .scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass
        drawer = self._open_picker(open_btn, drawer_sel)

        # 抽屉里的搜索框是 iView，渲染慢一拍 —— 轮询到出现为止（页面级，抽屉是 portal）
        box = None
        waited = 0
        sel = (f'{drawer_sel} input[placeholder*="{search_ph}"], '
               f'input.ivu-input[placeholder*="{search_ph}"]')
        while waited < self.timeout:
            loc = self.page.locator(sel)
            if loc.count() and loc.first.is_visible():
                box = loc.first
                break
            self.page.wait_for_timeout(500)
            waited += 500
        if box is None:
            raise FillError("「添加稿件/视频」抽屉打开了，但没找到 avid 搜索框")

        footer = self.page.locator(f"{drawer_sel} .drawer-footer, .drawer-footer").last
        picked = 0
        missing = []
        for avid in avids:
            card = None
            for search_try in (1, 2):        # 搜一次没出来，清空重搜一次
                box.click()
                box.fill("")
                self.page.wait_for_timeout(200)
                box.fill(avid)
                box.press("Enter")
                self.page.wait_for_timeout(2600 if search_try == 1 else 4000)
                card = self._picker_result_card(drawer_sel, picker)
                if card is not None:
                    break
            if card is None:
                # 记下来接着往下加，最后一起报 —— 一个坏 avid 不该让整批白跑
                missing.append(avid)
                continue
            card.click()
            self.page.wait_for_timeout(700)
            # 点错了地方会弹视频预览大弹窗（.ivu-modal-wrap.fullmodal），关掉再重试一次
            if self._preview_modal_open():
                self.page.keyboard.press("Escape")
                self.page.wait_for_timeout(600)
            picked += 1
            got = None
            for _ in range(6):
                got = self._footer_count(footer, picker.get("count_text", "已选"))
                if got == picked:
                    break
                self.page.wait_for_timeout(400)
            if got is not None and got != picked:
                self._safe_cancel(footer, picker)
                raise FillError(f"勾到第 {picked} 个（avid {avid}）时，"
                                f"页面显示已选 {got} 个，对不上")

        if picked == 0:
            self._safe_cancel(footer, picker)
            raise FillError(f"一个稿件都没搜到（试了 {len(avids)} 个 avid）。"
                            f"先确认推广内容已选成 OGV推广，以及这些 avid 在本账户稿件库里")

        self._click_footer(footer, picker.get("confirm_button", "确定"))
        try:
            self.page.locator(f"{drawer_sel}.open").last.wait_for(
                state="hidden", timeout=self.timeout)
        except Exception:
            self.page.wait_for_timeout(1500)
        self.page.wait_for_timeout(2000)
        if missing:
            # 不抛 —— 已经加进去的稿件是有效的，跑完在结果里提示这几个没加上
            log.warning("这些 avid 在稿件库里搜不到，已跳过：%s", "、".join(missing))
            self.missing_archives = list(missing)

    def _click_footer(self, footer, text: str):
        btn = footer.get_by_text(re.compile(rf"^\s*{re.escape(text)}\s*$")).first
        if not btn.count():
            raise FillError(f"抽屉底部没有「{text}」按钮")
        btn.click()

    def _preview_modal_open(self) -> bool:
        try:
            m = self.page.locator(".ivu-modal-wrap.fullmodal:visible, "
                                  ".ivu-modal-wrap.center-modal:visible").first
            return bool(m.count())
        except Exception:
            return False

    def _safe_cancel(self, footer, picker: dict):
        if self._preview_modal_open():
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(600)
        try:
            self._click_footer(footer, picker.get("cancel_button", "取消"))
        except Exception:
            log.warning("关稿件抽屉失败，继续抛原来的错", exc_info=True)

    def _footer_count(self, footer, count_text: str) -> int | None:
        try:
            m = re.search(rf"{re.escape(count_text)}\s*(\d+)\s*/", footer.inner_text() or "")
            return int(m.group(1)) if m else None
        except Exception:
            return None

    def _picker_result_card(self, drawer_sel: str, picker: dict):
        """搜出来的结果卡：`.video-select-item`，点它本体即选中（缩略图是 background-image）。"""
        sel = picker.get("result_item") or f"{drawer_sel} .video-select-item"
        drawer = self.page.locator(f"{drawer_sel}.open").last
        if not drawer.count():
            drawer = self.page.locator(f"{drawer_sel}:visible").last
        for _ in range(8):
            cards = self.page.locator(sel)
            if cards.count():
                return cards.first
            # 兜底：抽屉里带「NNNN × 1080 px」的块
            alt = drawer.locator("div").filter(
                has_text=re.compile(r"\d+\s*[×xX]\s*\d+\s*px"))
            if alt.count():
                return alt.last
            self.page.wait_for_timeout(500)
        return None

    def _picker_count(self, drawer, count_text: str) -> int | None:
        try:
            foot = drawer.locator(".drawer-footer").first
            m = re.search(rf"{re.escape(count_text)}\s*(\d+)\s*/", foot.inner_text() or "")
            return int(m.group(1)) if m else None
        except Exception:
            return None

    def _dismiss_overlays(self):
        """收掉浮层（.ivu-select 下拉、see-more 展开层之类），免得挡住下一个输入框。"""
        try:
            self.page.keyboard.press("Escape")
            self.page.evaluate("""() => {
                for (const t of ['mousedown','click','mouseup'])
                    document.body.dispatchEvent(new MouseEvent(t, {bubbles:true}));
            }""")
            self.page.wait_for_timeout(200)
        except Exception:
            pass

    def _type_into(self, el, value: str):
        """填一个输入框，不走 mouse click（浮层挡住时 click 会超时，fill 不会）。"""
        try:
            el.fill("")
            el.fill(value)
        except Exception:
            self._dismiss_overlays()
            el.evaluate(
                "(e, v) => { e.focus(); e.value = v;"
                " e.dispatchEvent(new Event('input', {bubbles:true}));"
                " e.dispatchEvent(new Event('change', {bubbles:true})); }", value)
        self.page.wait_for_timeout(150)

    def add_titles(self, cfg: dict, titles: list[str]):
        if not titles:
            return
        ph = cfg.get("input_ph", "请输入2~40个字")
        add_btn = cfg.get("add_button", "新增标题")
        cap = int(cfg.get("max_count", 50))
        titles = titles[:cap]
        sel = (f'.media-editor input[placeholder*="{ph}"], '
               f'.media-editor textarea[placeholder*="{ph}"]')
        for i, t in enumerate(titles):
            boxes = self.page.locator(sel)
            if i >= boxes.count():
                self.click_button(add_btn)
                self.page.wait_for_timeout(400)
                boxes = self.page.locator(sel)
            if i >= boxes.count():
                raise FillError(f"点了「{add_btn}」但标题输入框没增加（要第 {i + 1} 个，"
                                f"只有 {boxes.count()} 个）")
            self._type_into(boxes.nth(i), t)
        self._dismiss_overlays()

    def add_covers(self, cfg: dict, paths: list[str]):
        if not paths:
            return
        mode_btn = cfg.get("mode_button", "自定义封面")
        try:
            btn = self.page.get_by_text(
                re.compile(rf"^\s*{re.escape(mode_btn)}\s*$")).last
            if btn.count() and not ACTIVE_RE.search(btn.get_attribute("class") or ""):
                btn.click()
                self.page.wait_for_timeout(500)
        except Exception:
            log.debug("切「%s」没成功，继续试上传", mode_btn, exc_info=True)

        file_sel = cfg.get("file_input", ".media-editor input[type=file]")
        count_text = cfg.get("count_text", "已添加")
        for path in paths:
            p = Path(path)
            if not p.exists():
                raise FillError(f"封面文件不存在：{path}")
            try:
                path = shrink(p, 700000)
            except ImageError as e:
                raise FillError(str(e)) from e
            before = self._cover_count(count_text)
            inp = self.page.locator(file_sel).first
            if not inp.count():
                raise FillError("封面区没有 input[type=file] 上传入口")
            inp.set_input_files(path)
            waited = 0
            while waited < self.timeout:
                self.page.wait_for_timeout(600)
                waited += 600
                now = self._cover_count(count_text)
                if now is not None and before is not None and now > before:
                    break
            else:
                raise FillError(f"封面 {Path(path).name} 传上去了但计数没涨，"
                                f"可能被判为不合规（尺寸/体积）")

    def _cover_count(self, count_text: str) -> int | None:
        try:
            m = re.search(rf"{re.escape(count_text)}\s*(\d+)\s*/",
                          self.page.locator(".media-editor").first.inner_text() or "")
            return int(m.group(1)) if m else None
        except Exception:
            return None

    def set_description(self, cfg: dict, value: str):
        if not value:
            return
        ph = cfg.get("input_ph", "即客户端广告卡片中UP主名称位置")
        el = self.page.locator(
            f'.media-editor input[placeholder*="{ph}"], '
            f'.media-editor textarea[placeholder*="{ph}"]').first
        if not el.count():
            raise FillError(f"素材描述框找不到（placeholder 含「{ph}」）")
        el.scroll_into_view_if_needed()
        self._dismiss_overlays()
        self._type_into(el, value)

    # ============================================================ 杂
    def _open_picker(self, button_text: str, drawer_sel: str):
        for attempt in (1, 2):
            self.click_button(button_text, prefer_last=True)
            drawer = self.page.locator(f"{drawer_sel}.open").last
            if not drawer.count():
                drawer = self.page.locator(f"{drawer_sel}:visible").last
            try:
                drawer.wait_for(state="visible",
                                timeout=4000 if attempt == 1 else self.timeout)
                self.page.wait_for_timeout(1200)
                return drawer
            except Exception:
                if attempt == 2:
                    raise FillError(f"点了「{button_text}」但抽屉没打开")
                self.page.wait_for_timeout(800)

    def _click_in_drawer(self, drawer, text: str):
        btn = drawer.locator(".drawer-footer").get_by_text(
            re.compile(rf"^\s*{re.escape(text)}\s*$")).first
        if not btn.count():
            btn = drawer.get_by_text(re.compile(rf"^\s*{re.escape(text)}\s*$")).last
        if not btn.count():
            raise FillError(f"抽屉里没有「{text}」按钮")
        btn.click()

    def click_button(self, text: str, prefer_last: bool = False):
        loc = self.page.get_by_text(re.compile(rf"^\s*{re.escape(text)}\s*$"))
        try:
            loc.first.wait_for(state="attached", timeout=self.timeout)
        except Exception as e:
            raise FillError(f"页面上找不到「{text}」") from e
        vis = []
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    vis.append(el)
            except Exception:
                continue
        if not vis:
            raise FillError(f"页面上的「{text}」都是隐藏的")
        el = vis[-1] if prefer_last else vis[0]
        el.scroll_into_view_if_needed()
        try:
            el.click(timeout=6000)
        except Exception:
            # 被 sticky 浮层（.see-more 之类）挡住时，走 DOM click 绕过
            el.evaluate("e => (e.closest('button, a, [role=button]') || e).click()")
        self.page.wait_for_timeout(200)

    HANDLERS = {
        "bd_fill": _bd_fill,
        "bd_radio": _bd_radio,
        "bd_select": _bd_select,
        "bd_date_range": _bd_date_range,
        "ppt_card": _ppt_card,
        "card_by_text": _card_by_text,
        "audience": _audience,
    }
