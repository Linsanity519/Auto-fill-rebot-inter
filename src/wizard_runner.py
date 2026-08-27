"""wizard 模式主流程：活动 → 单元 → 创意。

⚠ 与 src/runner.py 完全独立。老配置（价格配置）走 Runner，一行不受影响。

和单弹窗表单的三个关键差别：
  1. 成功判据是 URL 变化，不是弹窗消失 —— 这里是整页表单 + 真实跳转
  2. 一个活动下要连着建 N 个单元，活动只建一次
  3. 切换资源位会重置已填字段，单元层必须填两遍（refill_passes）
"""
from __future__ import annotations

import csv
import logging
import time
import re
from datetime import datetime
from pathlib import Path

from . import wizard_data as D
from . import wizard_schema as W
from .browser import Browser
from .filler import FillError
from .images import is_url, prefetch
from .preview import PreviewRow
from .runstate import StateMixin
from .ui import BaseUI, ConsoleUI, Stopped
from .wizard_filler import WizardFiller

log = logging.getLogger(__name__)

DRY_TAG = ""          # 试跑时给活动/单元名加的前缀；留空 = 不加


def _key(unit: dict) -> str:
    """断点的 key：「资源位/单元名」。

    ⚠ 不能只用单元名：同一个单元名在不同资源位下是两条不同的配置，
      都要建。也不能用列表下标 —— 用户在 Excel 中间插一行就全错位了。
    """
    from . import wizard_data as _D
    return f"{unit.get('position', '')}/{str(unit['header'].get(_D.UNIT_NAME, '')).strip()}"


