"""价格面板配置的控件填写（老后台 manager.bilibili.co）。

⚠ 独立于 src/filler.py 和 src/wizard_filler.py。那两套分别服务 antd 弹窗和
  Formily 整页表单，这边是第三套 DOM，一行都不通用。

定位策略（详见 docs/价格面板配置-配置项抓取.md）：
  页面上的类名是 CSS-in-JS 编译出来的哈希（tw-bywk2o / tw-6hmssk / …），
  发版即失效，所以**一个都不用**。唯一稳定的结构是：

      <div>                    ← 字段块
        <label>字段名</label>   ← 必是父节点的第一个子元素
        <div>…控件…</div>
      </div>

  所以全部定位都是「按 label 文字找到字段块 → 在块内找控件」。
  「label 必须是父节点的第一个子元素」这一条很关键：页面上每个单选/复选项
  本身也是 <label>，不加这个条件会把选项当成字段。

⚠ 点击一律用 JS 的 el.click()。实测这套页面的 Vue 监听的是普通 click，
  JS 点得动；而且不受元素被别的浮层挡住的影响（这个后台的下拉浮层很爱挡东西）。
  唯一的例外是拖拽 —— 那个必须用真实鼠标事件，见 arrange()。
"""
from __future__ import annotations

import logging
import re

from .filler import FillError, split_multi

log = logging.getLogger(__name__)

# 找字段块：label 文字全等，且它是父节点的第一个子元素
JS_BLOCK = """
(name) => {
  const norm = s => (s || '').trim().split('\\n').join('');
  const labs = [...document.querySelectorAll('label')]
      .filter(l => norm(l.innerText) === name)
      .filter(l => l.parentElement && l.parentElement.firstElementChild === l);
  return labs.length ? labs[labs.length - 1].parentElement : null;
}
"""

# 当前真正展开着的那个下拉浮层。
#
# 这套 UI 把浮层 teleport 到 body 底下，而且**关掉的浮层不从 DOM 里删**，只是
# display:none —— 所以必须挑「现在显示着的那一个」。
#
# ⚠ 判据不能只看 li[role=option] 可不可见：页面里内联的 vue-multiselect
#   （省份 / sku选择 / 价格面板pid）的选项也带 role=option，一起扫的话
#   会把 239 个省份和 SKU 混进来，填「频次周期」时可能点到「安徽」上去。
#   真正的浮层长这样：body 的直接子 div，里面**就一个** <ul role="listbox">。
JS_POPUP = """
() => [...document.body.children].find(d =>
    d.children.length === 1 &&
    d.firstElementChild.tagName === 'UL' &&
    d.firstElementChild.getAttribute('role') === 'listbox' &&
    getComputedStyle(d).display !== 'none') || null
"""

JS_OPEN_OPTIONS = f"""
() => {{
  const box = ({JS_POPUP})();
  if (!box) return [];
  return [...box.querySelectorAll('li[role=option]')].map(li => li.textContent.trim());
}}
"""

JS_CLICK_OPEN_OPTION = f"""
(text) => {{
  const box = ({JS_POPUP})();
  if (!box) return false;
  const hit = [...box.querySelectorAll('li[role=option]')]
      .find(li => li.textContent.trim() === text);
  if (!hit) return false;
  hit.click();
  return true;
}}
"""


# 候选池的「先探一下」时长：拉得出来的一两秒就到，拉不出来等多久都没用。
PROBE_MS = 2500


def _norm(s: str) -> str:
    return (s or "").strip().replace("\n", "")


