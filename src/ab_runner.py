"""AB 实验续期执行器。

和 DMP 延期同一套骨架：运行前读取「我的实验」里状态为「实验中」的实验，
逐条打开「其他 → 续期」，把到期日选到平台允许的最晚一天，再提交。

和 DMP 的三处实质差异，都是页面本身决定的：
1. 列表默认是全站 7800+ 条，必须先点「我的实验」收敛；这个筛选不写进 URL，
   每次重新打开列表都要重新点一次。
2. 可选日期有上限（平台限制实验最长时长），上限可能落在下个月，所以要往后
   翻月找真正最晚的那天，不能像 DMP 那样只看当前月。
3. 弹窗里「续期」一步到位，没有 DMP 那种额外的「保存」步骤。

页面选择器集中在 config/forms/AB实验延期.yaml。页面改版后优先改配置，不要把
业务 DOM 细节散落到流程代码中。
"""
from __future__ import annotations

import csv
import logging
import re
from datetime import date, datetime
from pathlib import Path

from .browser import Browser
from .dmp_date import fmt as fmt_date
from .filler import FillError
from .preview import PreviewRow
from .runstate import StateMixin
from .ui import BaseUI, ConsoleUI, Stopped

log = logging.getLogger(__name__)


SCOPE_MINE = "mine"
SCOPE_ID_LIST = "id_list"
SCOPE_LABELS = {
    SCOPE_MINE: "我的实验 → 最晚日期",
    SCOPE_ID_LIST: "按清单指定实验ID",
}


class NotExtendable(FillError):
    """这个实验平台已经不让续期了（日期面板里一个可选日期都没有）。

    不是脚本出错，所以不该记成失败、更不该中断整批；单独标出来跳过就行。
    """


