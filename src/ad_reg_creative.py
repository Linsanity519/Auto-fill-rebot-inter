"""常规商广的创意层：从「我的视频」按位置取一个视频 + 6 条素材标题 + 素材描述。

⚠ 页面 DOM 和原生商广是同一套（iView），但创意的加法完全不同：
  原生按 avid 搜；这里是「我的视频」Tab 里按列表位置取第 K+i 个。
  所以单独一个文件，不塞进 ad_filler。

选择器 2026-09-03 挂在用户已登录的调试 Chrome 上逐步验证过：
  · 抽屉子账户 品牌银行/三连账户 → 选「三连账户」→ Tab「我的视频」→ 282 条、20/页
  · 卡片 .video-select-item，勾 .video-checkbox .ivu-checkbox-wrapper，底部「已选 n/10」
  · 翻页 .ivu-page / .ivu-page-item，确定按钮就叫「确定」
  · 6 条标题：创意块里点「批量添加」→ .batch-title-drawer → textarea 逐条打字+回车 →「保存」
  · 素材描述：创意块里 placeholder「请输入2 ~ 10个字…」的输入框，页面必填
"""
from __future__ import annotations

import logging
import re

from .fill_core import FillError, wait_until

log = logging.getLogger(__name__)

DRAWER_OPEN = ".ivu-drawer-wrap.__drawer-show"