class PriceFiller:
    def __init__(self, page, timeout: int = 20000, on_note=None, on_empty=None):
        self.page = page
        self.timeout = timeout
        self._on_note = on_note
        # 候选池空了时的补救动作。定向那条链路上，pid / 组合价格的候选是跟着
        # 「价格面板panel_type」拉的，**重选一次它就会重拉**（运营实测）。
        # 这个动作只有 pp_runner 知道怎么做，所以由它注入。
        self._on_empty = on_empty

    def _note(self, msg: str):
        log.warning(msg)
        if self._on_note:
            try:
                self._on_note(msg)
            except Exception:
                pass

    # ---------------------------------------------------------------- 等待
    def wait_until(self, cond, timeout: int | None = None, step: int = 120) -> bool:
        """等到 cond() 为真就走，超时返回 False。

        ⚠ 不写死 sleep：内网快慢差很多，固定值不是白等就是不够。
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

    # ---------------------------------------------------------------- 定位
    def block(self, label: str, required: bool = True):
        """按 label 文字拿到字段块的 ElementHandle。找不到时 required=False 返回 None。"""
        h = self.page.evaluate_handle(JS_BLOCK, label)
        el = h.as_element()
        if el is None and required:
            raise FillError(f"页面上找不到字段「{label}」"
                            f"（是不是资源位还没选中？「其他设置」要选完资源位才出现）")
        return el

    def has(self, label: str) -> bool:
        return self.block(label, required=False) is not None

    @staticmethod
    def _click(el):
        el.evaluate("el => { el.scrollIntoView({block:'center'}); el.click(); }")

    # ---------------------------------------------------------------- 文本
    def text(self, label: str, value: str, index: int = 0):
        blk = self.block(label)
        inputs = blk.query_selector_all("input[type=text], input:not([type]), textarea")
        if len(inputs) <= index:
            raise FillError(f"「{label}」下没有第 {index + 1} 个输入框")
        inp = inputs[index]
        inp.evaluate("el => el.scrollIntoView({block:'center'})")
        inp.fill("")
        inp.fill(str(value))

    def date(self, label: str, value: str, index: int = 0):
        """flatpickr 的日期框。

        ⚠ flatpickr 是「改了 input.value 它自己不知道」的那种控件，光 fill 不够，
          必须把 input / change 事件补上，否则页面上看着填了、保存时提交的还是旧值。
          新建态这两个框本来就带着活动的起止时间，所以 Excel 留空 = 用默认，是安全的。
        """
        blk = self.block(label)
        inputs = blk.query_selector_all("input")
        if len(inputs) <= index:
            raise FillError(f"「{label}」下没有第 {index + 1} 个日期框")
        inp = inputs[index]
        inp.evaluate("""(el, v) => {
            el.scrollIntoView({block:'center'});
            el.value = v;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            if (el._flatpickr) el._flatpickr.setDate(v, true);
        }""", str(value))
        got = inp.evaluate("el => el.value")
        if _norm(got) != _norm(str(value)):
            raise FillError(f"「{label}」第 {index + 1} 个日期框没填进去："
                            f"想填 {value}，框里是 {got}")

    # ---------------------------------------------------------------- 单选
    def radio(self, label: str, value: str):
        blk = self.block(label)
        opts = blk.query_selector_all("label")
        texts = [_norm(o.inner_text()) for o in opts]
        idx = self._pick(texts, value)
        if idx is None:
            raise FillError(f"「{label}」下没有选项「{value}」。页面上有：{texts}")
        self._click(opts[idx])
        self.page.wait_for_timeout(150)

    @staticmethod
    def _pick(texts: list[str], value: str):
        """先全等，再前缀，最后包含。

        ⚠ 必须先全等：「投放流量池」的选项是「特殊最优池(慎重使用…)」，
          用包含匹配没问题；但「生效平台」里 Android 是 Android HD 的前缀，
          只用包含就会点错。
        """
        v = _norm(value)
        for match in (lambda t: t == v, lambda t: t.startswith(v), lambda t: v in t):
            for i, t in enumerate(texts):
                if match(t):
                    return i
        return None

    # ---------------------------------------------------------------- 复选组
    def checkbox(self, label: str, value: str):
        """多选组双向同步：该勾的勾上、不该勾的取消。

        ⚠ 必须双向：这个页面上「运营商」「收银台类型」默认就带着勾，
          只勾不取消的话，配「只投中国移动」会变成三家都投。
        """
        want = set(split_multi(str(value)))
        blk = self.block(label)
        state = blk.evaluate("""el => [...el.querySelectorAll('label')].map(l => {
            const box = l.querySelector('input[type=checkbox]');
            return box ? [(l.innerText || '').trim().split('\\n').join(''), box.checked] : null;
        }).filter(Boolean)""")
        if not state:
            raise FillError(f"「{label}」下没找到复选框")

        boxes = [l for l in blk.query_selector_all("label")
                 if l.query_selector("input[type=checkbox]")]
        seen = {t for t, _ in state}
        for i, (text, checked) in enumerate(state):
            if (text in want) != checked:
                self._click(boxes[i])
                self.page.wait_for_timeout(80)

        missing = want - seen
        if missing:
            if not (want & seen):
                raise FillError(f"「{label}」要勾的 {sorted(want)} 页面上一个都没有，"
                                f"只有：{sorted(seen)}")
            self._note(f"「{label}」页面上没有 {'、'.join(sorted(missing))}，已跳过；"
                       f"实际勾上的是 {'、'.join(sorted(want & seen))}")

    # ---------------------------------------------------------------- 下拉
    def _is_vue_multiselect(self, blk) -> bool:
        return bool(blk.query_selector(".multiselect"))

    def select(self, label: str, value: str, contains: bool = False, index=None,
               force: bool = False):
        """单选下拉。这个后台有两种下拉，块里有没有 .multiselect 就能分出来。

        index 是「这个字段块里第几个下拉」。用在「选中类型」上：
        面板个数=2个 时它底下有「面板1」「面板2」两个独立字段块，可以按 label 找；
        但**面板个数=1个 时那两个 label 根本不存在**，下拉直接挂在「选中类型」里。
        按下标数就两种情况都能定位。
        """
        blk = self.block(label)
        scope = blk if index is None else self._nth_select(blk, label, index)
        if self._is_vue_multiselect(scope):
            self._vue_pick(scope, label, value, contains)
        else:
            self._popup_pick(scope, label, value, contains, force)

    def _nth_select(self, blk, label: str, index: int):
        """字段块里第 index 个下拉控件（radio/checkbox 不算）。"""
        h = blk.evaluate_handle("""(el, i) => {
            const inputs = [...el.querySelectorAll('input')]
                .filter(x => x.type === 'text' || !x.type);
            const inp = inputs[i];
            if (!inp) return null;
            return inp.closest('.multiselect') || inp.parentElement;
        }""", index)
        got = h.as_element()
        if got is None:
            n = blk.evaluate("""el => [...el.querySelectorAll('input')]
                .filter(x => x.type === 'text' || !x.type).length""")
            raise FillError(f"「{label}」里只有 {n} 个下拉，取不到第 {index + 1} 个")
        return got

    def _open_input(self, inp, real_click: bool = True):
        """点开一个下拉。所有「teleport 到 body 的浮层」那一族都走这里。

        real_click=False 时只发合成 click。vue-multiselect（价格面板pid / sku选择 /
        省份）要用这个：它的 .multiselect__input 没打字时宽度是 0，真实鼠标点会
        因为「元素不可见」超时，退回合成点击又白等 3 秒；而且实测真实点击有时会
        把刚展开的面板又收回去（表现成「下拉里没有 134，能看到的：[]」）。

        ⚠ 先把上一个下拉的浮层收掉。浮层会盖住下面的字段，不收的话这一下点击
          被它吃掉 —— 而且更坑的是：**紧接着读到的选项是上一个下拉的**，
          于是报出「组合价格第 1 个下拉里没有 134，能看到的是 1223450133(充电券…)」
          这种驴唇不对马嘴的话。
        ⚠ 用真实鼠标点，不要只发合成 click。「商品ID」那种远程搜索框**点开之前
          是 readonly、点开才变可编辑**，合成 click 打不开这个状态，
          于是既没有选项也打不了字。
        """
        if self.page.evaluate(JS_OPEN_OPTIONS):
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(200)
        inp.evaluate("el => el.scrollIntoView({block:'center'})")
        if real_click:
            try:
                inp.click(timeout=3000)
            except Exception:
                inp.evaluate("el => { el.focus(); el.click(); }")
        else:
            inp.evaluate("el => { el.focus(); el.click(); }")
        return inp

    def _open(self, blk, real_click: bool = True):
        inp = blk.query_selector("input")
        if inp is None:
            raise FillError("这个下拉里没有可点的输入框")
        return self._open_input(inp, real_click)

    def _popup_pick(self, blk, label: str, value: str, contains: bool,
                    force: bool = False):
        """浮层 teleport 到 body 上的那种下拉（人群选组 / 频次周期 / 买赠商品…）。

        这一族里有两种，只能按 input 当场的可编辑状态区分，不能看 placeholder：

          纯下拉    input 恒 readonly（买赠商品、人群选组、频次周期…）。
                    往里面 fill 只会把浮层弄没了，然后报「下拉里没有 xxx，
                    现在能看到的：[]」—— 只能按文字挑。
          远程搜索  **点开之前 readonly、点开之后变可编辑**（商品ID）。
                    它带 remote 属性，选项要打字去后台搜，实测要等好几秒。
                    不打字的话只有默认那一两条，想要的那个永远不在里面。

        （要搜的另一种「价格面板pid」是 vue-multiselect，走 _vue_pick。）
        """
        # ⚠ 已经就是这个值就别再点了。这些下拉大多有默认值（人群那两个默认就是
        #   「不限」），白点一次不但没意义，还平白多开一次浮层去挡住后面的字段。
        # ⚠ force=True 时**必须真的重选一遍**：定向那条链路的补救动作就是
        #   「重选一次 panel_type 让候选重拉」，值本来就已经是对的 ——
        #   走这条短路的话补救等于没做（实测栽过一次：日志说重选了，其实没动）。
        if not force:
            cur = blk.evaluate("el => { const i = el.querySelector('input'); return i ? i.value : ''; }")
            if self._match([cur], value, contains) is not None:
                return

        inp = self._open(blk)
        typed = False
        if contains and not inp.evaluate("el => !!el.readOnly"):
            # ⚠ delay 只是为了让远程搜索的防抖认得出「在打字」，不用一个字 60ms
            inp.type(str(value), delay=15)
            typed = True

        # ⚠ 等的是「**匹配上的那条**出现」而不是「有选项了」：远程搜索回来之前，
        #   浮层里躺着的是默认那几条，只等「有选项」会当场拿错。
        self.wait_until(
            lambda: self._match(self.page.evaluate(JS_OPEN_OPTIONS), value, contains) is not None,
            timeout=self.timeout)

        texts = self.page.evaluate(JS_OPEN_OPTIONS)
        hit = self._match(texts, value, contains)
        if hit is None:
            how = "打字搜了一轮" if typed else "点开了"
            raise FillError(f"「{label}」{how}但没有「{value}」。现在能看到的：{texts[:12]}")
        if not self.page.evaluate(JS_CLICK_OPEN_OPTION, hit):
            raise FillError(f"「{label}」下拉里的「{hit}」点不动")
        # ⚠ 选完要确认浮层收了。它不收就一直盖着下面的字段，
        #   后面那个控件点不开、也读不到选项。
        if not self.wait_until(lambda: not self.page.evaluate(JS_OPEN_OPTIONS), timeout=1000):
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(200)

    def _vue_open(self, blk, label: str):
        """点开 vue-multiselect，并**确认它真的开了**。

        ⚠ 判据是 `.multiselect--active` 这个类，不是「等一会儿有没有选项」。
          没打开的时候它压根不渲染 li.multiselect__element —— 等多久都是 0 条，
          于是报成「下拉里没有 134，能看到的：[]」，看着像没这条数据，
          实际上是根本没展开（这个坑调了两轮时间才发现）。
        ⚠ 要点的是外面那层 .multiselect__tags，不是里面的 input：
          input 没打字时宽度是 0，点它经常不生效。
        """
        # ⚠ 先把别人的浮层收掉。浮层是 teleport 到 body 上的，会**盖在这个控件上面**，
        #   点下去被它吃掉 —— 控件压根没展开，也就不渲染任何选项，
        #   最后报成「下拉里没有 134，能看到的：[]」，看着像没这条数据。
        #   （实测：定向那条链路上，上面「价格面板panel_type」的浮层没收，
        #     下面的「价格面板pid」就一直出不来。）
        if self.page.evaluate(JS_OPEN_OPTIONS):
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(200)

        is_open = """el => {
            const m = el.querySelector('.multiselect');
            return !!m && m.className.includes('multiselect--active');
        }"""

        # ⚠ 已经开着的先关掉再重开。候选是跟着「选中的 SKU 卡片」现拉的，
        #   而**面板开着的时候换卡不会重拉** —— 直接读就会拿到上一张卡的池子
        #   （实测：配「超大连续包年」，读回来一列全是「连续包年」的 pid）。
        #   关一次再开，等于逼它按当前这张卡重新取一遍。
        if blk.evaluate(is_open):
            blk.evaluate("""el => {
                const ms = el.querySelector('.multiselect');
                const inp = ms && ms.querySelector('input');
                if (inp) inp.blur();
                document.body.click();
            }""")
            self.wait_until(lambda: not blk.evaluate(is_open), timeout=2000)

        for _ in range(3):
            if blk.evaluate(is_open):
                return
            blk.evaluate("""el => {
                const ms = el.querySelector('.multiselect');
                if (!ms) return;
                ms.scrollIntoView({block: 'center'});
                const tags = ms.querySelector('.multiselect__tags') || ms;
                tags.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
                tags.click();
                const inp = ms.querySelector('input');
                if (inp) inp.focus();
            }""")
            self.wait_until(lambda: blk.evaluate(is_open), timeout=2000)
        if not blk.evaluate(is_open):
            raise FillError(f"「{label}」的下拉点了 3 次都没展开")

    def _vue_pick(self, blk, label: str, value: str, contains: bool):
        """vue-multiselect（省份 / sku选择 / 价格面板pid）：选项就在块里面。"""
        self._vue_open(blk, label)
        inp = blk.query_selector("input")
        # ⚠ 打字之前**先等池子加载出来**。「价格面板pid」的候选是点中 SKU 卡片之后
        #   现拉的（panel/list?...&month=12，一个 SKU 一两百条），内网要好几秒。
        #   池子还空着就把「134」打进去，等于在空列表上过滤 —— 一条不剩，
        #   报出来是「下拉里没有 134，能看到的：[]」，看着像这个 pid 不存在，
        #   实际上它就在池子里（实测 190 条里正有 134）。这个坑排了三轮才定位。
        # ⚠ 先只等一小会儿（PROBE_MS）。候选拉得出来的话一两秒就到了；
        #   拉不出来就是**页面把池子丢了**，那种情况等多久都没用 ——
        #   要立刻去做补救动作（重选一次 panel_type）让它重拉。
        #   原来这里一上来就按 self.timeout 等满，12 个卡种光干等就好几分钟。
        if not self.wait_until(lambda: bool(self._vue_options(blk)), timeout=PROBE_MS):
            if self._recover(label):
                self._vue_open(blk, label)
                self.wait_until(lambda: bool(self._vue_options(blk)), timeout=self.timeout)
            elif not self.wait_until(lambda: bool(self._vue_options(blk)),
                                     timeout=self.timeout - PROBE_MS):
                self._note(f"「{label}」的候选一直没加载出来，先按现在能看到的找")
        if contains:
            # ⚠ 有的搜索框是 readonly（打字过滤这条路不存在），往里 fill 只会把
            #   浮层弄没、然后报「下拉里没有 xxx，能看到的：[]」。判据是 readOnly，
            #   不是 placeholder —— 好几个 ph 写着「请输入…」的其实都是纯下拉。
            if inp.evaluate("el => !!el.readOnly"):
                self._note(f"「{label}」的搜索框是只读的，改成直接在选项里找")
            else:
                before = self._vue_options(blk)
                inp.fill(str(value))
                # 过滤是本地的，列表一变就往下走；别写死 400ms
                self.wait_until(lambda: self._vue_options(blk) != before, timeout=1500)
        # ⚠ 等的是「**匹配上的那条**出现」，不是「有选项了」：
        #   「价格面板pid」打完字是去后台重搜的，内网实测要好几秒；
        #   而且重搜期间列表里躺的还是上一轮的旧选项 —— 只等「有选项」会拿到旧的，
        #   等超时又会把「其实马上就到」误判成「没有这一条」（实测卡在这儿过）。
        #   超时用 self.timeout，别再写死 5 秒。
        self.wait_until(
            lambda: self._match(self._vue_options(blk), value, contains) is not None,
            timeout=self.timeout)
        texts = self._vue_options(blk)
        hit = self._match(texts, value, contains)
        if hit is None:
            raise FillError(f"「{label}」的下拉里没有「{value}」。"
                            f"现在能看到的前几条：{texts[:8]}")
        # ⚠ 已经选中的项**不能再点**：vue-multiselect 的多选里，点一下是取消选中。
        #   上层永远只挑「还没选上的」，这里再兜一层。
        #
        # ⚠ 手势不统一，只能一种一种试：同一个页面上
        #     sku选择 / 省份 的选项  认 mousedown（@mousedown.prevent）
        #     价格面板pid 的选项     认 click
        #     标签上的 ×             认 mousedown
        #   两种一起发是不行的 —— 都认的话就等于点两下，选上又取消。
        #   所以先发一种，看状态变没变，没变再发另一种。
        before = self._vue_state(blk)
        ok = blk.evaluate("""(el, text) => {
            const li = [...el.querySelectorAll('li.multiselect__element')]
                .find(x => (x.innerText || '').trim() === text);
            if (!li) return 'missing';
            const opt = li.querySelector('span.multiselect__option') || li;
            if (opt.className.includes('--selected')) return 'already';
            opt.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
            opt.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true}));
            return 'ok';
        }""", hit)
        if ok == "ok":
            # ⚠ 别用固定 sleep 判「点动了没有」：选上之后那个标签是等后端回来才渲染的，
            #   内网慢的时候 250ms 根本不够，会把「点动了」误判成「没点动」，
            #   于是又补一次 click —— 多选里点第二下正好是**取消选中**，
            #   最后报「mousedown 和 click 都试过了」。所以要等状态真的变。
            changed = self.wait_until(lambda: self._vue_state(blk) != before, timeout=6000)
            if not changed:
                blk.evaluate("""(el, text) => {
                    const li = [...el.querySelectorAll('li.multiselect__element')]
                        .find(x => (x.innerText || '').trim() === text);
                    if (li) (li.querySelector('span.multiselect__option') || li).click();
                }""", hit)
                if not self.wait_until(lambda: self._vue_state(blk) != before, timeout=6000):
                    raise FillError(f"「{label}」下拉里点了「{hit}」但没选上"
                                    f"（mousedown 和 click 都试过了）")
        if ok == "missing":
            raise FillError(f"「{label}」下拉里的「{hit}」点不动")
        self.page.wait_for_timeout(200)

    @staticmethod
    def _vue_state(blk):
        """这个下拉当前选了什么。用来判断「刚才那下点动了没有」。"""
        return blk.evaluate("""el => [
            [...el.querySelectorAll('.multiselect__tag')].map(t => t.textContent.trim()).join('|'),
            (el.querySelector('.multiselect__single') || {}).textContent || '',
            el.querySelectorAll('.multiselect__option--selected').length,
        ]""")

    @staticmethod
    def _vue_options(blk) -> list[str]:
        return blk.evaluate("""el => [...el.querySelectorAll('li.multiselect__element')]
            .map(li => (li.innerText || '').trim())
            .filter(t => t && !t.includes('没有找到选项') && !t.includes('选项列表为空'))""")

    @staticmethod
    def _match(texts: list[str], value: str, contains: bool):
        v = _norm(str(value))
        for t in texts:
            if _norm(t) == v:
                return t
        if not contains:
            return None
        # pid / 商品ID：页面显示成「11439(normal,ipad,连续包年,人群包,148.00元)」，
        # 填的是 11439 —— 必须按「ID 后面紧跟左括号」认，
        # 否则 1143 会命中 11439，114 会命中一大片。
        for t in texts:
            if _norm(t).startswith(v + "("):
                return t
        for t in texts:
            if v in _norm(t):
                return t
        return None

    def search_select(self, label: str, value: str):
        self.select(label, value, contains=True)

    # ---------------------------------------------------------------- 多选
    def multiselect(self, label: str, value: str):
        """vue-multiselect 的多选（省份 / sku选择）：双向同步到 value 这个清单。"""
        want = split_multi(str(value))
        blk = self.block(label)
        for _ in range(60):
            cur = self._tags(blk)
            extra = [t for t in cur if t not in want]
            if extra:
                self._remove_tag(blk, extra[0])
                self.page.wait_for_timeout(200)
                continue
            missing = [t for t in want if t not in cur]
            if not missing:
                break
            self._vue_pick(blk, label, missing[0], contains=False)
            self.page.wait_for_timeout(200)
        else:
            raise FillError(f"「{label}」反复调整了 60 次还没对上，别再转了")

        cur = self._tags(blk)
        if set(cur) != set(want):
            raise FillError(f"「{label}」最后是 {cur}，想要的是 {want}")

    def multi_search_select(self, label: str, values: list, expect: str = ""):
        """按 ID 同步一个**多选**的可搜索下拉（价格面板pid 就是这种）。

        ⚠ 必须双向同步：多的删掉、少的补上。当单选写的话每跑一次就往里加一个，
          越攒越多还看不出来。
        ⚠ 选项文字是「140(normal,安卓,连续包年,所有用户,…)」，我们手里只有 140，
          所以比对的是**左括号前面那一截**，不能用全等。

        做法：**开一次、读一次全量、把要的逐个点掉**。逐个打字搜的话一个 pid 约
        5 秒，一个单元 12 个卡种 × 9 个 pid 就是九分钟；不打字池子本来就是全的。

        ⚠ 别拿「选项文字里有没有这个卡种名」当串卡的判据 —— **超大档位的 pid，
          描述里写的还是「连续包年」**（文档 §2.5 记过）。按那个判会把正常的
          全判成串卡。真要判就看「想要的 pid 在不在池子里」。
        ⚠ 每选一次控件会重渲染，所以不能攥着 li 的句柄：每次都在 evaluate 里
          按文字重新找。
        """
        want = [str(v).strip() for v in (values or []) if str(v).strip()]
        blk = self.block(label)
        head = lambda t: _norm(t).split("(")[0].strip()
        cur = lambda: [head(t) for t in self._tags(blk)]

        # ---- 1. 多出来的先删掉 ----
        for _ in range(len(want) + 20):
            tags = self._tags(blk)
            extra = [t for t in tags if head(t) not in want]
            if not extra:
                break
            n = len(tags)
            self._remove_tag(blk, extra[0])
            self.wait_until(lambda n=n: len(self._tags(blk)) < n, timeout=3000)

        if not [v for v in want if v not in cur()]:
            return

        # ---- 2. 开一次读全量；想要的没在池子里就补救一次再读 ----
        # ⚠ 轮询判据只看「池子非空」，别在每一轮里给 9 个 pid 各扫一遍 190 条候选 ——
        #   那是纯浪费。匹配放到池子到位之后做一次；真缺了再补救、再匹配一次。
        self._vue_open(blk, label)
        if not self.wait_until(lambda: bool(self._vue_options(blk)), timeout=PROBE_MS):
            if self._recover(label):
                self._vue_open(blk, label)
            self.wait_until(lambda: bool(self._vue_options(blk)), timeout=self.timeout)

        def pick_all():
            opts = self._vue_options(blk)
            got = {v: self._match(opts, v, contains=True) for v in want}
            return opts, got

        options, picked = pick_all()
        if any(h is None for h in picked.values()) and self._recover(label):
            self._vue_open(blk, label)
            self.wait_until(lambda: bool(self._vue_options(blk)), timeout=self.timeout)
            options, picked = pick_all()
        for v, hit in picked.items():
            if hit is None:
                raise FillError(f"「{label}」的候选里没有「{v}」。候选共 {len(options)} 条，"
                                f"前几条：{options[:6]}")

        # ---- 3. 一口气点完，最后统一核对，只补漏的 ----
        for _ in range(3):
            todo = [v for v in want if v not in cur()]
            if not todo:
                break
            for v in todo:
                self._click_vue_option(blk, picked[v])
                self.page.wait_for_timeout(50)
            self.wait_until(lambda: not [v for v in want if v not in cur()], timeout=4000)

        for v in [x for x in want if x not in cur()]:
            n = len(self._tags(blk))
            self._click_vue_option(blk, picked[v], plain=True)
            if not self.wait_until(lambda n=n: len(self._tags(blk)) > n, timeout=4000):
                raise FillError(f"「{label}」下拉里点了「{picked[v]}」但没选上"
                                f"（mousedown 和 click 都试过了）")

        got = cur()
        if set(got) != set(want):
            raise FillError(f"「{label}」最后是 {got}，想要的是 {want}")

    def _poke_search(self, blk):
        """空搜一下，逼 vue-multiselect 按当前选中的卡片重新取候选。"""
        inp = blk.query_selector("input")
        if inp is None or inp.evaluate("el => !!el.readOnly"):
            return
        before = self._vue_options(blk)
        inp.fill("0")
        self.wait_until(lambda: self._vue_options(blk) != before, timeout=1500)
        inp.fill("")
        self.wait_until(lambda: len(self._vue_options(blk)) > 1, timeout=self.timeout)

    @staticmethod
    def _click_vue_option(blk, text: str, plain: bool = False):
        """点 vue-multiselect 的一个选项。

        ⚠ ElementHandle.evaluate 的回调第一个参数是**元素**，第二个才是传进来的值。
        ⚠ 手势不统一：sku选择 / 省份 认 mousedown，价格面板pid 认 click。
          两种一起发等于点两下 —— 多选里第二下是取消选中，只能一种一种试。
        ⚠ 已经选中的不要再点，点了就是取消。
        """
        blk.evaluate(
            """(el, [text, plain]) => {
                const li = [...el.querySelectorAll('li.multiselect__element')]
                    .find(x => (x.innerText || '').trim() === text);
                if (!li) return;
                const opt = li.querySelector('span.multiselect__option') || li;
                if (opt.className.includes('--selected')) return;
                opt.scrollIntoView({block: 'nearest'});
                if (plain) { opt.click(); return; }
                opt.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
                opt.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true}));
            }""", [text, plain])

    @staticmethod
    def _tags(blk) -> list[str]:
        # textContent 而不是 innerText：标签多到换行、横向溢出时 innerText 会读空
        return blk.evaluate("""el => [...el.querySelectorAll('.multiselect__tag')]
            .map(t => (t.textContent || '').trim())""")

    @staticmethod
    def _remove_tag(blk, text: str):
        # ⚠ 同上：标签上那个 × 绑的是 mousedown，click 发过去没人接。
        blk.evaluate("""(el, text) => {
            const tag = [...el.querySelectorAll('.multiselect__tag')]
                .find(t => (t.textContent || '').trim() === text);
            if (!tag) return;
            const icon = tag.querySelector('.multiselect__tag-icon') || tag;
            icon.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
            icon.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true}));
        }""", text)

    # ================================================================ 资源位
    def pick_position(self, name: str):
        """在资源位表格里选中这一行。

        ⚠ 必须第一列全等：表格里 164 行全在 DOM 里（各 tab 的都在），
          「收银台价格面板」用包含匹配会同时命中「加更礼&结局点映收银台价格面板」。

        ⚠ 要单独等这张表。「单元名称」输入框比它先出来 ——
          表格是后台另拉一次接口才渲染的，拿输入框当「页面好了」的判据，
          十有八九会撞上「资源位表格里没有 xxx 这一行」。
        """
        if not self.wait_until(lambda: self._has_row(name), timeout=self.timeout):
            n = self.page.locator("tr").count()
            raise FillError(f"等不到资源位表格里的「{name}」这一行"
                            f"（表格现在有 {n} 行；是不是还没加载完，或者名字变了？）")

        ok = self.page.evaluate("""(name) => {
            const row = [...document.querySelectorAll('tr')].find(r => {
                const td = r.querySelector('td');
                return td && (td.innerText || '').trim() === name;
            });
            if (!row) return false;
            row.scrollIntoView({block: 'center'});
            row.click();
            return true;
        }""", name)
        if not ok:
            raise FillError(f"资源位表格里没有「{name}」这一行")

    def _has_row(self, name: str) -> bool:
        return bool(self.page.evaluate("""(name) => [...document.querySelectorAll('tr')]
            .some(r => {
                const td = r.querySelector('td');
                return td && (td.innerText || '').trim() === name;
            })""", name))

    # ================================================================ 套餐排列
    # ⚠ 卡片名一律读 .panel-type-text 的 textContent，不读 innerText。
    #   innerText 依赖布局，卡片横向溢出、正在重渲染时都可能读回空串。
    JS_ORDER = """
    () => {
      const ul = document.querySelector('.panel-sort-content');
      if (!ul) return null;
      return [...ul.children].map(c => c.className.includes('panel-division')
          ? '||' : (c.querySelector('.panel-type-text') || c).textContent.trim());
    }
    """

    def panel_order(self, strict: bool = True) -> list[str]:
        """套餐排列当前的样子。分隔线是 '||'，它前面是面板1、后面是面板2。

        ⚠ 「容器还没渲染出来」和「一个卡片都没有」必须分开：
          以前两种都返回 []，于是 Vue 重渲染的那一瞬间读到 []，
          上层就以为「一个 SKU 都没选」，回头又去点一遍下拉 ——
          而 vue-multiselect 点已选中的项是**取消选中**，于是勾上、取消、勾上……
          80 次循环全耗在这个来回上（实测就是这么卡住的）。
        """
        got = self.page.evaluate(self.JS_ORDER)
        if got is None:
            if not strict:
                return []
            if not self.wait_until(
                    lambda: self.page.evaluate(self.JS_ORDER) is not None, timeout=5000):
                raise FillError("套餐排列还没渲染出来（资源位选中了吗？）")
            got = self.page.evaluate(self.JS_ORDER)
        return got

    def sku_chips(self) -> list[str]:
        return [x for x in self.panel_order() if x != "||"]

    def set_skus(self, skus: list[str]):
        """把「sku选择」调成正好这一批。

        ⚠ 增删都走「sku选择」那个多选框本身（标签上的 ×、下拉里的选项），
          不去点卡片上的 ×。两条路效果一样，但多选框的标签和它的选项在同一个
          字段块里、状态永远自洽；卡片在另一个容器里，重渲染时会短暂读不到，
          拿它当依据就会误判成「没选中」。卡片只用来做最后的核对。
        """
        want = list(dict.fromkeys(skus))
        self.multiselect("sku选择", ",".join(want))
        if not self.wait_until(lambda: set(self.sku_chips()) == set(want), timeout=8000):
            raise FillError(f"sku选择 已经是 {want}，但套餐排列里是 {self.sku_chips()}，"
                            f"两边对不上")

    def close_popups(self):
        """把展开着的下拉都收起来。

        ⚠ 拖拽走的是**真实鼠标事件**，被浮层挡住就整个失效，而且一声不吭 ——
          表现成「拖了 3 次纹丝不动，最后报没拖到位」。
          sku选择 是 vue-multiselect，选完之后它的选项面板还开着，正好盖在
          套餐排列上面，所以拖之前必须先收。（别的操作都用 JS 点，不受影响，
          就这一处是真鼠标。）
        """
        self.page.evaluate("""() => {
            if (document.activeElement && document.activeElement.blur)
                document.activeElement.blur();
            const h = [...document.querySelectorAll('h3')]
                .find(x => x.innerText.trim() === '其他设置');
            if (h) h.click();
        }""")
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(300)

    def _settle(self, quiet_ms: int = 700):
        """等套餐排列连续 quiet_ms 没变化，认为渲染完了。"""
        last, stable = None, 0
        for _ in range(40):
            now = self.panel_order()
            if now == last:
                stable += 150
                if stable >= quiet_ms:
                    return
            else:
                last, stable = now, 0
            self.page.wait_for_timeout(150)

    def arrange(self, panels: list[list[str]]):
        """把卡片拖成 [面板1…, 分隔线, 面板2…, 分隔线, 隐藏sku面板…] 这个顺序。

        这是整张表单里唯一没法「填」、只能拖的东西。实测出来的规则：

          1. **往左拖是准的**：把 A 拖到第 i 个元素上（i < A 现在的下标）
             = A 落到第 i 位，原来那个往右让。每次都一样。
          2. **分隔线自己 draggable=false，拖不动**，只能当落点。
          3. **往右拖不准**。把卡片拖到分隔线上，它确实会进下一段，但落在
             下一段的哪个位置**不固定** —— 实测同一批拖拽里，有的落在段首、
             有的直接甩到末尾（见 2026-08-26 的日志）。

        所以分两步走，只在「顺序」这一步用往左拖：

          A. 先只管**分段**：哪张卡片在哪一段。要往后挪就拖到分隔线上（往右，
             落点不管，只要跨过去就行）；要往前挪就拖到目标段的段首（往左，准）。
             每拖一次都重新读一遍页面，没跨过去就再来，最多几轮。
          B. 段内成员对了之后再排**顺序**：每一段从左到右摆，要摆到第 i 位的
             那张卡此刻一定在第 i 位或更右边（左边的都已经摆好了），所以永远是
             往左拖 —— 准，而且不会跨过分隔线，不会打乱别的段。

        ⚠ 别退回「按下标从左到右一次摆完」：那样卡片都到位了、分隔线还堆在原地
          （实测：想要 [连,|,年,|,月]，摆出来 [连,年,|,|,月]）。分隔线的位置
          不是拖出来的，是被卡片挤出来的。

        ⚠ 分隔线条数由页面上的「面板个数」决定：N 段 = N-1 条。所以调用前
          「面板个数」必须已经填好。
        """
        self.close_popups()
        items = lambda: self.page.locator(".panel-sort-content > div")
        # ⚠ 等列表稳下来再动手：改完 sku选择 之后 Vue 还在重渲染，
        #   这时候拖等于对着旧节点拖，一声不吭地不生效。
        self._settle()
        cur = self.panel_order()

        segs = [list(x) for x in panels]
        dividers = cur.count("||")
        if len(segs) > dividers + 1:
            extra = [x for x in segs[dividers + 1:] if x]
            if extra:
                raise FillError(
                    f"页面上只有 {dividers + 1} 段（分隔线 {dividers} 条），"
                    f"但还要再排 {extra} —— 「面板个数」和面板清单对不上")
            segs = segs[:dividers + 1]
        while len(segs) < dividers + 1:
            segs.append([])

        desired: list[str] = []
        for i, seg in enumerate(segs):
            if i:
                desired.append("||")
            desired += seg
        if len(cur) != len(desired):
            raise FillError(f"套餐排列对不上：页面上是 {cur}，要排成 {desired}。"
                            f"（sku选择 是不是没同步好？）")

        def div_index(order, k):
            """第 k 条分隔线（从 1 数）在当前序列里的下标。"""
            seen = 0
            for idx, x in enumerate(order):
                if x == "||":
                    seen += 1
                    if seen == k:
                        return idx
            raise FillError(f"套餐排列里没有第 {k} 条分隔线（页面上只有 {order.count('||')} 条）")

        def drag(src_name, dst_idx):
            cur_now = self.panel_order()
            if src_name not in cur_now:
                raise FillError(f"套餐排列里没有「{src_name}」，sku选择 是不是没选上？")
            src_idx = cur_now.index(src_name)
            items().nth(src_idx).drag_to(items().nth(dst_idx))
            self.page.wait_for_timeout(450)
            # ⚠ 拖拽是整页唯一用真实鼠标事件的地方，失效时一声不吭。把每一步的
            #   前后序列记进日志 —— 排这类问题时没有它就只能靠猜。
            log.info("拖「%s」(%d) → 落点 %d：%s  →  %s",
                     src_name, src_idx, dst_idx, cur_now, self.panel_order())

        def split(order):
            """当前序列切成几段（不含分隔线）。"""
            out, cur_seg = [], []
            for x in order:
                if x == "||":
                    out.append(cur_seg)
                    cur_seg = []
                else:
                    cur_seg.append(x)
            out.append(cur_seg)
            return out

        def seg_of(now_segs, name):
            for si, seg in enumerate(now_segs):
                if name in seg:
                    return si
            return -1

        def seg_start(order, si):
            """第 si 段（0 起）的第一个下标。"""
            return 0 if si == 0 else div_index(order, si) + 1

        want_seg = {name: si for si, seg in enumerate(segs) for name in seg}

        # ---- A. 先只管分段 ----
        for _ in range(len(want_seg) * 2 + 6):
            order = self.panel_order()
            now_segs = split(order)
            bad = [(n, w) for n, w in want_seg.items() if seg_of(now_segs, n) != w]
            if not bad:
                break
            name, want = bad[0]
            have = seg_of(now_segs, name)
            if have < 0:
                raise FillError(f"套餐排列里没有「{name}」，sku选择 是不是没选上？")
            if want < have:
                drag(name, seg_start(order, want))       # 往前挪：往左拖，准
            else:
                drag(name, div_index(order, have + 1))   # 往后挪：只能拖到分隔线上
        else:
            now_segs = split(self.panel_order())
            bad = [f"「{n}」应该在第 {w + 1} 段，现在在第 {seg_of(now_segs, n) + 1} 段"
                   for n, w in want_seg.items() if seg_of(now_segs, n) != w]
            raise FillError("套餐排列的分段没弄对：" + "；".join(bad)
                            + "。（页面上手动拖一下再点提交也行）")

        # ---- B. 段内排顺序：一律往左拖 ----
        for si, seg in enumerate(segs):
            for pos, name in enumerate(seg):
                for _ in range(3):
                    order = self.panel_order()
                    if split(order)[si][pos] == name:
                        break
                    drag(name, seg_start(order, si) + pos)
                else:
                    self._note(f"「{name}」拖了 3 次没到第 {si + 1} 段第 {pos + 1} 位，"
                               f"接着往下走，最后会整体核对")

        got = self.panel_order()
        if got != desired:
            raise FillError(f"套餐排列没拖到位。想要：{desired}；现在：{got}。"
                            f"（页面上手动拖一下再点提交也行）")

    def active_chip(self) -> str:
        """当前选中的是哪张卡片。下面那几个搭售字段就是它的。"""
        return self.page.evaluate("""() => {
            const it = document.querySelector('.panel-type-item-active');
            return it ? (it.innerText || '').trim().split('\\n')[0] : '';
        }""")

    def click_chip(self, sku: str):
        """点中某个 SKU 的卡片 —— 下面那几个搭售字段是**这张卡片的**配置。

        ⚠ 必须验证它真的选中了，不能「点了就算数」。刚拖完套餐排列时 Vue 还在
          重渲染，这一下经常落空；而落空之后页面上**什么异常都没有**，只是下面
          那几个字段仍旧属于上一张卡片 —— 于是 pid 填到别的 SKU 头上，
          报错还长得像「下拉里没有 134」（实测：配连续包年，pid 下拉里
          出来的全是季度大会员的）。
        """
        last = ""
        for _ in range(4):
            ok = self.page.evaluate("""(sku) => {
                const item = [...document.querySelectorAll('.panel-type-item')]
                    .find(i => (i.innerText || '').trim().split('\\n')[0] === sku);
                if (!item) return false;
                item.scrollIntoView({block: 'center'});
                (item.querySelector('.panel-type-text') || item).click();
                return true;
            }""", sku)
            if not ok:
                raise FillError(f"套餐排列里没有「{sku}」这张卡片")
            if self.wait_until(lambda: self.active_chip() == sku, timeout=3000):
                self.page.wait_for_timeout(250)     # 让下面那几个字段渲染完
                return
            last = self.active_chip()
            self._settle()
        raise FillError(f"点了 4 次「{sku}」的卡片都没选中（现在选中的是"
                        f"「{last or '一张都没有'}」），后面的搭售会填到别的 SKU 上")

    # ================================================================ 组合价格
    def set_combine(self, pairs: list[list[str]]):
        """0元购的「组合价格」：点 N 次「增加组合价格」，再逐行填两个下拉。"""
        # ⚠ 「搭售类型」刚选完，0元购那一段还在渲染 —— 不等一下就点加号，
        #   按钮在、点了却不加行（实测「要 1 行，点了加号只出来 0 行」）。
        self._settle()
        blk = self.block("组合价格")

        # 点一次验一次，没加上就再点。别「按缺几行点几下」——那样一次没生效就永远差着。
        for _ in range(len(pairs) * 3 + 3):
            have = self._combine_rows(blk)
            if have >= len(pairs):
                break
            blk.evaluate("""el => {
                const btn = el.querySelector('button.add-combine-btn');
                if (btn) { btn.scrollIntoView({block:'center'}); btn.click(); }
            }""")
            self.wait_until(lambda n=have: self._combine_rows(blk) > n, timeout=3000)

        rows = self._combine_rows(blk)
        if rows < len(pairs):
            raise FillError(f"「组合价格」要 {len(pairs)} 行，反复点加号也只出来 {rows} 行")

        for i, (pid, goods) in enumerate(pairs):
            self._combine_fill(blk, i * 2, pid)
            self._combine_fill(blk, i * 2 + 1, goods)

    @staticmethod
    def _combine_rows(blk) -> int:
        n = blk.evaluate("el => el.querySelectorAll('.combine-select').length")
        return n // 2

    def _combine_fill(self, blk, index: int, value: str):
        """组合价格那两列没有独立 label，只能按下标认。

        readonly 的当下拉挑（和「买赠商品」同族），可输入的就直接打字。
        """
        sels = blk.query_selector_all(".combine-select")
        if len(sels) <= index:
            raise FillError(f"「组合价格」里没有第 {index + 1} 个下拉")
        inp = sels[index].query_selector("input")
        self._open_input(inp)

        if not inp.evaluate("el => el.readOnly"):
            inp.type(str(value), delay=15)

        # ⚠ 等「匹配上的那条」出现，不是「有选项」：这两格也是远程搜的，
        #   回来之前浮层里躺的还是上一轮的旧选项。
        if not self.wait_until(
                lambda: self._match(self.page.evaluate(JS_OPEN_OPTIONS), value, True) is not None,
                timeout=self.timeout):
            if self._recover(f"组合价格第 {index + 1} 个下拉"):
                self._open_input(inp)
                if not inp.evaluate("el => el.readOnly"):
                    inp.type(str(value), delay=60)
                self.wait_until(
                    lambda: self._match(self.page.evaluate(JS_OPEN_OPTIONS), value, True) is not None,
                    timeout=self.timeout)
        texts = self.page.evaluate(JS_OPEN_OPTIONS)
        hit = self._match(texts, value, contains=True)
        if hit is None:
            raise FillError(f"「组合价格」第 {index + 1} 个下拉里没有「{value}」。"
                            f"能看到的前几条：{texts[:6]}")
        self.page.evaluate(JS_CLICK_OPEN_OPTION, hit)
        self.page.wait_for_timeout(250)

    def _recover(self, label: str) -> bool:
        """候选池空了，试一次补救动作。成功发起过就返回 True。

        ⚠ 定向那条链路上，「价格面板pid」和「组合价格」的候选是跟着
          「价格面板panel_type」拉回来的，偶尔会拉成空。**重选一次 panel_type
          它就会重拉**（运营实测，也是这个坑唯一的解法）。
          补救动作由 pp_runner 注入，这里只负责在该用的时候用一次。
        """
        if not self._on_empty:
            return False
        # 一张卡片上补救一次就够了：pid 拉出来之后，同一张卡的组合价格也跟着有了。
        if getattr(self, "_recovered", False):
            return False
        self._recovered = True
        self._note(f"「{label}」候选是空的，重选一次 panel_type 让它重新拉")
        try:
            self._on_empty()
        except Exception as e:
            self._note(f"重选 panel_type 没成功：{e}")
            return False
        self._settle()
        return True

    # ================================================================ 二次确认
    def confirm_modal(self) -> str:
        """点掉保存时弹出来的二次确认（「优先级重复」那种）。返回弹窗标题，没弹就返回空。

        ⚠ 判据就是「页面上有没有可见的『确定』按钮」——页面自己的底栏是
          「保存并下一步 / 保存返回 / 取消」，没有确定，所以不会误伤。
        ⚠ 这段 JS 里**一个反斜杠转义都不要写**（换行、空白那一类），很容易在某一层
          （Python 字符串 / 生成脚本 / heredoc）被提前解释成真的换行或制表符，
          塞进 JS 源码就是语法错误，而报出来只有一句「Invalid or unexpected token」，
          完全看不出是转义的锅 —— 这个坑连着栽了两次。要取首行就用 charCode。
        """
        return self.page.evaluate("""() => {
            const NL = String.fromCharCode(10);
            const vis = e => { const r = e.getBoundingClientRect();
                               return r.width > 0 && r.height > 0; };
            const flat = t => (t || '').split(NL).join('').split(' ').join('');
            const btns = [...document.querySelectorAll('button')]
                .filter(b => vis(b) && flat(b.innerText) === '确定');
            if (!btns.length) return '';
            const b = btns[btns.length - 1];      // 后挂上去的浮层排在后面
            let title = '';
            for (let p = b; p; p = p.parentElement) {
                const t = (p.innerText || '').trim().split(NL)[0];
                if (t && t.length <= 20) title = t;
                if (p.getBoundingClientRect().height > 200) break;
            }
            b.scrollIntoView({block: 'center'});
            b.click();
            return title || '二次确认';
        }""")

    # ================================================================ 页面报错
    def form_errors(self) -> str:
        """保存被拒时，把页面上标红的话捞出来 —— 不然只能说一句「看截图」。"""
        msgs = self.page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('[class*="error"], [class*="invalid"]').forEach(e => {
                const t = (e.innerText || '').trim();
                if (t && t.length < 60) out.push(t);
            });
            return [...new Set(out)].slice(0, 8);
        }""")
        return f"页面报错：{msgs}" if msgs else "页面上没找到明确的错误提示，看截图。"