class WizardRunner(StateMixin):
    def __init__(self, settings: dict, form_cfg: dict, ui: BaseUI | None = None):
        self.s = settings
        self.f = form_cfg
        self.ui = ui or ConsoleUI()
        self.shot_dir = Path(settings["screenshot_dir"])
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self.auto = False
        self.created = []          # 记录建出来的活动/单元，跑完报给用户
        self._init_state()

    # ---------------- 等待 ----------------
    def _wait(self, page, cond, timeout: int | None = None, step: int = 150) -> bool:
        """等到 cond() 为真就返回 True，超时 False。

        ⚠ 全流程不写死「睡几秒」：网好的时候不该白等，网差的时候固定值又不够。
          上限跟着 settings.timeout 走，用户改一个数就能整体放宽。
        """
        deadline = self.s["timeout"] if timeout is None else timeout
        waited = 0
        while True:
            try:
                if cond():
                    return True
            except Exception:
                pass
            if waited >= deadline:
                return False
            page.wait_for_timeout(step)
            waited += step

    def _unit_form_ready(self, page) -> bool:
        """单元表单能填了：基本信息那个输入框在，且资源位表格已经渲染。"""
        try:
            return page.locator("input.ant-input").count() > 0
        except Exception:
            return False

    # ---------------- 预检 ----------------
    def preview(self) -> list[PreviewRow]:
        data = D.load(self.s["data_file"], self.f, self.s)
        issues = D.validate(self.f, data)
        # 问题按单元归拢，界面上一行一个单元
        rows = []
        for i, u in enumerate(data["units"]):
            name = str(u["header"].get(D.UNIT_NAME, "")) or "(未命名)"
            mine = [x for x in issues if x.startswith(f"[{u['position']}] 第{u['row']}行")]
            # payload 保留 position/header/creatives 原样：run() 就是按这三个键消费单元数据的
            rows.append(PreviewRow(
                index=i + 1, name=name, kind=u["position"], detail_count=len(u["creatives"]),
                issues=mine, done=self.state.is_done(_key(u)), payload=u,
            ))
        # 活动层和策略中心的问题不属于任何一个单元，挂在第一行上，
        # 免得「校验通过」满屏绿、真正拦路的那条反而没人看见
        head = [x for x in issues if x.startswith("活动") or x.startswith("[策略中心]")]
        if head and rows:
            rows[0].issues = head + rows[0].issues
        self._data = data
        return rows

    # ---------------- 主流程 ----------------
    def run(self, units: list[dict] | None = None):
        data = getattr(self, "_data", None) or D.load(self.s["data_file"], self.f, self.s)
        activity = data["activity"]
        units = units if units is not None else data["units"]

        dry = self.s.get("dry_run")
        total = len(units)
        stats = {"ok": 0, "failed": 0, "skipped": 0, "dry": 0}
        results = []

        self.ui.log(f"「{self.f['name']}」共 {total} 个单元" + ("（试跑：创意层只填不保存）" if dry else ""))
        self.ui.progress(0, total, stats)
        self.ui.log("跑的时候尽量别把 Chrome 切到别的标签页 —— 后台标签会被浏览器降频，"
                    "实测一条创意从 0.7 秒变成 10 秒。脚本每条会自己把页面拨回前台。")

        try:
            with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
                wf = WizardFiller(b.page, self.s["timeout"],
                                  on_note=lambda m: self.ui.log(f"    {m}", "warn"))
                # ⚠ 放在连上浏览器之后：连不上就该立刻报错，
                #   别让人先等几分钟图下完才看见「连不上 Chrome」
                self._prefetch_images(units)

                # ---- 活动：新建，或挂到已有活动 ----
                self.ui.checkpoint()
                act_name = str(activity.get("活动名称", "")).strip()
                exist_id = str(activity.get("已有活动ID", "")).strip()

                if exist_id:
                    # 「活动」sheet 填了活动ID → 不建新活动，直接往这个活动下加单元
                    self.ui.log(f"挂到已有活动（ID {exist_id}），不新建活动")
                    unit_url = self._unit_url_from(exist_id, act_name or f"活动{exist_id}",
                                                   str(activity.get("活动类型ID", "5")).strip() or "5")
                else:
                    if dry and DRY_TAG:
                        act_name = f"{DRY_TAG}{act_name}"
                    self.ui.log(f"第一步：建活动「{act_name}」")
                    try:
                        self._do_activity(b.page, wf, activity, act_name)
                    except Stopped:
                        raise
                    except Exception as e:
                        log.exception("活动层失败")
                        shot = self._shot(b.page, 0, "activity_error")
                        self.ui.log(f"活动层失败：{e}", "error")
                        self.ui.log(f"    截图：{shot}")
                        self.ui.finished("配置中止", f"活动没建成功，后面的单元没法继续。\n\n{e}", False)
                        return results
                    unit_url = self._unit_url(b.page, act_name)
                    self.ui.log("活动已创建，进入单元页", "ok")

                b.page.goto(unit_url, wait_until="domcontentloaded")
                self._wait(b.page, lambda: self._unit_form_ready(b.page))

                # ---- 逐个单元 ----
                for i, u in enumerate(units):
                    self.ui.checkpoint()
                    pos = u["position"]
                    name = str(u["header"].get(D.UNIT_NAME, "")).strip()
                    if dry and DRY_TAG:
                        name = f"{DRY_TAG}{name}"
                    label = f"[{i + 1}/{total}]"
                    self.ui.log(f"{label} {pos} · {name} —— 填写中（{len(u['creatives'])} 条创意）")
                    if u.get("strategy_note"):
                        self.ui.log(f"    {u['strategy_note']}")

                    try:
                        b.front()
                        if i > 0:
                            b.page.goto(unit_url, wait_until="domcontentloaded")
                            self._wait(b.page, lambda: self._unit_form_ready(b.page))

                        t0 = time.monotonic()
                        self._do_unit(b.page, wf, u, name)
                        spent = {"单元填写": time.monotonic() - t0}
                        self._shot(b.page, i + 1, "unit")

                        action = self._ask(label, f"{pos} · {name} 单元", dry)
                        if action == "stop":
                            break
                        if action == "skip":
                            stats["skipped"] += 1
                            results.append(self._row(i, pos, name, "skipped", "用户跳过"))
                            continue

                        t0 = time.monotonic()
                        self._submit_step(b.page, W.STEP_UNIT)
                        spent["保存单元"] = time.monotonic() - t0
                        self.created.append(("单元", name, b.page.url))
                        self.ui.log(f"{label} 单元已保存，进入创意页", "ok")
                        t0 = time.monotonic()
                        self._wait_creative_ready(b.page, pos)
                        spent["等创意页"] = time.monotonic() - t0

                        # ---- 创意 ----
                        for j, c in enumerate(u["creatives"], 1):
                            b.front()          # 被切到后台的话每条要慢十倍，见 Browser.front
                            if j > 1:
                                self._add_creative(b.page, pos)
                            self._pick_creative_tab(b.page, j)
                            wf.fill(W.creative_fields(self.f, pos), c, scope=f"创意{j} ")
                        spent["创意填写"] = time.monotonic() - t0 - spent["等创意页"]
                        self._shot(b.page, i + 1, "creative")

                        if dry:
                            stats["dry"] += 1
                            results.append(self._row(i, pos, name, "dry_run", "创意未保存"))
                            self.ui.log(f"{label} 创意已填好，试跑不保存", "ok")
                        else:
                            action = self._ask(label, f"{pos} · {name} 创意", False)
                            if action == "stop":
                                break
                            if action == "skip":
                                stats["skipped"] += 1
                                results.append(self._row(i, pos, name, "skipped", "创意被跳过"))
                                continue
                            t1 = time.monotonic()
                            self._save_creative(b.page, pos)
                            spent["保存创意"] = time.monotonic() - t1
                            stats["ok"] += 1
                            results.append(self._row(i, pos, name, "ok", ""))
                            # ⚠ 单元和创意都在后台真存在了才记断点。
                            #   重跑不跳过的话会建出重复的单元。
                            self.state.mark_done(_key(u))
                            # 每条跑完报一下时间都花在哪，慢了自己就能看出来
                            self.ui.log(f"{label} 完成（" + "、".join(
                                f"{k} {v:.0f}s" for k, v in spent.items()) + "）", "ok")

                    except Stopped:
                        raise
                    except Exception as e:
                        msg = str(e)
                        log.exception("%s 失败", label)
                        shot = self._shot(b.page, i + 1, "error")
                        stats["failed"] += 1
                        results.append(self._row(i, pos, name, "failed", msg))
                        self.state.mark_failed(_key(u), f"{pos}/{name}", msg)
                        self.ui.log(f"{label} 失败：{msg}", "error")
                        self.ui.log(f"    截图：{shot}")
                        if not self.ui.ask_continue(msg):
                            break

                    self.ui.progress(i + 1, total, stats)

        except Stopped:
            self.ui.log("已停止", "warn")
        finally:
            self._write(results)
            self._report(results, total, stats, dry)

        return results

    def _prefetch_images(self, units: list[dict]):
        """开跑前把所有图片网址并发下好。

        ⚠ 下载本身在内网只有十几 KB/s，一张底图 16 秒（实测）。放在填表中间做，
          表现就是「填到图片那一列卡住」；而且同一批创意里图片是一张张串着下的。
          挪到开跑前并发下完，填的时候全部命中缓存（fetch_image 只认磁盘缓存，
          不用改填写逻辑）。下不下得来不影响开跑 —— 真填到再报准确的错。
        """
        urls = []
        for u in units:
            fields = W.flatten(W.creative_fields(self.f, u["position"]))
            names = [f["name"] for f in fields if str(f.get("type", "")).startswith("upload")]
            for c in u["creatives"]:
                urls += [c.get(n) for n in names if c.get(n)]
        urls = [x for x in urls if is_url(str(x))]
        if not urls:
            return
        n = len(dict.fromkeys(urls))
        self.ui.log(f"先把 {n} 张图下到本地（内网下载慢，放在这里一次下完，"
                    f"省得填到一半卡住）")
        t0 = time.monotonic()
        ok, bad = prefetch(urls)
        self.ui.log(f"图片准备好 {ok}/{n} 张，用了 {time.monotonic() - t0:.0f}s",
                    "ok" if not bad else "warn")
        if bad:
            self.ui.log(f"有 {len(bad)} 张下不下来（网址失效？填到那一条会报错，"
                        f"想避免就先把模板里的网址改对）：", "warn")
            for u in bad[:5]:
                self.ui.log(f"    {u}", "warn")
            if len(bad) > 5:
                self.ui.log(f"    …… 还有 {len(bad) - 5} 张", "warn")

    # ---------------- 各步 ----------------
    def _do_activity(self, page, wf, activity: dict, name: str):
        step = W.step_by_key(self.f, W.STEP_ACTIVITY)
        page.goto(step["url"], wait_until="domcontentloaded")
        page.wait_for_selector(step["ready_selector"], state="visible", timeout=self.s["timeout"])
        self._wait(page, lambda: page.locator(".ant-formily-item input").count() > 0,
                   timeout=5000)

        data = dict(activity)
        data["活动名称"] = name
        wf.fill(step["fields"], data, scope="活动层 ")
        self._submit_step(page, W.STEP_ACTIVITY)

    def _unit_url(self, page, act_name: str) -> str:
        """活动建成后拼直连单元页的 URL。

        ⚠ 保存活动后系统跳到的那个页面是 iframe 壳（内容是 manager.bilibili.co），
        元素都在 iframe 里，定位一层套一层还容易踩跨域。直连路由同一个页面、
        没有 iframe，所以这里从跳转 URL 里取到 activityId 后自己拼。
        """
        from urllib.parse import parse_qs, quote, urlparse

        step = W.step_by_key(self.f, W.STEP_ACTIVITY)
        tpl = step.get("unit_url_template")
        if not tpl:
            return page.url

        q = parse_qs(urlparse(page.url).query)
        vals = {k: (q.get(k, [""])[0]) for k in (step.get("carry_params") or [])}
        if not vals.get("activityId"):
            raise FillError(f"活动建完了但 URL 里没有 activityId，拿不到活动。当前地址：{page.url}")
        return self._unit_url_from(vals["activityId"], act_name,
                                   vals.get("activityType") or "5")

    def _unit_url_from(self, activity_id: str, act_name: str, act_type: str) -> str:
        from urllib.parse import quote

        step = W.step_by_key(self.f, W.STEP_ACTIVITY)
        tpl = step["unit_url_template"]
        url = tpl.format(activityId=activity_id, activityName=quote(act_name),
                         activityType=act_type)
        log.info("直连单元页：%s", url)
        return url

    def _do_unit(self, page, wf, u: dict, name: str):
        step = W.step_by_key(self.f, W.STEP_UNIT)
        pos = u["position"]
        # ⚠ 页面加载慢的时候 15 秒不够（实测有两条死在这），
        #   「等页面出来」按导航的宽限给，别和「找一个控件」共用一个数
        page.wait_for_selector(step["ready_selector"], state="visible",
                               timeout=max(self.s["timeout"] * 3, 45000))

        t0 = time.monotonic()
        self._pick_position(page, step, pos)
        log.info("选资源位「%s」花了 %.1f 秒", pos, time.monotonic() - t0)

        fields = W.unit_fields(self.f, pos)
        data = dict(u["header"])
        data[D.UNIT_NAME] = name

        # ⚠ 选完资源位，页面会异步重渲染一次，把先填的字段清掉（浏览器里实测）。
        #   以前的做法是无条件填两遍 —— 稳，但每个单元白花十几秒。
        #   现在填一遍之后拿「单元名称」当哨兵：它是第一个填的，被重置一定跑不掉。
        #   没被清就直接过，清了才补第二遍。
        passes = max(int(step.get("refill_passes", 1)), 1)
        for k in range(passes):
            try:
                wf.fill(fields, data, scope="单元层 " if k == 0 else "单元层(补填) ")
            except FillError:
                if k == passes - 1:
                    raise
                page.wait_for_timeout(500)
                continue
            if self._unit_name_kept(page, name):
                break
            self.ui.log("    页面把已填内容清掉了，重填一遍", "warn")

    def _unit_name_kept(self, page, name: str) -> bool:
        try:
            box = page.locator("input.ant-input").first
            return (box.input_value() or "").strip() == name.strip()
        except Exception:
            return False        # 读不到就当被清了，补填一遍更保险

    def _pick_position(self, page, step, pos: str):
        """在资源位表格里选中目标行。"""
        picker = step.get("position_picker") or {}
        real = W.real_position_name(self.f, pos)

        all_tab = picker.get("all_tab")
        if all_tab:
            tab = page.locator(".ant-tabs-tab-btn").filter(
                has_text=re.compile(rf"^\s*{re.escape(all_tab)}\s*$")).first
            if tab.count():
                tab.click()
                # 等表格把「全部」那一屏刷出来
                self._wait(page, lambda: page.locator(
                    picker.get("row_container", ".ant-table-tbody tr")).count() > 3,
                    timeout=8000)

        rows = page.locator(picker.get("row_container", ".ant-table-tbody tr"))
        target = rows.filter(
            has=page.locator("td", has_text=re.compile(rf"^\s*{re.escape(real)}\s*$"))).first
        if not target.count():
            raise FillError(f"资源位表格里找不到「{real}」")

        # ⚠ 这张表是虚拟滚动 + 固定高度容器：行在 DOM 里但不在可视区时
        #   Playwright 会一直等「element is not visible」直到超时。
        #   排在前几行的资源位碰巧能点中，靠后的（如开通提示条）必然失败。
        try:
            target.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            log.info("滚动到资源位行失败，直接尝试点击")

        try:
            target.click(timeout=8000)
        except Exception:
            # 还是点不到就用 JS 点，绕开可见性判定
            target.evaluate("el => el.click()")
        # 选中资源位后页面会重渲染出这个位专属的字段，等它渲染完再填
        self._wait(page, lambda: page.locator(".ant-formily-item").count() > 5)

    def _submit_step(self, page, step_key: str):
        """点「保存并下一步」，以 URL 变化为成功判据。"""
        step = W.step_by_key(self.f, step_key)
        before = page.url

        btn = page.get_by_role("button", name=step["next_button"]).first
        if not btn.count():
            btn = page.locator("button").filter(has_text=step["next_button"]).first
        if not btn.count():
            raise FillError(f"找不到「{step['next_button']}」按钮")
        btn.click()

        done = step.get("done_when") or {}
        want_sub = done.get("url_contains")
        want_re = done.get("url_matches")
        deadline = self.s["timeout"]
        waited = 0
        while waited < deadline:
            url = page.url
            if url != before:
                if want_sub and want_sub in url:
                    return
                if want_re and re.search(want_re, url):
                    return
                if not want_sub and not want_re:
                    return
            # ⚠ 二次确认弹窗（「优先级重复」那种）要在整个等待期间反复看：
            #   它的内容是从后台拉的（还带分页），2 秒内根本弹不出来。
            #   只在点完按钮后瞄一眼的话，弹窗晚到就永远没人点确定，
            #   最后报「点了保存并下一步但没跳转」，看日志完全看不出是被弹窗挡住了。
            self._confirm_modal(page)
            page.wait_for_timeout(500)
            waited += 500

        raise FillError(f"点了「{step['next_button']}」但没跳转。{self._page_errors(page)}")

    def _confirm_modal(self, page):
        """点掉二次确认弹窗（如「优先级重复」提示）。

        ⚠ antd 会在双字按钮的两个汉字之间插一个全角空格，渲染出来是「确 定」。
        \\s 匹配不到全角空格（U+3000），所以按钮文字必须先把所有空白剔掉再比，
        否则弹窗点不掉，表现为「点了保存但没跳转」。
        """
        for _ in range(2):
            try:
                btns = page.locator(".ant-modal-wrap button, .ant-modal button")
                for i in range(btns.count()):
                    b = btns.nth(i)
                    if not b.is_visible():
                        continue
                    t = re.sub(r"\s|　", "", b.inner_text() or "")
                    if t in ("确定", "确认"):
                        b.click()
                        # 等弹窗真的消失，别按秒数猜
                        self._wait(page, lambda: page.locator(
                            ".ant-modal-wrap:visible, .ant-modal:visible").count() == 0,
                            timeout=6000)
                        break
                else:
                    return
            except Exception:
                return

    def _wait_creative_ready(self, page, pos: str):
        """等创意表单真正渲染出来。

        ⚠ URL 变了不代表表单能用：创意页先出壳（「上传创意素材 / 创意1」），
          表单区再异步渲染，实测能差好几秒。

        ⚠ 判据不能是「等 input[type=file] 出现」—— 上传框是选了「配置类型 = 图片」
          之后才渲染的，默认「仅文案」时页面上一个都没有，等到超时也等不到
          （实测：9 条里有 2 条就死在这，报「等了 25 秒创意表单还没渲染出来」）。
          改成等「保存创意」按钮 + 表单区有控件，三套创意系统都成立。
        """
        step = W.step_by_key(self.f, W.STEP_CREATIVE)
        sysname = W.creative_system(self.f, pos)
        save_text = (step.get("creative_submit") or {}).get(sysname, "保存创意")
        pat = re.compile(rf"^\s*{re.escape(save_text)}\s*$")

        for k in range(50):
            # ⚠ 先看一眼再睡：以前是进来就睡 500ms，页面早就好了也要白等半秒。
            if k:
                page.wait_for_timeout(500)
            try:
                if "创意模板获取失败" in page.inner_text("body"):
                    raise FillError("页面报「创意模板获取失败」，这个资源位的创意模板没加载出来")
                btn = page.locator("button").filter(has_text=pat)
                if not btn.count() or not btn.first.is_visible():
                    continue
                # 按钮在了，再确认表单区确实渲染了（光有壳的时候控件很少）
                if page.locator("input[type=text], textarea, label").count() < 4:
                    continue
                # 控件数量连着两次一样，才算渲染稳了（Vue 是一批批挂上来的）
                n = page.locator("input, textarea, label").count()
                page.wait_for_timeout(400)
                if page.locator("input, textarea, label").count() != n:
                    continue
                return
            except FillError:
                raise
            except Exception:
                continue
        raise FillError(f"等了 25 秒都没等到创意页的「{save_text}」按钮，看截图确认页面状态")

    # 页签文字：「创意3」「创意3 ✕」
    TAB_RE = r"^\s*创意(\d+)\s*[✕×xX]?\s*$"

    def _tab_state(self, page) -> dict[int, str]:
        """一次 JS 取回每条创意页签的 class 签名：{3: 'tw-1fi194s tw-qwyd0k|...'}。

        选中的那条 class 上会多挂一个类（v1 是 emotion 生成的哈希、新版是
        `-tab-active`），所以「谁的类最多」= 谁被选中，「签名变了」= 切过去了。
        整页只发一次 evaluate，比逐个 get_attribute 快一个量级。
        """
        try:
            raw = page.evaluate("""(re) => {
                const pat = new RegExp(re);
                const out = {};
                document.querySelectorAll('li, a, span, div').forEach(e => {
                    if (e.children.length > 1) return;
                    const m = pat.exec((e.innerText || '').trim());
                    if (!m) return;
                    out[m[1]] = (out[m[1]] || '') + '|' + (e.className || '');
                });
                return out;
            }""", self.TAB_RE)
            return {int(k): v for k, v in raw.items()}
        except Exception:
            return {}

    @staticmethod
    def _tab_is_active(state: dict[int, str], index: int) -> bool:
        """这条页签是不是已经选中了。

        判据是「类比别人多」而不是类名里有没有 active —— v1 创意页的选中态是
        emotion 哈希（`tw-1fi194s tw-qwyd0k`），类名里没有任何可读的词。
        只有一条创意时本来就在它上面，直接算选中。
        """
        if index not in state:
            return False
        if len(state) == 1:
            return True
        mine = len(state[index].split())
        return all(mine > len(v.split()) for k, v in state.items() if k != index)

    def _pick_creative_tab(self, page, index: int):
        """切到第 index 条创意的 tab。

        ⚠ 点「+添加创意」只是把 tab 建出来，页面**不会**自动切过去 ——
          不切的话三条创意会全填进「创意1」（后面覆盖前面），
          创意2、创意3 是空的，最后报「保存创意没反应」，
          看日志完全看不出是这个原因（实测踩过）。

        ⚠ 别再等「class 里出现 active/selected」：v1 创意页的选中态是 emotion
          哈希类名，那个条件永远不成立，每条创意白烧满 3 秒超时（实测 8 条
          创意每条 6 秒，其中 3.5 秒耗在这里）。改成盯签名变化，通常 50ms 返回。
        """
        before = self._tab_state(page)
        if self._tab_is_active(before, index):
            return                        # 已经在这条上，再点一次只会白等
        tab = page.locator("li, a, span, div").filter(
            has_text=re.compile(rf"^\s*创意{index}\s*[✕×xX]?\s*$")).first
        if not tab.count():
            if index == 1:
                return                    # 只有一条创意时本来就在它上面
            raise FillError(f"找不到「创意{index}」这个页签，切不过去")
        try:
            tab.click(timeout=8000)
        except Exception:
            tab.evaluate("el => el.click()")
        # 等页签真的切过去：签名变了，或这条成了「类最多」的那个
        self._wait(page, lambda: (lambda st: st != before or self._tab_is_active(st, index))(
            self._tab_state(page)), timeout=3000)

    def _add_creative(self, page, pos: str):
        for text in ("+添加创意", "新增创意"):
            btn = page.locator("button, a, span").filter(
                has_text=re.compile(rf"^\s*{re.escape(text)}\s*$")).first
            if btn.count():
                n = page.locator("span, div, li, a").filter(
                    has_text=re.compile(r"^\s*创意\d+\s*[✕×xX]?\s*$")).count()
                btn.click()
                # 等新页签真的加出来
                self._wait(page, lambda: page.locator("span, div, li, a").filter(
                    has_text=re.compile(r"^\s*创意\d+\s*[✕×xX]?\s*$")).count() > n,
                    timeout=8000)
                return
        raise FillError("找不到「添加创意」按钮")

    def _save_creative(self, page, pos: str):
        """点「保存创意」，以「跳走了 / 出成功提示」为成功判据。

        ⚠ 别拿 _page_errors() 的返回值当判据 —— 它没找到错误时返回的是
          「页面上没有明确报错，看截图。」，这句话里就有「报错」两个字，
          用 `if "报错" in err` 判会把每一次成功都判成失败（实测 9 条全军覆没，
          单元建出来了、创意其实也存进去了，界面上却报「保存创意被拒」）。
        """
        step = W.step_by_key(self.f, W.STEP_CREATIVE)
        sysname = W.creative_system(self.f, pos)
        text = (step.get("creative_submit") or {}).get(sysname, "保存创意")

        btn = page.locator("button").filter(
            has_text=re.compile(rf"^\s*{re.escape(text)}\s*$")).first
        if not btn.count():
            btn = page.get_by_role("button", name=text).first
        if not btn.count():
            raise FillError(f"找不到「{text}」按钮")

        before = page.url
        btn.click()

        for _ in range(24):                     # 最多等 12 秒
            self._confirm_modal(page)           # 同上：弹窗可能晚到
            page.wait_for_timeout(500)
            if page.url != before:
                return                          # 跳回创意列表 = 存下了
            ok = page.locator(".el-message--success, .ant-message-success, "
                              ".mega-ant-message-success")
            try:
                if ok.count() and ok.first.is_visible():
                    return
            except Exception:
                pass
        raise FillError(f"点了「{text}」但页面没反应（既没跳走也没成功提示）。"
                        f"{self._page_errors(page)}")

    def _page_errors(self, page) -> str:
        msgs = []
        for sel in (".ant-form-item-explain-error", ".ant-message-error",
                    ".ant-message-notice-content", ".ant-notification-notice-description",
                    ".el-message--error"):
            try:
                msgs += [t.strip() for t in page.locator(sel).all_inner_texts() if t.strip()]
            except Exception:
                pass
        uniq = [m for m in dict.fromkeys(msgs) if m]

        # ⚠ Formily 的必填提示只是字段下面一行红字，上面几个选择器都抓不到。
        #   不点出「哪个字段红了」的话，报错永远是「点了保存但没跳转」，
        #   得翻截图才知道原因 —— 这里直接把红字所属的字段名带出来。
        bad = self._required_fields_with_error(page)
        if bad:
            return f"这些必填项没填/没生效：{bad}" + (f"；页面报错：{uniq}" if uniq else "")
        return f"页面报错：{uniq}" if uniq else "页面上没有明确报错，看截图。"

    def _required_fields_with_error(self, page) -> list[str]:
        try:
            return page.evaluate("""() => {
                const out = [];
                document.querySelectorAll('.ant-formily-item, .ant-form-item').forEach(it => {
                    if (!/必填|不能为空|请选择|请输入/.test(it.innerText || '')) return;
                    if (!/该字段是必填字段|必填|不能为空/.test(it.innerText || '')) return;
                    const lb = it.querySelector('label');
                    const name = (lb ? lb.innerText : it.innerText.split('\\n')[0] || '')
                        .replace(/[*:：\\s]/g, '');
                    if (name && !out.includes(name)) out.push(name);
                });
                return out.slice(0, 8);
            }""")
        except Exception:
            return []

    # ---------------- 交互 / 输出 ----------------
    def _ask(self, label, what, dry) -> str:
        if dry or self.auto:
            return "submit"
        action = self.ui.confirm(label, what)
        if action == "auto":
            self.auto = True
            return "submit"
        return action

    def _shot(self, page, idx, tag):
        ts = datetime.now().strftime("%H%M%S")
        path = self.shot_dir / f"{self.f['name']}_{idx:03d}_{tag}_{ts}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
        except Exception:
            return "(截图失败)"
        return str(path)

    def _row(self, i, pos, name, status, error):
        return {"序号": i + 1, "资源位": pos, "单元名称": name, "状态": status, "错误": error}

    def _write(self, results):
        if not results:
            return
        path = Path(self.s["result_file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("w", newline="", encoding="utf-8-sig") as fh:
                w = csv.DictWriter(fh, fieldnames=list(results[0].keys()))
                w.writeheader()
                w.writerows(results)
        except PermissionError:
            self.ui.log(f"{path} 被占用（Excel 开着？），结果没写进去", "warn")

    def _report(self, results, total, stats, dry):
        lines = [f"配置类型：{self.f['name']}", f"共 {total} 个单元"]
        if dry:
            lines.append(f"试跑 {stats['dry']} 个（活动和单元已真实创建，创意未保存）")
        else:
            lines.append(f"成功 {stats['ok']} 个")
        if stats["skipped"]:
            lines.append(f"跳过 {stats['skipped']} 个")
        if stats["failed"]:
            lines.append(f"失败 {stats['failed']} 个 ← 看结果表的「错误」列")

        if self.created:
            lines += ["", "本次创建（需要清理的话按这个找）："]
            lines += [f"  {k}：{n}" for k, n, _ in self.created[:20]]

        lines += ["", f"明细：{self.s['result_file']}", f"截图：{self.s['screenshot_dir']}"]
        bad = stats["failed"] > 0
        self.ui.finished("配置完成" if not bad else "配置完成（有失败）", "\n".join(lines), not bad)
