"""按 form profile 把一条记录填进表单。

新增控件类型：写一个 _fill_xxx 方法，登记进 HANDLERS。

⚠ antd 允许在构建期改 class 前缀。默认是 ant-，但 DMP（marin/full-ogv-data）
  这套前端打包成 full_ogv_data_antd-。所有 antd 相关的 selector 都要走
  self._c()/self._sel()，不能再写死 "ant-"，否则换个系统就全部失配。
  前缀在表单配置里用 antd_prefix 指定。
"""
import logging
import re

log = logging.getLogger(__name__)

DEFAULT_ANTD_PREFIX = "ant"


def split_multi(value: str) -> list[str]:
    """'iPhone,Android' / 'iPhone，Android' / 'iPhone、Android' → ['iPhone','Android']"""
    return [x.strip() for x in re.split(r"[,，、;；]", value) if x.strip()]


class FillError(Exception):
    pass


class Filler:
    def __init__(self, page, form_cfg: dict):
        self.page = page
        self.cfg = form_cfg
        self.waits = {w["after"]: w["wait_for"] for w in (form_cfg.get("waits") or [])}
        self.prefix = form_cfg.get("antd_prefix") or DEFAULT_ANTD_PREFIX

    # ---- antd class 前缀 ----
    def _c(self, name: str) -> str:
        """antd 的 class 名，如 _c('select-open') → 'ant-select-open'。"""
        return f"{self.prefix}-{name}"

    def _sel(self, name: str) -> str:
        return f".{self._c(name)}"

    @property
    def _dropdown(self) -> str:
        """下拉选项渲染在 body 下的浮层里，不在表单 DOM 内。"""
        return f"{self._sel('select-dropdown')}:not({self._sel('select-dropdown-hidden')})"

    @property
    def _option(self) -> str:
        return self._sel("select-item-option")

    @property
    def _select_root(self) -> str:
        """⚠ 必须精确匹配 class 令牌 ant-select。
        用 contains(@class,'ant-select') 会匹配到组件内部的 ant-select-content 等节点，
        圈错范围后读不到 .ant-select-selection-item，表现为"点了但没生效"。
        """
        return ("xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), "
                f"' {self._c('select')} ')][1]")

    # ================= 对外 =================
    def fill_record(self, record: dict):
        """填主表 + 所有明细行。"""
        self._fill_fields(self.cfg["fields"], record["header"], scope=None)

        list_cfg = self.cfg.get("list")
        items = record.get("items") or []
        if not list_cfg or not items:
            return

        if len(items) > list_cfg.get("max_rows", 20):
            raise FillError(f"明细行 {len(items)} 条，超过上限 {list_cfg['max_rows']}")

        variant_fields = self._pick_variant(list_cfg, record["header"])

        for idx, item in enumerate(items):
            if idx > 0:
                self._add_list_row(list_cfg, variant_fields, idx)
            fields = [
                {**f, "selector": list_cfg["id_template"].format(i=idx, key=f["key"])}
                for f in variant_fields
            ]
            try:
                self._fill_fields(fields, item, scope=f"第{idx + 1}项")
            except FillError as e:
                raise FillError(f"[第{idx + 1}项] {e}") from e

    def _pick_variant(self, list_cfg, header: dict):
        """明细行的字段集随主表某个字段的值变化（如「限制类型」）。"""
        if "variants" not in list_cfg:
            return list_cfg["fields"]

        key = list_cfg["variants_by"]
        value = str(header.get(key, "")).strip()
        variants = list_cfg["variants"]
        if value not in variants:
            raise FillError(
                f"「{key}」的值「{value}」没有对应的明细字段配置。已配置：{list(variants)}"
            )
        log.info("明细行采用「%s」变体", value)
        return variants[value]

    # ================= 内部 =================
    def _fill_fields(self, fields, data: dict, scope):
        for f in fields:
            name = f["name"]
            value = str(data.get(name, "")).strip()

            if not value:
                if f.get("required"):
                    where = f"{scope}的" if scope else ""
                    raise FillError(f"必填字段「{where}{name}」数据为空")
                continue

            self._validate_option(f, value)

            handler = self.HANDLERS.get(f.get("type", "fill"))
            if handler is None:
                raise FillError(f"字段「{name}」的 type={f.get('type')} 不认识")

            try:
                handler(self, f, value)
            except FillError:
                raise
            except Exception as e:
                raise FillError(f"填「{name}」失败（{f['selector']}）：{e}") from e

            # 选了某个值之后才出现的字段（如「人群选组」→「人群选择」）
            revealed = (f.get("reveals") or {}).get(value)
            if revealed:
                log.info("「%s」=「%s」触发附加字段：%s",
                         name, value, [r["name"] for r in revealed])
                for sub in revealed:
                    self.page.wait_for_selector(sub["selector"], state="visible")
                self._fill_fields(revealed, data, scope=scope)

            if name in self.waits:
                self.page.wait_for_selector(self.waits[name])

    def _validate_option(self, f, value):
        """配置里写了 options 就先本地校验，比等页面报错快、错误信息也清楚。"""
        opts = f.get("options")
        if not opts:
            return
        vals = split_multi(value) if f.get("type") == "checkbox_sync" else [value]
        bad = [v for v in vals if v not in opts]
        if bad:
            raise FillError(f"「{f['name']}」的值 {bad} 不在可选项里。可选：{opts}")

    def _add_list_row(self, list_cfg, variant_fields, idx):
        self.page.click(list_cfg["add_button"])
        probe = list_cfg["id_template"].format(i=idx, key=variant_fields[0]["key"])
        self.page.wait_for_selector(probe)

    def _locator(self, f):
        return self.page.locator(f["selector"]).first

    # ================= 控件类型 =================
    def _fill(self, f, value):
        el = self._locator(f)
        el.wait_for(state="visible")
        el.fill("")
        el.fill(value)

    def _select(self, f, value):
        """原生 <select>"""
        el = self._locator(f)
        el.wait_for(state="visible")
        try:
            el.select_option(label=value)
        except Exception:
            el.select_option(value=value)

    def _select_antd(self, f, value):
        """antd Select：selector 指向内部 input，要点它的 .ant-select 外壳。

        难点：antd 用 rc-virtual-list 做虚拟滚动，DOM 里只渲染可视区那几个选项。
        「会员卡种」25 个选项只渲染前 10 个，不滚动就会误报"找不到"。
        """
        inp = self._locator(f)
        inp.wait_for(state="attached")
        wrapper = inp.locator(self._select_root)
        self._open_dropdown(wrapper)

        dropdown = self.page.locator(self._dropdown).last
        dropdown.wait_for(state="visible")

        options = dropdown.locator(self._option)

        # 支持搜索的下拉：打字过滤。
        # ⚠ 这类下拉是远程搜索，打开时是「暂无数据」，输入后要等接口回来才有选项，
        #   实测约 2 秒。固定等一小会儿会读到 0 个选项然后误报"找不到"，必须轮询。
        if f.get("search"):
            inp.type(value, delay=30)
            for _ in range(40):
                self.page.wait_for_timeout(200)
                if options.count():
                    break
            else:
                raise FillError(
                    f"「{f['name']}」搜索「{value}」等了 8 秒没返回任何选项。"
                    f"确认这个值在系统里存在，或者网络是否正常。")

        holder = dropdown.locator(".rc-virtual-list-holder")
        has_virtual = f.get("virtual_scroll") and holder.count() > 0

        # match: exact  —— 选项文字 == 数据值（默认）
        # match: contains —— 选项文字包含数据值。搜索型下拉用这个：
        #   输入 "35697"，选项显示的是 "【暑促短信】8月1日2(35697)"，两者并不相等
        mode = f.get("match", "exact")
        if mode == "contains":
            def find():
                return options.filter(has_text=value)
        else:
            exact_re = re.compile(rf"^\s*{re.escape(value)}\s*$")
            def find():
                return options.filter(has_text=exact_re)

        seen = set()
        for _ in range(40):
            hit = find()
            if hit.count():
                if mode == "contains" and hit.count() > 1:
                    texts = hit.all_inner_texts()
                    raise FillError(
                        f"「{f['name']}」用「{value}」匹配到 {len(texts)} 个候选，无法确定选哪个："
                        f"{texts[:5]}。请在数据里填更精确的值。"
                    )
                chosen = hit.first.inner_text().strip()
                hit.first.click()
                self._confirm_selected(f, wrapper, value, chosen)
                self._close_dropdown(wrapper, inp)
                return
            seen.update(options.all_inner_texts())
            if not has_virtual:
                break
            at_bottom = holder.evaluate(
                "el => { const before = el.scrollTop;"
                " el.scrollTop += el.clientHeight * 0.7;"
                " return el.scrollTop === before; }"
            )
            self.page.wait_for_timeout(150)
            if at_bottom:
                seen.update(options.all_inner_texts())
                break

        raise FillError(
            f"下拉里没有「{value}」。页面实际选项（{len(seen)} 个）：{sorted(seen)}"
        )

    def _open_dropdown(self, wrapper):
        """展开下拉。点击是「切换」不是「打开」——已经展开时再点会关掉，
        所以必须先看 select-open，否则前一步残留的展开状态会让这次失败。
        """
        def is_open():
            return self._c("select-open") in (wrapper.get_attribute("class") or "")

        if not is_open():
            wrapper.click()
        for _ in range(25):
            if is_open():
                return
            self.page.wait_for_timeout(100)
        wrapper.click()          # 再试一次
        self.page.wait_for_timeout(400)

    def _shown_value(self, wrapper) -> str:
        """读选择框当前显示的值。

        ⚠ 这套后台混用了两种下拉：
          · 标准 antd Select  → 值在 .ant-select-selection-item
          · ProComponents 的 ant-pro-filed-search-select → 值在 .ant-select-content-value，
            压根没有 selection-item
        只认第一种会误判成"没选上"，两种都得试。
        """
        for sel in (self._sel("select-selection-item"), self._sel("select-content-value")):
            loc = wrapper.locator(sel).first
            try:
                if loc.count():
                    txt = (loc.get_attribute("title") or loc.inner_text() or "").strip()
                    if txt:
                        return txt
            except Exception:
                continue
        return ""

    def _confirm_selected(self, f, wrapper, value, chosen):
        """确认选中生效。

        ⚠ 不要用「下拉浮层消失」当判据：浮层节点是复用的，选完之后不一定
        立刻进入 hidden 状态，会白等到超时。直接读选择框显示的文字才靠谱。
        """
        for _ in range(20):
            shown = self._shown_value(wrapper)
            if shown and (shown == chosen or value in shown or shown in chosen):
                if shown != value:
                    log.info("「%s」输入 %s → 选中「%s」", f["name"], value, shown)
                return
            self.page.wait_for_timeout(100)

        raise FillError(
            f"点了「{chosen}」但选择框显示的是「{self._shown_value(wrapper) or '(空)'}」，选中没生效")

    def _close_dropdown(self, wrapper, inp):
        """收起下拉。这套控件选完之后浮层不会自动收，
        留着会挡住下面的字段，导致后续点击被判定为不可交互。
        用 blur 而不是 Esc —— Esc 有可能一路把整个弹窗关掉。
        """
        def is_open():
            return self._c("select-open") in (wrapper.get_attribute("class") or "")

        def floating():
            try:
                return self.page.locator(self._dropdown).count() > 0
            except Exception:
                return False

        if not is_open() and not floating():
            return

        try:
            inp.evaluate("el => el.blur()")
        except Exception:
            pass
        for _ in range(10):
            if not is_open() and not floating():
                return
            self.page.wait_for_timeout(100)

        # blur 对部分控件不生效（浮层留在原地挡住下面的字段），
        # 再点一下弹窗标题这种中性区域把它逼退
        try:
            title = self.page.locator(self._sel("modal-title")).first
            if title.count():
                title.click()
                self.page.wait_for_timeout(300)
        except Exception:
            pass

    def _select_ui(self, f, value):
        """Element-UI 等其它 UI 库的假下拉（保留，给别的表单用）。"""
        trigger = self._locator(f)
        trigger.wait_for(state="visible")
        trigger.click()
        if f.get("search"):
            trigger.fill(value)
            self.page.wait_for_timeout(300)

        opt_sel = f.get("option_selector") or ".el-select-dropdown__item"
        options = self.page.locator(opt_sel)
        options.first.wait_for(state="visible")
        exact = options.filter(has_text=re.compile(rf"^\s*{re.escape(value)}\s*$"))
        target = exact.first if exact.count() else options.filter(has_text=value).first
        if not target.count():
            raise FillError(f"下拉里找不到选项「{value}」")
        target.click()
        self.page.wait_for_timeout(200)

    def _checkbox_sync(self, f, value):
        """把复选组同步成数据里指定的状态——该勾的勾上，不该勾的取消。

        这类表单常有默认全选（比如「平台」默认 iPhone+Android+pc），
        只做"点击想要的项"会得到错误结果，必须双向同步。

        ⚠ 禁用项要跳过。DMP 的「推广形式」里 titan 是禁用且默认勾选的，
          按双向同步的逻辑会去点它取消勾选，而 Playwright 点不动禁用元素，
          只会一路等到超时。
        """
        want = set(split_multi(value))
        item = self._form_item_by_label(f["selector"])
        boxes = item.locator(self._sel("checkbox-wrapper"))
        n = boxes.count()
        if not n:
            raise FillError(f"「{f['name']}」下没找到复选框")

        seen, locked = set(), []
        for i in range(n):
            box = boxes.nth(i)
            text = box.inner_text().strip()
            cls = box.get_attribute("class") or ""
            seen.add(text)
            if self._c("checkbox-wrapper-disabled") in cls:
                locked.append(text)
                continue
            checked = self._c("checkbox-wrapper-checked") in cls
            if (text in want) != checked:
                box.click()

        missing = want - seen
        if missing:
            raise FillError(f"「{f['name']}」页面上没有这些选项：{sorted(missing)}，实际有：{sorted(seen)}")
        if locked:
            log.info("「%s」有禁用项，保持页面默认：%s", f["name"], locked)

    def _radio(self, f, value):
        container = self._locator(f)
        container.wait_for(state="visible")
        target = container.locator(f.get("label_selector", "label")).filter(has_text=value).first
        if not target.count():
            raise FillError(f"找不到选项「{value}」")
        target.click()

    def _date(self, f, value):
        el = self._locator(f)
        el.wait_for(state="visible")
        el.click()
        el.fill(value)
        el.press("Enter")
        self.page.keyboard.press("Escape")

    def _upload(self, f, value):
        self._locator(f).set_input_files(value)

    def _js(self, f, value):
        self.page.eval_on_selector(f["selector"], f"(el, value) => {{ {f['script']} }}", value)

    def _click(self, f, value):
        self._locator(f).click()

    # ---- 工具 ----
    def _form_item_by_label(self, label_text):
        """按 label 文字定位 antd 的 form-item 容器（用于没有 id 的复选组/单选组）。"""
        item = self.page.locator(self._sel("form-item")).filter(
            has=self.page.locator(
                f"{self._sel('form-item-label')} label",
                has_text=re.compile(rf"^\s*{re.escape(label_text)}\s*$"),
            )
        ).first
        if not item.count():
            raise FillError(f"按 label「{label_text}」找不到表单项")
        return item

    HANDLERS = {
        "fill": _fill,
        "select": _select,
        "select_antd": _select_antd,
        "select_ui": _select_ui,
        "checkbox_sync": _checkbox_sync,
        "radio": _radio,
        "date": _date,
        "upload": _upload,
        "js": _js,
        "click": _click,
    }