# yaml 里 type 到方法的对应。加控件类型 = 这里加一行 + 上面加一个方法。
HANDLERS = {
    "pp_text": lambda f, w, label, v: w.text(label, v, f.get("index", 0)),
    # ⚠ 「展示不超过」和它的字段块共用一个 label（都叫「频次限制」），块里第 1 个
    #   输入框是「每日/每周」那个下拉，数字在第 2 个 —— 所以 yaml 里给它写了 index: 1。
    "pp_number": lambda f, w, label, v: w.text(label, v, f.get("index", 0)),
    "pp_date": lambda f, w, label, v: w.date(label, v, f.get("index", 0)),
    "pp_radio": lambda f, w, label, v: w.radio(label, v),
    "pp_checkbox": lambda f, w, label, v: w.checkbox(label, v),
    "pp_select": lambda f, w, label, v: w.select(label, v, index=f.get("index")),
    "pp_multiselect": lambda f, w, label, v: w.multiselect(label, v),
    "pp_select_search": lambda f, w, label, v: w.search_select(label, v),
}


def apply_field(filler: PriceFiller, f: dict, value: str):
    """按字段定义填一个值。value 已经保证非空。"""
    label = f.get("label") or f["name"]
    fn = HANDLERS.get(f.get("type"))
    if fn is None:
        raise FillError(f"字段「{f['name']}」的 type={f.get('type')} 不认识")
    try:
        fn(f, filler, label, value)
    except FillError:
        raise
    except Exception as e:
        raise FillError(f"填「{f['name']}」失败：{e}") from e