class AbRunner(StateMixin):
    def __init__(self, settings: dict, form_cfg: dict, ui: BaseUI | None = None):
        self.s = settings
        self.f = form_cfg
        self.ui = ui or ConsoleUI()
        self.shot_dir = Path(settings["screenshot_dir"])
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self._init_state()
        self.auto = False
        self.scope = (settings.get("ab_scope") or form_cfg.get("scope") or SCOPE_MINE)
        if self.scope not in SCOPE_LABELS:
            raise FillError(f"不认识的延期范围「{self.scope}」，可选：{list(SCOPE_LABELS)}")


    # ---------------- 预检 ----------------
    def preview(self) -> list[PreviewRow]:
        """mine：读「我的实验」里状态为「实验中」的实验。

        id_list：把清单和页面对一遍 —— 搜不到的 ID 在这一步就标红，
        不用等跑到一半才发现。
        """
        with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
            self._open_list(b.page)
            if self.scope == SCOPE_ID_LIST:
                targets = self._collect_by_search(b.page, self._load_wanted())
                self._clear_search(b.page)
            else:
                targets = self._all_active_targets(b.page)

        rows = []
        for i, target in enumerate(targets, 1):
            header = {"实验名称": target["name"],
                      "实验ID": target["id"],
                      "状态": target["status"],
                      "当前到期日": target["end_date"]}
            if target.get("want_date_raw"):
                header["清单指定延期至"] = target["want_date_raw"]
            rows.append(PreviewRow(
                index=i,
                name=target["name"],
                kind=SCOPE_LABELS[self.scope],
                detail_count=0,
                issues=list(target.get("issues") or []),
                done=self.state.is_done(target["key"]),
                # 与 Gui 的通用详情弹窗保持兼容
                payload={"header": header, "items": []},
            ))
        return rows

    def _load_wanted(self) -> list[dict]:
        from .ab_data import load as load_list

        path = self.s.get("data_file")
        if not path:
            raise FillError("「按清单指定实验ID」需要先选一个实验清单文件（Excel/CSV）")
        rows = load_list(path)
        self.ui.log(f"实验清单：{Path(path).name}，共 {len(rows)} 行")
        return rows

    def _collect_by_search(self, page, wanted: list[dict]) -> list[dict]:
        """清单模式：逐个 ID 用页面搜索框查，查到就带上目标日期，查不到就标红。

        ⚠ 这里才需要搜索：点名的实验可能排在第几十页，翻过去要等很久；
          而搜索框支持按实验ID精确命中，一次就到。
        """
        out = []
        for n, w in enumerate(wanted, 1):
            if w["issues"]:
                out.append(self._missing(w, w["issues"][0]))
                continue
            self.ui.log(f"[{n}/{len(wanted)}] 查实验 {w['id']}")
            try:
                hit = self._search_for(page, w["id"])
            except Exception as e:
                out.append(self._missing(w, f"搜索「{w['id']}」时出错：{e}"))
                continue
            if hit is None:
                out.append(self._missing(
                    w, f"页面上搜不到实验ID「{w['id']}」（确认 ID 没写错，以及这个实验还在不在）"))
                continue
            target = dict(hit)
            target["page"] = 1              # 搜索后目标就在第一页，不需要翻页
            target["want_date"] = w["date"]
            target["want_date_raw"] = w["date_raw"]
            target["issues"] = []
            out.append(target)
        return out

    def _missing(self, w: dict, why: str) -> dict:
        return {"key": w["id"] or f"row{w['row']}", "id": w["id"], "name": w["name"] or w["id"],
                "status": "", "end_date": "", "page": 1,
                "want_date": w.get("date"), "want_date_raw": w.get("date_raw", ""),
                "issues": [why]}

    # ---------------- 主流程 ----------------
    def run(self, records: list[dict] | None = None):
        # GUI 每次开跑都会新建执行器，preview 的缓存不在这个实例里，
        # 所以勾选范围只从 records 的实验 ID 还原，不依赖任何预检缓存。
        wanted = None
        if records is not None:
            wanted = {str(r.get("header", {}).get("实验ID"))
                      for r in records if r.get("header", {}).get("实验ID")}
            wanted = wanted or None

        dry = bool(self.s.get("dry_run"))
        stats = {"ok": 0, "failed": 0, "skipped": 0, "dry": 0}
        results = []

        try:
            with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
                self._open_list(b.page)
                if self.scope == SCOPE_ID_LIST:
                    targets = [t for t in self._collect_by_search(b.page, self._load_wanted())
                               if not t.get("issues")]
                else:
                    # 先完整扫描分页，随后按页处理。不能只处理第一页，
                    # 否则会漏掉后面的实验中实验。
                    targets = self._all_active_targets(b.page)
                if wanted is not None:
                    targets = [t for t in targets if t["id"] in wanted]

                total = len(targets)
                self.ui.log(f"「{self.f['name']}」范围：{SCOPE_LABELS[self.scope]}，"
                            f"待处理 {total} 个实验" +
                            ("（试跑，不提交）" if dry else ""))
                self.ui.progress(0, total, stats)

                # id_list 的目标是搜出来的，都记在第 1 页；靠逐个搜索定位，不翻页
                for page_no in sorted({t["page"] for t in targets}):
                    if self.scope != SCOPE_ID_LIST:
                        self._goto_page(b.page, page_no)
                    for i, target in [(i, t) for i, t in enumerate(targets, 1)
                                      if t["page"] == page_no]:
                        if self.state.is_done(target["key"]):
                            self.ui.log(f"[{i}/{total}] {target['name']} 已完成过，跳过")
                            continue
                        self.ui.checkpoint()
                        label = f"[{i}/{total}]"
                        try:
                            if self.scope == SCOPE_ID_LIST:
                                # 上一条处理完搜索框还停在上一个 ID 上，这里重新定位
                                if self._search_for(b.page, target["id"]) is None:
                                    raise FillError(f"搜不到实验ID「{target['id']}」了")
                            self._open_extension(b.page, target)
                            picked = self._pick_date(b.page, target)

                            # 已经顶在上限上：最晚可选日就是现在的到期日，提交等于空操作
                            # （平台多半还会因为日期没变而报错），直接跳过更干净。
                            if self._no_gain(target["end_date"], picked):
                                self._close_dialog(b.page)
                                stats["skipped"] += 1
                                results.append(self._result(
                                    i, target, "no_change", "已经是最晚可选日期，无需续期", picked))
                                self.ui.log(f"{label} {target['name']} 已是最晚可选日期"
                                            f"（{picked}），跳过", "warn")
                                continue

                            self._screenshot(b.page, i, "ready")
                            self.ui.log(
                                f"{label} {target['name']} 到期日 {target['end_date']} "
                                f"→ 已选：{picked}", "ok")

                            action = "submit" if (dry or self.auto) else self.ui.confirm(label, target["name"])
                            if action == "auto":
                                self.auto, action = True, "submit"
                            if action == "stop":
                                return results
                            if action == "skip":
                                self._close_dialog(b.page)
                                stats["skipped"] += 1
                                results.append(self._result(i, target, "skipped", "用户跳过", picked))
                                continue
                            if dry:
                                self._close_dialog(b.page)
                                stats["dry"] += 1
                                results.append(self._result(i, target, "dry_run", "未提交", picked))
                                continue

                            self._submit(b.page)
                            self.state.mark_done(target["key"])
                            stats["ok"] += 1
                            results.append(self._result(i, target, "ok", "", picked))
                            self.ui.log(f"{label} {target['name']} 已续期到 {picked}", "ok")
                        except Stopped:
                            raise
                        except NotExtendable as e:
                            # 平台不让续了，不是脚本的错，跳过就好，别中断整批
                            self._close_dialog(b.page)
                            stats["skipped"] += 1
                            results.append(self._result(i, target, "not_extendable", str(e), ""))
                            self.ui.log(f"{label} {target['name']} {e}，跳过", "warn")
                            continue
                        except Exception as e:
                            msg = str(e)
                            log.exception("AB 续期失败：%s", target["name"])
                            shot = self._screenshot(b.page, i, "error")
                            self.state.mark_failed(target["key"], target["name"], msg)
                            stats["failed"] += 1
                            results.append(self._result(i, target, "failed", msg, ""))
                            self.ui.log(f"{label} {target['name']} 失败：{msg}", "error")
                            self.ui.log(f"    错误截图：{shot}")
                            self._close_dialog(b.page)
                            if not self.ui.ask_continue(msg):
                                return results
                        finally:
                            self.ui.progress(i, total, stats)

                # 别把页面留在「只剩最后搜的那一条」的状态
                if self.scope == SCOPE_ID_LIST:
                    self._clear_search(b.page)
        except Stopped:
            self.ui.log("已停止", "warn")
        finally:
            self._write_results(results)
            self._report(stats, results, dry)
        return results

    @staticmethod
    def _no_gain(old: str, picked: str) -> bool:
        """选出来的最晚日期并不比现在的到期日更晚。

        两边都必须是 YYYY-MM-DD 才比较；格式对不上时一律当作「有变化」，
        宁可多提交一次，也不要因为解析失败把该续的实验静默跳过。
        """
        date = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        if not (date.match(old or "") and date.match(picked or "")):
            return False
        return picked <= old

    # ---------------- 页面操作 ----------------
    def _open_list(self, page):
        page.goto(self.f["form_url"], wait_until="domcontentloaded")
        page.wait_for_selector(self.f.get("ready_selector", ".el-table"), state="visible")
        page.wait_for_timeout(self.f.get("after_open_wait", 1500))
        self._select_my_experiments(page)

    def _select_my_experiments(self, page):
        """点「我的实验」把列表收敛到自己创建的实验。

        这个筛选不进 URL，页面一刷新就没了，所以每次打开列表都要点一次。
        它不是开关，重复点仍然是「我的实验」，不会被反选回全量。
        """
        text = self.f.get("my_experiment_text", "我的实验")
        for selector in self.f.get("my_experiment_selectors", []):
            buttons = page.locator(selector)
            for i in range(buttons.count()):
                button = buttons.nth(i)
                if button.is_visible():
                    button.click()
                    page.wait_for_timeout(self.f.get("after_filter_wait", 2000))
                    return
        # 退回按文案找，页面改版换了 class 时还能撑住
        loc = page.get_by_text(text, exact=True)
        for i in range(loc.count()):
            if loc.nth(i).is_visible():
                loc.nth(i).click()
                page.wait_for_timeout(self.f.get("after_filter_wait", 2000))
                return
        raise FillError(f"列表页找不到「{text}」筛选按钮；请更新 my_experiment_selectors")

    def _search_box(self, page):
        for selector in self.f.get("search_input_selectors", []):
            loc = page.locator(selector)
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    return loc.nth(i)
        return None

    def _search_for(self, page, keyword: str):
        """用搜索框按实验ID定位，命中就返回那一行的 target，搜不到返回 None。"""
        box = self._search_box(page)
        if box is None:
            raise FillError("列表页找不到搜索框；请更新 search_input_selectors")
        box.fill("")
        box.fill(str(keyword))
        box.press("Enter")
        page.wait_for_timeout(self.f.get("search_wait", 2500))

        # 搜索是模糊匹配，可能带出名字里含这串数字的实验，所以按 ID 精确认领
        for target in self._scan_page(page)[0] + self._inactive_on_page(page):
            if target["id"] == str(keyword):
                return target
        return None

    def _inactive_on_page(self, page) -> list[dict]:
        """搜索结果里状态不是「实验中」的行。

        清单模式是用户点名的，不该因为状态不符就当作「搜不到」而误导人；
        搜到了但状态不对，要明确告诉他这个实验不是实验中。
        """
        rows = self._table(page).locator(self.f.get("row_selector", "tbody tr"))
        name_col = int(self.f.get("name_column", 0))
        status_col = int(self.f.get("status_column", 1))
        active_text = self.f.get("active_status", "实验中")
        id_pattern = self.f.get("id_pattern", r"ID[:：]\s*(\d+)")
        out = []
        for i in range(rows.count()):
            row = rows.nth(i)
            if not row.is_visible():
                continue
            cells = [x.strip() for x in row.locator("td").all_inner_texts()]
            if len(cells) <= status_col or cells[status_col] == active_text:
                continue
            raw_name = cells[name_col] if len(cells) > name_col else ""
            found = re.search(id_pattern, raw_name)
            if not found:
                continue
            out.append({
                "key": found.group(1),
                "id": found.group(1),
                "name": (raw_name.splitlines() or [""])[0].strip(),
                "status": cells[status_col],
                "end_date": "",
                "issues": [f"这个实验现在是「{cells[status_col]}」，不是「{active_text}」，续不了"],
            })
        return out

    def _clear_search(self, page):
        """收尾清空搜索框。

        不清的话页面会一直停在「只剩最后搜的那一条」，下次有人手动打开
        这个页面会以为实验没了。
        """
        try:
            box = self._search_box(page)
            if box is None or not (box.input_value() or "").strip():
                return
            box.fill("")
            box.press("Enter")
            page.wait_for_timeout(self.f.get("search_wait", 2500))
        except Exception:
            pass

    def _table(self, page):
        """页面里有多张表（实验管理/联调实验/发布管理），只认当前可见那张。"""
        tables = page.locator(self.f.get("table_selector", ".el-table"))
        fallback = None
        for i in range(tables.count()):
            table = tables.nth(i)
            if not table.is_visible():
                continue
            if table.locator(self.f.get("row_selector", "tbody tr")).count():
                return table
            fallback = fallback or table
        if fallback is not None:
            return fallback
        raise FillError("列表页找不到可见的实验表格")

    def _scan_page(self, page) -> tuple[list[dict], bool]:
        """扫当前页，返回（本页的「实验中」记录，本页是否出现了非「实验中」的行）。

        列表是按状态分组排的，「实验中」全部排在最前面，所以一旦扫到一行不是
        「实验中」，后面的行和后面所有页就都不会再有了 —— 扫描到此为止即可，
        不用把 18 页全翻一遍。
        """
        rows = self._table(page).locator(self.f.get("row_selector", "tbody tr"))
        count = rows.count()
        active_text = self.f.get("active_status", "实验中")
        name_col = int(self.f.get("name_column", 0))
        status_col = int(self.f.get("status_column", 1))
        date_col = int(self.f.get("end_date_column", 3))
        id_pattern = self.f.get("id_pattern", r"ID[:：]\s*(\d+)")
        out = []
        hit_inactive = False
        for i in range(count):
            row = rows.nth(i)
            if not row.is_visible():
                continue
            cells = [x.strip() for x in row.locator("td").all_inner_texts()]
            if len(cells) <= status_col:
                continue
            # 按列精确匹配，不用整行包含：实验名里带「实验中」不该被选进来
            if cells[status_col] != active_text:
                hit_inactive = True
                break
            raw_name = cells[name_col] if len(cells) > name_col else ""
            name = (raw_name.splitlines() or [""])[0].strip()
            found = re.search(id_pattern, raw_name)
            exp_id = found.group(1) if found else ""
            end_date = ""
            if len(cells) > date_col:
                parts = [x.strip() for x in cells[date_col].splitlines() if x.strip()]
                end_date = parts[-1] if parts else ""
            out.append({
                "key": exp_id or f"{name}|{i}",
                "id": exp_id,
                "name": name,
                "status": active_text,
                "end_date": end_date,
            })
        return out, hit_inactive

    def _all_active_targets(self, page) -> list[dict]:
        """逐页扫描，扫到第一个非「实验中」就收工。翻页后只保留没见过的实验 ID。"""
        targets, seen, page_no = [], set(), 1
        max_pages = int(self.f.get("max_scan_pages", 40))
        while True:
            found, hit_inactive = self._scan_page(page)
            for target in found:
                if target["key"] in seen:
                    continue
                seen.add(target["key"])
                target["page"] = page_no
                targets.append(target)
            if hit_inactive:
                self.ui.log(f"第 {page_no} 页已经出现非「"
                            f"{self.f.get('active_status', '实验中')}」的实验，"
                            f"后面都不会再有了，停止翻页")
                return targets
            if page_no >= max_pages or not self._advance_page(page):
                return targets
            page_no += 1

    def _goto_page(self, page, page_no: int):
        self._open_list(page)
        for _ in range(1, page_no):
            if not self._advance_page(page):
                raise FillError(f"列表只有不足 {page_no} 页，无法定位目标实验")

    def _advance_page(self, page) -> bool:
        """翻到下一页；未找到可用的下一页按钮代表已经是最后一页。"""
        for selector in self.f.get("next_page_selectors", []):
            buttons = page.locator(selector)
            for i in range(buttons.count()):
                button = buttons.nth(i)
                if not button.is_visible() or not button.is_enabled():
                    continue
                if button.get_attribute("disabled") is not None:
                    continue
                if button.get_attribute("aria-disabled") == "true":
                    continue
                if "disabled" in (button.get_attribute("class") or ""):
                    continue
                button.click()
                page.wait_for_timeout(self.f.get("page_wait", 1200))
                return True
        return False

    def _row_for(self, page, target: dict):
        rows = self._table(page).locator(self.f.get("row_selector", "tbody tr"))
        id_pattern = self.f.get("id_pattern", r"ID[:：]\s*(\d+)")
        name_col = int(self.f.get("name_column", 0))
        for i in range(rows.count()):
            row = rows.nth(i)
            if not row.is_visible():
                continue
            cells = row.locator("td")
            if cells.count() <= name_col:
                continue
            raw = cells.nth(name_col).inner_text() or ""
            found = re.search(id_pattern, raw)
            if target["id"] and found and found.group(1) == target["id"]:
                return row
            if not target["id"] and target["name"] and target["name"] in raw:
                return row
        raise FillError(f"列表中找不到实验「{target['name']}」(ID:{target['id']})")

    @staticmethod
    def _poll(page, find, timeout_ms: int, step: int = 100):
        """轮询直到 find() 返回非 None。

        ⚠ 下拉菜单和弹窗都有进场动画，点完立刻枚举经常扑空（元素还没挂上来，
          或者还没进入可见状态）。所有「点完等它出来」的地方都必须走这里，
          不能用固定 sleep 赌渲染速度。
        """
        waited = 0
        while True:
            got = find()
            if got is not None:
                return got
            if waited >= timeout_ms:
                return None
            page.wait_for_timeout(step)
            waited += step

    def _open_extension(self, page, target: dict):
        """点开这一行操作列的「其他」，再点菜单里的「续期」。"""
        row = self._row_for(page, target)
        last_cell = row.locator("td").last
        trigger = None
        for selector in self.f.get("more_menu_selectors", []):
            loc = last_cell.locator(selector)
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    trigger = loc.nth(i)
                    break
            if trigger is not None:
                break
        if trigger is None:
            more_text = self.f.get("more_menu_text", "其他")
            loc = last_cell.get_by_text(more_text, exact=True)
            if loc.count() and loc.first.is_visible():
                trigger = loc.first
        if trigger is None:
            raise FillError(f"「{target['name']}」行找不到操作列的「"
                            f"{self.f.get('more_menu_text', '其他')}」入口")
        trigger.click()

        # 每一行在 DOM 里都有自己的菜单，只有刚展开那个是可见的
        item_text = self.f.get("extension_menu_item", "续期")
        selector = self.f.get("menu_item_selector", ".el-dropdown-menu__item")

        def find_item():
            items = page.locator(selector)
            for i in range(items.count()):
                item = items.nth(i)
                if item.is_visible() and item.inner_text().strip() == item_text:
                    return item
            return None

        item = self._poll(page, find_item, int(self.f.get("menu_wait", 3000)))
        if item is None:
            raise FillError(f"「{target['name']}」的菜单里找不到「{item_text}」")
        if "is-disabled" in (item.get_attribute("class") or ""):
            raise FillError(f"「{target['name']}」的「{item_text}」是禁用状态")
        item.click()
        page.wait_for_timeout(self.f.get("after_menu_wait", 1200))

    def _visible_dialog(self, page):
        loc = page.locator(self.f.get("dialog_selector", ".el-dialog"))
        for i in range(loc.count()):
            if loc.nth(i).is_visible():
                return loc.nth(i)
        return None

    def _dialog(self, page):
        dialog = self._poll(page, lambda: self._visible_dialog(page),
                            int(self.f.get("dialog_wait", 4000)))
        if dialog is None:
            raise FillError(f"没等到「{self.f.get('dialog_title', '实验续期')}」弹窗")
        return dialog

    def _avail_cells(self, page) -> list:
        """读当前显示月里可选的日期格。

        ⚠ 面板刚挂载/刚翻月时会有一小段空窗期，这时读到的是 0 个可选日期。
          之前就是因为直接采信了这个 0，把「还没渲染完」误判成「不能续期」，
          所以读到空必须重试几轮再下结论。
        """
        selector = self.f["date_available_selector"]
        retries = int(self.f.get("empty_read_retries", 3))
        for attempt in range(retries + 1):
            loc = page.locator(selector)
            cells = [loc.nth(i) for i in range(loc.count()) if loc.nth(i).is_visible()]
            if cells or attempt == retries:
                return cells
            page.wait_for_timeout(self.f.get("empty_read_wait", 350))
        return []

    def _month_state(self, page):
        """返回（本月可选日期格，本月最后一天是否还可选）。

        本月最后一天已经变灰，就说明可选范围的上限落在本月，不用再往后翻月。
        ⚠ 不能只看「本月有没有灰格子」：当月今天之前的日期本来就是灰的，
          那种灰不代表到顶了，所以判据必须是「最后一天」而不是「有没有」。
        """
        avail = self._avail_cells(page)
        loc = page.locator(self.f.get(
            "month_cells_selector",
            ".el-date-table td:not(.prev-month):not(.next-month)"))
        last = None
        for i in range(loc.count() - 1, -1, -1):
            if loc.nth(i).is_visible():
                last = loc.nth(i)
                break
        last_open = last is not None and "disabled" not in (last.get_attribute("class") or "")
        return avail, last_open

    def _month_label(self, page) -> str:
        loc = page.locator(self.f.get("month_label_selector", ".el-date-picker__header-label"))
        parts = [loc.nth(i).inner_text().strip() for i in range(loc.count())
                 if loc.nth(i).is_visible()]
        return " ".join(parts)

    def _step_month(self, page, selectors: list[str]) -> bool:
        """翻一个月。等标题真的变了再返回，不靠固定 sleep 赌渲染速度。"""
        before = self._month_label(page)
        for selector in selectors:
            loc = page.locator(selector)
            for i in range(loc.count()):
                button = loc.nth(i)
                if not (button.is_visible() and button.is_enabled()):
                    continue
                button.click()
                deadline = int(self.f.get("month_wait", 500)) + 1500
                waited = 0
                while waited < deadline:
                    page.wait_for_timeout(100)
                    waited += 100
                    if self._month_label(page) != before:
                        return True
                return True
        return False

    def _date_input(self, dialog):
        for selector in self.f.get("date_input_selectors", ["input"]):
            loc = dialog.locator(selector)
            for i in range(loc.count()):
                if loc.nth(i).is_visible() and loc.nth(i).is_enabled():
                    return loc.nth(i)
        raise FillError("续期弹窗里找不到到期日期输入框")

    def _month_ym(self, page) -> tuple[int, int]:
        """把「2026 年 11 月」解析成 (2026, 11)。"""
        nums = re.findall(r"\d+", self._month_label(page))
        if len(nums) < 2:
            raise FillError(f"读不出日期面板的年月：{self._month_label(page)!r}")
        return int(nums[0]), int(nums[1])

    def _goto_month(self, page, year: int, month: int) -> bool:
        """把面板翻到指定年月。"""
        for _ in range(int(self.f.get("max_month_lookahead", 12)) * 2 + 2):
            cy, cm = self._month_ym(page)
            if (cy, cm) == (year, month):
                return True
            delta = (year - cy) * 12 + (month - cm)
            sel = (self.f.get("next_month_selectors", []) if delta > 0
                   else self.f.get("prev_month_selectors", []))
            if not self._step_month(page, sel):
                return False
        return False

    def _avail_cell_for_day(self, page, day: int):
        for cell in self._avail_cells(page):
            if (cell.inner_text() or "").strip() == str(day):
                return cell
        return None

    def _pick_date(self, page, target: dict) -> str:
        """选到期日。

        清单里写了「延期至」就尽量选那天；那天超过平台上限（或平台不给选）时，
        自动收敛到最晚可选日期 —— 这是模板里向用户承诺过的行为。
        没写「延期至」就直接取最晚。
        """
        dialog = self._dialog(page)
        date_input = self._date_input(dialog)

        # ⚠ 原到期日以弹窗里的初始值为准。搜索结果页的「开始/结束时间」列
        #   渲染不全（两行会显示成同一个日期），拿它判断会误判成「无需续期」。
        original = (date_input.input_value() or "").strip()
        if original:
            target["end_date"] = original

        cells = self._seek_latest_month(page, date_input)
        want = target.get("want_date")
        if want is not None:
            cy, cm = self._month_ym(page)
            latest = date(cy, cm, int((cells[-1].inner_text() or "0").strip() or 0))
            if want < latest:
                if self._goto_month(page, want.year, want.month):
                    cell = self._avail_cell_for_day(page, want.day)
                    if cell is not None:
                        cell.click()
                        page.wait_for_timeout(self.f.get("month_wait", 500))
                        return (date_input.input_value() or "").strip() or fmt_date(want)
                self.ui.log(f"清单指定的 {fmt_date(want)} 在平台上不可选，"
                            f"改用最晚可选日期 {fmt_date(latest)}", "warn")
                if not self._goto_month(page, latest.year, latest.month):
                    raise FillError("日期面板翻回上限月失败")
                cells = self._avail_cells(page)

        if not cells:
            raise FillError(f"{self._month_label(page)} 没有可选日期")
        cells[-1].click()
        page.wait_for_timeout(self.f.get("month_wait", 500))
        # 输入框回填的才是真正生效的日期，比读单元格文本可靠
        value = (date_input.input_value() or "").strip()
        return value or f"{self._month_label(page)} 最后一天"

    def _seek_latest_month(self, page, date_input) -> list:
        """把面板停在「上限所在月」，返回该月的可选日期格。

        可选范围有上限，而且上限可能落在当前显示月之后，所以先往后翻月探路，
        记住最后一个还有可选日期的月份，再退回那个月。
        """
        date_input.click()
        # 先等日期面板真的挂上来，再读可选日期
        page.wait_for_selector(self.f.get("panel_ready_selector", ".el-date-table"),
                               state="visible")
        page.wait_for_timeout(self.f.get("panel_wait", 900))

        max_fwd = int(self.f.get("max_month_lookahead", 12))
        stop_after = int(self.f.get("stop_after_empty_months", 1))
        next_sel = self.f.get("next_month_selectors", [])
        prev_sel = self.f.get("prev_month_selectors", [])

        # best = 最后一个「有可选日期」的月份距初始月的步数；-1 = 还没见过
        best, steps, empty_streak = -1, 0, 0
        while True:
            avail, last_open = self._month_state(page)
            if avail:
                best, empty_streak = steps, 0
                # 本月末尾已经变灰 -> 上限就在本月，没必要再往后翻
                if not last_open:
                    break
            else:
                empty_streak += 1
                # 之前已经找到过可选月，现在开始空了，说明到头了
                if best >= 0 or empty_streak >= stop_after:
                    break
            if steps >= max_fwd or not self._step_month(page, next_sel):
                break
            steps += 1

        if best < 0:
            raise NotExtendable("平台没给出任何可选到期日，该实验已不能再续期")

        for _ in range(steps - best):
            if not self._step_month(page, prev_sel):
                raise FillError("日期面板回退月份失败；请检查 prev_month_selectors")

        cells = self._avail_cells(page)
        if not cells:
            raise FillError(f"回退到 {self._month_label(page)} 后反而没有可选日期了")
        return cells

    def _submit(self, page):
        """点「续期」，再点二次确认气泡里的「确定」。

        ⚠ 弹窗上的「续期」不是终点：点完平台还会在按钮边上弹一个
          「确定续期吗?」的小气泡（el-popconfirm），不点它这次续期根本不生效。
        """
        dialog = self._dialog(page)
        texts = self.f.get("submit_texts", ["续期"])
        for text in texts:
            button = dialog.get_by_text(text, exact=True)
            for i in range(button.count()):
                if button.nth(i).is_visible():
                    button.nth(i).click()
                    self._confirm_popup(page)
                    page.wait_for_timeout(self.f.get("after_submit_wait", 1500))
                    self._check_result(page)
                    return
        raise FillError(f"续期弹窗里找不到提交按钮：{'、'.join(texts)}")

    def _confirm_popup(self, page):
        """点掉「确定续期吗?」气泡里的「确定」。气泡没出现就当这一步不存在。"""
        selectors = self.f.get("popconfirm_ok_selectors", [])

        def find_ok():
            for selector in selectors:
                loc = page.locator(selector)
                for i in range(loc.count()):
                    if loc.nth(i).is_visible():
                        return loc.nth(i)
            return None

        ok = self._poll(page, find_ok, int(self.f.get("popconfirm_wait", 3000)))
        if ok is None:
            return
        ok.click()
        page.wait_for_timeout(self.f.get("after_confirm_wait", 800))

    def _check_result(self, page):
        errors = []
        for selector in self.f.get("error_selectors", []):
            loc = page.locator(selector)
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    text = (loc.nth(i).inner_text() or "").strip()
                    if text:
                        errors.append(text)
        if errors:
            raise FillError("续期被页面拒绝：" + "；".join(dict.fromkeys(errors)))
        # 没有错误提示时，弹窗还开着同样说明没提交成功。关闭有动画，轮询等一会儿再下结论。
        gone = self._poll(page,
                          lambda: True if self._visible_dialog(page) is None else None,
                          int(self.f.get("dialog_close_wait", 3000)))
        if gone is None:
            raise FillError("点了「续期」但弹窗没关闭，判定为未生效")

    def _close_dialog(self, page):
        # ⚠ 日期面板是浮层，会盖住弹窗底部的「取消」，直接点会被它拦下来。
        #   所以先 Escape 收掉面板，再点取消。
        if page.locator(self.f.get("panel_ready_selector", ".el-date-table")).count():
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(400)
            except Exception:
                pass
        for text in self.f.get("cancel_texts", ["取消", "关闭"]):
            try:
                button = page.get_by_text(text, exact=True)
                for i in range(button.count() - 1, -1, -1):
                    if button.nth(i).is_visible():
                        button.nth(i).click(timeout=self.f.get("cancel_click_timeout", 3000))
                        page.wait_for_timeout(400)
                        return
            except Exception:
                pass
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

    # ---------------- 输出 ----------------
    def _screenshot(self, page, idx: int, tag: str) -> str:
        path = self.shot_dir / f"{self.f['name']}_{idx:04d}_{tag}_{datetime.now():%H%M%S}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:
            return "(截图失败)"

    @staticmethod
    def _result(i, target, status, error, picked):
        return {"序号": i, "实验名称": target["name"], "实验ID": target["id"],
                "状态": status, "原到期日": target["end_date"],
                "续期至": picked, "错误": error}

    def _write_results(self, results):
        if not results:
            return
        path = Path(self.s["result_file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(results[0]))
            writer.writeheader()
            writer.writerows(results)

    def _report(self, stats, results, dry):
        lines = [f"配置类型：{self.f['name']}", f"延期范围：{SCOPE_LABELS[self.scope]}"]
        lines.append(f"试跑 {stats['dry']} 个" if dry else f"成功 {stats['ok']} 个")
        if stats["skipped"]:
            lines.append(f"跳过 {stats['skipped']} 个")
        if stats["failed"]:
            lines.append(f"失败 {stats['failed']} 个")
        lines += ["", f"明细：{self.s['result_file']}", f"截图：{self.s['screenshot_dir']}"]
        self.ui.finished("AB实验延期完成" if not stats["failed"] else "AB实验延期完成（有失败）",
                         "\n".join(lines), not stats["failed"])
