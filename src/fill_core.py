"""四套 filler 共用的原语：和 DOM 栈无关的那部分。

## 为什么有这个文件

`filler` / `wizard_filler` / `pp_filler` / `ad_filler` 分别服务 antd 弹窗、
Formily 整页、Vue+tw- 老后台、iView 商广后台 —— **选择器确实一行都不能互抄**，
这个隔离是对的，不要去合并它们。

但「等到条件成立」「按文字挑一条」「等渲染稳定」「报错该怎么措辞」这几样和
DOM 无关，各写了一遍：那个「盯着 cond 轮询到超时」的循环就有 4 份一模一样的拷贝
（`pp_filler.wait_until` / `wizard_filler.wait_until` / `wizard_runner._wait` /
`pp_creative._wait`），另外还有一批各自长出来的 `_wait_rows` / `_wait_gone` /
`_wait_list_changed`，都是同一个循环套一个自己的判据。
新接一个后台时，这部分不该再抄第五遍。

## 怎么用

**存量的四套 filler 不改**（都在线上跑着，没有测试网兜底，动它们不划算）。
**下一个新 filler 建在这上面**：

    from .fill_core import FillError, wait_until, norm, pick, option_error

    class XxxFiller:
        def __init__(self, page, timeout=15000, on_note=None):
            self.page, self.timeout, self._on_note = page, timeout, on_note

        def select(self, label, value):
            blk = self._block(label)             # ← 只有这一步是这套 DOM 特有的
            self._open(blk)
            wait_until(self.page, lambda: self._options(blk), self.timeout)
            texts = self._options(blk)
            hit = pick(texts, value)
            if hit is None:
                raise option_error(label, value, texts)
            ...

也就是说：**每套 filler 自己只负责「怎么找到这个字段块」「怎么把浮层点开」
「怎么读出选项文字」这三件 DOM 特有的事**，剩下的都从这里拿。

⚠ 这里的函数一律**不吃 form yaml 的字段字典**，只吃朴素的 str/list。
  一旦开始 `f.get("xxx")`，就又和某一套 yaml 结构绑死了，共用不起来。
"""
from __future__ import annotations

import logging
import re

# 从 filler 引，不在这里重新定义 —— runner 里全是 `except FillError`，
# 必须是同一个类对象，不然新 filler 抛的异常会穿透整个主流程。
from .filler import FillError, split_multi   # noqa: F401  （给新 filler 转出去）

log = logging.getLogger(__name__)


# ============================================================ 等待
def wait_until(page, cond, timeout: int, step: int = 120) -> bool:
    """等到 cond() 为真就立刻返回 True，超时返回 False。

    ⚠ 全项目不写 `wait_for_timeout(2000)` 这种死等：内网快慢差很多，
      固定值不是白等就是不够。上限跟着 settings.timeout 走，用户改一个数
      就能整体放宽。

    cond() 抛异常按「还没成立」算 —— 元素还没渲染出来时读它必然抛，
    这是正常过程，不是错误。
    """
    waited = 0
    while True:
        try:
            if cond():
                return True
        except Exception:
            pass
        if waited >= timeout:
            return False
        page.wait_for_timeout(step)
        waited += step


def wait_stable(page, read, quiet_ms: int = 700, step: int = 150,
                timeout: int = 6000):
    """等 read() 的返回值连续 quiet_ms 没变化，认为渲染完了。返回最后读到的值。

    用在「拿不到明确完成信号」的地方：列表在陆续渲染、拖拽之后顺序在回弹。
    和 wait_until 的区别是它等的是**不再变**，不是**变成某个值**。

    ⚠ read() 要能便宜地反复调用（读一次 DOM 就够），别在里面点东西。
    """
    last, stable, waited = None, 0, 0
    while waited < timeout:
        try:
            now = read()
        except Exception:
            now = None
        if now == last and now is not None:
            stable += step
            if stable >= quiet_ms:
                return now
        else:
            last, stable = now, 0
        page.wait_for_timeout(step)
        waited += step
    return last


# ============================================================ 文本匹配
def norm(s) -> str:
    """比对页面文字前的归一化：去首尾空白、去换行。

    ⚠ 只做这两样，**不要**顺手去掉全部空格 —— 有的选项文字里空格是有意义的
      （「连续包月 首月优惠」和「连续包月首月优惠」在有的后台是两个 SKU）。
    """
    return (s or "").strip().replace("\n", "")


def pick(texts: list, value, contains: bool = False):
    """从候选文字里挑出对应 value 的那一条；挑不到返回 None。

    三级匹配，顺序不能换：

      1. **完全相等**（归一化之后）。绝大多数情况走这一档。
      2. `contains=True` 时：**「value + 左括号」开头**。
         页面把 pid 显示成 `11439(normal,ipad,连续包年,人群包,148.00元)`，
         Excel 里填的是 `11439`。
         ⚠ 这一档必须在纯子串之前，而且必须带上那个左括号 ——
           光用子串的话 `1143` 会命中 `11439`、`114` 会命中一大片。
      3. `contains=True` 时：纯子串兜底。

    ⚠ 默认 contains=False。模糊匹配只在「页面显示值带后缀说明」这种确定场景
      开，随手开会静默选错一条，而且选错了页面不会报任何错。
    """
    v = norm(str(value))
    if not v:
        return None
    for t in texts:
        if norm(t) == v:
            return t
    if not contains:
        return None
    for t in texts:
        if norm(t).startswith(v + "("):
            return t
    for t in texts:
        if v in norm(t):
            return t
    return None


