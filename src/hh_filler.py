"""花火稿件授权 这套 DOM 的控件填法。

⚠ 独立于别的 filler。各家后台的 DOM 栈完全不同（Formily / Vue+tw- / iView /
  Arco / antd），选择器一行都不能互抄 —— 这个隔离是刻意的，别去合并。

**和 DOM 无关的部分一律从 src/fill_core.py 拿**（等待、按文字挑一条、报错措辞）。

定位策略：这一页是 **iView**（`ivu-*`）。用到的类名有两种，分开看：

  · `ivu-modal-content` / `ivu-select-item` / `ivu-radio-wrapper-checked` …
    —— iView 自己的公共类，跨版本稳定，可以用（不是编译哈希类名）。
  · `search-type-select` / `video-auto-complete` / `bind-time-type` /
    `bind-auth-range` / `form-footer` —— 业务组件自己写的语义类名，也稳定。
    它们写在 config/forms/花火稿件授权.yaml 的 dialog: 段里，改版只改 yaml。

⚠ 这一页**没有** label→字段块 那种通用结构可用：三个控件的 label 都是嵌在
  `<div>` 里的 `<span>`，同一个弹窗里「视频」「授权时间」「共享范围」各只出现一次，
  按语义类名定位比按 label 文字爬 DOM 更短也更准，所以这里没实现 _block()。

弹窗里的交互顺序（顺序不能换）：
  1. 类型下拉默认是「视频链接」，**每次开弹窗都要切成「稿件bvid」**；
     先填 bvid 再切类型的话，输入框会被清空。
  2. 输入 bvid → 页面自己发 archive/status 校验，转圈期间点「确定」会被吞掉。
  3. 授权时间 / 共享范围 两个单选组（都有默认值，值相同时不用点）。
  4. 勾协议 →「确定」。协议没勾时「确定」点了没反应，也不报错。
"""
from __future__ import annotations

import logging

from .fill_core import FillError, field_error, option_error, verify_error, wait_until

log = logging.getLogger(__name__)

# 提交后页面弹的 toast。iView 的 Message / Notice 两种都收。
TOAST_SEL = ".ivu-message-notice-content, .ivu-notice-desc, .ivu-notice-title"


