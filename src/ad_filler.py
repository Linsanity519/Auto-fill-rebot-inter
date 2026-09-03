"""原生商广页面的控件填写（iView）。

⚠ 独立于 src/filler.py 和 src/wizard_filler.py。那两套是给 antd / Formily 写的，
  这个页面一个 antd 类都没有，共用只会互相拖累。

这套页面的三个坑：
  1. 绝大多数「单选」不是 <input type=radio>，是 <div class="radio-item">，
     选中态靠 class 里有没有 active 判断 —— 用 Playwright 的 check() 一律无效。
  2. 同一个 placeholder 在页面上出现多次（三个监测链接框一模一样），
     只能按出现顺序取第几个。
  3. 创意块没有 id、也没有稳定的顺序，但 .single-creative-wrapper 的 data-id
     里带着 avid（形如 c_0_116453323900575_0.377），按 avid 认块最稳。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .ad_image import ImageError, shrink
from .filler import FillError

log = logging.getLogger(__name__)

ITEM = ".ivu-form-item"
# 页面上这几种节点都是「点一下就选中」的卡片式选项，选中态一律看 class 里的 active：
#   .radio-item             推广内容 / 计划预算 / 监测链接 / 广告投放位置 …（最常见）
#   .ppt-new-item           推广目的那四张大卡
#   .launch-type-item-new   竞价策略（稳定成本投放 / 最大转化投放）
#   .ivu-radio-wrapper      少数几个还是标准 iView 单选（单元预算）
CARD = ".radio-item, .ppt-new-item, .launch-type-item-new, .ivu-radio-wrapper"
ACTIVE_RE = re.compile(r"(^|\s)(active|ivu-radio-wrapper-checked)(\s|$)")
# 打开着的抽屉（定向、添加稿件都用它）
DRAWER_OPEN = ".ivu-drawer-wrap.__drawer-show"


class AdFiller:
    def __init__(self, page, timeout: int = 15000):
        self.page = page
        self.timeout = timeout

    # ============================================================ 对外
    def fill(self, fields: list[dict], values: dict, scope: str = ""):
        """按字段清单填一层。值的来源见 _value_of。"""
        for f in fields:
            if not self._applies(f, values):
                continue
            handler = self.HANDLERS.get(f.get("type"))
            if handler is None:
                raise FillError(f"字段「{f['name']}」的 type={f.get('type')} 不认识")

            value = self._value_of(f, values)
            # 定向那一项是「两个都没填就什么都不做」，值为空也要进 handler
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
        """when: [准备阶段字段名, 值] —— 只有那个字段等于该值时这一项才填。

        「投放日期区间」只在准备阶段选了「设置起止时间」时才存在，就靠这个。
        """
        when = f.get("when")
        if not when:
            return True
        name, want = when[0], str(when[1])
        return str(values.get(name, "")).strip() == want

    @staticmethod
    def _value_of(f: dict, values: dict) -> str:
        """一个字段最终要填什么。

        value:      yaml 里写死的固定值（推广目的这类每次都一样的）
        from_prep:  取准备阶段那个字段的值
        default:    兜底
        否则按字段名去 values 里取（单元名称、创意的标题/描述都走这条）
        """
        if "value" in f:
            return str(f["value"])
        src = f.get("from_prep")
        if isinstance(src, str):
            got = str(values.get(src, "")).strip()
            return got or str(f.get("default", ""))
        got = values.get(f["name"], "")
        got = "" if got is None else str(got).strip()
        return got or str(f.get("default", ""))

    # ============================================================ 定位
    def _item(self, label: str, scope=None):
        """按 label 文字圈出表单项容器。

        ⚠ 页面上 label 有「单元名称」也有「转化目标及出价 」（尾巴带空格和问号图标），
          所以先试全等，不中再退到「包含」。
        ⚠ 要等：填完上一项页面常会重渲染，下一项是随后才挂上去的，立刻取就是 0 个。
        ⚠ scope 传定向抽屉时只在抽屉里找 —— 「人群包」这个 label 抽屉内外都有。
        """
        root = scope if scope is not None else self.page
        exact = re.compile(rf"^\s*\*?\s*{re.escape(label)}\s*[:：]?\s*$")
        item = root.locator(ITEM).filter(
            has=self.page.locator("label.ivu-form-item-label", has_text=exact))
        try:
            item.first.wait_for(state="attached", timeout=self.timeout)
        except Exception:
            item = root.locator(ITEM).filter(
                has=self.page.locator("label.ivu-form-item-label",
                                      has_text=re.compile(re.escape(label))))
            if not item.count():
                raise FillError(f"按 label「{label}」找不到表单项")
        return item.first

    def _visible_inputs(self, ph: str):
        """placeholder 命中的全部可见输入框，按 DOM 顺序。"""
        return self.page.locator(
            f'input[placeholder*="{ph}"]:visible, textarea[placeholder*="{ph}"]:visible')

    # ============================================================ 控件
    def _fill_ph(self, f, value, values):
        ph = f["ph"]
        loc = self._visible_inputs(ph)
        n = loc.count()
        if not n:
            raise FillError(f"页面上没有 placeholder 含「{ph}」的输入框")
        i = int(f.get("ph_index", 0))
        if i >= n:
            raise FillError(f"placeholder「{ph}」只有 {n} 个框，取不到第 {i + 1} 个")
        el = loc.nth(i)
        el.fill("")
        el.fill(value)

    def _card_radio(self, f, value, values):
        """label 圈出的表单项里，点文字等于 value 的那张卡片。"""
        self._click_card(self._item(f["label"]), value, f["label"], force=bool(f.get("force")))

    def _card_by_text(self, f, value, values):
        """没有 label 可依附的卡片组（竞价策略）—— 全页按文字找。

        ⚠ 竞价策略那两张卡不在 .ivu-form-item-content 里（label 和内容是兄弟节点），
          只能全页找。为降低误点，要求卡片文字以 value 开头。
        """
        cards = self.page.locator(CARD).filter(
            has_text=re.compile(rf"^\s*{re.escape(value)}"))
        if not cards.count():
            raise FillError(f"页面上找不到选项「{value}」")
        card = cards.first
        if not ACTIVE_RE.search(card.get_attribute("class") or ""):
            card.click()
            self.page.wait_for_timeout(300)

    def _card_texts(self, scope) -> list[str]:
        cards = scope.locator(CARD)
        return [(cards.nth(i).inner_text() or "").strip().split("\n")[0].strip()
                for i in range(cards.count())]

    def _click_card(self, scope, value: str, label: str, force: bool = False):
        # ⚠ 联动卡片组（常规商广的「推广内容」跟着「推广目的」变）：上游刚点完，
        #   这一组可能还在重渲染，选项一时读不到。给它一点时间出现再找。
        #   非联动的组（原生商广全是）第一轮就命中，不会有额外等待。
        from .fill_core import wait_until
        wait_until(self.page, lambda: value in self._card_texts(scope), 5000)

        cards = scope.locator(CARD)
        seen = []
        for i in range(cards.count()):
            c = cards.nth(i)
            text = (c.inner_text() or "").strip().split("\n")[0].strip()
            seen.append(text)
            if text != value:
                continue
            cls = c.get_attribute("class") or ""
            if "disabled" in cls:
                raise FillError(f"「{label}」的选项「{value}」是禁用状态，点不了")
            # ⚠ force：「推广目的」就算已经是选中态也要再点一次 —— 下游「推广内容」
            #   的选项只在真正 click 时才重算，不点就是上一轮的旧列表。
            if force or not ACTIVE_RE.search(cls):
                c.click()
                self.page.wait_for_timeout(500)
            return
        raise FillError(f"「{label}」下没有选项「{value}」。实际有：{seen}")

    def _radio_ivu(self, f, value, values):
        """标准 iView 单选组（单元预算是这种）。"""
        item = self._item(f["label"])
        target = item.locator(".ivu-radio-wrapper").filter(
            has_text=re.compile(rf"^\s*{re.escape(value)}\s*$")).first
        if not target.count():
            avail = item.locator(".ivu-radio-wrapper").all_inner_texts()
            raise FillError(f"「{f['label']}」下没有选项「{value}」。实际有：{avail}")
        if "ivu-radio-wrapper-checked" not in (target.get_attribute("class") or ""):
            target.click()
            self.page.wait_for_timeout(200)

    def _select_ivu(self, f, value, values):
        item = self._item(f["label"])
        sel = item.locator(".ivu-select").first
        if not sel.count():
            raise FillError(f"「{f['label']}」下没有下拉框")
        cur = sel.locator(".ivu-select-selected-value").first
        if cur.count() and (cur.inner_text() or "").strip() == value:
            return
        sel.click()
        self.page.wait_for_timeout(600)
        opts = self.page.locator(".ivu-select-dropdown:visible li")
        hit = opts.filter(has_text=re.compile(rf"^\s*{re.escape(value)}\s*$")).first
        # 联动下拉（常规商广的转化目标跟着推广目的/内容变）：选项可能还在拉，等一下
        if not hit.count():
            from .fill_core import wait_until
            wait_until(self.page, lambda: hit.count() > 0, 5000)
        if not hit.count():
            avail = opts.all_inner_texts()
            self.page.keyboard.press("Escape")
            raise FillError(f"「{f['label']}」没有选项「{value}」。实际有：{avail}")
        hit.click()
        self.page.wait_for_timeout(400)

    def _date_range(self, f, value, values):
        """iView 的日期区间框：直接打字，回车收起面板，再回读校验。"""
        el = self._visible_inputs(f["ph"]).first
        if not el.count():
            raise FillError(f"找不到日期框（placeholder 含「{f['ph']}」）")
        el.click()
        self.page.wait_for_timeout(300)
        el.fill("")
        el.fill(value)
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(600)
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(300)
        got = (el.input_value() or "").strip()
        if got.replace(" ", "") != value.replace(" ", ""):
            raise FillError(f"日期没填进去：想填「{value}」，框里现在是「{got}」")

    # ---------------------------------------------------------- 定向人群
    def _audience(self, f, value, values):
        """只改「人群包」一项，其余定向保持页面默认的「不限」。

        ⚠ 「编辑定向」打开的是一个右侧抽屉（.ivu-drawer-wrap.__drawer-show），
          「人群包」这个 label 抽屉里外各有一份（外面那份是只读摘要），
          所有定位都必须限定在抽屉里，否则会对着摘要点，点完什么也没发生。
        ⚠ 收尾一定要点抽屉底部那个「确认」并等抽屉关掉：抽屉留着不关，
          后面所有字段的点击都会被它的遮罩挡住。
        ⚠ 指定 / 排除是两个可以同时打开的开关，不是三选一；两个都没填就什么都不做。
        """
        src = f.get("from_prep") or {}
        include = str(values.get(src.get("include", ""), "")).strip()
        exclude = str(values.get(src.get("exclude", ""), "")).strip()
        if not include and not exclude:
            return

        drawer = self._open_drawer(f.get("open_button", "编辑定向"))

        item = self._item("人群包", scope=drawer)
        if include:
            self._pick_audience(item, f.get("include_option", "指定人群包"), include)
        if exclude:
            self._pick_audience(item, f.get("exclude_option", "排除人群包"), exclude)

        confirm = f.get("confirm_button", "确认")
        btn = drawer.locator(".drawer-footer").get_by_text(
            re.compile(rf"^\s*{re.escape(confirm)}\s*$")).first
        if not btn.count():
            raise FillError(f"定向抽屉底部没有「{confirm}」按钮")
        btn.click()
        try:
            drawer.wait_for(state="hidden", timeout=self.timeout)
        except Exception as e:
            raise FillError("点了「确认」但定向抽屉没关掉，后面的字段会被它挡住") from e
        self.page.wait_for_timeout(800)

    def _pick_audience(self, item, option: str, name: str):
        """点开「指定/排除人群包」，在它下面那块列表里勾中名字含 name 的人群包。"""
        tab = item.locator(".radio-item").filter(
            has_text=re.compile(rf"^\s*{re.escape(option)}\s*$")).first
        if not tab.count():
            avail = item.locator(".radio-item").all_inner_texts()
            raise FillError(f"「人群包」下没有「{option}」。实际有：{avail}")
        if not ACTIVE_RE.search(tab.get_attribute("class") or ""):
            tab.click()
            self.page.wait_for_timeout(1200)

        # ⚠ 「全部」那一行也是 .list-item（class 上多个 checkbox-all），别点着它
        rows = item.locator(".list-item:not(.checkbox-all)").filter(has_text=name)
        if not rows.count():
            avail = item.locator(".list-item").all_inner_texts()[:20]
            raise FillError(f"「{option}」里没有人群包「{name}」。前 20 个：{avail}")
        # ⚠ 点 <li> 本身不算勾选（和稿件抽屉一个毛病），要点里面那个 checkbox 的 label
        box = rows.first.locator(".ivu-checkbox-wrapper").first
        if not box.count():
            raise FillError(f"「{name}」这一行里没找到勾选框")
        if not self._checked(box):
            box.click()
            self.page.wait_for_timeout(600)
        if not self._checked(box):
            raise FillError(f"点了「{name}」但没勾上，「{option}」还是空的")

    @staticmethod
    def _checked(box) -> bool:
        return "ivu-checkbox-wrapper-checked" in (box.get_attribute("class") or "")

    # ---------------------------------------------------------- 创意
    def add_archives(self, picker: dict, passes: list[list[str]]):
        """把这个单元的稿件加进来。passes 是分好趟的 avid 清单，一趟开一次抽屉。

        ⚠ 为什么要分趟：抽屉里同一个 avid 只有一个卡片、勾一次就是选中态，
          同一个 avid 要挂两条创意就只能分两次「确定」。ad_data.add_passes
          按「第几次出现」分趟，既最少开抽屉次数，又保证先加的是 _seq 小的那条。
        ⚠ 每次「确定」都是往已有创意后面追加，不是覆盖 —— 实测过。
        """
        for avids in passes:
            if avids:
                self._add_one_pass(picker, avids)

    def _add_one_pass(self, picker: dict, avids: list[str]):
        drawer = self._open_drawer(picker.get("open_button", "添加稿件/视频"))
        search_ph = picker.get("search_ph", "请输入稿件bvid或avid搜索")
        box = drawer.locator(f'input[placeholder*="{search_ph}"]').first
        if not box.count():
            raise FillError("「添加稿件/视频」抽屉没打开，或者搜索框变了")

        item_sel = picker.get("item_selector", ".video-drawer .video-select-item")
        check_sel = picker.get("check_selector", ".video-checkbox label")
        for i, avid in enumerate(avids, 1):
            box.fill("")
            box.fill(avid)
            box.press("Enter")
            self.page.wait_for_timeout(2000)
            items = self.page.locator(item_sel)
            if not items.count():
                self._cancel_picker(picker)
                raise FillError(f"avid {avid} 在稿件库里搜不到（先确认「推广内容」"
                                f"已经选成 OGV推广，否则搜不到 OGV 稿件）")
            # ⚠ 点卡片本身不算勾选，必须点右下角那个 checkbox
            items.first.locator(check_sel).first.click()
            self.page.wait_for_timeout(500)
            got = self._picked_count()
            if got is not None and got != i:
                self._cancel_picker(picker)
                raise FillError(f"勾到第 {i} 个（avid {avid}）时，"
                                f"页面显示已选 {got} 个，对不上")

        self._click_in_drawer(picker.get("confirm_button", "确定"))
        drawer.wait_for(state="hidden", timeout=self.timeout)
        self.page.wait_for_timeout(2000)

    def _picked_count(self) -> int | None:
        """读抽屉底部的「已选 n/10」。读不到返回 None（不因此中断）。"""
        try:
            foot = self.page.locator(f"{DRAWER_OPEN} .drawer-footer").first
            m = re.search(r"已选\s*(\d+)\s*/", foot.inner_text() or "")
            return int(m.group(1)) if m else None
        except Exception:
            return None

    def _cancel_picker(self, picker: dict):
        try:
            self._click_in_drawer(picker.get("cancel_button", "取消"))
            self.page.wait_for_timeout(800)
        except Exception:
            log.warning("关抽屉失败，继续抛原来的错", exc_info=True)

    def _click_in_drawer(self, text: str):
        btn = self.page.locator(f"{DRAWER_OPEN} .drawer-footer").get_by_text(
            re.compile(rf"^\s*{re.escape(text)}\s*$")).first
        if not btn.count():
            raise FillError(f"抽屉底部没有「{text}」按钮")
        btn.click()

    def creative_block(self, creative_cfg: dict, avid: str, seq: int = 0):
        """按 avid + 第几次出现，切到那条创意并返回它的表单容器。

        ⚠ 同一个 avid 可以在一个单元里挂多条创意（封面/标题一一对应），
          所以只按 avid 匹配会命中多个块，还得再取第 seq 个。
        ⚠ 页面一次只渲染一条创意的表单（别的是 display:none），
          所以返回之前必须先点左边那一列的「创意N」卡片把它切出来，
          否则后面 fill 会一直等一个永远不可见的输入框。
        """
        sel = creative_cfg.get("block_selector", ".single-creative-wrapper")
        attr = creative_cfg.get("block_avid_attr", "data-id")
        ids = self.page.locator(sel).evaluate_all(
            f"els => els.map(e => e.getAttribute('{attr}') || '')")
        hits = [i for i, x in enumerate(ids) if f"_{avid}_" in x]
        if len(hits) <= seq:
            raise FillError(f"要找 avid {avid} 的第 {seq + 1} 条创意，页面上只有 {len(hits)} 条。"
                            f"现有：{ids}")
        idx = hits[seq]

        switch = creative_cfg.get("switch_selector", ".every-card .material-card")
        cards = self.page.locator(switch)
        if cards.count() > idx:
            card = cards.nth(idx)
            if not ACTIVE_RE.search(card.get_attribute("class") or ""):
                card.click()
                self.page.wait_for_timeout(800)

        block = self.page.locator(sel).nth(idx)
        try:
            block.wait_for(state="visible", timeout=self.timeout)
        except Exception as e:
            raise FillError(f"切到 avid {avid} 的第 {seq + 1} 条创意后，它的表单还是没显示出来") from e
        return block

    def fill_creative(self, creative_cfg: dict, avid: str, data: dict, seq: int = 0):
        block = self.creative_block(creative_cfg, avid, seq)
        block.scroll_into_view_if_needed()
        for f in creative_cfg.get("fields", []):
            value = str(data.get(f["name"], "")).strip()
            if not value:
                if f.get("required"):
                    raise FillError(f"avid {avid} 的「{f['name']}」没有值")
                continue
            try:
                if f["type"] == "fill_ph_in_block":
                    el = block.locator(f'input[placeholder*="{f["ph"]}"], '
                                       f'textarea[placeholder*="{f["ph"]}"]').first
                    if not el.count():
                        raise FillError(f"创意块里没有 placeholder 含「{f['ph']}」的框")
                    el.fill("")
                    el.fill(value)
                elif f["type"] == "replace_cover":
                    self._replace_cover(block, f, value)
                else:
                    raise FillError(f"创意字段 type={f['type']} 不认识")
            except FillError:
                raise
            except Exception as e:
                raise FillError(f"avid {avid} 填「{f['name']}」失败：{e}") from e

    def _replace_cover(self, block, f: dict, path: str):
        """把这条创意的封面换成指定图片。

        ⚠ 走的是「替换封面」而不是「添加封面」：
          hover 原始封面卡 → 点 ↻（.action-area）→ 开「图片素材」抽屉 →
          往抽屉里的 input[type=file] 塞文件 → 等「已选 1/1」→ 点确认。
          内联那个「添加封面」口传上去是多加一张（1/6 变 2/6），原图还在，不是要的效果。
        ⚠ 超过 max_bytes 的先压再传，压缩只降编码质量、不动尺寸。
        """
        p = Path(path)
        if not p.exists():
            raise FillError(f"封面文件不存在：{path}")
        cap = int(f.get("max_bytes", 700000))
        try:
            path = shrink(p, cap)
        except ImageError as e:
            raise FillError(str(e)) from e

        card = block.locator(f.get("card_selector",
                                   ".prog-archive-materials > li .material-card")).first
        if not card.count():
            raise FillError("创意块里没找到封面卡片")
        card.scroll_into_view_if_needed()
        # ⚠ ↻ 按钮平时是隐藏的，必须先 hover 到卡片上它才出现
        card.hover()
        self.page.wait_for_timeout(600)
        btn = card.locator(f.get("replace_button", ".action-area")).first
        if not btn.count():
            raise FillError("封面卡上没有「替换封面」按钮")
        drawer = None
        for attempt in (1, 2):
            btn.click()
            drawer = self.page.locator(DRAWER_OPEN).first
            try:
                drawer.wait_for(state="visible", timeout=4000 if attempt == 1 else self.timeout)
                break
            except Exception:
                if attempt == 2:
                    raise FillError("点了「替换封面」但「图片素材」抽屉没打开")
                card.hover()
                self.page.wait_for_timeout(800)
        self.page.wait_for_timeout(800)

        inp = drawer.locator("input[type=file]").first
        if not inp.count():
            raise FillError("「图片素材」抽屉里没有上传入口")
        inp.set_input_files(path)

        # 上传是异步的，等「已选 0/1」变成「已选 1/1」再确认
        picked = f.get("picked_text", "已选")
        deadline = self.timeout
        waited = 0
        while waited < deadline:
            self.page.wait_for_timeout(600)
            waited += 600
            m = re.search(rf"{re.escape(picked)}\s*(\d+)\s*/", drawer.inner_text() or "")
            if m and int(m.group(1)) >= 1:
                break
        else:
            self._close_drawer(drawer)
            raise FillError(f"封面 {Path(path).name} 传上去了但抽屉里没被选中，"
                            f"可能被后台判为不合规（尺寸比例/体积）")

        ok = drawer.get_by_text(
            re.compile(rf"^\s*{re.escape(f.get('confirm_button', '确认'))}\s*$")).last
        if not ok.count():
            self._close_drawer(drawer)
            raise FillError("「图片素材」抽屉里没有确认按钮")
        ok.click()
        drawer.wait_for(state="hidden", timeout=self.timeout)
        self.page.wait_for_timeout(800)

    def _close_drawer(self, drawer):
        try:
            drawer.get_by_text(re.compile(r"^\s*取消\s*$")).last.click()
            self.page.wait_for_timeout(600)
        except Exception:
            log.warning("关抽屉失败", exc_info=True)

    # ---------------------------------------------------------- 杂
    def _open_drawer(self, button_text: str):
        """点一个按钮把抽屉打开，返回抽屉的 locator。

        ⚠ 要能重试一次：这些按钮周围的节点是 Vue 动态渲染的，正好在重渲染的
          瞬间点下去，事件会落在一个马上被替换掉的节点上，静默丢失 ——
          表现就是「点了但抽屉没开」。所以先短等一会儿，没开就再点一次。
        """
        for attempt in (1, 2):
            self._click_text(button_text)
            drawer = self.page.locator(DRAWER_OPEN).first
            try:
                drawer.wait_for(state="visible", timeout=4000 if attempt == 1 else self.timeout)
                self.page.wait_for_timeout(1200)
                return drawer
            except Exception:
                if attempt == 2:
                    raise FillError(f"点了「{button_text}」但抽屉没打开")
                log.info("「%s」点了没反应，重试一次", button_text)
                self.page.wait_for_timeout(800)

    def click_button(self, text: str, prefer_last: bool = False):
        """点页面上文字正好等于 text 的按钮/链接。

        ⚠ 只在「可见的」里面挑。页面上藏着好几份同名按钮：光「保存」就有三个
          —— 批量加标题抽屉里一个、footer-actions 里一个、底部操作条里一个，
          前两个都是 display:none。取 .last 会点到隐藏的那个，然后一直等到超时。
        ⚠ 要等，不能只判 count()：选完「推广内容 = OGV推广」之后页面会重渲染，
          「编辑定向」那块是随后才挂上去的，立刻取就是 0 个。
        prefer_last —— 底部操作条上的按钮通常是 DOM 里最后一个可见的那个。
        """
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
            raise FillError(f"页面上的「{text}」都是隐藏的，点不了")
        (vis[-1] if prefer_last else vis[0]).click()

    def _click_text(self, text: str):
        self.click_button(text)

    HANDLERS = {
        "fill_ph": _fill_ph,
        "card_radio": _card_radio,
        "card_by_text": _card_by_text,
        "radio_ivu": _radio_ivu,
        "select_ivu": _select_ivu,
        "date_range": _date_range,
        "audience": _audience,
    }
