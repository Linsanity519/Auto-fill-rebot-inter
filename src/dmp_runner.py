"""DMP 人群延期执行器。

页面：人群管理列表 → 操作 → 人群延期 → 人群有效期至 → 选日期 → 保存。

三种延期范围（yaml 的 scope / 界面上的「延期范围」，默认 active＝老行为不变）：

  active   所有「生效中」人群  → 延到系统允许的最晚日期      （原有功能）
  id_list  Excel 清单里指定的人群ID → 延到清单里写的日期；
           日期超过系统上限时自动改成上限；留空同样取上限
  mine     所有「我创建的」人群 → 延到系统允许的最晚日期

每完成一个人群会等 after_each_wait（默认 5 秒）再做下一个，给系统反应时间。
这是**有意的节流**，不是「等页面加载」—— 等页面一律按条件等，见下。

⚠ 这个文件里不写「等 N 秒再往下走」（CLAUDE.md 硬约定第 1 条）。
  1.1.18 之前这里有十几处写死的等待，网一卡就读到半截的页面，线上实测：
    · 点「我创建的」后固定等 2.5 秒就读列表，而接口实测要 4.7 秒才回来 ——
      读到的是「全部」那一屏，于是一批里夹着别人的人群，报「列表中找不到人群」；
    · 翻页固定等 15 秒没等到就当成「已经是最后一页」—— 29 个只做了 6 个、
      0 个失败就收工，用户看到的是「成功」。
  现在的判据：
    · 列表刷新 → 盯住列表接口（_ListWatch）：请求发出去、回来了、表格里的行
      和接口返回的一模一样，才算刷新完。上限 list_ready_timeout。
    · 菜单 / 弹窗 / 日期面板 → 等它出现（或消失），上限 settings.timeout。
    · 最后一页 → 按接口给的 total_num 算，不靠「翻不动」猜。翻页失败是报错，不是收工。

页面选择器集中在 config/forms/DMP延期.yaml。页面改版后优先改配置，不要把
业务 DOM 细节散落到流程代码中。
"""
from __future__ import annotations

import csv
import logging
import math
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .browser import Browser
from .dmp_date import DateError, DatePanel, fmt, parse_date
from .filler import FillError
from .fill_core import wait_until
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


