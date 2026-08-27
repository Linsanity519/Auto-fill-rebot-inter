"""DMP 人群延期执行器。

页面：人群管理列表 → 操作 → 人群延期 → 人群有效期至 → 选日期 → 保存。

三种延期范围（yaml 的 scope / 界面上的「延期范围」，默认 active＝老行为不变）：

  active   所有「生效中」人群  → 延到系统允许的最晚日期      （原有功能）
  id_list  Excel 清单里指定的人群ID → 延到清单里写的日期；
           日期超过系统上限时自动改成上限；留空同样取上限
  mine     所有「我创建的」人群 → 延到系统允许的最晚日期

每完成一个人群会等 after_each_wait（默认 5 秒）再做下一个，给系统反应时间。

页面选择器集中在 config/forms/DMP延期.yaml。页面改版后优先改配置，不要把
业务 DOM 细节散落到流程代码中。
"""
from __future__ import annotations

import csv
import logging
import re
from datetime import datetime
from pathlib import Path

from .browser import Browser
from .dmp_date import DateError, DatePanel, fmt, parse_date
from .filler import FillError
from .preview import PreviewRow
from .runstate import StateMixin
from .ui import BaseUI, ConsoleUI, Stopped

log = logging.getLogger(__name__)

SCOPE_ACTIVE = "active"
SCOPE_ID_LIST = "id_list"
SCOPE_MINE = "mine"
SCOPE_LABELS = {
    SCOPE_ACTIVE: "全部生效中人群",
    SCOPE_ID_LIST: "指定人群ID",
    SCOPE_MINE: "我创建的人群",
}