class AdRegCreative:
    def __init__(self, page, timeout: int = 15000):
        self.page = page
        self.timeout = timeout

    # ------------------------------------------------------------ 加视频
    def add_video_by_index(self, picker: dict, index: int) -> str:
        """打开抽屉 → 三连账户 →「我的视频」→ 翻到第 index 个（0 起）→ 勾上 → 确定。

        返回勾中视频的标题。
        """
        per = int(picker.get("per_page", 20))
        page_no = index // per + 1
        pos = index % per

        drawer = self._open_drawer(picker.get("open_button", "添加稿件/视频"))
        self._pick_sub_account(drawer, picker.get("sub_account", ""))
        self._switch_tab(drawer, picker.get("tab", "我的视频"))

        card_sel = picker.get("card_selector", ".video-select-item")
        wait_until(self.page, lambda: drawer.locator(card_sel).count() > 0, self.timeout)
        if not drawer.locator(card_sel).count():
            self._cancel(drawer, picker)
            raise FillError("「我的视频」里一个视频都没有 —— 多半是子账户没切到"
                            f"「{picker.get('sub_account', '')}」")

        self._goto_page(drawer, picker, page_no)

        cards = drawer.locator(card_sel)
        n = cards.count()
        if pos >= n:
            self._cancel(drawer, picker)
            raise FillError(f"第 {index + 1} 个视频落在第 {page_no} 页第 {pos + 1} 位，"
                            f"但这页只有 {n} 个 —— 「视频数量 + 跳过前几个」超出「我的视频」总数了")
        card = cards.nth(pos)
        title = ""
        try:
            title = (card.locator(".video-name, .vm").first.inner_text() or "").strip()[:40]
        except Exception:
            title = (card.inner_text() or "").strip().split("\n")[-1][:40]

        card.locator(picker.get("check_selector", ".video-checkbox .ivu-checkbox-wrapper")).first.click()
        self.page.wait_for_timeout(500)

        got = self._picked_count(drawer, picker.get("count_text", "已选"))
        if got is not None and got != 1:
            self._cancel(drawer, picker)
            raise FillError(f"勾了第 {index + 1} 个视频，页面显示已选 {got} 个，对不上")

        self._click_confirm(drawer, picker.get("confirm_button", "确定"))
        try:
            drawer.wait_for(state="hidden", timeout=self.timeout)
        except Exception as e:
            raise FillError("点了「确定」但加视频的抽屉没关掉") from e
        self.page.wait_for_timeout(1500)

        if not self._creative_wrapper().count():
            raise FillError("视频加进来了，但页面上没出现创意块")
        return title

    # ------------------------------------------------------------ 6 条素材标题
    def fill_titles(self, titles_cfg: dict, titles: list[str]):
        """创意块里点「批量添加」→ 抽屉 textarea 逐条打字+回车 → 保存。"""
        if not titles:
            raise FillError("没有素材标题可填")

        w = self._creative_wrapper()
        opener = titles_cfg.get("open_button", "批量添加")
        btn = w.get_by_text(opener, exact=True).first
        if not btn.count():
            raise FillError(f"创意块里没有「{opener}」按钮")
        btn.click()
        self.page.wait_for_timeout(1200)

        dsel = titles_cfg.get("drawer_selector", ".batch-title-drawer")
        drawer = self.page.locator(dsel).filter(visible=True).first
        ta = self.page.locator(titles_cfg.get("textarea_selector", f"{dsel} textarea")).filter(
            visible=True).first
        if not wait_until(self.page, lambda: ta.count() and ta.is_visible(), self.timeout):
            raise FillError("「批量添加」抽屉里没有可填的文本框")

        key = titles_cfg.get("confirm_key", "Enter")
        added = titles_cfg.get("added_text", "已添加")
        mx = int(titles_cfg.get("max", 6))
        for i, t in enumerate(titles[:mx], 1):
            ta.fill(t)
            self.page.wait_for_timeout(200)
            ta.press(key)
            self.page.wait_for_timeout(500)
            got = self._added_count(drawer, added)
            if got is not None and got < i:
                raise FillError(f"输了第 {i} 条标题「{t}」但抽屉显示已添加 {got} 条，"
                                f"可能这条不合规（长度 2~40 字？含违禁词？）")

        save = titles_cfg.get("save_button", "保存")
        sb = drawer.get_by_text(save, exact=True).first
        if not sb.count():
            raise FillError(f"「批量添加」抽屉里没有「{save}」按钮")
        sb.click()
        try:
            drawer.wait_for(state="hidden", timeout=self.timeout)
        except Exception:
            log.warning("批量添加抽屉没检测到关闭，继续")
        self.page.wait_for_timeout(800)

    # ------------------------------------------------------------ 素材描述
    def fill_desc(self, desc_cfg: dict, value: str):
        if not value:
            raise FillError("「素材描述」是页面必填项，但准备页没填")
        w = self._creative_wrapper()
        ph = desc_cfg.get("ph", "请输入2 ~ 10个字")
        el = w.locator(f'input[placeholder*="{ph}"], textarea[placeholder*="{ph}"]').first
        if not el.count():
            raise FillError(f"创意块里没有 placeholder 含「{ph}」的输入框")
        el.fill("")
        el.fill(value)

    # ------------------------------------------------------------ 内部
    def _creative_wrapper(self):
        """当前可见的那个创意块（一次只渲染一条）。"""
        return self.page.locator(".single-creative-wrapper").filter(visible=True).first

    def _open_drawer(self, button_text: str):
        for attempt in (1, 2):
            self._click_visible(button_text)
            drawer = self.page.locator(".ivu-drawer").filter(
                has=self.page.locator(".tab-link")).filter(visible=True).first
            try:
                drawer.wait_for(state="visible", timeout=4000 if attempt == 1 else self.timeout)
                self.page.wait_for_timeout(1500)
                return drawer
            except Exception:
                if attempt == 2:
                    raise FillError(f"点了「{button_text}」但加稿件的抽屉没打开")
                self.page.wait_for_timeout(800)

    def _pick_sub_account(self, drawer, name: str):
        if not name:
            return
        btn = drawer.get_by_text(name, exact=True).first
        if btn.count():
            try:
                btn.click()
                self.page.wait_for_timeout(1800)
            except Exception:
                log.warning("切子账户「%s」没点动，继续", name)

    def _switch_tab(self, drawer, tab: str):
        link = drawer.locator(".tab-link").filter(has_text=tab).first
        if not link.count():
            link = drawer.get_by_text(tab, exact=True).first
        if not link.count():
            raise FillError(f"加稿件抽屉里没有「{tab}」这个 Tab")
        link.click()
        self.page.wait_for_timeout(2500)

    def _goto_page(self, drawer, picker: dict, page_no: int):
        if page_no <= 1:
            return
        pager = drawer.locator(picker.get("page_selector", ".ivu-page")).first
        if not pager.count():
            raise FillError(f"要翻到第 {page_no} 页，但抽屉里没有翻页控件")
        item = pager.locator(picker.get("page_item_selector", ".ivu-page-item")).filter(
            has_text=re.compile(rf"^\s*{page_no}\s*$")).first
        if item.count():
            item.click()
            self.page.wait_for_timeout(1800)
            return
        nxt = pager.locator(".ivu-page-next")
        for _ in range(page_no - 1):
            nxt.click()
            self.page.wait_for_timeout(1400)

    def _picked_count(self, drawer, count_text: str):
        try:
            m = re.search(rf"{re.escape(count_text)}\s*(\d+)\s*/", drawer.inner_text() or "")
            return int(m.group(1)) if m else None
        except Exception:
            return None

    def _added_count(self, drawer, added_text: str):
        try:
            m = re.search(rf"{re.escape(added_text)}\s*(\d+)\s*/", drawer.inner_text() or "")
            return int(m.group(1)) if m else None
        except Exception:
            return None

    def _click_confirm(self, drawer, text: str):
        btn = drawer.locator("button").filter(
            has_text=re.compile(rf"^\s*{re.escape(text)}\s*$")).filter(visible=True).last
        if not btn.count():
            raise FillError(f"加稿件抽屉底部没有「{text}」按钮")
        btn.click()

    def _cancel(self, drawer, picker: dict):
        try:
            self._click_confirm(drawer, picker.get("cancel_button", "取消"))
            self.page.wait_for_timeout(800)
        except Exception:
            log.warning("关抽屉失败", exc_info=True)

    def _click_visible(self, text: str):
        loc = self.page.get_by_text(re.compile(rf"^\s*{re.escape(text)}\s*$"))
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    el.click()
                    return
            except Exception:
                continue
        raise FillError(f"页面上找不到可点的「{text}」")