class _ListWatch:
    """盯着人群列表接口：发出去几个、还有几个没回来、最后回来的是哪个。

    列表页上所有「等它刷新完」都靠它判定，不按秒数猜 —— 同一个动作
    （点筛选、翻页、搜索）接口有时 1 秒回来有时 5 秒，固定等多久都不对。

    ⚠ 只认 GET：接口在另一个域名上，浏览器会先发一个 OPTIONS 预检，
      它的响应体是空的，拿它当列表数据会解析失败。
    ⚠ 事件回调里只记账、不读响应体：sync API 的回调里做阻塞调用不安全，
      响应体留到主流程里再取。
    """

    def __init__(self, page, pattern: str):
        self.page = page
        self.pattern = pattern
        self.sent = 0           # 发出去的列表请求总数
        self.pending = 0        # 还没回来的
        self.finished = 0       # 回来了的（含失败）
        self.consumed = 0       # 主流程已经读过的 finished 序号
        # ⚠ 读的是「最后发出去的」那个请求，不是「最后回来的」：两个请求同时在路上时
        #   （比如页面刚打开自己还在拉第一屏，我们又点了搜索），回来的顺序不一定，
        #   拿「最后回来的」可能读到旧那一屏 —— 实测过一次把搜得到的人群判成搜不到。
        self._last = None       # 最后发出去的那个 request
        self._errors = {}       # id(request) → 失败原因
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_finished)
        page.on("requestfailed", self._on_failed)

    def _mine(self, req) -> bool:
        try:
            return req.method == "GET" and self.pattern in req.url
        except Exception:
            return False

    def _on_request(self, req):
        if self._mine(req):
            self.sent += 1
            self.pending += 1
            self._last = req

    def _on_finished(self, req):
        if self._mine(req):
            self.pending = max(0, self.pending - 1)
            self.finished += 1

    def _on_failed(self, req):
        if self._mine(req):
            self.pending = max(0, self.pending - 1)
            self.finished += 1
            try:
                why = req.failure or "请求失败"
            except Exception:
                why = "请求失败"
            self._errors[id(req)] = str(why)

    @property
    def last_url(self) -> str:
        try:
            return self._last.url if self._last is not None else ""
        except Exception:
            return ""

    def _read(self) -> dict:
        """最后一次回来的列表数据（接口 data 那一层）。

        响应体读不到（浏览器已经把它回收了）返回 {}，由调用方退回看页面。
        ⚠ 接口**明确报错**必须抛出去，不能当成空列表：线上实测翻页时接口卡了 35 秒、
          回来的不是列表，页面上的分页器跟着没了 —— 当成空列表处理，
          就又变成「已经是最后一页」静默收工。
        """
        self.consumed = self.finished
        err = self._errors.get(id(self._last))
        if err:
            raise FillError(f"人群列表接口请求失败（{err}）—— 内网/VPN 断了？")
        try:
            resp = self._last.response()
            status = resp.status if resp else None
            body = resp.json() if resp else None
        except Exception:
            log.info("列表接口响应体读不到，退回只看页面", exc_info=True)
            return {}
        body = body if isinstance(body, dict) else {}
        code, data = body.get("code"), body.get("data")
        if status != 200 or code not in (0, None) or not isinstance(data, dict) \
                or not isinstance(data.get("crowd_list"), list):
            msg = str(body.get("message") or body.get("msg") or "")[:80]
            raise FillError(f"人群列表接口返回异常（HTTP {status}，code={code}"
                            f"{'，' + msg if msg else ''}）—— 后台卡了或者在发版")
        return data

    def settle(self, since: int, timeout: int, dom_keys, start_timeout: int = 5000) -> dict | None:
        """等「since 之后发出的列表请求」全部回来、并且表格已经渲染成接口给的那一批。

        返回接口 data；超时返回 None（self.sent == since 说明请求压根没发出去）。
        ⚠ 两段围栏：请求是点下去当场就发的，start_timeout 内没发出去就别再等了
          （等满 45 秒只是白等）；发出去之后才用 timeout 等它回来。
        """
        if not wait_until(self.page, lambda: self.sent > since, min(start_timeout, timeout)):
            return None
        if not wait_until(self.page, lambda: self.pending == 0, timeout):
            return None
        return self._render(timeout, dom_keys)

    def idle(self, timeout: int, dom_keys) -> dict | None:
        """如果有列表请求在路上（比如保存后页面自己刷新列表），等它回来并渲染完。

        返回新数据；期间没有新请求返回 None（列表没动过）。
        """
        wait_until(self.page, lambda: self.pending == 0, timeout)
        if self.finished <= self.consumed:
            return None
        return self._render(timeout, dom_keys)

    def _render(self, timeout: int, dom_keys) -> dict:
        data = self._read()
        rows = data.get("crowd_list")
        if isinstance(rows, list):
            ids = [str(r.get("crowd_id", "")) for r in rows if isinstance(r, dict)]
            # 实测接口一回来 0.1 秒内表格就换好了；这里等的是这 0.1 秒
            if not wait_until(self.page, lambda: dom_keys() == ids, timeout):
                log.warning("列表接口回来了，但表格 %d 毫秒内没渲染成接口给的那一批", timeout)
        return data


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
        self._watch: _ListWatch | None = None
        self._meta: dict = {}       # 最近一次列表接口返回的分页信息
        # 界面/命令行传进来的优先；都没有就用 yaml 里的；再没有就是老行为
        self.scope = (settings.get("dmp_scope") or self.f.get("scope") or SCOPE_ACTIVE)
        if self.scope not in SCOPE_LABELS:
            raise FillError(f"不认识的延期范围「{self.scope}」，可选：{list(SCOPE_LABELS)}")

    # ---------------- 两个围栏时间 ----------------
    def _list_fence(self) -> int:
        """列表接口的上限。它比页面控件慢得多（实测 1~5 秒，内网抖一下更久）。"""
        return int(self.f.get("list_ready_timeout", 45000))

    def _ui_fence(self) -> int:
        """菜单 / 弹窗 / 日期面板出现或消失的上限。"""
        return int(self.s.get("timeout", 15000))

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
                self._clear_search(b.page)
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
                        "失效时间": fmt(target["expire"]) if target.get("expire") else "",
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
                "status": "", "page": 0, "cells": [], "creator": "", "expire": None,
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

        ctx = {"dry": dry, "stats": stats, "results": results, "cooldown": cooldown, "i": 0,
               "seen": set()}

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
            ctx["stopped"] = True
        finally:
            if not ctx.get("stopped"):
                self._account_unreached(wanted_keys, ctx)
            self._write_results(results)
            self._report(stats, results, dry)
        return results

    def _account_unreached(self, wanted_keys, ctx: dict):
        """预检列出来、这一轮却一次都没走到的人群，记成失败。

        ⚠ 这是兜底，不是正常路径：翻页/列表出了意外，宁可报「没走到」，
          也不能像 1.1.18 之前那样安安静静地「成功 6 个、失败 0 个」收工 ——
          那种结果用户根本不会去查。
        """
        pending = [t for t in self._keep_wanted(
            [t for t in getattr(self, "_targets", []) if not t.get("issues")], wanted_keys)
            if t["key"] not in ctx["seen"] and not self.state.is_done(t["key"])]
        if not pending:
            return
        names = "、".join(t["name"] for t in pending[:5]) + ("…" if len(pending) > 5 else "")
        self.ui.log(f"有 {len(pending)} 个人群这一轮没走到：{names}（重跑会接着做）", "error")
        for t in pending:
            ctx["i"] += 1
            ctx["stats"]["failed"] += 1
            ctx["results"].append(self._result(ctx["i"], t, "failed",
                                               "这一轮没走到（列表翻页中断），重跑会接着做", ""))

    # ---------------- 两种走法 ----------------
    def _run_by_list(self, page, wanted_keys, ctx: dict):
        """清单模式：逐个 ID 用搜索框直达。

        ⚠ 这里才需要搜索：点名的人群可能排在第 19 页，翻过去要等一分多钟。
        """
        self._open_list(page)
        targets = self._collect_by_search(page, self._load_wanted())

        for t in [x for x in targets if x.get("issues")]:
            self.ui.log(f"跳过「{t['name']}」：{t['issues'][0]}", "warn")
            ctx["seen"].add(t["key"])
        targets = [t for t in targets if not t.get("issues")]
        targets = self._keep_wanted(targets, wanted_keys)

        total = len(targets)
        self.ui.log(f"待处理 {total} 个人群")
        self.ui.progress(0, total, ctx["stats"])

        try:
            for target in targets:
                # 每条开头重新搜一次（在 _locate_row 里）：上一条处理完，
                # 搜索框还停在上一个 ID 上
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
            self._refresh(page, lambda: (inp.fill(""), self._press_search(page, inp)), "清空搜索")
        except Exception:
            log.info("收尾清空搜索框失败，忽略", exc_info=True)

    def _run_by_pages(self, page, wanted_keys, ctx: dict):
        """全量 / 我创建的：顺着列表一页一页往下延，处理完当前页再翻页。

        ⚠ 不预扫、不搜索、不回头：这两种范围本来就是「整页整页地延」，
          先扫一遍再翻回去逐个定位等于把 20 多页翻两遍，纯浪费。
          断点（state）按人群 key 记，中断重跑照样能跳过已完成的。
        ⚠ 「最后一页」按接口的 total_num 算。翻页失败不再当成「到底了」——
          那样会静默漏掉后面所有页（1.1.18 线上：29 个只做了 6 个）。
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
            for t in batch:
                t["page"] = page_no
            self.ui.log(f"第 {page_no} 页：{len(batch)} 个符合范围")
            for target in batch:
                if self._process_one(page, target, total, ctx) == "stop":
                    return
            # 预检列出来的都走过了，后面的页不用再翻（每翻一页要等 5 秒接口）
            planned = self._keep_wanted(
                [t for t in getattr(self, "_targets", []) if not t.get("issues")], wanted_keys)
            if planned and all(t["key"] in ctx["seen"] for t in planned):
                self.ui.log(f"预检列出的 {len(planned)} 个都处理过了，后面的页不用再翻")
                return
            try:
                if not self._goto_page(page, page_no + 1):
                    self.ui.log(f"已经是最后一页（共 {page_no} 页）")
                    return
            except FillError as e:
                self.ui.log(f"翻到第 {page_no + 1} 页失败：{e}", "error")
                self._screenshot(page, ctx["i"], "pager")
                return          # 没走到的由 _account_unreached 记成失败，不静默
            page_no += 1

    def _keep_wanted(self, targets: list[dict], wanted_keys) -> list[dict]:
        """按预检界面上用户勾选/保留的结果过滤。"""
        if wanted_keys is None:
            return targets
        return [t for t in targets
                if (str(t.get("id", "")).strip(), str(t["name"]).strip()) in wanted_keys]

    # ---------------- 单条处理 ----------------
    def _process_one(self, page, target: dict, total: int, ctx: dict) -> str:
        """处理一个人群。返回 'next' 继续，'stop' 中止整轮。"""
        stats, results, dry, cooldown = ctx["stats"], ctx["results"], ctx["dry"], ctx["cooldown"]
        ctx["seen"].add(target["key"])

        if self.state.is_done(target["key"]):
            self.ui.log(f"{target['name']} 已完成过，跳过")
            return "next"

        self.ui.checkpoint()
        ctx["i"] += 1
        i = ctx["i"]
        label = f"[{i}/{total}]" if total else f"[{i}]"

        # 已经顶在系统上限上的，连弹窗都不用开（上限第一条算出来之后才知道）
        if self._already_max(target):
            stats["skipped"] += 1
            results.append(self._result(i, target, "no_change",
                                        "已经是系统最晚可选日期，无需延期", fmt(target["expire"])))
            self.ui.log(f"{label} {target['name']} 已是最晚可选日期（{fmt(target['expire'])}），跳过")
            self.state.mark_done(target["key"])
            self.ui.progress(i, total, stats)
            return "next"

        try:
            self._open_extension(page, target)
            picked, capped = self._pick_date(page, target)

            # 选出来的日期不比现在的失效时间晚：保存等于没延（清单里写了个更早的日期
            # 甚至会把有效期改短），直接跳过
            cur = target.get("expire")
            if cur and parse_date(picked) and parse_date(picked) <= cur:
                self._close_dialog(page)
                stats["skipped"] += 1
                results.append(self._result(i, target, "no_change",
                                            f"现在的失效时间 {fmt(cur)} 已不早于 {picked}，无需延期",
                                            picked))
                self.ui.log(f"{label} {target['name']} 失效时间 {fmt(cur)} 已不早于 {picked}，跳过")
                self.state.mark_done(target["key"])
                return "next"

            self._screenshot(page, i, "ready")

            note = "（超过系统上限，已改为最晚可选日期）" if capped else ""
            self.ui.log(f"{label} {target['name']} 已选日期：{picked}{note}",
                        "warn" if capped else "ok")

            action = "submit" if (dry or self.auto) else self.ui.confirm(label, target["name"])
            if action == "auto":
                self.auto, action = True, "submit"
            if action == "stop":
                self._close_dialog(page)
                ctx["stopped"] = True
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
            if self.ui.ask_continue(msg):
                return "next"
            ctx["stopped"] = True       # 用户选了不继续：剩下的是人主动不做，不算「没走到」
            return "stop"
        finally:
            self.ui.progress(i, total, stats)

    def _already_max(self, target: dict) -> bool:
        """不开弹窗就能断定「已经是最晚可选日期」：上限已知、没点名日期、失效时间已到上限。"""
        return bool(self._max_limit and not target.get("want_date")
                    and target.get("expire") and target["expire"] >= self._max_limit)

    def _cooldown(self, page, ms: int, label: str):
        """完成一个之后的节流（给后台留反应时间，yaml 的 after_each_wait）。

        分段等，用户点停止时不用干等满 5 秒。
        """
        left = ms
        while left > 0:
            self.ui.checkpoint()
            step = min(500, left)
            page.wait_for_timeout(step)
            left -= step

    # ---------------- 列表：打开 / 刷新 / 翻页 ----------------
    def _watch_on(self, page) -> _ListWatch:
        if self._watch is None or self._watch.page is not page:
            self._watch = _ListWatch(page, self.f.get("list_api", "/crowd/list/page"))
        return self._watch

    def _dom_keys(self, page) -> list[str]:
        """表格里当前这一批行的 key（= 人群ID），按显示顺序。"""
        try:
            return page.locator(self.f.get("row_selector", "tbody tr[data-row-key]")).evaluate_all(
                "(els, a) => els.map(e => e.getAttribute(a) || '')",
                self.f.get("row_key_attribute", "data-row-key"))
        except Exception:
            return ["<读不到>"]

    def _refresh(self, page, action, what: str) -> dict:
        """做一个会让列表重新拉数据的动作，等到新数据真的渲染出来。返回接口 data。

        ⚠ 等的是「这个动作之后发出的请求」回来 —— 不能只看「列表现在是静止的」：
          动作之后、响应回来之前，页面上摆的还是旧那一屏，它本来就是静止的。
        """
        w = self._watch_on(page)
        fence = self._list_fence()
        try:
            self._idle(page)        # 先让路上的请求落地，免得和这次的混在一起
        except FillError:
            log.info("动作之前那一次列表刷新是坏的，忽略，以这次为准", exc_info=True)
        since = w.sent
        action()
        data = w.settle(since, fence, lambda: self._dom_keys(page))
        if data is None:
            if w.sent == since:
                raise FillError(f"{what}之后页面没有去刷新人群列表（按钮没点上？）")
            raise FillError(f"{what}之后等了 {fence // 1000} 秒人群列表还没刷新出来"
                            f"（内网卡了？可以把 yaml 的 list_ready_timeout 调大）")
        self._meta = data
        return data

    def _open_list(self, page):
        """打开列表页并等到第一屏数据真的渲染出来。

        ⚠ 这是个 SPA：DOM 里先有空的 <table> 骨架，行要等接口回来才渲染，
          实测能差十几秒。所以等的是列表接口，不是 table 标签。
        ⚠ 冷加载（刚连上 VPN、刚登录完）偶尔会拖很久，超时先刷新重试一次。
        """
        url = self.f["form_url"]
        w = self._watch_on(page)
        fence = self._list_fence()
        for attempt in (1, 2):
            since = w.sent
            try:
                if attempt == 1:
                    page.goto(url, wait_until="domcontentloaded")
                else:
                    page.reload(wait_until="domcontentloaded")
            except Exception as e:
                raise FillError(_nav_error(url, e)) from e
            # 页面加载本身要时间，请求不会点下去就发 —— 这里「发出去」也按整个围栏等
            try:
                data = w.settle(since, fence, lambda: self._dom_keys(page), start_timeout=fence)
            except FillError as e:
                if attempt == 2:
                    raise
                self.ui.log(f"{e}，刷新重试一次", "warn")
                continue
            if data is not None:
                self._meta = data
                if self._dom_keys(page):
                    return
                # ⚠ 实测后台偶尔第一下返回空列表（连「我创建的人群数量」都是 0），
                #   过一会儿自己就好。人群列表现实里不会是空的，刷新再确认一次，
                #   不然预检会报「0 个人群」
                if attempt == 1:
                    self.ui.log("人群列表是空的，刷新再确认一次", "warn")
                    continue
                self.ui.log("列表是空的（页面显示无数据）", "warn")
                return
            if _host(page.url) != _host(url):
                break           # 被跳去登录页了，刷新没用
            if attempt == 1:
                self.ui.log(f"人群列表 {fence // 1000} 秒没加载出来，刷新重试一次", "warn")

        if _host(page.url) != _host(url):
            raise FillError(f"打开人群列表时页面跳到了 {_host(page.url)} —— 多半是 DMP 后台的登录"
                            f"过期了。在 Chrome 里重新登录一下，再点「载入并检查」")
        raise FillError(f"人群列表等了 {fence // 1000} 秒（还刷新重试了一次）都没加载出来 —— "
                        f"内网/VPN 卡了？在 Chrome 里手动打开这个页面看看能不能出数据")

    def _apply_scope_filter(self, page):
        """mine 模式：把列表筛成「我创建的」。

        两条路，页面上有哪个用哪个：
          ① 列表上方有「我创建的」这类筛选按钮/标签页 → 点它（mine_filter_texts）
          ② 表格里有「创建人」列 → 按 mine_creator 配的用户名比对（creator_column）
        两个都没配就直接报错，不能默默把别人的人群一起延了。

        ⚠ 单选框的选中状态是立刻变的，列表要等接口回来（实测 4.7 秒）才换。
          1.1.18 之前在这里固定等 2.5 秒就读列表，读到的是「全部」那一屏。
        """
        if self.scope != SCOPE_MINE:
            return

        radio_sel = self.f.get("mine_radio_selector", "label[class*=radio-wrapper]")
        for text in self.f.get("mine_filter_texts") or []:
            lab = page.locator(radio_sel).filter(has_text=text).first
            try:
                if not lab.count() or not lab.is_visible():
                    continue
            except Exception:
                continue
            checked = lambda: "checked" in (lab.get_attribute("class") or "")  # noqa: E731
            if not checked():
                self._refresh(page, lab.click, f"点「{text}」")
                if not wait_until(page, checked, self._ui_fence()):
                    self.ui.log(f"点了「{text}」但没看到它被选中，继续试下一个写法", "warn")
                    continue
            self.ui.log(f"已筛选到「{text}」")
            self._mine_by_column = False
            return

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

    def _cur_page(self, page) -> int:
        n = self._meta.get("curr_page_num")
        if n:
            return int(n)
        try:   # 接口响应体读不到时退回看分页器上高亮的那一页
            t = page.locator("li[class*=pagination-item-active]").first.get_attribute("title")
            return int(t or 1)
        except Exception:
            return 1

    def _page_count(self) -> int | None:
        """总页数；接口没给就返回 None（退回看「下一页」按钮是不是禁用）。"""
        total, size = self._meta.get("total_num"), self._meta.get("curr_page_size")
        if total is None or not size:
            return None
        return max(1, math.ceil(int(total) / int(size)))

    def _pager_button(self, page, forward: bool):
        """「下一页 / 上一页」里真正能点的那个 button；按钮禁用或不存在返回 None。"""
        key = "next_page_selectors" if forward else "prev_page_selectors"
        for selector in self.f.get(key) or []:
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
                    return inner if inner.count() else item
                except Exception:
                    continue
        return None

    def _next_disabled(self, page) -> bool:
        """分页器还在、而且「下一页」是灰的 —— 页面自己说「到底了」。

        ⚠ 分页器整个不见了不算：接口报错时 antd 会把分页器一起收掉，
          那不是到底，是出错了。
        """
        try:
            items = page.locator("li[class*=pagination-next]")
            for i in range(items.count()):
                it = items.nth(i)
                if it.is_visible() and ("disabled" in (it.get_attribute("class") or "")
                                        or it.get_attribute("aria-disabled") == "true"):
                    return True
        except Exception:
            pass
        return False

    def _reopen_list(self, page):
        """列表出错之后的恢复：重新打开列表（「我创建的」范围再筛一次），回到第 1 页。"""
        self._open_list(page)
        self._apply_scope_filter(page)

    def _goto_page(self, page, n: int) -> bool:
        """把列表翻到第 n 页。n 超过总页数返回 False（这才是「到底了」）。

        能前能后：保存之后页面若自己把列表刷回了第 1 页，这里会翻回来。
        翻页时接口出错/超时：重新打开列表再翻过去，最多 page_retries 次；
        还不行就抛 FillError —— **任何时候都不把「翻不动」当成最后一页**。
        「到底了」只认两个依据：接口给的总数算出来没有第 n 页，或者分页器上
        「下一页」确实是灰的。
        """
        retries = int(self.f.get("page_retries", 2))
        for _ in range(200):
            try:
                self._idle(page)
                cur = self._cur_page(page)
                if cur == n:
                    return True
                pages = self._page_count()
                if pages is not None and n > pages:
                    return False
                forward = cur < n
                btn = self._pager_button(page, forward)
                if btn is None:
                    if forward and pages is None and self._next_disabled(page):
                        return False
                    raise FillError(f"要去第 {n} 页（当前第 {cur} 页，共 {pages or '?'} 页），"
                                    f"但分页器上的「{'下' if forward else '上'}一页」点不了")
                step = cur + (1 if forward else -1)
                self._refresh(page, btn.click, f"翻到第 {step} 页")
                if self._cur_page(page) == cur:
                    raise FillError(f"点了「{'下' if forward else '上'}一页」，列表还停在第 {cur} 页")
            except FillError as e:
                if retries <= 0:
                    raise
                retries -= 1
                self.ui.log(f"{e}。重新打开列表，再翻到第 {n} 页", "warn")
                self._reopen_list(page)
        raise FillError(f"翻了 200 次还没到第 {n} 页")

    def _idle(self, page):
        """如果页面自己在刷新列表（比如刚保存完），等它刷完再动。"""
        if self._watch is None:
            return
        data = self._watch.idle(self._list_fence(), lambda: self._dom_keys(page))
        if data:
            self._meta = data

    # ---------------- 列表：读 ----------------
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
        expire_column = self.f.get("expire_column")

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
                "expire": parse_date(cell(expire_column)),
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
        """扫描所有分页。列表翻页后只保留还未见过的稳定 key。

        翻页失败直接抛出去（预检报错），不能拿半截名单当全部。
        """
        targets, seen, page_no = [], set(), 1
        while True:
            for target in self._targets_on_page(page):
                if target["key"] in seen:
                    continue
                seen.add(target["key"])
                target["page"] = page_no
                targets.append(target)
            if not self._goto_page(page, page_no + 1):
                return targets
            page_no += 1

    def _press_search(self, page, inp):
        btn = page.locator(self.f.get("search_button_selector", "")).first
        if btn.count():
            btn.click()
        else:
            inp.press("Enter")

    def _search_for(self, page, keyword: str):
        """用列表自带的搜索框定位一个人群，返回它那一行的数据；找不到返回 None。

        ⚠ 清单模式不能靠「扫完所有分页再翻页定位」：列表有 21 页，某个人群排在
          第 19 页时，光定位就要翻十几次、每次还得等三秒重渲染。搜索一步到位，
          而且人群排序变了也不受影响。
        ⚠ 等的是「这次搜索发出的请求」回来并渲染完，不是「列表看起来安静了」——
          搜索响应回来之前，旧那一屏本来就是安静的，一判就过（误判成搜不到）。
        """
        sel = self.f.get("search_input_selector")
        inp = page.locator(sel).first
        if not inp.count():
            raise FillError(f"页面上找不到搜索框（{sel}）")

        kw = str(keyword).strip()
        # ⚠ 搜索框里已经是这个词时，再点搜索页面认为条件没变，**不会重新请求**
        #   （清单模式下预检刚搜过它、或者保存后页面自己刷新过列表，都会这样）。
        #   眼前这一屏如果就是它的搜索结果，直接用；不是就先清空搜索框逼页面重新请求。
        self._idle(page)
        try:
            same = (inp.input_value() or "").strip() == kw
        except Exception:
            same = False
        if same:
            rows = self._targets_on_page(page)
            if rows and all(self._same(r, kw) or kw in r["name"] for r in rows):
                return next((r for r in rows if self._same(r, kw)), None)

        last = None
        for attempt in range(int(self.f.get("search_attempts", 3))):
            if same or attempt:
                try:
                    self._refresh(page, lambda: (inp.fill(""), self._press_search(page, inp)),
                                  "清空搜索框")
                except FillError:
                    log.info("清空搜索框没触发刷新，照样往下搜", exc_info=True)
                same = False
            try:
                self._refresh(page, lambda: (inp.fill(kw), self._press_search(page, inp)),
                              f"搜「{kw}」")
            except FillError as e:
                last = e
                log.info("第 %d 次搜「%s」没等到结果：%s", attempt + 1, kw, e)
                continue
            # 读到的得是「带着这个关键词」的那次请求，不然就是别的刷新混进来了
            if kw not in unquote(self._watch.last_url):
                last = FillError(f"搜「{kw}」的请求没发出去（读到的是 {self._watch.last_url[-60:]}）")
                log.info("第 %d 次：%s", attempt + 1, last)
                continue
            return next((r for r in self._targets_on_page(page) if self._same(r, kw)), None)
        raise last or FillError(f"搜「{kw}」失败")

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

    def _locate_row(self, page, target: dict):
        """找到目标那一行，找不到时尽量自己把列表拨回去再找。

        · 清单模式：每条都重新搜一次它的 ID
        · 逐页模式：先等页面自己的刷新（保存后）落定；行不在当前页时
          翻回它原来那一页 —— 保存后列表被刷回第 1 页也不会把后面几条全报成找不到
        """
        if self.scope == SCOPE_ID_LIST:
            cid = target.get("id") or target["name"]
            if self._search_for(page, cid) is None:
                raise FillError(f"搜不到人群「{cid}」了（刚被删了？）")
            return self._row_for(page, target)

        self._idle(page)
        try:
            return self._row_for(page, target)
        except FillError:
            want, cur = target.get("page"), self._cur_page(page)
            if not want or want == cur:
                raise
            self.ui.log(f"列表不在第 {want} 页了（现在是第 {cur} 页），翻回去", "warn")
            self._goto_page(page, want)
            return self._row_for(page, target)

    # ---------------- 弹窗 ----------------
    def _modal(self, page):
        return page.locator(f"{self.f.get('modal_selector', '[class*=modal-content]')}:visible")

    def _click_visible_text(self, page, texts: list[str], scope=None) -> str:
        """按钮文字点击。默认只在延期弹窗里找（弹窗不在就找整页）。

        ⚠ antd 会在双字按钮的两个汉字之间插一个全角空格，渲染出来是「保 存」「取 消」。
          get_by_text(exact=True) 匹配不到，表现为「找不到保存按钮」。
          所以用「字与字之间允许任意空白」的正则去匹配。
        ⚠ 不要把整页的按钮 all() 出来逐个 inner_text()：列表一重渲染，拿到手的元素
          就脱离了 DOM，inner_text() 会按默认超时干等 15 秒 —— 线上关一次弹窗等了 30 秒。
          一个 locator + filter 一次解析完，点的时候 Playwright 自己重新定位。
        """
        if not texts:
            return ""
        root = scope
        if root is None:
            modal = self._modal(page)
            root = modal.first if modal.count() else page
        cands = root.locator("button:visible, a:visible, span[role=button]:visible")
        for t in texts:
            btn = cands.filter(has_text=_spaced(t)).first
            try:
                if btn.count():
                    btn.click()
                    return t
            except Exception:
                log.info("点「%s」失败", t, exc_info=True)
        raise FillError(f"找不到可点击按钮：{'、'.join(texts)}")

    def _open_extension(self, page, target: dict):
        """打开这一行的「人群延期」弹窗，并核对弹窗里的人群是不是它。

        ⚠ 「操作」是 antd Dropdown.Button：左边「操 作」是主按钮，点它没用，
          菜单挂在右边那个省略号触发器上，而且是 hover 展开不是 click。
        ⚠ 鼠标如果已经停在触发器上（上一条刚点过同一位置），再 hover 同一坐标
          不会触发 mouseover，菜单永远不展开。所以每次先把鼠标挪到角落。
        ⚠ 只点**可见**的那个菜单项：上一行的菜单收起后还留在 DOM 里（只是隐藏），
          不挑可见的话可能点到别的人群的「人群延期」上。弹窗打开后再核对一次名称兜底。
        """
        row = self._locate_row(page, target)
        row.wait_for(state="visible", timeout=self._ui_fence())
        row.scroll_into_view_if_needed()

        trig_sel = self.f.get("op_trigger_selector", "button.full_ogv_data_antd-dropdown-trigger")
        trig = row.locator(trig_sel).first
        if not trig.count():
            raise FillError(f"「{target['name']}」行找不到操作下拉触发器（{trig_sel}）")

        menu_text = self.f.get("extension_menu_text", "人群延期")
        item_sel = self.f.get("menu_item_selector", "li.full_ogv_data_antd-dropdown-menu-item")
        item = page.locator(f"{item_sel}:visible").filter(has_text=menu_text).first
        fence = self._ui_fence()

        for attempt in range(3):
            page.mouse.move(5, 5)
            # 上一个人群的菜单收起来了再 hover，免得两个菜单同时可见
            wait_until(page, lambda: not item.count(), fence)
            trig.hover()
            if wait_until(page, lambda: item.count() > 0, fence // 3):
                try:
                    item.click()
                except Exception:
                    log.info("菜单项点击失败（菜单刚好收起？），重试", exc_info=True)
                    continue
                if not wait_until(page, lambda: self._modal(page).count() > 0, fence):
                    raise FillError(f"点了「{menu_text}」，等了 {fence // 1000} 秒延期弹窗还没出来")
                self._check_modal_target(page, target)
                return
            log.info("第 %d 次没能展开操作菜单，重试", attempt + 1)

        hint = ""
        if target.get("status") in (self.f.get("non_extendable_status") or []):
            hint = f"（这个人群是「{target['status']}」，系统本来就不给延期）"
        raise FillError(f"「{target['name']}」的操作菜单里没有「{menu_text}」{hint}")

    def _check_modal_target(self, page, target: dict):
        """弹窗里写的人群名称必须是这一个 —— 延错人群比延不到严重得多。"""
        name = (target.get("name") or "").strip()
        if not name:
            return
        modal = self._modal(page).first
        try:
            shown = modal.evaluate(
                "m => [m.innerText || '', ...[...m.querySelectorAll('input,textarea')]"
                ".map(i => i.value || '')].join('\\n')")
        except Exception:
            return      # 读不到就不拦，定位本身已经是按 data-row-key 的
        if _squeeze(name) not in _squeeze(shown):
            self._close_dialog(page)
            raise FillError(f"打开的延期弹窗不是「{name}」的（弹窗里没有这个名称），"
                            f"为防延错人群，这一条不做")

    def _pick_date(self, page, target: dict) -> tuple[str, bool]:
        """选日期。返回 (实际选中的日期, 是否因为超上限被截断)。

        清单里没写日期、或者写的日期超过系统允许的最晚日期时，都取系统最晚日期。

        ⚠ 系统上限是「今天 + N 天」，一次运行里对所有人群都是同一天，
          所以只在第一个人群身上翻月确认一次，之后直接复用 —— 每个人群
          都翻六个月面板纯属白等。缓存万一失效（比如跨了零点），
          pick 会选不中并抛错，这里捕获后重算一次，不会写错日期。
        """
        panel = DatePanel(page, self.f, timeout=self._ui_fence())
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
        if texts:
            self._click_visible_text(page, texts)

    def _page_errors(self, page) -> list[str]:
        errors = []
        for selector in self.f.get("error_selectors", []):
            try:
                errors += [x.strip() for x in page.locator(selector).all_inner_texts() if x.strip()]
            except Exception:
                continue
        return list(dict.fromkeys(errors))

    def _save(self, page):
        """点保存，等到「弹窗关了」或「页面报错」其中一个出现。

        ⚠ 1.1.18 之前是点完固定等 2 秒再看报错：后台慢的时候 2 秒内既没关也没报错，
          就当成了成功 —— 其实还没保存完，下一条又去 hover 了。
        """
        self._click_visible_text(page, self.f.get("save_texts", ["保存"]))
        fence = self._list_fence()
        done = wait_until(page, lambda: not self._modal(page).count() or self._page_errors(page),
                          fence)
        errors = self._page_errors(page)
        if errors:
            raise FillError("保存被页面拒绝：" + "；".join(errors))
        if not done:
            raise FillError(f"点了保存，等了 {fence // 1000} 秒弹窗还没关（没保存成功？看一下截图）")

    def _close_dialog(self, page):
        """不保存地关掉弹窗。失败/跳过/试跑都会走到这里，必须尽力关干净，
        否则下一条会被上一条的遮罩挡住。

        ⚠ antd 的弹窗按 Esc 本身就会关，而且关的时候有一段淡出动画。
          按完 Esc 立刻去点「取消」，点的是一个正在被移除的按钮 —— Playwright 会一直
          等它「稳定」到超时（实测一次关弹窗干等 30 秒）。所以先等它自己消失，
          没消失再点「取消」。
        """
        fence = self._ui_fence()
        gone = lambda: not self._modal(page).count()   # noqa: E731
        try:
            # 第一下 Esc 可能只收掉了日期浮层，所以最多按两下
            for _ in range(2):
                page.keyboard.press("Escape")
                if wait_until(page, gone, min(2000, fence)):
                    return
            try:
                self._click_visible_text(page, self.f.get("cancel_texts") or ["取消", "关闭"])
            except FillError:
                pass
            if not wait_until(page, gone, fence):
                log.warning("延期弹窗 %d 毫秒内没关掉", fence)
        except Exception:
            log.info("关弹窗失败，忽略", exc_info=True)

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
        no_change = sum(1 for r in results if r["状态"] == "no_change")
        if no_change:
            lines.append(f"已是最晚日期、无需延期 {no_change} 个")
        user_skipped = stats["skipped"] - no_change
        if user_skipped:
            lines.append(f"跳过 {user_skipped} 个")
        if stats["failed"]:
            lines.append(f"失败 {stats['failed']} 个")

        capped = [r for r in results if r["状态"] == "ok" and "截断" in (r["错误"] or "")]
        if capped:
            lines.append(f"其中 {len(capped)} 个填的日期超过系统上限，已改为系统最晚可选日期")

        lines += ["", f"明细：{self.s['result_file']}", f"截图：{self.s['screenshot_dir']}"]
        self.ui.finished("DMP延期完成" if not stats["failed"] else "DMP延期完成（有失败）",
                         "\n".join(lines), not stats["failed"])


_GAP = "[\\s -​　]*"


def _spaced(text: str):
    """「保存」→ 能匹配「保 存」「保 存」「 保存 」的整串正则（antd 双字按钮）。"""
    chars = [re.escape(c) for c in _squeeze(text)]
    return re.compile("^" + _GAP + _GAP.join(chars) + _GAP + "$")


def _squeeze(text: str) -> str:
    """剔掉所有空白，含全角空格 U+3000 和 antd 实际用的 U+2005。

    「保 存」→「保存」。\\s 匹配不到全角空格，只用 strip/replace(' ') 会漏。
    """
    return re.sub(r"[\s -​　]", "", text or "")


def _host(url: str) -> str:
    try:
        return urlsplit(url or "").netloc
    except ValueError:
        return ""


# Chromium 的网络错误码 → 人话。原来界面上直接显示
# 「: net::ERR_NAME_NOT_RESOLVED at https://… Call log: …」，看着像程序坏了。
_NET_HINTS = (
    ("ERR_NAME_NOT_RESOLVED", "域名解析失败 —— 多半是内网/VPN 没连上"),
    ("ERR_INTERNET_DISCONNECTED", "电脑断网了"),
    ("ERR_CONNECTION", "连不上服务器 —— 内网/VPN 断了，或者后台在发版"),
    ("ERR_NETWORK_CHANGED", "网络刚切换过（换了 Wi-Fi / VPN 重连）"),
    ("ERR_TIMED_OUT", "服务器一直没响应 —— 内网卡了"),
    ("Timeout", "页面一直没加载完 —— 内网卡了"),
)


def _nav_error(url: str, e: Exception) -> str:
    raw = str(e)
    if "closed" in raw.lower():
        return "操作的那个 Chrome 标签页被关掉了。重新点「载入并检查」即可"
    why = next((hint for code, hint in _NET_HINTS if code in raw), "")
    if why:
        return f"打不开 DMP 人群列表（{why}）。网络好了之后重新点「载入并检查」"
    return f"打不开 DMP 人群列表：{raw.splitlines()[0] if raw else type(e).__name__}"
