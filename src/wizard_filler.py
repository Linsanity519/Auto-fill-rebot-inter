"""wizard 模式的控件填写。

⚠ 独立于 src/filler.py。老配置的 Filler 一行不动，避免相互影响。
   通用能力（antd 下拉的虚拟滚动/远程搜索轮询）从老 Filler 复用，不重写。

这套页面比价格配置麻烦在三点：
  1. 单元层是 Formily（.ant-formily-item），基本信息区是 antd Form（.ant-form-item）
  2. 创意层三套系统：v1/v2 是 Vue（label 点选），新版是 React 且 class 前缀是 mega-ant-
  3. 大部分字段没有 id，只能按 label 文字定位
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from . import wizard_schema as W
from .filler import FillError, split_multi
from .images import fetch_image, is_url

log = logging.getLogger(__name__)

# 单元层混用两种表单容器
FORM_ITEM = ".ant-formily-item, .ant-form-item"


def mode_matches(cur: str, value: str, f: dict) -> bool:
    """下拉里现在显示的值，算不算已经是我们要填的那个。"""
    if f.get("match") == "contains" or f.get("option_match") == "contains":
        return value in cur or cur in value
    return cur == value


class WizardFiller:
    def __init__(self, page, timeout: int = 15000, on_note=None):
        self.page = page
        self.timeout = timeout
        # 跳过了什么这类事得让人看见（界面日志），不能只写进 run.log
        self._on_note = on_note

    # ---------------------------------------------------------------- 等待
    def wait_until(self, cond, timeout: int | None = None, step: int = 120) -> bool:
        """等到 cond() 为真就立刻返回 True，超时返回 False。

        ⚠ 别再写 wait_for_timeout(2000) 这种死等：网好的时候白等两秒，
          网差的时候两秒又不够。统一改成「盯着条件」——条件一到就走，
          没到就接着等，等到上限（默认跟着 settings.timeout 走）为止。
        """
        deadline = self.timeout if timeout is None else timeout
        waited = 0
        while True:
            try:
                if cond():
                    return True
            except Exception:
                pass
            if waited >= deadline:
                return False
            self.page.wait_for_timeout(step)
            waited += step

    def _control_count(self) -> int:
        """页面上有多少个可填控件。用来判断「条件字段冒出来了没有」。"""
        try:
            return self.page.evaluate(
                "() => document.querySelectorAll('input, textarea, label').length")
        except Exception:
            return -1

    def _note(self, msg: str):
        log.warning(msg)
        if self._on_note:
            try:
                self._on_note(msg)
            except Exception:
                pass

    # ============================================================ 对外
    def fill(self, fields: list[dict], data: dict, scope: str = ""):
        """按字段清单填一层。reveals 里的条件字段填完主字段后递归处理。"""
        for f in fields:
            name = f["name"]
            value = str(data.get(name, "")).strip()

            if not value:
                if f.get("required"):
                    raise FillError(f"{scope}必填字段「{name}」数据为空")
                continue

            handler = self.HANDLERS.get(f.get("type"))
            if handler is None:
                raise FillError(f"字段「{name}」的 type={f.get('type')} 不认识")

            t0 = time.monotonic()
            # 记一下当前控件数：待会儿判断「条件字段有没有冒出来」用
            before_n = self._control_count() if f.get("reveals") else 0
            try:
                handler(self, f, value)
            except FillError:
                raise
            except Exception as e:
                raise FillError(f"{scope}填「{name}」失败：{e}") from e
            cost = time.monotonic() - t0
            # ⚠ 慢在哪一步，日志得自己说得出来 —— 之前只能靠盯着屏幕猜
            if cost >= 3:
                self._note(f"填「{name}」花了 {cost:.0f} 秒")
            elif cost >= 1.5:
                log.info("填「%s」花了 %.1f 秒", name, cost)

            for val, subs in (f.get("reveals") or {}).items():
                if self._match_option(value, val, f):
                    # ⚠ 选了值之后条件字段是异步渲染出来的。以前固定睡 300ms：
                    #   渲染快的时候白等，慢的时候又不够（count() 读到 0 就走了兜底路径）。
                    #   改成等控件数量真的变化，通常一百多毫秒就返回。
                    self.wait_until(lambda: self._control_count() != before_n, timeout=1500)
                    self.fill(subs, data, scope)

    @staticmethod
    def _match_option(value: str, option: str, f: dict) -> bool:
        # 多选字段（我想投放）的 reveals：值是「A,B,C」，命中其中一项就算触发
        if f.get("reveal_match") == "contains":
            return option in [v.strip() for v in value.replace("，", ",").split(",")]
        if f.get("option_match") == "contains":
            return value.startswith(option) or option in value
        return value == option

    # ============================================================ 定位
    def _prefix(self, f: dict) -> str:
        """新版创意页的 antd class 前缀是 mega-ant，不是 ant。"""
        return f.get("prefix", "ant")

    def _item_by_label(self, label: str, index: int = 0):
        """按 label 文字定位表单项容器。Formily 和 antd Form 都试。

        ⚠ 页面上 label 写成「*生效平台」「生效平台：」，不能用全等匹配。
        """
        pat = re.compile(rf"^\s*\*?\s*{re.escape(label)}\s*[:：]?\s*$")
        item = self.page.locator(FORM_ITEM).filter(
            has=self.page.locator("label", has_text=pat)
        )
        n = item.count()
        if not n:
            # 退一步：容器自己的文本以 label 开头
            item = self.page.locator(FORM_ITEM).filter(has_text=pat)
            n = item.count()
        if not n:
            raise FillError(f"按 label「{label}」找不到表单项")
        # ⚠ label_index: -1 = 取最后一个。人群那几级的 label 同名（都叫「人群选组」），
        #   下标会随「排除人群选组」展开与否前后移动；按填写顺序取最后一个反而是稳的。
        #   见 docs/资源位投放-人群配置抓取.md
        if index < 0:
            return item.last
        if index >= n:
            raise FillError(f"label「{label}」只有 {n} 个，取不到第 {index + 1} 个")
        return item.nth(index)

    def _scope(self, f: dict):
        return self._item_by_label(f["label"], f.get("label_index", 0))

    # ============================================================ 控件
    def _fill(self, f, value):
        el = self.page.locator(f["selector"]).first
        el.wait_for(state="visible", timeout=self.timeout)
        el.fill("")
        el.fill(value)

    TEXTY = ("input:not([type=radio]):not([type=checkbox]):not([type=file])"
             ":not([type=hidden]), textarea")

    def _fill_by_label(self, f, value):
        """按 label 填输入框。

        ⚠ v1/v2 创意页没有 .ant-form-item / .ant-formily-item 容器，
        label 和它对应的 input 只是兄弟节点。所以圈不到容器时，
        退回「找到 label 元素 → 取它之后最近的一个输入框」。
        """
        try:
            item = self._scope(f)
            inp = item.locator(self.TEXTY).first
            if inp.count():
                inp.fill("")
                inp.fill(value)
                return
        except FillError:
            pass

        inp = self._input_after_label(f["label"])
        if inp is None:
            raise FillError(f"按 label「{f['label']}」找不到可输入的框")
        inp.fill("")
        inp.fill(value)

    def _input_after_label(self, label: str):
        """找到 label 文字所在节点，取 DOM 顺序上它之后最近的输入框。

        ⚠ 从窄到宽找：先只看 <label>，再看 <span>，最后才全页扫 <div>。
          一上来就 "label, span, div" 的话，Playwright 要把页面上所有 div 都
          取回来逐个匹配文字 —— 创意页 DOM 一大就要好几秒，用户看到的
          「填到跳转链接卡很久」就是这么来的。
        """
        pat = re.compile(rf"^\s*\*?\s*{re.escape(label)}\s*[:：]?\s*$")
        lab = None
        for sel in ("label", "span", "div"):
            cand = self.page.locator(sel).filter(has_text=pat).last
            if cand.count():
                lab = cand
                break
        if lab is None:
            return None
        after = lab.locator(f"xpath=following::*[self::input or self::textarea]"
                            f"[not(@type='radio') and not(@type='checkbox')"
                            f" and not(@type='file') and not(@type='hidden')][1]")
        return after.first if after.count() else None

    def _fill_by_ph(self, f, value):
        """按 placeholder 定位输入框。创意页很多框既没 id 也没规范 label。"""
        ph = f.get("ph")
        if ph:
            el = self.page.locator(f"input[placeholder*='{ph}'], textarea[placeholder*='{ph}']").first
            if el.count():
                el.fill("")
                el.fill(value)
                return
        # 退回 label 方式
        self._fill_by_label(f, value)

    def _radio_by_label(self, f, value):
        item = self._scope(f)
        target = item.locator("label").filter(has_text=self._opt_re(value, f)).first
        if not target.count():
            avail = item.locator("label").all_inner_texts()
            raise FillError(f"「{f['label']}」下没有选项「{value}」。实际有：{avail}")
        target.click()

    def _vue_radio(self, f, value):
        """v1/v2 创意页：整页都是 <label>选项</label>，没有分组容器。

        ⚠ 只能全页找。为降低误点，优先全等匹配，再退到包含匹配。
        """
        labels = self.page.locator("label")
        exact = labels.filter(has_text=re.compile(rf"^\s*{re.escape(value)}\s*$"))
        target = exact.first if exact.count() else labels.filter(has_text=value).first
        if not target.count():
            raise FillError(f"页面上找不到选项「{value}」")
        target.click()
        self.page.wait_for_timeout(120)     # 点一下的落地缓冲；后面的条件字段由 reveals 那边等

    def _checkbox_sync_formily(self, f, value):
        """多选组双向同步（默认全选的组必须取消不要的）。

        ⚠ 这类字段（生效平台 / 运营商 / 内容类型 / ep付费状态）各资源位的可选项
          是不一样的子集：策略中心配的是全集，某个资源位没有 public、没有繁体包
          都很正常。页面上没有的就跳过，不当错误 —— 为一个用不上的平台
          把整条单元卡住，不值当。只有一个都对不上时才报错（那说明配错了）。
        """
        want = set(split_multi(value))
        item = self._scope(f)
        boxes = item.locator(".ant-checkbox-wrapper")
        n = boxes.count()
        if not n:
            raise FillError(f"「{f['label']}」下没找到复选框")

        # ⚠ 一次性把「每个框的文字 + 勾没勾」读回来，别逐个 inner_text/get_attribute：
        #   生效平台有 13 个框，逐个读是几十次浏览器往返，一个字段就要好几秒。
        #   读回来之后只点该动的那几个。
        state = item.evaluate("""el => [...el.querySelectorAll('.ant-checkbox-wrapper')]
            .map(w => [ (w.innerText||'').trim(),
                        w.className.includes('ant-checkbox-wrapper-checked') ])""")
        seen = {txt for txt, _ in state}
        for i, (text, checked) in enumerate(state):
            if (text in want) != checked:
                boxes.nth(i).click()

        missing = want - seen
        if missing:
            if not (want & seen):
                raise FillError(
                    f"「{f['label']}」要勾的 {sorted(want)} 这个资源位一个都没有，"
                    f"页面上只有：{sorted(seen)}")
            self._note(f"「{f['label']}」这个资源位没有 {'、'.join(sorted(missing))}，"
                       f"已跳过；实际勾上的是 {'、'.join(sorted(want & seen))}")

    def _select_antd_by_label(self, f, value):
        """antd Select。复用老 Filler 的下拉逻辑（虚拟滚动 + 远程搜索轮询）。"""
        from .filler import Filler

        px = self._prefix(f)
        item = self._scope(f)

        # ⚠ 一个 formily-item 里可能嵌着两个 select（如「人群选组」里还套着
        #   二级的「人群名称」）。取 .first 会拿到外层那个，
        #   表现为「下拉里没有 35697，实际只有 OGV DMP 人群包」。
        #   所以优先按 placeholder 精确认领自己的那个。
        wrapper = None
        ph = f.get("placeholder") or f.get("ph")
        if ph:
            cand = item.locator(f".{px}-select").filter(
                has=self.page.locator(f"[placeholder*='{ph}']"))
            if cand.count():
                wrapper = cand.first
        if wrapper is None:
            cands = item.locator(f".{px}-select")
            n = cands.count()
            if n == 0:
                raise FillError(f"「{f['label']}」下没找到下拉框")
            # 多个时取最内层（最后一个），它才是这个 label 直接对应的控件
            wrapper = cands.last if n > 1 else cands.first

        # ⚠ 已经是想要的值就别再点了。单元层要填两遍（切资源位会重置字段），
        #   第二遍再点一次同一个选项，antd 会当成取消 —— 表现是「版本限制」
        #   第一遍好好的、第二遍变空，最后报「点了保存并下一步但没跳转」。
        #   顺带：远程搜索的下拉（填ID显示人群名）有值就跳过，省掉一次几秒的搜索。
        cur = ""
        picked = wrapper.locator(f".{px}-select-selection-item")
        if picked.count():
            cur = (picked.first.inner_text() or "").strip()
        if cur:
            if f.get("search"):
                return
            if mode_matches(cur, value, f):
                return

        inp = wrapper.locator("input").first
        shim = Filler(self.page, {})
        shim.ANTD_OPEN = f"{px}-select-open"

        self._open(wrapper, px)
        dropdown = self._dropdown_of(wrapper, inp, px)

        options = dropdown.locator(f".{px}-select-item-option")

        if f.get("search"):
            inp.type(value, delay=30)
            if not self.wait_until(lambda: options.count() > 0):
                raise FillError(
                    f"「{f['label']}」搜索「{value}」等到超时也没返回选项。"
                    f"确认这个值在系统里存在，或者网络是不是断了。")

        mode = f.get("match", "exact")
        if mode == "contains":
            hit = options.filter(has_text=value)
        else:
            hit = options.filter(has_text=re.compile(rf"^\s*{re.escape(value)}\s*$"))

        # ⚠ 远程搜索的下拉不能死等文字匹配：填人群ID、搜出来只显示人群名，
        #   永远等不到「文字里有 27629」的那条，白白耗掉 4.5 秒 —— 一次投放里
        #   这个动作要做几十次，全是干等（用户反馈「选人群包总卡一下」就是这个）。
        #   有结果就赶紧往下走，交给下面「只出一条就点它」处理。
        # 远程搜索的下拉别死等文字匹配（填ID、显示人群名，永远等不到），
        # 有结果就赶紧往下走，交给下面「只出一条就点它」处理
        self.wait_until(lambda: hit.count() > 0,
                        timeout=900 if f.get("search") else 4500)

        if not hit.count() and f.get("search"):
            # ⚠ 远程搜索的下拉（人群包）：填的是人群ID，搜出来的那条却只显示人群名，
            #   比如填 27629 出来的是「防打扰人群」，按文字根本对不上。
            #   只搜出一条时它就是这个 ID 对应的包，直接点；多条才算歧义。
            #   不点的话 ID 只是留在输入框里，看着像填了、其实没选中（实测踩过）。
            texts = options.all_inner_texts()
            if len(texts) == 1:
                log.info("「%s」搜「%s」只出一条「%s」，按它选中", f["label"], value, texts[0].strip())
                hit = options.first
            elif len(texts) > 1:
                raise FillError(
                    f"「{f['label']}」搜「{value}」出了 {len(texts)} 条，分不清要哪个："
                    f"{[x.strip() for x in texts[:8]]}。把值写得更准一点")

        if not hit.count():
            raise FillError(f"「{f['label']}」下拉里没有「{value}」。实际：{options.all_inner_texts()[:15]}")

        if mode == "contains" and hit.count() > 1:
            texts = hit.all_inner_texts()
            raise FillError(f"「{f['label']}」用「{value}」匹配到 {len(texts)} 个：{texts[:5]}，填精确点")

        hit.first.click()
        self.page.wait_for_timeout(400)
        self.page.keyboard.press("Escape")

    def _open(self, wrapper, px):
        """展开下拉。点击是切换，已展开时再点会关掉，必须先判状态。"""
        def is_open():
            return f"{px}-select-open" in (wrapper.get_attribute("class") or "")

        if not is_open():
            wrapper.click()
        for _ in range(25):
            if is_open():
                return
            self.page.wait_for_timeout(100)
        wrapper.click()
        self.page.wait_for_timeout(400)

    def _dropdown_of(self, wrapper, inp, px):
        """精确拿到这个 select 自己的浮层。

        ⚠ 页面上常同时存在多个浮层，取 .last 会拿错（实测在单元层填人群时
        会拿到「人群选组」的浮层）。input 的 aria-owns 指向自己的列表节点，
        用它回溯才可靠。
        """
        oid = inp.get_attribute("aria-owns") or inp.get_attribute("aria-controls")
        if oid:
            node = self.page.locator(f"#{oid}")
            if node.count():
                dd = node.locator(f"xpath=ancestor::div[contains(@class,'{px}-select-dropdown')][1]")
                if dd.count():
                    return dd.first
        return self.page.locator(f".{px}-select-dropdown:not(.{px}-select-dropdown-hidden)").last

    def _multiselect_antd(self, f, value):
        """antd 多选下拉（人群的「我想投放」「在上述投放中我想排除」「人群分组ID」）。

        ⚠ 选项是虚拟列表 + 可搜索：不能滚着找，逐个输入搜索再点全等的那条。

        ⚠ 必须幂等 —— 单元层配了 refill_passes: 2（切资源位会重置字段，要填两遍）。
          「搜到就点」在第二遍会把第一遍选好的又点掉（antd 多选里点已选项 = 取消），
          标签一掉，它下面的「XX天数」那行跟着消失，报的却是「找不到两个数字框」，
          根本看不出是这儿的问题。所以先读已选，只补缺的、删多的。
        """
        item = self._scope(f)
        box = item.locator(".ant-select-multiple").first
        if not box.count():
            box = item.locator(".ant-select").last
        if not box.count():
            raise FillError(f"「{f['label']}」下没找到多选框")

        want = split_multi(value)
        have = self._picked_tags(box)
        for extra in [x for x in have if x not in want]:
            self._remove_tag(box, extra)
        todo = [x for x in want if x not in self._picked_tags(box)]
        if not todo:
            return

        inp = box.locator("input").first
        dd = self.page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)").last
        for one in todo:
            box.click()
            self.wait_until(lambda: dd.count() > 0, timeout=3000)
            inp.fill("")
            inp.type(one, delay=25)
            # 选项是后台搜出来的，等它真的返回，别按秒数猜
            self.wait_until(lambda: dd.locator(".ant-select-item-option").count() > 0)
            opt = dd.locator(".ant-select-item-option").filter(
                has_text=re.compile(rf"^\s*{re.escape(one)}\s*$")).first
            if not opt.count():
                avail = dd.locator(".ant-select-item-option").all_inner_texts()[:8]
                raise FillError(f"「{f['label']}」里没有「{one}」。搜出来的是：{avail}")
            opt.click()
            # 等标签真的进到框里，再去点下一个
            self.wait_until(lambda: one in self._picked_tags(box), timeout=5000)
        self.page.keyboard.press("Escape")

    @staticmethod
    def _picked_tags(box) -> list[str]:
        """多选框里已经选中的标签文字。"""
        out = []
        items = box.locator(".ant-select-selection-item")
        for i in range(items.count()):
            txt = (items.nth(i).inner_text() or "").strip()
            # 标签自带一个「×」，inner_text 里会带上，去掉
            txt = txt.rstrip("×✕✖ ").strip()
            if txt:
                out.append(txt)
        return out

    def _remove_tag(self, box, text: str):
        tag = box.locator(".ant-select-selection-item").filter(has_text=text).first
        if not tag.count():
            return
        x = tag.locator(".ant-select-selection-item-remove").first
        (x if x.count() else tag).click()
        self.page.wait_for_timeout(400)

    def _number_range_by_label(self, f, value):
        """一行两个数字框（人群天数区间：「即期 n 天至 m 天」）。填「1-30」。"""
        rng = W.parse_range(value)
        if rng is None:
            raise FillError(f"「{f['name']}」要填「小-大」两个数字，例 1-30（上界填 -1 = 不限），"
                            f"实际填了「{value}」")
        parts = [str(rng[0]), str(rng[1])]

        # ⚠ 这一行没有真正的 <label>：「在期大会员即期」几个字是控件区里的一个纯 div，
        #   formily-item 的 label 元素是空的（实地抓的，见 docs/资源位投放-人群配置抓取.md）。
        #   所以按「item 里包含这段文字 + 里面有两个数字框」来认，不走 _scope。
        # ⚠ 这一行是选中标签之后才渲染出来的，慢一点就找不到，等一会儿再判死
        item = None
        for _ in range(12):
            cands = self.page.locator(FORM_ITEM).filter(has_text=f["label"])
            for i in range(cands.count()):
                c = cands.nth(i)
                if c.locator(".ant-input-number").count() == 2:
                    item = c      # 投放侧先填、排除侧后填，取最后一个命中的正好对
            if item is not None:
                break
            self.page.wait_for_timeout(500)
        if item is None:
            raise FillError(
                f"找不到「{f['label']}」那一行的两个数字框 —— "
                f"通常是上面「我想投放」里没选中对应的人群标签")
        boxes = item.locator(".ant-input-number input")
        if boxes.count() < 2:
            raise FillError(f"「{f['label']}」下没找到两个数字框（找到 {boxes.count()} 个）")
        for i, v in enumerate(parts):
            box = boxes.nth(i)
            box.fill("")
            box.fill(v)
            self.page.wait_for_timeout(200)

    def _multiselect_vue(self, f, value):
        """v1 创意页的 vue-multiselect（固化权益、开卡赠礼）。"""
        picks = split_multi(value)
        limit = f.get("max_pick")
        if limit and len(picks) > int(limit):
            raise FillError(f"「{f['name']}」最多选 {limit} 个，填了 {len(picks)} 个")

        ms = self.page.locator(".multiselect").filter(has_text=f.get("label", "")).first
        if not ms.count():
            ms = self.page.locator(".multiselect").first
        if not ms.count():
            raise FillError(f"页面上找不到「{f['name']}」的多选下拉")

        for p in picks:
            ms.click()
            self.page.wait_for_timeout(500)
            opt = ms.locator(".multiselect__option").filter(has_text=p).first
            if not opt.count():
                raise FillError(f"「{f['name']}」里没有「{p}」")
            opt.click()
            self.page.wait_for_timeout(300)
        self.page.keyboard.press("Escape")

    def _upload_by_label(self, f, value):
        """图片上传。value 可以是本地路径，也可以是 http(s) 网址。

        网址先下到 output/_images/ 再当本地文件传 —— 素材本来就都在 CDN 上，
        让人先另存到本地再填路径纯属多此一举。同一个网址只下一次。

        ⚠ v1/v2 创意页没有 .ant-form-item / .ant-formily-item 容器，
        按 label 圈不到范围。所以三级回退：
          ① label 容器里的 file input
          ② 按页面上「图片(1020*300)」这类文字找它附近的 file input
          ③ 整页只有一个 file input 时直接用它
        多个上传框且都定位不到时宁可报错，也不瞎传（传错框比不传更难查）。
        """
        path = fetch_image(value) if is_url(value) else Path(value)
        if not path.exists():
            raise FillError(f"图片不存在：{value}")
        label = f.get("label", f["name"])

        # ① label 容器
        try:
            fi = self._scope(f).locator("input[type=file]").first
            if fi.count():
                self._send(fi, path)
                return
        except FillError:
            pass

        # ② 按 label 文字找最近的上传区
        node = self.page.locator("*", has_text=re.compile(re.escape(label))).last
        try:
            if node.count():
                fi = node.locator("input[type=file]").first
                if fi.count():
                    self._send(fi, path)
                    return
        except Exception:
            pass

        # ③ 整页唯一
        alls = self.page.locator("input[type=file]")
        n = alls.count()
        if n == 1:
            self._send(alls.first, path)
            return
        if n == 0:
            raise FillError(f"「{label}」找不到上传控件（页面上没有 input[type=file]）")
        raise FillError(
            f"「{label}」定位不到自己的上传控件，页面上有 {n} 个上传框，不敢乱传。"
            f"请把 yaml 里这个字段的 label 改成页面上的原文。")

    def _send(self, fi, path: Path):
        """选文件并等它真的传完。

        ⚠ 不写死「睡 2 秒」：小图 200ms 就好了，大图 2 秒还不够。
          盯着页面上出现预览图/文件名 —— 出来了就说明后台收下了。
        """
        # ⚠ 不能等「预览图出现」：好几个创意页压根不显示预览，每张图都要把
        #   20 秒上限等满（实测一轮两张图白白吃掉 45 秒）。
        #   改成等上传的网络请求回来 —— 这才是「后台收下了」的真信号。
        def _is_upload(resp):
            u = (resp.url or "").lower()
            return any(k in u for k in ("upload", "/bfs/", "oss", "file"))

        try:
            with self.page.expect_response(_is_upload, timeout=max(self.timeout, 20000)):
                fi.set_input_files(str(path))
            self.page.wait_for_timeout(300)
            return
        except Exception:
            pass                       # 没抓到上传请求就退回看预览

        ok = self.wait_until(
            lambda: self.page.locator(
                "img, .el-upload-list__item, .ant-upload-list-item").count() > 0,
            timeout=5000)
        if not ok:
            log.info("传完 %s 没看到上传请求也没看到预览，继续往下走", path.name)

    def _date_by_label(self, f, value):
        """单个日期选择器。"""
        item = self._scope(f)
        inp = item.locator("input").first
        inp.click()
        self.page.wait_for_timeout(400)
        inp.fill(value)
        self.page.wait_for_timeout(400)
        inp.press("Enter")
        self.page.wait_for_timeout(300)
        self.page.keyboard.press("Escape")

    def _date_range_start(self, f, value):
        self._range(f, value, which=0)

    def _date_range_end(self, f, value):
        self._range(f, value, which=1)

    def _range(self, f, value, which: int):
        """RangePicker 的一段。

        ⚠ 开始段和结束段是两个 input，antd 靠焦点在两段间切换。
        必须先 Escape 收掉上一次残留的浮层，否则点结束段会被判回开始段
        （在浏览器里实测过：不收浮层时结束段永远填不进去）。
        """
        item = self._scope(f)
        inputs = item.locator("input")
        if inputs.count() < 2:
            raise FillError(f"「{f['label']}」不是区间选择器（只有 {inputs.count()} 个输入框）")

        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(300)

        inp = inputs.nth(which)
        inp.click()
        self.page.wait_for_timeout(500)
        inp.fill(value)
        self.page.wait_for_timeout(500)
        inp.press("Enter")
        self.page.wait_for_timeout(600)

        got = (inp.input_value() or "").strip()
        if not got:
            raise FillError(f"「{f['label']}」第 {which + 1} 段填了「{value}」但没生效")

    def _opt_re(self, value, f):
        if f.get("option_match") == "contains":
            return re.compile(re.escape(value))
        return re.compile(rf"^\s*{re.escape(value)}\s*$")

    HANDLERS = {
        "fill": _fill,
        "fill_by_label": _fill_by_label,
        "fill_by_ph": _fill_by_ph,
        "radio_by_label": _radio_by_label,
        "vue_radio": _vue_radio,
        "checkbox_sync_formily": _checkbox_sync_formily,
        "select_antd_by_label": _select_antd_by_label,
        "multiselect_vue": _multiselect_vue,
        "multiselect_antd": _multiselect_antd,
        "number_range_by_label": _number_range_by_label,
        "upload_by_label": _upload_by_label,
        "date_by_label": _date_by_label,
        "date_range_start": _date_range_start,
        "date_range_end": _date_range_end,
    }