class DmpRunner(StateMixin):
    def __init__(self, settings: dict, form_cfg: dict, ui: BaseUI | None = None):
        self.s = settings
        self.f = form_cfg
        self.ui = ui or ConsoleUI()
        self.shot_dir = Path(settings["screenshot_dir"])
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self._init_state()
        self.auto = False
        self._max_limit = None      # 本次运行算出的系统最晚可选日期，算一次全程复用
        # 界面/命令行传进来的优先；都没有就用 yaml 里的；再没有就是老行为
        self.scope = (settings.get("dmp_scope") or self.f.get("scope") or SCOPE_ACTIVE)
        if self.scope not in SCOPE_LABELS:
            raise FillError(f"不认识的延期范围「{self.scope}」，可选：{list(SCOPE_LABELS)}")


    # ---------------- 预检 ----------------
    def preview(self) -> list[PreviewRow]:
        """读取当前列表，按延期范围筛出要处理的人群。

        id_list 模式会把清单和页面对一遍：页面上没有的 ID 在这里就标红，
        不会等跑到一半才发现。
        """
        wanted = self._load_wanted() if self.scope == SCOPE_ID_LIST else None

        with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
            self._open_list(b.page)
            if self.scope == SCOPE_ID_LIST:
                targets = self._collect_by_search(b.page, wanted)
            else:
                self._apply_scope_filter(b.page)
                targets = self._all_targets(b.page)

        self._targets = targets

        rows = []
        for i, target in enumerate(targets, 1):
            want = target.get("want_date")
            rows.append(PreviewRow(
                index=i,
                name=target["name"] or target.get("id") or "(未命名)",
                kind=SCOPE_LABELS[self.scope],
                detail_count=0,
                issues=list(target.get("issues") or []),
                done=self.state.is_done(target["key"]),
                # 与 Gui 的通用详情弹窗保持兼容
                payload={
                    "header": {
                        "人群ID": target.get("id", ""),
                        "人群名称": target["name"],
                        "状态": target.get("status", ""),
                        "延期至": fmt(want) if want else "系统最晚可选日期",
                    },
                    "items": [],
                },
            ))
        return rows

    def _load_wanted(self) -> list[dict]:
        from .dmp_data import load as load_list

        path = self.s.get("data_file")
        if not path:
            raise FillError("「指定人群ID」需要先选一个人群清单文件（Excel/CSV）")
        rows = load_list(path)
        self.ui.log(f"人群清单：{Path(path).name}，共 {len(rows)} 行")
        return rows

    def _collect_by_search(self, page, wanted: list[dict]) -> list[dict]:
        """清单模式：逐个 ID 用页面搜索框查，查到就带上目标日期，查不到就标红。"""
        out = []
        for n, w in enumerate(wanted, 1):
            if not w["id"]:
                out.append(self._missing(w, "人群ID 为空，这一行没法执行"))
                continue
            self.ui.log(f"[{n}/{len(wanted)}] 查人群 {w['id']}")
            try:
                hit = self._search_for(page, w["id"])
            except Exception as e:
                out.append(self._missing(w, f"搜索「{w['id']}」时出错：{e}"))
                continue
            if hit is None:
                out.append(self._missing(
                    w, f"页面上搜不到人群ID「{w['id']}」（确认 ID 没写错，以及这个人群还在不在）"))
                continue

            target = dict(hit)
            target["page"] = 1          # 搜索后目标就在第一页，不需要翻页
            target["want_date"] = w["date"]
            target["issues"] = list(w["issues"])

            # ⚠ 已失效的人群，操作菜单里压根没有「人群延期」这一项（实测：生效中 8 项，
            #   已失效只有 6 项）。这是系统限制，延不了。预检就说清楚，
            #   别等跑到一半报「菜单展不开」让人去翻截图。
            blocked = self.f.get("non_extendable_status") or []
            if target.get("status") in blocked:
                target["issues"].append(
                    f"这个人群是「{target['status']}」，系统不提供延期（操作菜单里没有「人群延期」）")
            # ⚠ 名称对不上只是提醒，不能当成拦截理由：程序本来就按 ID 定位，
            #   清单里的名称列只是给人看的。放进 issues 会让这条被直接跳过。
            if w.get("name") and target["name"] and w["name"] != target["name"]:
                self.ui.log(f"人群 {w['id']} 清单里写的是「{w['name']}」，"
                            f"页面上是「{target['name']}」，按 ID 执行", "warn")
            out.append(target)
        return out

    @staticmethod
    def _missing(w: dict, why: str) -> dict:
        return {"key": f"missing|{w['id']}|{w['row']}", "id": w["id"],
                "name": w.get("name") or w["id"] or f"第{w['row']}行",
                "status": "", "page": 0, "cells": [], "creator": "",
                "want_date": w["date"], "issues": list(w["issues"]) + [why]}

    @staticmethod
    def _same(target: dict, cid: str) -> bool:
        """判断页面上这一行是不是清单里的这个 ID。

        ⚠ 只做整格全等，不拿 ID 去 row_text 里做子串匹配 ——
          「35697」会命中覆盖人数「356970」那一列，延错人群比延不到严重得多。
        """
        cid = str(cid).strip()
        if not cid:
            return False
        if str(target.get("id", "")).strip() == cid:
            return True
        if str(target.get("key", "")).strip() == cid:
            return True
        return any(c.strip() == cid for c in target.get("cells", []))

    # ---------------- 主流程 ----------------
    def run(self, records: list[dict] | None = None):
        # GUI 传来的 records 是 _record；真正的定位信息保留在 preview 缓存里。
        wanted_keys = None
        if records is not None and hasattr(self, "_targets"):
            picked = {(str(r["header"].get("人群ID", "")).strip(),
                       str(r["header"].get("人群名称", "")).strip()) for r in records}
            wanted_keys = picked

        dry = bool(self.s.get("dry_run"))
        stats = {"ok": 0, "failed": 0, "skipped": 0, "dry": 0}
        results = []
        cooldown = int(self.f.get("after_each_wait", 5000))

        ctx = {"dry": dry, "stats": stats, "results": results, "cooldown": cooldown, "i": 0}

        try:
            with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
                self.ui.log(f"「{self.f['name']}」范围：{SCOPE_LABELS[self.scope]}"
                            + ("（试跑，不保存）" if dry else ""))
                if cooldown:
                    self.ui.log(f"每完成一个等待 {cooldown / 1000:.0f} 秒再做下一个")

                if self.scope == SCOPE_ID_LIST:
                    self._run_by_list(b.page, wanted_keys, ctx)
                else:
                    self._run_by_pages(b.page, wanted_keys, ctx)
        except Stopped:
            self.ui.log("已停止", "warn")
        finally:
            self._write_results(results)
            self._report(stats, results, dry)
        return results

    # ---------------- 两种走法 ----------------
    def _run_by_list(self, page, wanted_keys, ctx: dict):
        """清单模式：逐个 ID 用搜索框直达。

        ⚠ 这里才需要搜索：点名的人群可能排在第 19 页，翻过去要等一分多钟。
        """
        self._open_list(page)
        targets = self._collect_by_search(page, self._load_wanted())

        for t in [x for x in targets if x.get("issues")]:
            self.ui.log(f"跳过「{t['name']}」：{t['issues'][0]}", "warn")
        targets = [t for t in targets if not t.get("issues")]
        targets = self._keep_wanted(targets, wanted_keys)

        total = len(targets)
        self.ui.log(f"待处理 {total} 个人群")
        self.ui.progress(0, total, ctx["stats"])

        try:
            for target in targets:
                if self._search_for(page, target.get("id") or target["name"]) is None:
                    self.ui.log(f"搜不到人群「{target.get('id') or target['name']}」了，跳过", "warn")
                    continue
                if self._process_one(page, target, total, ctx) == "stop":
                    return
        finally:
            # 收尾：把搜索框清掉。不清的话页面会一直停在「只剩最后搜的那一个人群」，
            # 下次有人手动打开这个页面会以为人群没了。
            self._clear_search(page)

    def _clear_search(self, page):
        try:
            inp = page.locator(self.f.get("search_input_selector", "")).first
            if not inp.count() or not (inp.input_value() or "").strip():
                return
            inp.fill("")
            btn = page.locator(self.f.get("search_button_selector", "")).first
            if btn.count():
                btn.click()
            else:
                inp.press("Enter")
            page.wait_for_timeout(800)
        except Exception:
            pass

    def _run_by_pages(self, page, wanted_keys, ctx: dict):
        """全量 / 我创建的：顺着列表一页一页往下延，处理完当前页再翻页。

        ⚠ 不预扫、不搜索、不回头：这两种范围本来就是「整页整页地延」，
          先扫一遍再翻回去逐个定位等于把 20 多页翻两遍，纯浪费。
          断点（state）按人群 key 记，中断重跑照样能跳过已完成的。
        """
        self._open_list(page)
        self._apply_scope_filter(page)

        # 进度条的分母用预检时的数量；没走过预检就先不显示总数
        total = len([t for t in getattr(self, "_targets", []) if not t.get("issues")])
        self.ui.log(f"待处理 {total} 个人群" if total else "顺着列表逐页处理")
        self.ui.progress(0, total, ctx["stats"])

        page_no = 1
        while True:
            batch = self._keep_wanted(self._targets_on_page(page), wanted_keys)
            self.ui.log(f"第 {page_no} 页：{len(batch)} 个符合范围")
            for target in batch:
                if self._process_one(page, target, total, ctx) == "stop":
                    return
            if not self._advance_page(page):
                self.ui.log(f"已经是最后一页（共 {page_no} 页）")
                return
            page_no += 1

    def _keep_wanted(self, targets: list[dict], wanted_keys) -> list[dict]:
        """按预检界面上用户勾选/保留的结果过滤。"""
        if wanted_keys is None:
            return targets
        return [t for t in targets
                if (str(t.get("id", "")).strip(), str(t["name"]).strip()) in wanted_keys]

    # ---------------- 单条处理 ----------------
    def _process_one(self, page, target: dict, total: int, ctx: dict) -> str:
        """处理一个人群。返回 'next' 继续，'stop' 中止整轮。

        调用前请确保这一行已经在当前可见的列表里。
        """
        stats, results, dry, cooldown = ctx["stats"], ctx["results"], ctx["dry"], ctx["cooldown"]

        if self.state.is_done(target["key"]):
            self.ui.log(f"{target['name']} 已完成过，跳过")
            return "next"

        self.ui.checkpoint()
        ctx["i"] += 1
        i = ctx["i"]
        label = f"[{i}/{total}]" if total else f"[{i}]"

        try:
            self._open_extension(page, target)
            picked, capped = self._pick_date(page, target)
            self._screenshot(page, i, "ready")

            note = "（超过系统上限，已改为最晚可选日期）" if capped else ""
            self.ui.log(f"{label} {target['name']} 已选日期：{picked}{note}",
                        "warn" if capped else "ok")

            action = "submit" if (dry or self.auto) else self.ui.confirm(label, target["name"])
            if action == "auto":
                self.auto, action = True, "submit"
            if action == "stop":
                return "stop"
            if action == "skip":
                self._close_dialog(page)
                stats["skipped"] += 1
                results.append(self._result(i, target, "skipped", "用户跳过", picked))
                return "next"
            if dry:
                self._close_dialog(page)
                stats["dry"] += 1
                results.append(self._result(i, target, "dry_run", "未确认、未保存", picked))
                self._cooldown(page, cooldown, label)
                return "next"

            self._confirm_extension(page)
            self._save(page)
            self.state.mark_done(target["key"])
            stats["ok"] += 1
            results.append(self._result(i, target, "ok",
                                        "已按系统上限截断" if capped else "", picked))
            self.ui.log(f"{label} {target['name']} 已延期至 {picked} 并保存", "ok")
            self._cooldown(page, cooldown, label)
            return "next"

        except Stopped:
            raise
        except Exception as e:
            msg = str(e)
            log.exception("DMP 延期失败：%s", target["name"])
            shot = self._screenshot(page, i, "error")
            self.state.mark_failed(target["key"], target["name"], msg)
            stats["failed"] += 1
            results.append(self._result(i, target, "failed", msg, ""))
            self.ui.log(f"{label} {target['name']} 失败：{msg}", "error")
            self.ui.log(f"    错误截图：{shot}")
            self._close_dialog(page)
            return "next" if self.ui.ask_continue(msg) else "stop"
        finally:
            self.ui.progress(i, total, stats)

    def _cooldown(self, page, ms: int, label: str):
        """完成一个之后的冷却。分段等，用户点停止时不用干等满 5 秒。"""
        left = ms
        while left > 0:
            self.ui.checkpoint()
            step = min(500, left)
            page.wait_for_timeout(step)
            left -= step

    # ---------------- 页面操作 ----------------
    def _open_list(self, page):
        """打开列表页并等到数据真的渲染出来。

        ⚠ 这是个 SPA：DOM 里先有空的 <table> 骨架，行要等接口回来才渲染，
          实测能差十几秒。只 sleep 一个固定时间会读到 0 行，
          表现为「一个人群都没找到」——所以这里轮询等行出现。
          列表本来就是空的也要能正常结束，所以同时认 antd 的空态。
        """
        page.goto(self.f["form_url"], wait_until="domcontentloaded")
        page.wait_for_selector(self.f.get("ready_selector", "table"), state="visible",
                               timeout=self.s["timeout"])
        if self._wait_rows(page):
            page.wait_for_timeout(self.f.get("after_open_wait", 1200))
            return

        # ⚠ 冷加载（比如换了标签页、刚登录完）偶尔会拖很久。这里刷新重试一次，
        #   而不是直接当成「列表是空的」—— 全量/我创建的范围下，
        #   误判成空列表就是「一个都不处理还不报错」，比报错更难发现。
        self.ui.log("列表没渲染出来，刷新重试一次", "warn")
        page.reload(wait_until="domcontentloaded")
        if not self._wait_rows(page):
            self.ui.log("刷新后列表仍然是空的。如果你确认列表里有人群，"
                        "把 yaml 的 list_ready_timeout 调大再试", "warn")
        page.wait_for_timeout(self.f.get("after_open_wait", 1200))

    def _wait_rows(self, page) -> bool:
        """等数据行渲染出来。返回 True=有行，False=一直没等到行。"""
        row_selector = self.f.get("row_selector", "tbody tr")
        empty_selector = self.f.get("empty_selector", ".full_ogv_data_antd-empty, .ant-empty")
        deadline = int(self.f.get("list_ready_timeout", 30000))
        waited = 0
        while waited < deadline:
            try:
                if page.locator(row_selector).count():
                    return True
                if empty_selector and page.locator(empty_selector).count():
                    self.ui.log("列表是空的（页面显示无数据）", "warn")
                    return True
            except Exception:
                pass
            page.wait_for_timeout(500)
            waited += 500
        return False

    def _apply_scope_filter(self, page):
        """mine 模式：把列表筛成「我创建的」。

        两条路，页面上有哪个用哪个：
          ① 列表上方有「我创建的」这类筛选按钮/标签页 → 点它（mine_filter_texts）
          ② 表格里有「创建人」列 → 按 mine_creator 配的用户名比对（creator_column）
        两个都没配就直接报错，不能默默把别人的人群一起延了。
        """
        if self.scope != SCOPE_MINE:
            return

        radio_sel = self.f.get("mine_radio_selector", "label[class*=radio-wrapper]")
        for text in self.f.get("mine_filter_texts") or []:
            try:
                lab = page.locator(radio_sel).filter(has_text=text).first
                if not lab.count() or not lab.is_visible():
                    continue
                lab.click()
                # ⚠ 单选框的选中状态是立刻变的，但列表要 3 秒以上才重渲染。
                #   所以先等 class 变成 -checked 确认点中了，再等列表安定。
                for _ in range(20):
                    page.wait_for_timeout(250)
                    if "checked" in (lab.get_attribute("class") or ""):
                        break
                else:
                    self.ui.log(f"点了「{text}」但没看到它被选中，继续试下一个写法", "warn")
                    continue
                page.wait_for_timeout(self.f.get("after_filter_wait", 2000))
                self._wait_rows(page)
                self.ui.log(f"已筛选到「{text}」")
                self._mine_by_column = False
                return
            except Exception:
                continue

        creator_col = self.f.get("creator_column")
        creator = str(self.f.get("mine_creator") or "").strip()
        if creator_col is not None and creator:
            self._mine_by_column = True
            self.ui.log(f"列表上没有「我创建的」筛选，改按「创建人 = {creator}」过滤")
            return

        raise FillError(
            "「我创建的」这个范围还没配好：页面上没找到 mine_filter_texts 里的筛选按钮，"
            "config/forms/DMP延期.yaml 里的 creator_column / mine_creator 也没填。"
            "二选一配上再跑（截一张列表页的图给我，我来填）。")

    ROW_JS = """(els, attr) => els.map(e => ({
        key: e.getAttribute(attr) || '',
        cells: [...e.querySelectorAll('td')].map(td => (td.innerText || '').trim()),
        text: (e.innerText || '').trim(),
        shown: e.offsetParent !== null,
    }))"""

    def _rows_of(self, page) -> list[dict]:
        """把当前这一屏的表格行读成结构化数据。

        ⚠ 一次 evaluate 取回整张表，不要 rows.nth(i) 逐行读：这个列表随时会
          异步重渲染，先 count() 再逐个 nth(i) 的话，中途行数变少就会卡在
          「等 nth(7) 出现」直到超时。一次性取还顺带把几十次跨进程调用省成一次。
        """
        key_attr = self.f.get("row_key_attribute", "data-row-key")
        name_column = int(self.f.get("name_column", 1))
        id_column = self.f.get("id_column")
        creator_column = self.f.get("creator_column")

        try:
            raw = page.locator(self.f.get("row_selector", "tbody tr")).evaluate_all(
                self.ROW_JS, key_attr)
        except Exception:
            return []

        out = []
        for i, r in enumerate(raw):
            cells = r.get("cells") or []
            if not r.get("shown") or not cells:
                continue

            def cell(idx):
                idx = None if idx is None else int(idx)
                return cells[idx].strip() if idx is not None and len(cells) > idx else ""

            name = cell(name_column) or cells[0]
            key = r.get("key") or f"{name}|{i}"
            out.append({
                "key": key,
                "id": cell(id_column) or (key if str(key).isdigit() else ""),
                "name": name,
                "creator": cell(creator_column),
                "cells": cells,
                "row_text": r.get("text", ""),
            })
        return out

    def _targets_on_page(self, page) -> list[dict]:
        """当前这一页里符合当前范围的人群。"""
        status_text = self._status_filter()
        creator = str(self.f.get("mine_creator") or "").strip()
        by_column = self.scope == SCOPE_MINE and getattr(self, "_mine_by_column", False)

        out = []
        for r in self._rows_of(page):
            if status_text and status_text not in r["row_text"]:
                continue
            if by_column and r["creator"] != creator:
                continue
            r["status"] = status_text or self._status_of(r)
            out.append(r)
        return out

    def _status_filter(self) -> str:
        """这个范围要不要按状态过滤。

        active / mine 沿用「生效中」；id_list 是用户点名的，不再按状态挑，
        免得清单里写了个已过期的人群却被静默忽略。
        """
        if self.scope == SCOPE_ID_LIST:
            return ""
        if self.scope == SCOPE_MINE:
            return str(self.f.get("mine_status", self.f.get("active_status", "生效中")) or "")
        return str(self.f.get("active_status", "生效中") or "")

    def _status_of(self, r: dict) -> str:
        for known in (self.f.get("known_status") or ["生效中", "已失效", "未生效", "计算中"]):
            if known in r["row_text"]:
                return known
        return ""

    def _all_targets(self, page) -> list[dict]:
        """扫描所有分页。列表翻页后只保留还未见过的稳定 key。"""
        targets, seen, page_no = [], set(), 1
        while True:
            for target in self._targets_on_page(page):
                if target["key"] in seen:
                    continue
                seen.add(target["key"])
                target["page"] = page_no
                targets.append(target)
            if not self._advance_page(page):
                return targets
            page_no += 1

    def _first_key(self, page) -> str:
        """当前页第一行的 key，用来判断列表有没有真的换过一批数据。"""
        try:
            loc = page.locator(self.f.get("row_selector", "tbody tr"))
            if not loc.count():
                return ""
            return loc.first.get_attribute(self.f.get("row_key_attribute", "data-row-key")) or ""
        except Exception:
            return ""

    def _wait_list_changed(self, page, before: str, timeout: int) -> bool:
        """等列表换成另一批数据。

        ⚠ 不能点完就按固定时间往下走：这个页面重新渲染实测要 3 秒以上，
          早读一步会读到上一页的数据 —— 表现为「翻页了但内容没变」，
          扫描时整份名单都是重复的第一页。
        """
        waited = 0
        while waited < timeout:
            page.wait_for_timeout(400)
            waited += 400
            cur = self._first_key(page)
            if cur and cur != before:
                page.wait_for_timeout(self.f.get("page_settle_wait", 800))
                return True
        return False

    def _advance_page(self, page) -> bool:
        """翻到下一页；翻不动（按钮禁用或内容没变）代表已经是最后一页。"""
        before = self._first_key(page)
        timeout = int(self.f.get("page_change_timeout", 15000))

        for selector in self.f.get("next_page_selectors", []):
            items = page.locator(selector)
            for i in range(items.count()):
                item = items.nth(i)
                try:
                    if not item.is_visible():
                        continue
                    cls = item.get_attribute("class") or ""
                    if "disabled" in cls or item.get_attribute("aria-disabled") == "true":
                        continue
                    # ⚠ antd 的 li.pagination-next 本身点了没反应，
                    #   真正响应点击的是它里面那个 button。
                    inner = item.locator("button").first
                    (inner if inner.count() else item).click()
                except Exception:
                    continue
                return self._wait_list_changed(page, before, timeout)
        return False

    def _search_for(self, page, keyword: str):
        """用列表自带的搜索框定位一个人群，返回它那一行的数据；找不到返回 None。

        ⚠ 清单模式不能靠「扫完所有分页再翻页定位」：列表有 21 页，某个人群排在
          第 19 页时，光定位就要翻十几次、每次还得等三秒重渲染。搜索一步到位，
          而且人群排序变了也不受影响。
        """
        sel = self.f.get("search_input_selector")
        inp = page.locator(sel).first
        if not inp.count():
            raise FillError(f"页面上找不到搜索框（{sel}）")

        for attempt in range(int(self.f.get("search_attempts", 3))):
            before = self._snapshot(page)
            inp.click()
            inp.fill("")
            inp.fill(str(keyword))

            btn = page.locator(self.f.get("search_button_selector", "")).first
            if btn.count():
                btn.click()
            else:
                inp.press("Enter")

            applied, hit = self._await_search(page, keyword, before)
            if applied:
                return hit
            log.info("第 %d 次搜「%s」没等到结果生效，重发", attempt + 1, keyword)
        return None

    def _snapshot(self, page) -> tuple:
        rows = self._targets_on_page(page)
        return (len(rows), rows[0]["key"] if rows else "")

    def _await_search(self, page, keyword: str, before: tuple) -> tuple[bool, dict | None]:
        """等搜索结果真正渲染出来。返回 (搜索是否已生效, 命中的那一行)。

        ⚠ 判据不能只是「列表稳定了」：搜索响应回来之前，页面上摆的还是搜索前
          那一批，而它本来就是静止的 —— 一判就过，于是要么拿到旧列表里同 ID 的行
          （随后列表在底下重渲染，点击落到别的行上），要么因为目标不在旧列表里
          就误判成「这个人群不存在」。所以必须确认列表**确实换成了搜索结果**：
          要么内容变了，要么已经收窄到几行以内。
        """
        deadline = int(self.f.get("search_timeout", 15000))
        max_rows = int(self.f.get("search_max_rows", 5))
        empty_sel = self.f.get("empty_selector", ".full_ogv_data_antd-empty")
        kw = str(keyword).strip()
        waited = 0

        while waited < deadline:
            page.wait_for_timeout(400)
            waited += 400

            if page.locator(empty_sel).count():
                return True, None                      # 页面明确显示「无数据」

            rows = self._targets_on_page(page)
            hit = next((r for r in rows if self._same(r, kw)), None)

            # 这一屏是不是「这个关键词」的结果：每一行都对得上才算。
            # ⚠ 不能只看「行数很少」：上一个人群搜完，列表本来就只剩一行，
            #   拿它当成本次搜索的结果，就会把还没搜到的人群误判成不存在。
            mine = bool(rows) and all(self._same(r, kw) or kw in r["name"] for r in rows)
            if mine:
                page.wait_for_timeout(self.f.get("page_settle_wait", 800))
                return True, hit

            changed = (len(rows), rows[0]["key"] if rows else "") != before
            if changed:
                if hit:
                    page.wait_for_timeout(self.f.get("page_settle_wait", 800))
                    return True, hit
                if len(rows) <= max_rows:
                    return True, None                  # 结果出来了，但里面没有它
        return False, None

    def _row_for(self, page, target: dict):
        """定位目标人群那一行。

        ⚠ 绝对不能用 rows.nth(i) 这种按下标的定位：这个列表会异步重渲染，
          拿到下标之后再去 hover/click，中间只要刷新一次，下标 i 指向的就是
          另一个人群了 —— 轻则报「元素不可见」超时，重则在错误的人群上执行延期。
          按 data-row-key 拼选择器，Playwright 每次动作都会重新解析，
          行挪到哪都能跟上。
        """
        key_attr = self.f.get("row_key_attribute", "data-row-key")
        base = self.f.get("row_selector", "tbody tr")
        key = str(target.get("key", "")).strip()

        if key and not key.startswith("missing|") and '"' not in key:
            marker = f"[{key_attr}]"
            keyed = (base.replace(marker, f'[{key_attr}="{key}"]') if marker in base
                     else f'{base}[{key_attr}="{key}"]')
            row = page.locator(keyed).first
            if row.count():
                return row

        # 页面没给稳定键时退回名称匹配；带上状态，避免点到同名的失效记录。
        # 用一次性快照找下标，别边遍历边读 DOM，免得列表重渲染时卡到超时。
        for i, r in enumerate(self._rows_of(page)):
            if target["name"] and target["name"] in r["row_text"] and (
                    not target.get("status") or target["status"] in r["row_text"]):
                return page.locator(base).nth(i)
        raise FillError(f"列表中找不到人群「{target['name']}」")

    def _click_visible_text(self, page, texts: list[str], scope=None) -> str:
        """按钮文字点击。

        ⚠ antd 会在双字按钮的两个汉字之间插一个全角空格，渲染出来是「保 存」「取 消」。
          get_by_text(exact=True) 匹配不到，表现为「找不到保存按钮」。
          所以这里把候选按钮的文字先剔掉所有空白再比。
        """
        if not texts:
            return ""
        want = {_squeeze(t) for t in texts}
        root = scope or page
        for btn in awaitable_all(root.locator("button, a, span[role=button]")):
            try:
                if not btn.is_visible():
                    continue
                if _squeeze(btn.inner_text()) in want:
                    btn.click()
                    return btn.inner_text().strip()
            except Exception:
                continue
        raise FillError(f"找不到可点击按钮：{'、'.join(texts)}")

    def _open_extension(self, page, target: dict):
        """打开这一行的「人群延期」弹窗。

        ⚠ 「操作」是 antd Dropdown.Button：左边「操 作」是主按钮，点它没用，
          菜单挂在右边那个省略号触发器上，而且是 hover 展开不是 click。
        ⚠ 鼠标如果已经停在触发器上（上一条刚点过同一位置），再 hover 同一坐标
          不会触发 mouseover，菜单永远不展开。所以每次先把鼠标挪到角落。
        """
        row = self._row_for(page, target)
        row.wait_for(state="visible", timeout=self.s["timeout"])
        row.scroll_into_view_if_needed()

        trig_sel = self.f.get("op_trigger_selector", "button.full_ogv_data_antd-dropdown-trigger")
        trig = row.locator(trig_sel).first
        if not trig.count():
            raise FillError(f"「{target['name']}」行找不到操作下拉触发器（{trig_sel}）")

        menu_text = self.f.get("extension_menu_text", "人群延期")
        item_sel = self.f.get("menu_item_selector", "li.full_ogv_data_antd-dropdown-menu-item")
        item = page.locator(item_sel).filter(has_text=menu_text).last

        for attempt in range(3):
            page.mouse.move(5, 5)
            page.wait_for_timeout(300)
            trig.hover()
            page.wait_for_timeout(self.f.get("menu_open_wait", 1200))
            try:
                if item.count() and item.is_visible():
                    item.click()
                    page.wait_for_timeout(1500)
                    return
            except Exception:
                pass
            log.info("第 %d 次没能展开操作菜单，重试", attempt + 1)

        hint = ""
        if target.get("status") in (self.f.get("non_extendable_status") or []):
            hint = f"（这个人群是「{target['status']}」，系统本来就不给延期）"
        raise FillError(f"「{target['name']}」的操作菜单里没有「{menu_text}」{hint}")

    def _pick_date(self, page, target: dict) -> tuple[str, bool]:
        """选日期。返回 (实际选中的日期, 是否因为超上限被截断)。

        清单里没写日期、或者写的日期超过系统允许的最晚日期时，都取系统最晚日期。

        ⚠ 系统上限是「今天 + N 天」，一次运行里对所有人群都是同一天，
          所以只在第一个人群身上翻月确认一次，之后直接复用 —— 每个人群
          都翻六个月面板纯属白等。缓存万一失效（比如跨了零点），
          pick 会选不中并抛错，这里捕获后重算一次，不会写错日期。
        """
        panel = DatePanel(page, self.f)
        want = target.get("want_date")
        if isinstance(want, str):
            want = parse_date(want)

        try:
            panel.open(self.f.get("date_field_label"))
            if self._max_limit is not None:
                try:
                    picked, capped, _ = panel.pick_capped(want, self._max_limit)
                    return fmt(picked), capped
                except DateError:
                    self.ui.log("上次算出的最晚日期这次选不中了，重新翻月确认", "warn")
                    self._max_limit = None
                    panel.open(self.f.get("date_field_label"))

            picked, capped, limit = panel.pick_capped(want)
            if self._max_limit is None:
                self._max_limit = limit
                self.ui.log(f"系统最晚可选日期：{fmt(limit)}（本次运行内复用，不再逐个翻月）")
            return fmt(picked), capped
        except DateError as e:
            raise FillError(str(e)) from e

    def _confirm_extension(self, page):
        """有些页面选完日期还要先点一次「确认」。

        当前这个弹窗没有这一步（只有 取 消 / 保 存），所以 yaml 里 confirm_texts
        是空的 —— 空就直接跳过，别硬找一个不存在的按钮。
        """
        texts = self.f.get("confirm_texts") or []
        if not texts:
            return
        self._click_visible_text(page, texts)
        page.wait_for_timeout(500)

    def _save(self, page):
        self._click_visible_text(page, self.f.get("save_texts", ["保存"]))
        page.wait_for_timeout(self.f.get("after_save_wait", 1200))
        errors = []
        for selector in self.f.get("error_selectors", []):
            errors += [x.strip() for x in page.locator(selector).all_inner_texts() if x.strip()]
        if errors:
            raise FillError("保存被页面拒绝：" + "；".join(dict.fromkeys(errors)))

    def _close_dialog(self, page):
        """不保存地关掉弹窗。失败/跳过/试跑都会走到这里，必须尽力关干净，
        否则下一条会被上一条的遮罩挡住。"""
        page.keyboard.press("Escape")       # 先收掉可能还开着的日期浮层
        page.wait_for_timeout(200)
        try:
            self._click_visible_text(page, self.f.get("cancel_texts") or ["取消", "关闭"])
            page.wait_for_timeout(500)
            return
        except Exception:
            pass
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

    # ---------------- 输出 ----------------
    def _screenshot(self, page, idx: int, tag: str) -> str:
        path = self.shot_dir / f"{self.f['name']}_{idx:04d}_{tag}_{datetime.now():%H%M%S}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:
            return "(截图失败)"

    def _result(self, i, target, status, error, picked):
        want = target.get("want_date")
        return {"序号": i,
                "人群ID": target.get("id", ""),
                "人群名称": target["name"],
                "状态": status,
                "目标日期": fmt(want) if want else "（系统最晚）",
                "延期至": picked,
                "错误": error}

    def _write_results(self, results):
        if not results:
            return
        path = Path(self.s["result_file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(results[0]))
                writer.writeheader()
                writer.writerows(results)
        except PermissionError:
            self.ui.log(f"{path} 被占用（Excel 开着？），结果没写进去", "warn")

    def _report(self, stats, results, dry):
        lines = [f"配置类型：{self.f['name']}",
                 f"延期范围：{SCOPE_LABELS[self.scope]}"]
        lines.append(f"试跑 {stats['dry']} 个" if dry else f"成功 {stats['ok']} 个")
        if stats["skipped"]:
            lines.append(f"跳过 {stats['skipped']} 个")
        if stats["failed"]:
            lines.append(f"失败 {stats['failed']} 个")

        capped = [r for r in results if r["状态"] == "ok" and "截断" in (r["错误"] or "")]
        if capped:
            lines.append(f"其中 {len(capped)} 个填的日期超过系统上限，已改为系统最晚可选日期")

        lines += ["", f"明细：{self.s['result_file']}", f"截图：{self.s['screenshot_dir']}"]
        self.ui.finished("DMP延期完成" if not stats["failed"] else "DMP延期完成（有失败）",
                         "\n".join(lines), not stats["failed"])


def awaitable_all(locator):
    """同步 Playwright locator 的 all() 小包装，便于文本按钮逐个判断可见性。"""
    return locator.all()


def _squeeze(text: str) -> str:
    """剔掉所有空白，含全角空格 U+3000 和 antd 实际用的 U+2005。

    「保 存」→「保存」。\\s 匹配不到全角空格，只用 strip/replace(' ') 会漏。
    """
    return re.sub(r"[\s -​　]", "", text or "")