class HhFiller:
    def __init__(self, page, timeout: int = 15000, on_note=None, dialog: dict | None = None):
        self.page = page
        self.timeout = timeout
        self._on_note = on_note
        d = dialog or {}
        self.sel = {
            "modal": d.get("modal", ".ivu-modal-content"),
            "title": d.get("title", "添加稿件授权"),
            "type_select": d.get("type_select", ".search-type-select"),
            "type_value": d.get("type_value", "稿件bvid"),
            "video_input": d.get("video_input", ".video-auto-complete input.ivu-input"),
            "time_group": d.get("time_group", ".bind-time-type"),
            "range_group": d.get("range_group", ".bind-auth-range"),
            "agree_checkbox": d.get("agree_checkbox", ".form-footer input[type=checkbox]"),
            "submit_text": d.get("submit_text", "确定"),
            "cancel_text": d.get("cancel_text", "取消"),
        }

    def _note(self, msg: str):
        if self._on_note:
            self._on_note(msg)

    # ------------------------------------------------ 弹窗本身
    def modal(self):
        return self.page.locator(self.sel["modal"]).filter(has_text=self.sel["title"]).last

    def modal_open(self) -> bool:
        m = self.modal()
        try:
            return m.count() > 0 and m.is_visible()
        except Exception:
            return False

    def open_dialog(self):
        """点「添加稿件授权」，等弹窗出来。已经开着就直接用。"""
        # ⚠ 先收掉上一条留下的「发送绑定申请成功」弹窗：它盖着整页，
        #   不关的话「添加稿件授权」按钮点不动（点击落在遮罩上），第 2 条就卡死。
        self.dismiss_success()
        if self.modal_open():
            return self.modal()
        btn = self.page.get_by_text("添加稿件授权", exact=True).first
        if btn.count() == 0:
            raise field_error("添加稿件授权", "列表页上没找到这个按钮，确认当前在「花火内容授权」这个 tab")
        btn.click()
        if not wait_until(self.page, self.modal_open, self.timeout):
            raise FillError("点了「添加稿件授权」但弹窗没出来")
        return self.modal()

    def close_dialog(self):
        """收拾现场：失败/跳过之后必须收掉弹窗，不然下一条会填到这一个上。"""
        self.dismiss_success()
        if not self.modal_open():
            return
        try:
            self.modal().locator("button", has_text=self.sel["cancel_text"]).first.click()
        except Exception:
            try:
                self.modal().locator(".ivu-modal-close").first.click()
            except Exception:
                log.warning("关弹窗失败", exc_info=True)
        wait_until(self.page, lambda: not self.modal_open(), 4000)

    # ------------------------------------------------ 三个控件
    def set_type(self, value: str = ""):
        """左边那个类型下拉：视频链接 / 稿件avid / 稿件bvid。

        ⚠ 每次开弹窗都是「视频链接」，必须切 —— 不切的话 bvid 会被当链接校验，
          页面报「请输入正确的视频链接」。
        """
        value = value or self.sel["type_value"]
        sel = self.modal().locator(self.sel["type_select"])
        cur = sel.locator(".ivu-select-selected-value")
        if cur.count() and cur.inner_text().strip() == value:
            return
        sel.locator(".ivu-select-selection").click()
        if not wait_until(self.page, lambda: sel.locator("li.ivu-select-item").first.is_visible(),
                          self.timeout):
            raise option_error("视频类型", value, [])
        items = sel.locator("li.ivu-select-item")
        texts = [items.nth(i).inner_text().strip() for i in range(items.count())]
        if value not in texts:
            raise option_error("视频类型", value, texts)
        items.filter(has_text=value).first.click()
        # 回读核对：这类后台大量存在「点了没选上还不报错」
        if not wait_until(self.page,
                          lambda: cur.count() and cur.inner_text().strip() == value, 4000):
            raise verify_error("视频类型", cur.inner_text().strip() if cur.count() else "", value)

    def set_video(self, bvid: str):
        """填 bvid，并等页面那次远程校验转完圈。"""
        inp = self.modal().locator(self.sel["video_input"]).first
        if inp.count() == 0:
            raise field_error("视频", "弹窗里没找到 bvid 输入框")
        inp.click()
        inp.fill("")
        # ⚠ 用 type 逐字敲：这个输入框的远程校验挂在 input 事件的防抖上，
        #   直接 fill 整串在部分版本里只触发一次、偶发抓不到校验结果。
        inp.type(bvid, delay=30)
        got = inp.input_value().strip()
        if got != bvid:
            raise verify_error("视频", got, bvid)
        # 校验转圈结束（loading 图标消失）→ 浮层里点中那条候选，这条才算填上
        wait_until(self.page, lambda: not self._validating(), self.timeout)
        self._pick_candidate(bvid)
        tip = self.error_tip()
        if tip:
            raise FillError(f"bvid「{bvid}」页面校验没过：{tip}")

    def _pick_candidate(self, bvid: str):
        """敲完 bvid 之后，**必须点一下浮层里那条候选**，不然这条根本没填上。

        ⚠ 这是这套弹窗最隐蔽的两个坑，都在这儿：
          1. 光把字敲进输入框不算数 —— 表单的值来自「选中的那条候选」。
             不选的话输入框标红「请输入需要绑定的bilibili视频」，点确定没反应。
          2. 浮层被 iView 的 transfer 挪到了 **body 底下**（`.video-ac-dropdown`），
             不在弹窗里 —— 在 `.ivu-modal-content` 范围内怎么找都找不到它，
             会误判成「没有候选」。所以这里从 page 找，不从 modal 找。
          另外浮层开着的时候，下一次点击会被它吃掉（只关浮层、不点中控件），
          表现是「点了 30天 没选上」还不报错 —— 选中候选会顺带把浮层关掉。
        """
        opt = self.page.locator(".video-ac-dropdown li.ivu-select-item.auto-complete-option")

        def shown():
            return opt.count() > 0 and opt.first.is_visible()

        # 敲完一般就自己弹出来了；没弹（或者中途被别处的点击收掉了）就再点一下输入框叫它出来
        if not wait_until(self.page, shown, max(4000, self.timeout // 3)):
            try:
                self.modal().locator(self.sel["video_input"]).first.click()
            except Exception:
                pass
        if not wait_until(self.page, shown, self.timeout):
            raise FillError(f"bvid「{bvid}」没搜到可选的稿件"
                            + (f"：{self.error_tip()}" if self.error_tip() else
                               "（浮层里只有「无匹配数据」）"))
        if opt.count() > 1:
            # 正常只会有一条；多条说明这个 bvid 不够唯一，别替用户猜
            self._note(f"bvid「{bvid}」搜出 {opt.count()} 条候选，取了第一条")
        opt.first.click()
        wait_until(self.page, lambda: not opt.first.is_visible(), 4000)

    def _validating(self) -> bool:
        try:
            return self.modal().locator(".ivu-input-icon-validate.ivu-load-loop").count() > 0
        except Exception:
            return False

    def error_tip(self) -> str:
        try:
            tip = self.modal().locator(".ivu-form-item-error-tip")
            for i in range(tip.count()):
                t = tip.nth(i).inner_text().strip()
                if t:
                    return t
        except Exception:
            pass
        return ""

    def set_radio(self, group_sel: str, label: str, what: str):
        """单选按钮组：按选项文字点，点完回读 ivu-radio-wrapper-checked 核对。"""
        grp = self.modal().locator(group_sel)
        if grp.count() == 0:
            raise field_error(what, f"弹窗里没找到这一组（{group_sel}）")
        items = grp.locator("label.ivu-radio-wrapper")
        texts = [items.nth(i).inner_text().strip() for i in range(items.count())]
        if label not in texts:
            raise option_error(what, label, texts)
        idx = texts.index(label)
        if "ivu-radio-wrapper-checked" in (items.nth(idx).get_attribute("class") or ""):
            return                       # 已经是这个值了，别白点一下
        items.nth(idx).click()
        if not wait_until(self.page, lambda: "ivu-radio-wrapper-checked" in
                          (items.nth(idx).get_attribute("class") or ""), 4000):
            raise verify_error(what, "没选上", label)

    def set_time(self, value: str):
        self.set_radio(self.sel["time_group"], value, "授权时间")

    def set_range(self, value: str):
        self.set_radio(self.sel["range_group"], value, "共享范围")

    def agree(self):
        """勾《商业合作授权协议》。⚠ 不勾的话「确定」点了没反应，也不报错。"""
        box = self.modal().locator(self.sel["agree_checkbox"]).first
        if box.count() == 0:
            raise field_error("商业合作授权协议", "弹窗里没找到勾选框")
        if box.is_checked():
            return
        # ⚠ 真正的 <input> 被 iView 的样式盖住了，点不到，要点那个画出来的方框
        #   （`.ivu-checkbox`）。**不能点外层 label**：label 里连着
        #   《商业合作授权协议》那个 target=_blank 的链接，点中文字会给用户开一个新标签页，
        #   而勾选状态一点没变（实测过，1/3 的位置就落在链接上）。
        self.modal().locator(".form-footer .ivu-checkbox").first.click()
        if not wait_until(self.page, lambda: box.is_checked(), 4000):
            raise verify_error("商业合作授权协议", "没勾上", "已勾选")

    # ------------------------------------------------ 一条的完整填法
    def fill(self, bvid: str, time_value: str, range_value: str):
        self.open_dialog()
        self.set_type()                  # ⚠ 必须在 set_video 之前，切类型会清空输入框
        self.set_video(bvid)
        self.set_time(time_value)
        self.set_range(range_value)
        self.agree()

    # ------------------------------------------------ 提交
    def submit(self) -> str:
        """点「确定」，返回页面给的那句话。

        成功判据：弹窗关掉了。只看「点了确定」是不够的 —— 协议没勾、校验没过、
        7 天内第 4 次申请，这三种情况点了都毫无动静或只弹一个 toast。
        """
        self._clear_toast()
        self.modal().locator("button").filter(has_text=self.sel["submit_text"]).first.click()

        # ⚠ 判据是「成功弹窗冒出来了」，**不是**「填表弹窗关掉了」：
        #   实测这两件事不是同时发生的 —— 成功弹窗盖上来的时候，填表弹窗有时还在底下
        #   开着（多等一会儿才收）。按「关掉了」判，会把**已经发出去的**报成失败
        #   （2026-09-23 踩过：BV1Mqeq6GE3E 报失败，接口回查 auth_times 已经是 1），
        #   而这种误报最贵：同一视频 7 天只有 3 次机会，重跑一次就白烧一次。
        ok = wait_until(self.page, lambda: self._success_open() or not self.modal_open(),
                        self.timeout)
        msg = self.dismiss_success()
        if msg:
            # 点掉「我知道了」之后填表弹窗一般自己就收了；没收的话主动关掉，
            # 不然下一条会被 open_dialog 当成「已经开着的弹窗」接着填。
            if not wait_until(self.page, lambda: not self.modal_open(), 4000):
                self.close_dialog()
            return msg
        msg = self.toast() or self.error_tip()
        if not ok or self.modal_open():
            raise FillError(f"点了「{self.sel['submit_text']}」之后没等到「发送绑定申请成功」"
                            + (f"：{msg}" if msg else "（页面没给任何提示）"))
        return msg or "申请已发出"

    def _success_open(self) -> bool:
        btn = self.page.locator("button", has_text="我知道了")
        try:
            return btn.count() > 0 and btn.first.is_visible()
        except Exception:
            return False

    def dismiss_success(self) -> str:
        """收掉提交成功后弹的第二层弹窗，并把它那句话带出来当结果。

        ⚠ 这个弹窗是「添加视频授权 / 发送绑定申请成功 / …请到花火平台确认 /【我知道了】」，
          **标题和填表那个弹窗不一样**（添加视频授权 vs 添加稿件授权），所以 modal_open()
          看不见它 —— 别指望 submit 里那句「弹窗关掉了」能覆盖到。
          它带一层盖住整页的遮罩：不点掉，下一条连「添加稿件授权」都点不动
          （点击全落在遮罩上，而且不报错，表现就是整批从第 2 条起卡住）。
        """
        btn = self.page.locator("button", has_text="我知道了")
        try:
            if btn.count() == 0 or not btn.first.is_visible():
                return ""
        except Exception:
            return ""
        msg = ""
        try:
            box = self.page.locator(".ivu-modal-content").filter(has_text="我知道了").last
            # 取「发送绑定申请成功」这种结论句，整段说明太长，记进结果 csv 没意义
            msg = (box.inner_text().strip().split("\n")[1:2] or [""])[0].strip()
        except Exception:
            pass
        btn.first.click()
        if not wait_until(self.page, lambda: btn.count() == 0 or not btn.first.is_visible(), 5000):
            self._note("「我知道了」点了没关掉，下一条可能会被它挡住")
        return msg

    def _clear_toast(self):
        """把上一条留下的 toast 抹掉，免得读成这一条的结果。"""
        try:
            self.page.evaluate(
                "sel => document.querySelectorAll(sel).forEach(e => e.remove())", TOAST_SEL)
        except Exception:
            pass

    def toast(self) -> str:
        try:
            t = self.page.locator(TOAST_SEL)
            for i in range(t.count()):
                s = t.nth(i).inner_text().strip()
                if s:
                    return s
        except Exception:
            pass
        return ""
