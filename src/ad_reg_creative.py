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

from .fill_core import FillError, wait_stable, wait_until

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
                picked += 1
                # 等页面底部「已选 n/10」涨到 picked 为止（读不到计数就不卡着）
                wait_until(self.page,
                           lambda: self._counter(drawer, count_text) in (picked, None),
                           self.timeout)
                got = self._counter(drawer, count_text)
                if got is not None and got != picked:
                    self._cancel(drawer, picker)
                    raise FillError(f"勾到第 {picked} 个，页面显示已选 {got} 个，对不上")

        self._click_confirm(drawer, picker.get("confirm_button", "确定"))
        try:
            drawer.wait_for(state="hidden", timeout=self.timeout)
        except Exception as e:
            raise FillError("点了「确定」但加视频的抽屉没关掉") from e

        # 等这 N 条创意块渲染出来（以前是干等 1.5 秒就数，慢一点就数少了）
        wait_until(self.page,
                   lambda: self.page.locator(".single-creative-wrapper").count() >= len(indexes),
                   self.timeout)
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
        box = fi.locator(f'input[placeholder*="{cfg.get("url_ph", "请使用https链接开头的URL")}"]').first
        # 切「自定义链接」之后 URL 输入框才渲染出来，等它，别干等固定毫秒
        if not wait_until(self.page, lambda: box.count() > 0, self.timeout):
            raise FillError("落地页里没找到 URL 输入框")
        box.fill("")
        box.fill(url)
        wait_until(self.page, lambda: (box.input_value() or "").strip() == url, 3000)

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
            wait_until(self.page,
                       lambda: any(x in (opt.get_attribute("class") or "")
                                   for x in ("checked", "active")), 3000)

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
        drop = self.page.locator(".ivu-select-dropdown:visible .ivu-select-item")
        wait_until(self.page, lambda: drop.count() > 0, self.timeout)
        item = drop.first if drop.count() else bw.locator(".ivu-select-item").first
        if item.count():
            item.click()
            # 等下拉收起来，说明选中了
            wait_until(self.page,
                       lambda: self.page.locator(".ivu-select-dropdown:visible").count() == 0, 3000)

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

        psel = cfg.get("picker_selector", ".library-wrap")
        isel = cfg.get("item_selector", ".library-item")

        # ⚠ 别把 picker 提前绑成 `page.locator(f"{psel}:visible").first` 再去等 ——
        #   那个 .count() 是**点开的一瞬间**求值的，抽屉还没显示时会退化成
        #   「DOM 里第一个 .library-wrap」，那可能是别的创意那条**隐藏的**抽屉，
        #   于是就守着一个永远不会有内容的元素等到超时。
        #   locator 每次 poll 重新求值才是对的。
        def visible_picker(self=self, psel=psel):
            return self.page.locator(f"{psel}:visible").first

        # 组件列表是异步拉的，偶尔会拉回来空的（2026-09-08 第 8 个单元就是：
        # 抽屉开着、标题在、筛选行在，「共有 __ 个附加创意组件」那个数字是空的）。
        # 这种是一次性的，关掉重开一次通常就有了 —— 所以整段可以重试。
        attempts = int(cfg.get("retry", 3))
        for attempt in range(1, attempts + 1):
            opener.click()
            opened = wait_until(self.page,
                                lambda: visible_picker().count() > 0, self.timeout)
            got_items = opened and wait_until(
                self.page, lambda: visible_picker().locator(isel).count() > 0, self.timeout)
            if got_items:
                break
            if attempt < attempts:
                log.info("Story 组件列表这次是空的（第 %d/%d 次），关掉重开再试",
                         attempt, attempts)
                self._close_story(psel, cfg)
                continue
            # 到这儿是真拿不到。把「没打开」和「开了但空」分清楚 ——
            # 以前两种共用一句话，看日志的人根本不知道该去页面上看什么
            if not opened:
                raise FillError(
                    f"点了「选择」但 Story 组件抽屉没出来（等了 {self.timeout // 1000} 秒）。"
                    f"多半是被别的弹窗盖住了，重跑一次。")
            raise FillError(
                f"Story 组件抽屉开了，但里面一个组件都没有（试了 {attempts} 次，"
                f"抽屉上写的是「{self._story_count_text(visible_picker())}」）。"
                f"要么这个账号下确实没有可用的附加创意组件，要么后台这会儿没返回数据 —— "
                f"到页面上手动点一次「选择」看看列表是不是空的。")

        picker = visible_picker()
        # 选第一张卡：勾它右上角的方块（.component-checkbox），不是点卡片本体（会开预览）
        item = picker.locator(isel).first
        chk = item.locator(cfg.get("check_in_item", ".component-checkbox .ivu-checkbox-wrapper")).first
        target = chk if chk.count() else item
        target.click()
        # 等它真的被勾上，别干等固定毫秒
        wait_until(self.page,
                   lambda: "checked" in (target.get_attribute("class") or "")
                   or "checked" in (item.get_attribute("class") or ""),
                   2000)

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

    def _story_count_text(self, picker) -> str:
        """抽屉上「共有 N 个附加创意组件」那一行，只为把报错说清楚。"""
        try:
            m = re.search(r"共有\s*\d*\s*个[^\n]*", picker.inner_text() or "")
            return (m.group(0).strip() if m else "").replace("\n", " ") or "（这行也没读到）"
        except Exception:
            log.debug("读不到 Story 抽屉的计数行", exc_info=True)
            return "（这行也没读到）"

    def _close_story(self, psel: str, cfg: dict):
        """把 Story 抽屉关掉（点「取消」，没有就按 Esc），等它真的消失。"""
        try:
            cancel = self.page.locator(f"{psel}:visible button").filter(
                has_text=re.compile(r"^\s*取消\s*$")).last
            if cancel.count():
                cancel.click()
            else:
                self.page.keyboard.press("Escape")
        except Exception:
            log.debug("关 Story 抽屉时点取消失败，按 Esc 兜底", exc_info=True)
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
        wait_until(self.page, lambda: self.page.locator(f"{psel}:visible").count() == 0,
                   self.timeout)

    def _switch_to(self, creative_cfg: dict, i: int):
        """点左边第 i 张「创意N」卡，把那条创意的表单切出来。

        ⚠ 这里等的**不能**是「可见的 .single-creative-wrapper 多于 0 个」：
          点之前上一条创意的块本来就还在、还可见，这个条件恒真 —— 等于点完
          一点都没等。后面 _fill_titles 立刻去找按钮，撞上表单还没渲染完就报
          「没找到打开批量填标题抽屉的按钮」。刚传完 10 个视频那一下最慢，
          所以整批的前几条都好好的、偏偏中间某一条炸（实测 1.1.16，第 3 条）。
          换台快点的机器就复现不了 —— 是竞态，不是选择器错。
        """
        sw = creative_cfg.get("switch_selector", ".every-card .material-card")
        cards = self.page.locator(sw)
        if cards.count() <= i:
            raise FillError(f"要切到第 {i + 1} 条创意，左侧只有 {cards.count()} 张切换卡")
        cards.nth(i).click()
        # 等这条创意的表单**不再变**。拿不到「切换完成」的明确信号（没有可靠的
        # active 类名可用，带哈希的那些按约定不能碰），所以等渲染安静下来。
        wait_stable(self.page,
                    lambda: self.page.locator(
                        ".single-creative-wrapper").filter(visible=True).count(),
                    timeout=self.timeout)

    @staticmethod
    def _opener_names(titles_cfg: dict) -> list[str]:
        """打开「批量填标题」抽屉的那个按钮，页面上叫什么。

        ⚠ 这个按钮的文案后台改过：抓取记录里写的是「批量添加」（当时还没定位到，
          是照抽屉的 aria-label「批量添加标题」猜的），现在页面上是「+ 添加标题」。
          两个点开的是同一个抽屉，所以一律当别名处理，别再写死一个。
          新增一个叫法只要往 yaml 的 titles.open_buttons 里加一行。
        """
        names = titles_cfg.get("open_buttons")
        if not names:
            names = [titles_cfg.get("open_button", "添加标题")]
        return [str(n).strip() for n in names if str(n).strip()]

    def _find_button(self, scope, names: list[str]):
        """在 scope 里按文字找一个按钮。找到返回 (locator, 它实际显示的文字)，
        没找到返回 (None, "")。

        ⚠ 光用 get_by_text(..., exact=True) 是不够的：页面上的按钮常常带图标，
          文字是「+ 添加标题」这种，全等匹配一个都命不中 —— 这就是
          「创意块里没有「批量添加」按钮」的由来。所以先全等（最准），
          再退到**包含**匹配，而且只在按钮类元素上找，免得匹到把整块文字
          都包进去的祖先 div。
        """
        names = [n for n in names if n]
        if not names:
            return None, ""
        for n in names:
            b = scope.get_by_text(n, exact=True).first
            if b.count():
                return b, n
        pat = re.compile("|".join(re.escape(n) for n in names))
        for sel in ("button", ".ivu-btn", "[role=button]", "a"):
            b = scope.locator(sel).filter(has_text=pat).first
            if b.count():
                return b, " ".join((b.inner_text() or "").split())
        return None, ""

    @staticmethod
    def _save_names(titles_cfg: dict) -> list[str]:
        """抽屉里那个「填完了，落地」的按钮叫什么。实测是「确认」。"""
        names = titles_cfg.get("save_buttons")
        if not names:
            names = [titles_cfg.get("save_button", "确认")]
        return [str(n).strip() for n in names if str(n).strip()]

    @staticmethod
    def _counter_texts(titles_cfg: dict) -> list[str]:
        """抽屉底下那个计数的前缀。实测是「已选」。"""
        names = titles_cfg.get("added_texts")
        if not names:
            names = [titles_cfg.get("added_text", "已选")]
        return [str(n).strip() for n in names if str(n).strip()]

    def _find_title_opener(self, w, titles_cfg: dict):
        """创意块里那个「打开批量填标题抽屉」的按钮。

        ⚠ 「新增标题」是「再加一个空输入框」的另一个按钮，不能当成它。
          按现在的别名（添加标题 / 批量添加 / 批量添加标题）天然撞不上，
          但别把匹配放宽到「标题」两个字。
        """
        names = self._opener_names(titles_cfg)
        # ⚠ 必须**轮询**，不能只看一眼：_find_button 是纯快照（count() 立刻返回），
        #   而这个按钮所在的创意表单是异步渲染的。1.1.16 线上就是栽在这 ——
        #   报错说「按钮没找到」，人去页面上一看按钮明明在，于是一路怀疑是不是
        #   又改名了。名字没问题，是快照拍早了。
        wait_until(self.page, lambda: self._find_button(w, names)[0] is not None,
                   self.timeout)
        return self._find_button(w, names)

    def _fill_titles(self, titles_cfg: dict, titles: list[str]):
        if not titles:
            raise FillError("没有素材标题可填")
        w = self._wrapper()
        btn, opener = self._find_title_opener(w, titles_cfg)
        if btn is None:
            raise FillError(f"创意块里没找到打开批量填标题抽屉的按钮"
                            f"（找过这几个名字：{'、'.join(self._opener_names(titles_cfg))}）")
        log.info("素材标题：点「%s」打开抽屉", opener)
        btn.click()

        # ⚠ 抽屉的类名也别写死一个：.batch-title-drawer 是抓取时看到的，
        #   后台换一次皮就没了，而它换掉之后这里报的是「抽屉里没有可填的文本框」，
        #   人只会以为是页面没加载完。所以先按配置的类名找，找不到就退到
        #   「当前可见的、带 textarea 的那个抽屉/弹窗」。
        dsel = titles_cfg.get("drawer_selector", "[role=dialog]")
        drawer = self.page.locator(dsel).filter(visible=True).first
        if not wait_until(self.page, lambda: drawer.count() > 0, 4000):
            for alt in (titles_cfg.get("drawer_fallbacks")
                        or [".batch-title-drawer", ".ivu-drawer", ".ivu-modal", "[role=dialog]"]):
                cand = self.page.locator(alt).filter(visible=True).filter(
                    has=self.page.locator("textarea")).first
                if cand.count():
                    log.warning("没找到 %s，退到「%s」当批量填标题的抽屉", dsel, alt)
                    drawer = cand
                    break
        if not drawer.count():
            raise FillError(f"点了「{opener}」但批量填标题的抽屉没打开")

        ta = drawer.locator("textarea").filter(visible=True).first
        if not wait_until(self.page, lambda: ta.count() and ta.is_visible(), self.timeout):
            raise FillError(f"「{opener}」抽屉里没有可填的文本框")

        key = titles_cfg.get("confirm_key", "Enter")
        # ⚠ 抽屉底下那个计数，实测写的是「已选 6/6」，不是抓取记录里的「已添加」。
        #   读不出来时下面那段校验会整段跳过（_counter 返回 None 一律放行）——
        #   也就是「一条标题被页面悄悄拒掉」这件事就查不出来了。所以给一张别名表。
        added = self._counter_texts(titles_cfg)
        mx = int(titles_cfg.get("max", 6))

        # 抽屉里可能已经有标题（切来切去、或页面预填），先清空
        clr = drawer.get_by_text(re.compile(r"^\s*(全部清空|一键清空)\s*$")).first
        if clr.count():
            try:
                clr.click()
                wait_until(self.page,
                           lambda: self._counter(drawer, added) in (0, None), 3000)
            except Exception:
                pass

        for i, t in enumerate(titles[:mx], 1):
            ta.fill(t)
            wait_until(self.page, lambda: (ta.input_value() or "").strip() == t, 2000)
            ta.press(key)
            # 等「已添加 n」涨到 i；不涨说明这条被页面拒了，下面那句会说清楚
            wait_until(self.page, lambda: self._counter(drawer, added) in (i, None), 4000)
            got = self._counter(drawer, added)
            if got is not None and got < i:
                raise FillError(f"输了第 {i} 条标题「{t}」但抽屉显示已添加 {got} 条，"
                                f"可能这条不合规（2~40 字？违禁词？）")

        # ⚠ 同样是别名表：这个抽屉的落地按钮实测叫「确认」，不是抓取记录里的「保存」。
        saves = self._save_names(titles_cfg)
        sb, save_txt = self._find_button(drawer, saves)
        if sb is None:
            raise FillError(f"「{opener}」抽屉里没找到落地按钮"
                            f"（找过这几个名字：{'、'.join(saves)}）")
        log.info("素材标题：%d 条填完，点「%s」", min(len(titles), mx), save_txt)
        sb.click()
        try:
            drawer.wait_for(state="hidden", timeout=self.timeout)
        except Exception:
            log.warning("批量添加抽屉没检测到关闭，继续")

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
        wait_until(self.page, lambda: (el.input_value() or "").strip() == value, 3000)

    # ------------------------------------------------------------ 内部
    def _wrapper(self):
        """当前可见的那条创意块。页面约定一次只显示一条（见文件头）。

        ⚠ 直接取 `.first` 曾经是个静默炸弹：切换动画没走完时**两条**创意块会
          同时可见，`.first` 拿到的是**上一条** —— 于是标题/描述/落地页全填到
          上一条上，页面不报任何错，人也看不出来（那一条本来就该有内容）。
          比报错难查得多，所以在这儿把「不止一条」显式拦下来。
        """
        vis = self.page.locator(".single-creative-wrapper").filter(visible=True)
        # 多于一条 = 还在切换中，等它收敛；等不到再报
        wait_until(self.page, lambda: vis.count() == 1, self.timeout)
        n = vis.count()
        if n == 0:
            raise FillError("页面上没有可见的创意块（表单还没渲染出来？）")
        if n > 1:
            raise FillError(f"页面上同时有 {n} 条创意块可见，分不清该填哪条 —— "
                            f"创意切换没走完，或者页面结构变了")
        return vis.first

    def _open_drawer(self, button_text: str):
        for attempt in (1, 2):
            self._click_visible(button_text)
            drawer = self.page.locator(".ivu-drawer").filter(
                has=self.page.locator(".tab-link")).filter(visible=True).first
            try:
                drawer.wait_for(state="visible", timeout=4000 if attempt == 1 else self.timeout)
                # 抽屉是「壳先出来、Tab 后渲染」，等 Tab 真的在，再交出去
                wait_until(self.page, lambda: drawer.locator(".tab-link").count() > 0,
                           self.timeout)
                return drawer
            except Exception:
                if attempt == 2:
                    raise FillError(f"点了「{button_text}」但加稿件的抽屉没打开")
                # 重试前等它彻底消失，别干等固定毫秒
                wait_until(self.page,
                           lambda: self.page.locator(".ivu-drawer:visible").count() == 0, 3000)

    def _pick_sub_account(self, drawer, name: str):
        if not name:
            return
        btn = drawer.get_by_text(name, exact=True).first
        if btn.count():
            try:
                btn.click()
                # 换子账户会整列表重拉，没有明确完成信号 —— 等卡片数不再变
                wait_stable(self.page,
                            lambda: drawer.locator(".video-select-item").count(),
                            timeout=self.timeout)
            except Exception:
                log.warning("切子账户「%s」没点动，继续", name)

    def _switch_tab(self, drawer, tab: str):
        link = drawer.locator(".tab-link").filter(has_text=tab).first
        if not link.count():
            link = drawer.get_by_text(tab, exact=True).first
        if not link.count():
            raise FillError(f"加稿件抽屉里没有「{tab}」这个 Tab")
        link.click()
        # 切 Tab 也是重拉列表。调用方随后还会 wait_until(卡片>0)，这里只等它稳下来
        wait_stable(self.page,
                    lambda: drawer.locator(".video-select-item").count(),
                    timeout=self.timeout)

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

    def _counter(self, scope, text):
        """读「已选 3/6」这类计数里的当前值。text 可以是一个字符串，也可以是别名表。

        读不出来返回 None —— 调用方一律把 None 当「这页没有计数，别拦」。
        """
        names = [text] if isinstance(text, str) else list(text or [])
        try:
            txt = scope.inner_text() or ""
        except Exception:
            return None
        for n in names:
            m = re.search(rf"{re.escape(n)}\s*(\d+)\s*/", txt)
            if m:
                return int(m.group(1))
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
            wait_until(self.page, lambda: not drawer.is_visible(), 3000)
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
