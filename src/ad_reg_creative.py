"""常规商广的创意层：从「我的视频」按位置批量取视频（一个单元 ≤10 个），
每个视频一条创意，6 条素材标题 + 素材描述全批共用。

⚠ 页面 DOM 和原生商广是同一套（iView），但创意的加法不同：
  原生按 avid 搜；这里是「我的视频」Tab 里按列表位置勾。
  所以单独一个文件，不塞进 ad_filler。

选择器 2026-09-03 挂在用户已登录的调试 Chrome 上验证过：
  · 抽屉子账户 品牌银行/三连账户 → 选「三连账户」→ Tab「我的视频」→ 20/页
  · 卡片 .video-select-item，勾 .video-checkbox .ivu-checkbox-wrapper，底部「已选 n/10」
  · 勾选跨页保留（页 1 勾 3 个 + 页 2 勾 2 个 = 已选 5/10）
  · 翻页 .ivu-page / .ivu-page-item，确定按钮「确定」
  · 确定后 N 个 .single-creative-wrapper + N 张 .every-card 切换卡（一次只显示一条）
  · 每条创意：点「批量添加」→ .batch-title-drawer → textarea 逐条打字+回车 →「保存」
  · 素材描述：创意块里 placeholder「请输入2 ~ 10个字」的输入框，页面必填
"""
from __future__ import annotations

import logging
import re

from .fill_core import FillError, wait_until

log = logging.getLogger(__name__)