def pick_all(texts: list, values: list, contains: bool = False):
    """批量挑。返回 (命中表 {想要的值: 页面上的文字}, 没挑到的值列表)。

    多选下拉用：一次把要勾的全对上，缺哪几个一起报，
    别勾一个报一个 —— 用户要跑三趟才知道自己写错了三个。
    """
    hit, missing = {}, []
    for v in values:
        got = pick(texts, v, contains)
        if got is None:
            missing.append(v)
        else:
            hit[v] = got
    return hit, missing


def value_matches(value: str, option: str, mode: str = "") -> bool:
    """yaml 里 `reveals` / `option_match` 那种「这个值算不算命中这个选项」。

    mode:
      ""          完全相等（默认）
      "contains"  value 以 option 开头，或 option 是 value 的子串
      "multi"     value 是「A,B,C」这种多选值，option 是其中一项就算命中
    """
    if mode == "multi":
        return option in split_multi(value)
    if mode == "contains":
        return value.startswith(option) or option in value
    return value == option


def opt_regex(value: str, contains: bool = False):
    """给 Playwright 的 `get_by_text(re)` 用的正则。

    默认锚定首尾，避免「年卡」把「年卡优先·双面板」也选上。
    """
    if contains:
        return re.compile(re.escape(value))
    return re.compile(rf"^\s*{re.escape(value)}\s*$")


# ============================================================ 点击
def js_click(el):
    """滚到可视区中间再用 JS 点。

    ⚠ 用 JS 的 el.click() 而不是 Playwright 的 el.click()：
      这几个后台的下拉浮层很爱盖住别的元素，Playwright 会因为
      「被别的元素挡住」直接报错，而 JS 点不受遮挡影响。

    ⚠ 例外：**拖拽必须用真实鼠标事件**，JS 合成事件拖不动，
      而且被浮层挡住时一声不吭（表现成「拖了 3 次纹丝不动」）。
      还有少数「点开才变可编辑」的远程搜索框也吃真实点击，
      见 pp_filler._open_input 的注释。
    """
    el.evaluate("el => { el.scrollIntoView({block:'center'}); el.click(); }")


# ============================================================ 报错措辞
#
# ⚠ 这几个函数存在的意义就是**统一措辞**。这个项目的用户是运营，不是开发：
#   报错是他们唯一能看到的线索，「element not found」对他们等于没说。
#   约定是三段：【哪个字段】+【发生了什么】+【页面上现在是什么】。
#   最后那段最值钱 —— 有它，用户自己就能看出是 Excel 写错了还是页面变了。

def option_error(label: str, value, texts: list, what: str = "下拉",
                 limit: int = 12) -> FillError:
    """「候选里没有这一条」。texts 是当场读到的全部候选。"""
    seen = list(texts)[:limit]
    more = f"（共 {len(texts)} 条，只列前 {limit} 条）" if len(texts) > limit else ""
    if not texts:
        return FillError(
            f"「{label}」的{what}里一条候选都没有，要选的是「{value}」。"
            f"多半是{what}没真展开，或者上游的字段还没选。")
    return FillError(
        f"「{label}」的{what}里没有「{value}」。现在能看到的：{seen}{more}")


def missing_error(label: str, missing: list, texts: list, limit: int = 12) -> FillError:
    """多选：要勾的这几个页面上没有。一次报全，别一个一个报。"""
    seen = list(texts)[:limit]
    return FillError(
        f"「{label}」要选的 {missing} 在页面上没有。"
        f"候选共 {len(texts)} 条，能看到的：{seen}")


def field_error(label: str, hint: str = "") -> FillError:
    """「页面上根本找不到这个字段」。hint 写清楚最可能的原因。"""
    tail = f"（{hint}）" if hint else "（是不是上游的字段还没选，这一段还没渲染出来？）"
    return FillError(f"页面上找不到字段「{label}」{tail}")


def verify_error(label: str, got, want) -> FillError:
    """填了但没生效 —— 点了/输了之后回读对不上。

    ⚠ 每个「填」的动作都该回读核对一次。这套后台大量存在
      「点了没选上、还不报错」的控件，不核对就会静默配错，
      而配错的单元是真的会生效的。
    """
    return FillError(f"「{label}」填完之后是 {got}，想要的是 {want} —— 没生效")


def note(on_note, msg: str):
    """记一条「不致命但用户该知道」的事，同时进日志和界面。

    ⚠ 回调抛异常不能影响主流程 —— 它只是提示，不是业务。
    """
    log.warning(msg)
    if on_note:
        try:
            on_note(msg)
        except Exception:
            pass
