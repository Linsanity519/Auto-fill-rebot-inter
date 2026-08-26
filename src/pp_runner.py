"""价格面板配置主流程：一行 Excel = 一个单元。

⚠ 与 src/runner.py / src/wizard_runner.py 都独立。老配置一行不受影响。

和另外两套的关键差别：

  1. **活动是本批共用一个**，在「投放配置」页上选：本次新建活动（跑之前先建，
     活动页在新系统、用 WizardFiller）或挂到已有活动（界面上填个 ID）。
     拿到 activityId 之后拼 URL 直接进单元页。
  2. **填写有严格的先后顺序**，不能照 yaml 顺序无脑填：
        选资源位 → 「其他设置」才渲染出来
        面板个数 → 决定套餐排列分几段（N 段 = N-1 条分隔线）
        sku选择  → 决定套餐排列里有哪些卡片
        套餐排列 → 拖成方案里写的顺序
        逐张卡片 → 每个 SKU 各自的搭售配置
        选中类型=指定套餐 的那两个下拉 → 要等上面都好了才有得挑（phase: after）
  3. **提交前一定停下来**。套餐排列是拖出来的，拖歪了截图上一眼能看出来，
     所以这一步比别的配置更需要人眼过一遍。

取值一律走 pp_data.values_for()：策略中心 → Excel 这一行，后面的盖前面的。
值是**按单元名称算的**，所以每个单元都要各算一次 —— 同一批里「新客面板」和
「老客面板」可能命中不同的套餐方案/搭售方案。
每个 SKU 搭不搭售看策略里的角标（买赠SKU / 0元购SKU）；具体配什么 pid、
买赠什么商品，去 PID 映射表里按「PID映射方案」捞。
「面板个数」不配置，由三段面板推出来。
"""
from __future__ import annotations

import csv
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from . import pp_data as D
from . import wizard_schema as W
from .browser import Browser
from .filler import FillError
from .pp_creative import CreativeFiller
from .pp_filler import PriceFiller, apply_field
from .preview import PreviewRow
from .ui import BaseUI, ConsoleUI, Stopped

log = logging.getLogger(__name__)