class AdRegCreative:
    def __init__(self, page, timeout: int = 15000, skip: int = 0):
        self.page = page
        self.timeout = timeout
        # 准备页的「跳过前几个」，只用来把报错说清楚（第几个视频没了）
        self.skip = skip
        # 「空间设置」的兜底这一批里报没报过（见 _pick_space）
        self._space_fallback_seen = False

    # ------------------------------------------------------------ 批量加视频
    def add_videos(self, picker: dict, indexes: list[int]) -> int:
        """打开抽屉 → 三连账户 →「我的视频」→ 把 indexes（0 起的列表位置）全勾上 → 确定。

        返回实际加进来的创意数。
        """
        if not indexes:
            raise FillError("这个单元没有要加的视频")
        per = int(picker.get("per_page", 20))

        drawer = self._open_drawer(picker.get("open_button", "添加稿件/视频"))
        self._pick_sub_account(drawer, picker.get("sub_account", ""))
        self._switch_tab(drawer, picker.get("tab", "我的视频"))

        card_sel = picker.get("card_selector", ".video-select-item")
        check_sel = picker.get("check_selector", ".video-checkbox .ivu-checkbox-wrapper")
        count_text = picker.get("count_text", "已选")
        wait_until(self.page, lambda: drawer.locator(card_sel).count() > 0, self.timeout)
        if not drawer.locator(card_sel).count():
            self._cancel(drawer, picker)
            raise FillError("「我的视频」里一个视频都没有 —— 多半是子账户没切到"
                            f"「{picker.get('sub_account', '')}」")

        # 按页分组，一页一趟；勾选跨页保留
        by_page: dict[int, list[int]] = {}
        for g in sorted(indexes):
            by_page.setdefault(g // per + 1, []).append(g % per)

        picked = 0
        last_page = max(by_page)
        for page_no in sorted(by_page):
            self._goto_page(drawer, picker, page_no)   # 里面已经等到列表渲染出来
            cards = drawer.locator(card_sel)
            n_on_page = cards.count()
            for pos in by_page[page_no]:
                if pos >= n_on_page:
                    # ⚠ 只有**最后一页没满**才是真的「视频不够」。中间某页数不够
                    #   一定是页面出了别的问题（列表没刷新 / 被弹窗盖住），
                    #   以前两种情况共用一句「超出总数」，把人往改数量的方向带偏了。
                    self._cancel(drawer, picker)
                    want = self.skip + len(indexes)
                    if page_no == last_page:
                        raise FillError(
                            f"「我的视频」不够用：要取到第 {want} 个"
                            f"（跳过前 {self.skip} 个 + {len(indexes)} 个），"
                            f"但第 {page_no} 页只有 {n_on_page} 个，到这儿就没了。"
                            f"把准备页的「视频数量」调小，或者把「跳过前几个」减一点。")
                    raise FillError(
                        f"第 {page_no} 页本该是满的（每页 {per} 个），却只数到 {n_on_page} 个 ——"
                        f"要勾第 {pos + 1} 个。这不是视频不够，是这一页没正常渲染出来"
                        f"（网慢 / 抽屉被别的弹窗盖住）。重跑一次通常就好。")
                cards.nth(pos).locator(check_sel).first.click()
                self.page.wait_for_timeout(400)
                picked += 1
                got = self._counter(drawer, count_text)
                if got is not None and got != picked:
                    self._cancel(drawer, picker)
                    raise FillError(f"勾到第 {picked} 个，页面显示已选 {got} 个，对不上")

        self._click_confirm(drawer, picker.get("confirm_button", "确定"))
        try:
            drawer.wait_for(state="hidden", timeout=self.timeout)
        except Exception as e:
            raise FillError("点了「确定」但加视频的抽屉没关掉") from e
        self.page.wait_for_timeout(1500)

        got = self.page.locator(".single-creative-wrapper").count()
        if got != len(indexes):
            raise FillError(f"要加 {len(indexes)} 个视频，页面上出现了 {got} 条创意，对不上")
        return got

    # ------------------------------------------------------------ 逐条创意填内容
    def fill_creatives(self, creative_cfg: dict, creatives: list[dict]):
        """按 Excel 每行给这个单元的每一条创意填 素材标题/描述/落地页 + 两个默认项。"""
        for i, c in enumerate(creatives):
            self._switch_to(creative_cfg, i)
            self._fill_titles(creative_cfg.get("titles") or {}, c.get("titles") or [])
            self._fill_desc(creative_cfg.get("desc") or {}, str(c.get("素材描述", "")).strip())
            self._fill_landing(creative_cfg.get("landing") or {}, str(c.get("落地页", "")).strip())
            self._pick_space(creative_cfg.get("space") or {})
            self._pick_story(creative_cfg.get("story_component") or {})

    def _fill_landing(self, cfg: dict, url: str):
        if not url:
            raise FillError("「落地页」是页面必填项，但 Excel 里没填")
        w = self._wrapper()
        fi = w.locator(".ivu-form-item").filter(
            has=self.page.locator(f'label:has-text("{cfg.get("label", "落地页")}")')).first
        if not fi.count():
            fi = w.locator(".ivu-form-item", has_text=cfg.get("label", "落地页")).first
        # 选「自定义链接」
        opt = cfg.get("type_option", "自定义链接")
        tab = fi.locator(".radio-item").filter(has_text=re.compile(rf"^\s*{re.escape(opt)}\s*$")).first
        if tab.count() and not re.search(r"active", tab.get_attribute("class") or ""):
            tab.click()
            self.page.wait_for_timeout(400)
        box = fi.locator(f'input[placeholder*="{cfg.get("url_ph", "请使用https链接开头的URL")}"]').first
        if not box.count():
            raise FillError("落地页里没找到 URL 输入框")
        box.fill("")
        box.fill(url)
        self.page.wait_for_timeout(200)

    def _pick_space(self, cfg: dict):
        """空间设置：选第一个「稿件UP主空间」。

        ⚠ 这个选项对一部分视频（我的视频里没绑 UP 主空间的）是**禁用**的 ——
          页面会自己落到品牌那一档（截图里是「哔哩哔哩大会员 · 取自品牌头像」）。
          禁用时不强选，保持页面默认。这是**正常兜底，不是错**。
        ⚠ 只在这一批里第一次遇到时按 INFO 记一条并说清楚落到了哪儿，后面的走
          debug —— 2026-09-08 那轮 50 条创意刷了 50 行一模一样的话，
          真正的报错被埋在里面看不见了。
        """
        w = self._wrapper()
        fi = w.locator(".ivu-form-item", has_text=cfg.get("label", "空间设置")).first
        if not fi.count():
            raise FillError("创意块里没有「空间设置」这一项")
        want = cfg.get("option", "稿件UP主空间")
        opts = fi.locator(".ivu-radio-wrapper, .radio-item")
        opt = opts.filter(has_text=re.compile(rf"^\s*{re.escape(want)}\s*$")).first
        if not opt.count():
            raise FillError(f"「空间设置」里没有「{want}」。实际有：{opts.all_inner_texts()}")
        cls = opt.get_attribute("class") or ""
        if "disabled" in cls:
            if not self._space_fallback_seen:
                self._space_fallback_seen = True
                log.info("「空间设置」的「%s」这批视频不可选（页面禁用），"
                         "保持页面自己选好的「%s」。后面的创意同样处理，不再重复记录",
                         want, self._space_current(fi) or "默认项")
            else:
                log.debug("「空间设置」的「%s」不可选，保持页面默认", want)
        elif "checked" not in cls and "active" not in cls:
            opt.click()
            self.page.wait_for_timeout(400)

        # 自定义时下面有个品牌下拉（.brand-select-wrap），选第一个（页面一般已默认选中第一个，
        # 这里再点一遍保底）
        self._pick_first_brand()

    def _pick_first_brand(self):
        w = self._wrapper()
        bw = w.locator(".brand-select-wrap .ivu-select, .space-brand-select .ivu-select").first
        if not bw.count():
            return
        # 已经选了就不动
        if bw.locator(".ivu-select-item-selected").count():
            cur = bw.locator("input[type=hidden]").first
            try:
                if (cur.input_value() or "").strip():
                    return
            except Exception:
                pass
        bw.locator(".ivu-select-selection").first.click()
        self.page.wait_for_timeout(700)
        item = self.page.locator(".ivu-select-dropdown:visible .ivu-select-item").first
        if not item.count():
            item = bw.locator(".ivu-select-item").first
        if item.count():
            item.click()
            self.page.wait_for_timeout(400)

    def _pick_story(self, cfg: dict):
        """Story 转化组件：点「选择」开抽屉，挑第一个组件卡，确定。"""
        w = self._wrapper()
        fi = w.locator(".ivu-form-item", has_text=cfg.get("label", "Story转化组件")).first
        if not fi.count():
            return
        if "请选择" not in (fi.inner_text() or ""):
            return          # 已经有值了，不动

        opener = fi.get_by_text(cfg.get("open_button", "选择"), exact=True).first
        if not opener.count():
            raise FillError("Story转化组件里没有「选择」按钮")
        opener.click()
        self.page.wait_for_timeout(1500)

        psel = cfg.get("picker_selector", ".library-wrap")
        isel = cfg.get("item_selector", ".library-item")
        picker = self.page.locator(f"{psel}:visible").first
        if not picker.count():
            picker = self.page.locator(psel).first
        if not wait_until(self.page, lambda: picker.locator(isel).count() > 0, self.timeout):
            raise FillError("点了「选择」但 Story 组件抽屉没出来 / 里面没有可选组件")

        # 选第一张卡：勾它右上角的方块（.component-checkbox），不是点卡片本体（会开预览）
        item = picker.locator(isel).first
        chk = item.locator(cfg.get("check_in_item", ".component-checkbox .ivu-checkbox-wrapper")).first
        (chk if chk.count() else item).click()
        self.page.wait_for_timeout(500)

        cb = cfg.get("confirm_button", "确定")
        ok = self.page.locator(cfg.get("footer_confirm_selector",
                                       ".library-wrap .footer .ivu-btn-primary")).first
        if not ok.count():
            ok = self.page.locator(f"{psel}:visible button").filter(
                has_text=re.compile(rf"^\s*{re.escape(cb)}\s*$")).last
        if not ok.count():
            raise FillError("Story 组件抽屉里没有「确定」按钮")
        ok.click()
        try:
            self.page.locator(f"{psel}:visible").first.wait_for(state="hidden", timeout=self.timeout)
        except Exception:
            log.warning("Story 组件抽屉没检测到关闭，继续")
        self.page.wait_for_timeout(600)

    def _switch_to(self, creative_cfg: dict, i: int):
        """点左边第 i 张「创意N」卡，把那条创意的表单切出来。"""
        sw = creative_cfg.get("switch_selector", ".every-card .material-card")
        cards = self.page.locator(sw)
        if cards.count() <= i:
            raise FillError(f"要切到第 {i + 1} 条创意，左侧只有 {cards.count()} 张切换卡")
        cards.nth(i).click()
        self.page.wait_for_timeout(700)
        wait_until(self.page,
                   lambda: self.page.locator(".single-creative-wrapper").filter(
                       visible=True).count() > 0, self.timeout)

    def _fill_titles(self, titles_cfg: dict, titles: list[str]):
        if not titles:
            raise FillError("没有素材标题可填")
        w = self._wrapper()
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

        # 抽屉里可能已经有标题（切来切去、或页面预填），先清空
        clr = drawer.get_by_text(re.compile(r"^\s*(全部清空|一键清空)\s*$")).first
        if clr.count():
            try:
                clr.click()
                self.page.wait_for_timeout(400)
            except Exception:
                pass

        key = titles_cfg.get("confirm_key", "Enter")
        added = titles_cfg.get("added_text", "已添加")
        mx = int(titles_cfg.get("max", 6))
        for i, t in enumerate(titles[:mx], 1):
            ta.fill(t)
            self.page.wait_for_timeout(200)
            ta.press(key)
            self.page.wait_for_timeout(450)
            got = self._counter(drawer, added)
            if got is not None and got < i:
                raise FillError(f"输了第 {i} 条标题「{t}」但抽屉显示已添加 {got} 条，"
                                f"可能这条不合规（2~40 字？违禁词？）")

        save = titles_cfg.get("save_button", "保存")
        sb = drawer.get_by_text(save, exact=True).first
        if not sb.count():
            raise FillError(f"「批量添加」抽屉里没有「{save}」按钮")
        sb.click()
        try:
            drawer.wait_for(state="hidden", timeout=self.timeout)
        except Exception:
            log.warning("批量添加抽屉没检测到关闭，继续")
        self.page.wait_for_timeout(600)

    def _fill_desc(self, desc_cfg: dict, value: str):
        if not value:
            raise FillError("「素材描述」是页面必填项，但准备页没填")
        w = self._wrapper()
        ph = desc_cfg.get("ph", "请输入2 ~ 10个字")
        el = w.locator(f'input[placeholder*="{ph}"], textarea[placeholder*="{ph}"]').first
        if not el.count():
            raise FillError(f"创意块里没有 placeholder 含「{ph}」的输入框")
        el.fill("")
        el.fill(value)
        self.page.wait_for_timeout(200)

    # ------------------------------------------------------------ 内部
    def _wrapper(self):
        """当前可见的那条创意块。"""
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
        """翻到第 page_no 页，**等列表真的换过来**再返回。

        ⚠ 别退回「点一下 + wait_for_timeout(1600)」那个写法（硬约定第 1 条）。
          它是这么炸的：翻页是异步拉数据，1.6 秒不够时回到调用方
          `cards.count()` 数到 0，然后被当成「视频不够」报出来 ——
          实测 287 个视频、同样是第 3 页，上一个单元成、下一个单元败
          （2026-09-08 的 _5 成 / _6 败）。
        """
        cur = self._active_page(drawer, picker)
        if cur == page_no:
            return
        pager = drawer.locator(picker.get("page_selector", ".ivu-page")).first
        if not pager.count():
            if page_no > 1:
                raise FillError(f"要翻到第 {page_no} 页，但抽屉里没有翻页控件")
            return

        item = pager.locator(picker.get("page_item_selector", ".ivu-page-item")).filter(
            has_text=re.compile(rf"^\s*{page_no}\s*$")).first
        if item.count():
            item.click()
        else:
            # 页码没直接列出来（页数多时中间是「…」），只能一页页点「下一页」
            nxt = pager.locator(".ivu-page-next")
            for _ in range(max(0, page_no - (cur or 1))):
                nxt.click()
                if not self._wait_page_ready(drawer, picker, None):
                    break
        self._require_page_ready(drawer, picker, page_no)

    def _wait_page_ready(self, drawer, picker: dict, page_no: int | None) -> bool:
        """等「页码切过去了 **且** 这一页的卡片渲染出来了」。

        两个条件缺一不可：只等页码，卡片可能还在拉；只等卡片数>0，
        数到的可能还是上一页那批。page_no=None 表示不校页码（点「下一页」时用）。
        """
        card_sel = picker.get("card_selector", ".video-select-item")

        def ready():
            if page_no is not None and self._active_page(drawer, picker) != page_no:
                return False
            return drawer.locator(card_sel).count() > 0

        return wait_until(self.page, ready, self.timeout)

    def _require_page_ready(self, drawer, picker: dict, page_no: int):
        if self._wait_page_ready(drawer, picker, page_no):
            return
        # 到这儿是真没翻过去 / 真没渲染出来，不是「视频不够」—— 说清楚是哪一种
        act = self._active_page(drawer, picker)
        n = drawer.locator(picker.get("card_selector", ".video-select-item")).count()
        raise FillError(
            f"翻到第 {page_no} 页之后，等了 {self.timeout // 1000} 秒列表还是没出来"
            f"（当前停在第 {act if act is not None else '?'} 页，页面上 {n} 张卡）。"
            f"多半是网慢或者抽屉被别的弹窗盖住了，重跑一次；一直这样就把"
            f"「设置」里的超时调大。")

    def _active_page(self, drawer, picker: dict):
        try:
            act = drawer.locator(
                picker.get("page_item_selector", ".ivu-page-item") + "-active,"
                " .ivu-page-item.ivu-page-item-active").first
            if act.count():
                return int((act.inner_text() or "").strip())
        except Exception:
            pass
        return None

    def _space_current(self, fi) -> str:
        """「空间设置」当前实际落在哪 —— 只用来把日志写清楚，读不到就返回空。"""
        try:
            sel = fi.locator(".ivu-select-selected-value, .ivu-select-placeholder").first
            if sel.count():
                return (sel.inner_text() or "").strip()
        except Exception:
            log.debug("读不到「空间设置」当前的值", exc_info=True)
        return ""

    def _counter(self, scope, text: str):
        try:
            m = re.search(rf"{re.escape(text)}\s*(\d+)\s*/", scope.inner_text() or "")
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
