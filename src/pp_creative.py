"""价格面板的创意页填写。

⚠ 这是这个项目里的**第四套 DOM**，四套互不通用，选择器一个都不能抄：
    价格配置        antd            .ant-form-item
    资源位投放      Formily         .ant-formily-item / mega-ant-
    价格面板单元层  Vue + tw- 哈希   靠 <label> 文字找字段块
    价格面板创意层  Arco Design     .arco-form-item          ← 这一份

页面形态（2026-08-26 实抓，单元 40900 / 40817 / 40773 / 40540 / 33283）：

    上传创意素材
      [创意1] [创意2] …           ← antd Tabs，「新增创意」加一条
        套餐排列 *                ← 只读的 SKU 卡片条
        ── 选中那张卡片的字段 ──    ← ⚠ 同一时刻只显示一张卡的
        价格面板切换按钮 *          ← 面板级，切哪张卡都在
      [关 闭] [保 存]

⚠ **12 个 SKU 的字段组都在 DOM 里，但只有选中那张是可见的**（实测 38 个 input，
  其中 36 个 display:none）。所以所有定位都必须先按「可见」过滤，否则会填到
  别的 SKU 头上，而且页面上一点异常都看不出来。

⚠ 用这个直连地址打开时，**已存创意的内容不会回填**（实测 40900 已有创意 137896、
  40817 已有 3 条，页面照样是空表单）。也就是说这里永远是在填一条新创意。
  要读已经配了什么，走 /x/admin/vas/ads/originality/v3/by_unitid?unit_id=<id>。
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import images
from .filler import FillError

log = logging.getLogger(__name__)

ITEM = ".arco-form-item"


def _ascii_copy(path: str) -> str:
    """文件名里有非 ASCII 就先复制成一个纯 ASCII 名的再传。

    ⚠ 不是洁癖。贴在 Excel 格子里的图，抽出来的文件名是「单元_Y2_0.png」
      （sheet 名 + 单元格），带中文；实测这种名字**传上去没反应** —— 页面不报错、
      预览也不出，最后保存下来那一栏就是空的（单元 40908 的 icon 就这么丢了）。
      同一次跑里 ASCII 名的那张（网址下下来的 url_xxx.png）传得好好的。
    """
    p = Path(path)
    if p.name.isascii():
        return str(p)
    import hashlib
    import shutil
    safe = p.with_name(f"cell_{hashlib.md5(p.name.encode()).hexdigest()[:12]}{p.suffix}")
    if not safe.exists():
        shutil.copy2(p, safe)
    log.info("文件名带中文，先复制成 %s 再传", safe.name)
    return str(safe)


class CreativeFiller:
    def __init__(self, page, timeout: int = 20000, on_note=None):
        self.page = page
        self.timeout = timeout
        self.on_note = on_note

    def _note(self, msg: str):
        log.info(msg)
        if self.on_note:
            self.on_note(msg)

    # ---------------------------------------------------------------- 开页
    def open(self, url: str):
        self.page.goto(url, wait_until="domcontentloaded")
        # ⚠ 后台标签页会被 Chrome 降频，实测慢十几倍
        self.page.bring_to_front()
        if not self._wait(lambda: self.page.locator(f"{ITEM}:visible").count() > 0):
            body = self.page.inner_text("body")[:200].replace("\n", " ")
            raise FillError(f"创意页没加载出来（unitId 对不对？）。页面上是：{body}")

    def _wait(self, cond, timeout: int | None = None, step: int = 150) -> bool:
        waited, limit = 0, (timeout or self.timeout)
        while waited < limit:
            try:
                if cond():
                    return True
            except Exception:
                pass
            self.page.wait_for_timeout(step)
            waited += step
        return False

    # ---------------------------------------------------------------- 套餐卡片
    def _panel_box(self):
        box = self.page.locator(ITEM).filter(has_text="套餐排列").first
        if not box.count():
            raise FillError("创意页上找不到「套餐排列」那一段")
        return box

    def cards(self) -> list[str]:
        """卡片条上的 SKU 名，按页面顺序（= 面板1 从左到右，再面板2）。"""
        return self.page.evaluate(
            """
            () => {
              const items = [...document.querySelectorAll('.arco-form-item')];
              const box = items.find(it => (it.querySelector('label')||{}).innerText
                                            ?.includes('套餐排列'));
              if (!box) return [];
              return [...box.querySelectorAll('div,li,span')]
                .filter(el => el.children.length === 0 && el.innerText.trim()
                              && !/^面板\\d|推荐|隐藏sku/.test(el.innerText.trim()))
                .map(el => el.innerText.trim());
            }
            """)

    def pick_card(self, sku: str):
        """点中一张卡片。点完下面那些字段才是这个 SKU 的。"""
        box = self._panel_box()
        node = box.get_by_text(sku, exact=True).first
        if not node.count():
            raise FillError(f"「套餐排列」里没有「{sku}」这张卡片 —— "
                            f"现在有的是：{'、'.join(self.cards()) or '一张都没有'}")
        node.click()
        self.page.wait_for_timeout(400)

    # ---------------------------------------------------------------- 字段
    def _item(self, label: str):
        """按 label 找**可见**的那个字段块。

        ⚠ 必须 :visible。同一个 label（「sku角标文案」）在 DOM 里有十几份，
          只有选中卡片的那一份是显示出来的，取错了就填到别的 SKU 头上。
        """
        loc = self.page.locator(f"{ITEM}:visible").filter(has_text=label)
        n = loc.count()
        if not n:
            return None
        if n == 1:
            return loc.first
        # 「面板1:」这种短 label 会被「价格面板切换按钮」那个外层块一起命中，
        # 取最里面的那个（DOM 里排在最后的是嵌套最深的）
        return loc.nth(n - 1)

    def has(self, label: str) -> bool:
        return self._item(label) is not None

    def fill(self, label: str, value: str):
        blk = self._item(label)
        if blk is None:
            raise FillError(f"创意页上找不到字段「{label}」")
        inp = blk.locator("input[type='text'], input:not([type]), textarea").first
        if not inp.count():
            raise FillError(f"「{label}」这一块里没有可填的输入框")
        inp.click()
        inp.fill("")
        inp.type(str(value), delay=10)

    def radio(self, label: str, value: str):
        blk = self._item(label)
        if blk is None:
            raise FillError(f"创意页上找不到单选项「{label}」")
        # ⚠ 点 <label>，不是 <input>。Arco 的 input 被样式盖住，直接点它不生效。
        opt = blk.locator("label").filter(has_text=value).first
        if not opt.count():
            texts = blk.evaluate(
                "el => [...el.querySelectorAll('label')].map(x => x.innerText.trim())")
            raise FillError(f"「{label}」没有「{value}」这个选项，页面上是：{texts}")
        opt.click()
        self.page.wait_for_timeout(300)

    def upload(self, label: str, value: str):
        """上传图片 / svga。input[type=file] 是隐藏的，set_input_files 不受影响。

        value 三种填法都认（和资源位投放一样，别让人为了这个再另存一遍）：
          · 本地路径        直接用
          · 图片贴在格子里  读 Excel 那一步就已经抽成文件了（pp_data 里做的）
          · http(s) 网址    这里下到本地再传；素材本来就都在 CDN 上
        """
        blk = self._item(label)
        if blk is None:
            raise FillError(f"创意页上找不到上传项「{label}」")
        inp = blk.locator("input[type='file']").first
        if not inp.count():
            raise FillError(f"「{label}」这一块里没有文件输入框")

        path = str(value).strip()
        if images.is_url(path):
            try:
                path = str(images.fetch_image(path))
            except images.ImageError as e:
                raise FillError(f"「{label}」的图下不下来：{e}") from e
        elif not Path(path).exists():
            raise FillError(f"「{label}」找不到这个文件：{path}"
                            f"（可以填本地路径、图片网址，或者直接把图贴进 Excel 那一格）")

        # ⚠ 必须等预览图出来，而且**等不到要重传、再等不到要报错**。传图是往 CDN
        #   发的网络请求，实测会偶发地什么都不发生（页面不报错、预览也不出）。
        #   早先这里等超时了不管、直接往下走 —— 非必填的那几栏（icon）页面不会拦，
        #   于是保存下来就是空的、日志上还一片「成功」。静默丢素材比报错难查一百倍。
        local = _ascii_copy(path)
        for attempt in range(3):
            before = blk.locator("img").count()
            inp.set_input_files(local)
            if self._wait(lambda: blk.locator("img").count() > before,
                          timeout=max(self.timeout, 30000)):
                return
            self._note(f"「{label}」第 {attempt + 1} 次传上去没出预览图，重传")
        raise FillError(f"「{label}」传了 3 次都没出预览图，这一栏多半是空的。文件：{path}")

    def fill_or_upload(self, label: str, value: str):
        """既能填链接也能传文件的那种（sku红包弹窗动画）。

        ⚠ 这一栏和上面的图片不一样：页面本来就收链接，所以网址**直接填进去**，
          不用先下下来再传一遍。
        """
        v = str(value).strip()
        if v and not images.is_url(v) and Path(v).exists():
            self.upload(label, v)
        else:
            self.fill(label, v)

    # ---------------------------------------------------------------- 保存
    def save(self, text: str = "保 存"):
        btn = self.page.locator("button").filter(has_text=text).first
        if not btn.count():
            raise FillError(f"创意页上找不到「{text}」按钮")
        btn.click()
        ok = self._wait(
            lambda: self.page.locator(".arco-message-success, .ant-message-success").count() > 0,
            timeout=self.timeout)
        if not ok:
            raise FillError(f"点了「{text}」但没等到成功提示。{self.errors()}")

    def errors(self) -> str:
        """页面上标红的校验信息，攒成一句人话。

        ⚠ 类名是 `.arco-form-message`，不是 `.arco-form-item-message`（对着
          Arco 的文档想当然写错过一次，结果满屏「请填写小灰条文案」，
          机器人却报「页面上没有可读的报错」——白丢了一次排查）。
        """
        try:
            msgs = self.page.evaluate(
                """
                () => {
                  const sel = '.arco-form-message, .arco-form-item-message,'
                            + '.arco-message-error, .ant-message-error';
                  return [...document.querySelectorAll(sel)]
                    .filter(e => { const r = e.getBoundingClientRect();
                                   return r.width > 0 && r.height > 0; })
                    .map(e => e.innerText.trim()).filter(Boolean);
                }
                """)
        except Exception:
            msgs = []
        uniq = []
        for m in msgs:
            if m not in uniq:
                uniq.append(m)
        return ("页面提示：" + "；".join(uniq)) if uniq else "页面上没有可读的报错"