class PriceRunner:
    def __init__(self, settings: dict, form_cfg: dict, ui: BaseUI | None = None):
        self.s = settings
        self.f = form_cfg
        self.ui = ui or ConsoleUI()
        self.shot_dir = Path(settings["screenshot_dir"])
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self.auto = False
        self.created = []
        self._data = None

    # ---------------------------------------------------------------- 预检
    def preview(self) -> list[PreviewRow]:
        data = D.load(self.s["data_file"], self.f, self.s)
        issues = D.validate(self.f, data)
        rows = []
        for i, u in enumerate(data["units"]):
            mine = [x for x in issues if x.startswith(f"第{u['row']}行")]
            segs = D.panels_for(D.values_for(self.f, data, u))
            rows.append(PreviewRow(
                index=i + 1,
                name=u["header"].get(D.UNIT_NAME, "") or "(未命名)",
                kind=self.f.get("position", "价格面板"),
                detail_count=len({x for seg in segs for x in seg}),
                issues=mine, done=False, payload=u,
            ))
        # 准备阶段和跨行的问题不属于任何一行，挂到第一行上，
        # 否则「校验通过」满屏绿、真正拦路的那条没人看见
        head = [x for x in issues if not x.startswith("第")]
        if head:
            if rows:
                rows[0].issues = head + rows[0].issues
            else:
                rows.append(PreviewRow(index=1, name="(没读到单元)", kind="",
                                       detail_count=0, issues=head))
        self._data = data
        return rows

    # ---------------------------------------------------------------- 主循环
    def run(self, units: list[dict] | None = None):
        data = self._data or D.load(self.s["data_file"], self.f, self.s)
        units = units if units is not None else data["units"]

        dry = self.s.get("dry_run")
        total = len(units)
        stats = {"ok": 0, "failed": 0, "skipped": 0, "dry": 0}
        results = []

        self.ui.log(f"「{self.f['name']}」共 {total} 个单元"
                    + ("（空跑，不保存）" if dry else ""))
        self.ui.progress(0, total, stats)

        try:
            with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
                pf = PriceFiller(b.page, self.s["timeout"],
                                 on_note=lambda m: self.ui.log(f"    {m}", "warn"))

                # ---- 活动：本批共用一个，先确定下来（要新建就在这儿建）----
                self.ui.checkpoint()
                try:
                    self._ensure_activity(b.page, data)
                except Stopped:
                    raise
                except Exception as e:
                    log.exception("活动层失败")
                    shot = self._shot(b.page, 0, "activity_error")
                    self.ui.log(f"活动层失败：{e}", "error")
                    self.ui.log(f"    截图：{shot}")
                    self.ui.finished("配置中止", f"活动没建成功，后面的单元没法继续。\n\n{e}", False)
                    return results

                for i, u in enumerate(units):
                    self.ui.checkpoint()
                    name = u["header"].get(D.UNIT_NAME, "")
                    label = f"[{i + 1}/{total}]"
                    # ⚠ 用算出来的面板，不是 Excel 那两列 —— 多数单元那两列是空的，
                    #   SKU 由策略中心的「面板套餐」方案给
                    panels = D.panels_for(D.values_for(self.f, data, u))
                    self.ui.log(f"{label} {name} —— 填写中"
                                f"（{len({x for seg in panels for x in seg})} 个 SKU）")

                    try:
                        self._open_unit(b.page, pf, u, data)
                        self._fill_unit(pf, u, data)
                        shot = self._shot(b.page, i)
                        self.ui.log(f"{label} 已填写完成，"
                                    f"套餐排列是拖出来的，请在浏览器里核对一眼", "ok")

                        if dry:
                            stats["dry"] += 1
                            # 空跑不保存单元，也就没有 unitId，创意层没法挂上去
                            results.append(self._result(i, u, "dry_run", ""))
                        else:
                            action = "submit" if self.auto else self.ui.confirm(label, name)
                            if action == "auto":
                                self.auto, action = True, "submit"
                            if action == "stop":
                                self.ui.log("已停止", "warn")
                                break
                            if action == "skip":
                                stats["skipped"] += 1
                                results.append(self._result(i, u, "skipped", "用户跳过"))
                            else:
                                unit_id = self._save(b.page, pf)
                                self.ui.log(f"{label} 单元已保存（ID {unit_id or '?'}），开始填创意")
                                if not unit_id:
                                    raise FillError(
                                        "单元保存成功了，但跳转地址里没有 unitId，"
                                        f"创意层没法接着填。当前地址：{b.page.url}")
                                self._fill_creative(b.page, u, data, unit_id, dry)
                                self._shot(b.page, i, "creative")
                                stats["ok"] += 1
                                results.append(self._result(i, u, "ok", ""))
                                self.created.append(name)
                                self.ui.log(f"{label} 单元 + 创意都保存成功", "ok")

                    except Stopped:
                        raise
                    except Exception as e:
                        msg = str(e)
                        log.exception("%s 失败", label)
                        shot = self._shot(b.page, i, "error")
                        stats["failed"] += 1
                        results.append(self._result(i, u, "failed", msg))
                        self.ui.log(f"{label} 失败：{msg}", "error")
                        self.ui.log(f"    错误截图：{shot}")
                        if not self.ui.ask_continue(msg):
                            break

                    self.ui.progress(i + 1, total, stats)

        except Stopped:
            self.ui.log("已停止", "warn")
        finally:
            self._write_results(results)
            self._report(results, total, stats)

        return results

    # ---------------------------------------------------------------- 活动
    def _ensure_activity(self, page, data: dict) -> dict:
        """确定本批挂哪个活动。

        挂到已有活动  → 界面上填的 ID，什么都不用做
        本次新建活动  → 按「活动」sheet 那一行去建，建完把活动ID 记回 data

        ⚠ 活动页在**新系统**（rich-vip，Formily/antd），和这个配置类型其余部分跑的
          老后台（Vue + tw- 编译类名）不是一套 DOM —— 所以这一步单独用 WizardFiller，
          不能用 PriceFiller。资源位投放走的是同一个页面，字段定义也是照抄过来的。
        """
        from urllib.parse import parse_qs, urlparse

        from .wizard_filler import WizardFiller

        act = dict(data.get("activity") or {})
        exist = str(act.get("已有活动ID", "")).strip()
        if exist:
            self.ui.log(f"挂到已有活动（ID {exist}），不新建活动")
            return act

        spec = self.f.get("activity") or {}
        if not spec:
            raise FillError("这个配置类型没有活动层定义（yaml 里缺 activity 段）")
        name = str(act.get("活动名称", "")).strip()
        self.ui.log(f"第一步：建活动「{name}」")

        wf = WizardFiller(page, self.s["timeout"],
                          on_note=lambda m: self.ui.log(f"    {m}", "warn"))
        page.goto(spec["url"], wait_until="domcontentloaded")
        page.wait_for_selector(spec["ready_selector"], state="visible",
                               timeout=self.s["timeout"])
        wf.fill(spec["fields"], act, scope="活动层 ")

        before = page.url
        text = spec.get("next_button") or "保存并下一步"
        btn = page.get_by_role("button", name=text).first
        if not btn.count():
            btn = page.locator("button").filter(has_text=text).first
        if not btn.count():
            raise FillError(f"活动页上找不到「{text}」按钮")
        btn.click()

        want = (spec.get("done_when") or {}).get("url_matches")
        waited, step_ms = 0, 400
        while waited < self.s["timeout"]:
            if page.url != before and (not want or re.search(want, page.url)):
                break
            page.wait_for_timeout(step_ms)
            waited += step_ms
        else:
            raise FillError(f"点了「{text}」但页面没跳转，活动可能没建成。当前地址：{page.url}")

        q = parse_qs(urlparse(page.url).query)
        aid = (q.get("activityId") or [""])[0]
        if not aid:
            raise FillError(f"活动建完了但 URL 里没有 activityId。当前地址：{page.url}")
        act["已有活动ID"] = aid
        act["活动类型ID"] = (q.get("activityType") or [""])[0] or "5"
        data["activity"] = act
        self.ui.log(f"活动已创建（ID {aid}），进入单元页", "ok")
        return act

    # ---------------------------------------------------------------- 开页
    def _open_unit(self, page, pf: PriceFiller, unit: dict, data: dict):
        # 活动是本批共用一个（界面上选「本次新建活动」或「挂到已有活动」），
        # 不再逐单元从 Excel 里取 —— 见 pp_data._activity。
        act = data.get("activity") or {}
        act_id = str(act.get("已有活动ID") or act.get("活动ID") or "").strip()
        act_name = str(act.get("活动名称", "")).strip() or f"活动{act_id}"
        url = self.f["unit_url_template"].format(**{
            "活动ID": act_id,
            "活动名称": quote(act_name, safe=""),
            "活动类型ID": str(act.get("活动类型ID", "5")).strip() or "5",
        })

        ready = self.f.get("ready_selector")
        base = url.split("#")[0]

        if not page.url.startswith(base):
            page.goto(url, wait_until="domcontentloaded")
        else:
            # ⚠ 这个后台是 hash 路由：从一个单元页直接跳到另一个单元页只换 query，
            #   Vue 不重新挂载组件 —— 不处理的话第 2 行会在第 1 行填了一半的表单上
            #   接着填，activityId 还是上一行的。
            #
            #   处理办法是先退到单元列表、再进目标页，逼组件销毁重建。
            #   一开始用的是 page.reload()，但那是**整页重新下载**：内网实测直接
            #   ERR_CONNECTION_TIMED_OUT（README 里写过这边只有 20~40KB/s）。
            #   走 hash 是纯前端的，不发请求，也就没有这个问题。
            page.evaluate("() => { location.hash = '#/vip/resource-delivery/unit'; }")
            if ready:
                self._wait_gone(page, pf, ready)
            page.evaluate("(u) => { location.href = u; }", url)

        if ready and not pf.wait_until(
                lambda: page.locator(ready).count() > 0, timeout=self.s["timeout"]):
            raise FillError("单元页没加载出来（没登录？活动ID 对不对？）")

        pf.pick_position(self.f["position"])
        pos_ready = self.f.get("position_ready_selector")
        if pos_ready and not pf.wait_until(
                lambda: page.locator(pos_ready).count() > 0, timeout=self.s["timeout"]):
            raise FillError(f"选中「{self.f['position']}」之后「其他设置」没出现")

    @staticmethod
    def _wait_gone(page, pf: PriceFiller, selector: str):
        """等旧表单从 DOM 上消失 = 组件真的销毁了。

        等不到也不报错：最坏情况是页面本来就没渲染出那个控件，
        接着往下走会在 wait ready 那里给出更准确的报错。
        """
        pf.wait_until(lambda: page.locator(selector).count() == 0, timeout=8000)

    # ---------------------------------------------------------------- 填一条
    def _fill_unit(self, pf: PriceFiller, unit: dict, data: dict):
        h = unit["header"]
        vals = D.values_for(self.f, data, unit)
        panels = D.panels_for(vals)

        # ---- 1. Excel 里逐单元填的 + 准备页里的通用字段 ----
        # Excel 有值就以 Excel 为准（「优先级」两边都有，就是为了让个别单元能覆盖）
        excel_named = set()
        for f in D.unit_fields(self.f):
            if not f.get("type"):
                continue                     # 活动ID/面板套餐这些不是页面字段
            val = str(h.get(f["name"], "")).strip()
            excel_named.add(f["name"])
            if val:
                apply_field(pf, f, val)
            elif f.get("required"):
                raise FillError(f"必填的「{f['name']}」是空的")

        self._fill_strategy(pf, vals, phase="before", skip=excel_named)

        # ---- 2. 面板个数（推出来的，不是配的）----
        # ⚠ 必须在 arrange 之前：它决定套餐排列分几段（N 段 = N-1 条分隔线）
        pf.radio("面板个数", D.panel_count_of(panels))

        # ---- 3. sku选择 + 套餐排列 ----
        skus = list(dict.fromkeys(x for seg in panels for x in seg))
        pf.set_skus(skus)
        pf.arrange(panels)

        # ---- 4. 逐张卡片配搭售 ----
        for sku in skus:
            self._fill_sku(pf, data, vals, sku)

        # ---- 5. 要等前面都好了才有得挑的那几项 ----
        self._fill_strategy(pf, vals, phase="after", skip=excel_named)

    def _fill_strategy(self, pf: PriceFiller, vals: dict, phase: str, skip: set):
        """页面上那些普通控件。scope: manual 的不在这里填（runner 自己消费）。

        ⚠ 顺序按 yaml 里 unit_common 的书写顺序走，条件字段紧跟在它的触发字段后面
          （flatten 保证了这一点）—— 「内容设置 = 指定」得先填，「生效内容」
          才在页面上存在。
        """
        for f in W.flatten(W.unit_fields(self.f, D.position(self.f))):
            if f.get("scope") in ("manual", "derived") or f["name"] in skip:
                continue
            if str(f.get("phase", "before")) != phase:
                continue
            when = f.get("_when")
            if when and not W.when_active(f, vals.get(when[0], "")):
                continue          # 触发字段不是这个值，这一项页面上就没有
            val = str(vals.get(f["name"], "")).strip()
            if not val:
                if f.get("required"):
                    raise FillError(f"「{f['name']}」没配（策略中心或投放配置页）")
                continue
            apply_field(pf, f, val)

    def _fill_sku(self, pf: PriceFiller, data: dict, vals: dict, sku: str):
        """一个 SKU 的搭售配置。先点中它的卡片，下面那几个字段才是它的。"""
        m = D.sku_map(self.f, data, vals, sku)
        pf.click_chip(sku)

        # 异形SKU 没有「搭售类型」这一档，页面上直接就是 pid + 搭售商品。
        # ⚠ 这两个值来自 Excel 的「异形SKU·价格面板pid」「异形SKU·搭售商品ID」两列，
        #   不是策略中心 —— 它们一个单元一个样（见 yaml 的 sku_unit_fields）。
        if sku in (self.f.get("sku_map_skip") or []):
            pf.multi_search_select("价格面板pid", m.get("pid清单") or [])
            pf.search_select("搭售商品", m.get("搭售商品", ""))
            return

        tie = m.get("搭售类型", "") or "无"
        pf.radio("搭售类型", tie)
        if tie == "无":
            return

        if "买赠" in tie:
            # ⚠ pid 是多选：这个单元投几个平台就填几个（见 pp_data.pids_for_platforms）
            pf.multi_search_select("价格面板pid", m.get("pid清单") or [])
            # ⚠ 先选商品类型，「商品ID」那个框是选完才渲染出来的，顺序反了就找不到字段
            pf.select("买赠商品", m["买赠商品类型"])
            pf.search_select("商品ID", m["买赠商品ID"])
        if "0元购" in tie:
            pf.set_combine(D.combine_pairs(m.get("组合价格", "")))

    # ---------------------------------------------------------------- 保存
    def _save(self, page, pf: PriceFiller) -> str:
        """点「保存并下一步」，返回新建出来的单元ID。

        ⚠ 不能「点了就算成功」：后端校验不过时页面留在原地、只把那一行标红。
          这里以「离开单元新建页」为判据 —— 保存成功会跳到创意页。
        """
        btn = self.f.get("next_button", "保存并下一步")
        before = page.url
        page.get_by_role("button", name=btn).first.click()

        # ⚠ 保存时可能弹「优先级重复」这种二次确认（同优先级的单元列一张表让你确认），
        #   不点掉它页面就一直不跳。而且它的内容是从后台拉的，点完保存两秒内还没出来 ——
        #   所以整个等待期间都要盯着，不能只瞄一眼。
        def moved_or_confirm():
            if page.url != before:
                return True
            title = pf.confirm_modal()
            if title:
                self.ui.log(f"    弹了「{title}」，已点确定")
            return False

        moved = pf.wait_until(moved_or_confirm, timeout=self.s["timeout"])
        if not moved:
            raise FillError(f"点了「{btn}」但页面没跳走，保存被拒。{pf.form_errors()}")
        return self._unit_id_from(page.url)

    @staticmethod
    def _unit_id_from(url: str) -> str:
        """保存后跳到的地址里带着新单元的 ID，创意页要用它。

        ⚠ 这个后台是 hash 路由，参数可能在 ? 后面也可能在 #/... 后面，
          所以不能只 parse 一处 —— 整串扫一遍最省事也最不容易漏。
        """
        m = re.search(r"[?&#][^?&#]*?unitId=(\d+)", url) or re.search(r"unitId=(\d+)", url)
        return m.group(1) if m else ""

    # ---------------------------------------------------------------- 创意层
    def _fill_creative(self, page, unit: dict, data: dict, unit_id: str, dry: bool):
        """给刚建好的单元填一条创意。

        ⚠ 创意页在**新系统**（rich-vip，Arco Design），和单元页不是一套 DOM，
          所以单独用 CreativeFiller。
        ⚠ 字段随每个 SKU 的搭售类型变，和模板出列用的是同一个 pp_data.sku_plan，
          不然会出现「Excel 里有这一列、跑起来却不去填」。
        """
        spec = D.creative_spec(self.f)
        if not spec:
            return
        vals = D.values_for(self.f, data, unit)
        cre = unit.get("creative") or {}
        skus = D.panel_skus_of(vals)
        if not skus:
            self.ui.log("    这个单元一个 SKU 都没有，创意层跳过", "warn")
            return

        cf = CreativeFiller(page, self.s["timeout"],
                            on_note=lambda m: self.ui.log(f"    {m}", "warn"))
        url = spec["url"].format(unit_id=unit_id, position_id=spec.get("position_id", ""))
        cf.open(url)

        filled = 0
        for sku in skus:
            tie = D.tie_of(vals, sku)
            fields = [f for layer, f in D._fields_for_sku(self.f, sku, tie) if layer == "创意层"]
            todo = [(f, str(cre.get(f"{sku}{D.SKU_SEP}{f['name']}", "")).strip())
                    for f in fields]
            todo = [(f, v) for f, v in todo if v]
            if not todo:
                continue
            cf.pick_card(sku)
            for f, v in todo:
                # 级联：父字段没选到触发值的话页面上根本没有这一项
                when = f.get("_when")
                if when:
                    parent = str(cre.get(f"{sku}{D.SKU_SEP}{when[0]}", "")).strip()
                    if not W.when_active(f, parent):
                        continue
                self._apply_creative(cf, f, v)
                filled += 1

        for f in (spec.get("panel_fields") or []):
            v = str(cre.get(f["name"], "") or f.get("default", "")).strip()
            if v and cf.has(f["label"]):
                self._apply_creative(cf, f, v)
                filled += 1

        self.ui.log(f"    创意层填了 {filled} 项")
        if dry:
            self.ui.log("    （空跑，创意不保存）", "warn")
            return
        cf.save(spec.get("submit", "保 存"))

    @staticmethod
    def _apply_creative(cf: CreativeFiller, f: dict, value: str):
        label = f.get("label") or f["name"]
        kind = str(f.get("type", ""))
        if kind == "pp_radio":
            cf.radio(label, value)
        elif kind == "pp_upload":
            cf.upload(label, value)
        elif kind == "pp_fill_or_upload":
            cf.fill_or_upload(label, value)
        else:
            cf.fill(label, value)

    # ---------------------------------------------------------------- 输出
    def _shot(self, page, idx, tag="filled"):
        ts = datetime.now().strftime("%H%M%S")
        path = self.shot_dir / f"{self.f['name']}_{idx + 1:04d}_{tag}_{ts}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
        except Exception:
            return "(截图失败)"
        return str(path)

    def _result(self, idx, unit, status, error):
        segs = D.panels_for(D.values_for(self.f, self._data, unit)) if self._data else [[], [], []]
        return {
            "序号": idx + 1,
            "Excel行": unit["row"],
            "状态": status,
            "错误": error,
            "面板1": "、".join(segs[0]),
            "面板2": "、".join(segs[1]),
            "隐藏sku面板": "、".join(segs[2]),
            **{k: v for k, v in unit["header"].items()},
        }

    def _write_results(self, results):
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
            self.ui.log(f"{path} 被占用（是不是用 Excel 开着？），结果没写进去", "warn")

    def _report(self, results, total, stats):
        lines = [f"配置类型：{self.f['name']}", f"共 {total} 个单元"]
        if stats["dry"]:
            lines.append(f"空跑 {stats['dry']} 个（没保存）")
        else:
            lines.append(f"成功 {stats['ok']} 个")
            if stats["skipped"]:
                lines.append(f"跳过 {stats['skipped']} 个")
            if stats["failed"]:
                lines.append(f"失败 {stats['failed']} 个 ← 看结果表的「错误」列")
        lines += ["", f"明细：{self.s['result_file']}", f"截图：{self.s['screenshot_dir']}"]
        bad = stats["failed"] > 0
        self.ui.finished("配置完成" if not bad else "配置完成（有失败）",
                         "\n".join(lines), not bad)
