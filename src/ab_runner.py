"""AB 实验续期执行器。

和 DMP 延期同一套骨架：运行前读取「我的实验」里状态为「实验中」的实验，
逐条打开「其他 → 续期」，把到期日选到平台允许的最晚一天，再提交。

和 DMP 的三处实质差异，都是页面本身决定的：
1. 列表默认是全站 7800+ 条，必须先点「我的实验」收敛；这个筛选不写进 URL，
   每次重新打开列表都要重新点一次。
2. 可选日期有上限（平台限制实验最长时长），上限可能落在下个月，所以要往后
   翻月找真正最晚的那天，不能像 DMP 那样只看当前月。
3. 弹窗里「续期」一步到位，没有 DMP 那种额外的「保存」步骤。

⚠ 这个文件里不写「等 N 秒再往下走」（CLAUDE.md 硬约定第 1 条）。
  1.1.20 之前这里有 17 处写死的等待（点「我的实验」等 2 秒、搜索等 2.5 秒、
  翻页等 1.2 秒 …），和 1.1.18 的 DMP 延期是同一种炸法：网一卡就读到半截的
  列表 —— 读到的是筛选之前那一屏、搜不到明明存在的实验、翻页没等到当成最后一页。
  现在的判据（和 dmp_runner 一样，但**不共用代码**，两家 DOM / 接口都不同）：
    · 列表刷新 → 盯住列表接口（_ListWatch）：这个动作之后发出的请求回来了、
      表格里的实验 ID 和接口给的一模一样，才算刷新完。上限 list_ready_timeout。
    · 菜单 / 弹窗 / 日期面板 → 等它出现（或消失），上限 settings.timeout。
    · 最后一页 → 按接口给的 totalPageSize 算，不靠「翻不动」猜。翻页失败是报错，不是收工。
  列表接口的样子（抓取记录见 docs/AB实验延期-页面结构.md）：
    GET /ab/v3/experiment/list?...&userId=<我>&currentSize=<页码>&perPageSize=15&queryParam=<搜索词>
    → {"code": 200, "items": [...], "pageVO": {"currentSize", "perPageSize",
                                               "totalPageSize", "totalSize"}}

页面选择器集中在 config/forms/AB实验延期.yaml。页面改版后优先改配置，不要把
业务 DOM 细节散落到流程代码中。
"""
from __future__ import annotations

import csv
import logging
import math
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .browser import Browser
from .dmp_date import fmt as fmt_date, parse_date
from .filler import FillError
from .fill_core import wait_until
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


class _ReqLog:
    """盯着某一类接口请求：发出去几个、还有几个没回来、最后发出去的是哪个。

    ⚠ 只认 GET：跨域时浏览器会先发 OPTIONS 预检，响应体是空的。
    ⚠ 事件回调里只记账、不读响应体：sync API 的回调里做阻塞调用不安全。
    """

    def __init__(self, page, pattern: str):
        self.page = page
        self.rx = re.compile(pattern)
        self.sent = 0           # 发出去的请求总数
        self.pending = 0        # 还没回来的
        self.finished = 0       # 回来了的（含失败）
        # ⚠ 记「最后发出去的」那个，不是「最后回来的」：搜索时页面会先按旧页码发一次、
        #   紧接着按第 1 页再发一次（实测），回来的顺序不一定。
        self._last = None
        self._errors = {}       # id(request) → 失败原因
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_finished)
        page.on("requestfailed", self._on_failed)

    def _mine(self, req) -> bool:
        try:
            return req.method == "GET" and bool(self.rx.search(req.url))
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

    def last_group(self) -> str:
        """最后那个请求 URL 里 pattern 第一个括号抓到的东西（详情接口 = 实验 ID）。"""
        m = self.rx.search(self.last_url)
        return (m.group(1) if m and m.groups() else "") or ""


class _ListWatch(_ReqLog):
    """实验列表接口。列表页上所有「等它刷新完」都靠它判定，不按秒数猜。"""

    def __init__(self, page, pattern: str):
        super().__init__(page, pattern)
        self.consumed = 0       # 主流程已经读过的 finished 序号

    def _read(self) -> dict:
        """最后发出去的那个列表请求的响应体（整个 body）。

        响应体读不到（浏览器已经把它回收了）返回 {}，由调用方退回看页面。
        ⚠ 接口**明确报错**必须抛出去，不能当成空列表 —— 当成空列表，
          翻页时就会变成「已经是最后一页」静默收工（DMP 1.1.18 就是这么漏的）。
        """
        self.consumed = self.finished
        err = self._errors.get(id(self._last))
        if err:
            raise FillError(f"实验列表接口请求失败（{err}）—— 内网/VPN 断了？")
        try:
            resp = self._last.response()
            status = resp.status if resp else None
            body = resp.json() if resp else None
        except Exception:
            log.info("列表接口响应体读不到，退回只看页面", exc_info=True)
            return {}
        body = body if isinstance(body, dict) else {}
        code = body.get("code")
        # 这个后台成功是 code=200（不是 0）；两个都认
        if status != 200 or code not in (0, 200, None) or not isinstance(body.get("items"), list):
            msg = str(body.get("msg") or body.get("message") or "")[:80]
            raise FillError(f"实验列表接口返回异常（HTTP {status}，code={code}"
                            f"{'，' + msg if msg else ''}）—— 后台卡了或者在发版")
        return body

    def settle(self, since: int, timeout: int, dom_keys, start_timeout: int = 5000) -> dict | None:
        """等「since 之后发出的列表请求」全部回来、并且表格已经渲染成接口给的那一批。

        返回接口 body；超时返回 None（self.sent == since 说明请求压根没发出去）。
        ⚠ 两段围栏：请求是点下去当场就发的，start_timeout 内没发出去就别再等了；
          发出去之后才用 timeout 等它回来。
        """
        if not wait_until(self.page, lambda: self.sent > since, min(start_timeout, timeout)):
            return None
        return self._latest(timeout, dom_keys)

    def idle(self, timeout: int, dom_keys) -> dict | None:
        """如果有列表请求在路上（比如续期后页面自己刷新列表），等它回来并渲染完。

        返回新数据；期间没有新请求返回 None（列表没动过）。
        """
        wait_until(self.page, lambda: self.pending == 0, timeout)
        if self.finished <= self.consumed:
            return None
        return self._latest(timeout, dom_keys)

    def _latest(self, timeout: int, dom_keys) -> dict | None:
        """等路上的请求都回来，读最后发出的那个，等表格渲染成它。

        ⚠ 等渲染的时候页面又发了一个（搜索连发两个时的常态），就改等新的那个：
          表格会直接换成新那一批，死等旧那一批对上只会等满围栏。
        """
        body = None
        for _ in range(10):
            if not wait_until(self.page, lambda: self.pending == 0, timeout):
                return None
            mark = self.sent
            body = self._read()
            items = body.get("items")
            if not isinstance(items, list):
                return body
            ids = [str(r.get("id", "")) for r in items if isinstance(r, dict)]
            if not wait_until(self.page, lambda: dom_keys() == ids or self.sent != mark, timeout):
                log.warning("列表接口回来了，但表格 %d 毫秒内没渲染成接口给的那一批", timeout)
                return body
            if self.sent == mark:
                return body
        return body


class AbRunner(StateMixin):
    def __init__(self, settings: dict, form_cfg: dict, ui: BaseUI | None = None):
        self.s = settings
        self.f = form_cfg
        self.ui = ui or ConsoleUI()
        self.shot_dir = Path(settings["screenshot_dir"])
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self._init_state()
        self.auto = False
        self._watch: _ListWatch | None = None
        self._detail: _ReqLog | None = None
        self._meta: dict = {}       # 最近一次列表接口的 pageVO
        self._items: dict = {}      # 最近一次列表接口的 实验ID → item
        self.scope = (settings.get("ab_scope") or form_cfg.get("scope") or SCOPE_MINE)
        if self.scope not in SCOPE_LABELS:
            raise FillError(f"不认识的延期范围「{self.scope}」，可选：{list(SCOPE_LABELS)}")

    # ---------------- 两个围栏时间 ----------------
    def _list_fence(self) -> int:
        """列表接口的上限。它比页面控件慢得多，内网抖一下更久。"""
        return int(self.f.get("list_ready_timeout", 45000))

    def _ui_fence(self) -> int:
        """菜单 / 弹窗 / 日期面板出现或消失的上限。"""
        return int(self.s.get("timeout", 15000))

    # ---------------- 预检 ----------------
    def preview(self) -> list[PreviewRow]:
        """mine：读「我的实验」里状态为「实验中」的实验。

        id_list：把清单和页面对一遍 —— 搜不到的 ID 在这一步就标红，
        不用等跑到一半才发现。
        """
        wanted = self._load_wanted() if self.scope == SCOPE_ID_LIST else None
        with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
            self._open_list(b.page)
            if self.scope == SCOPE_ID_LIST:
                targets = self._collect_by_search(b.page, wanted)
                self._clear_search(b.page)
            else:
                self._select_my_experiments(b.page)
                targets = self._all_active_targets(b.page)

        # 同一个实例接着 run()（webapp 和 --cli 都是）：run 用它判断「预检列出的都走到没有」
        self._targets = targets

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
            # ⚠ 保留 hit 自带的 issues（「不是实验中，续不了」）—— 1.1.19 之前这里
            #   直接清成 []，已结束的实验也会被当成可续期的去跑
            target["issues"] = list(hit.get("issues") or [])
            out.append(target)
        return out

    def _missing(self, w: dict, why: str) -> dict:
        return {"key": w["id"] or f"row{w['row']}", "id": w["id"], "name": w["name"] or w["id"],
                "status": "", "end_date": "", "page": 1,
                "want_date": w.get("date"), "want_date_raw": w.get("date_raw", ""),
                "issues": [why]}

    # ---------------- 主流程 ----------------
    def run(self, records: list[dict] | None = None):
        # 勾选范围从 records 的实验 ID 还原（预检界面上用户可能删掉了几行）
        wanted = None
        if records is not None:
            wanted = {str(r.get("header", {}).get("实验ID")): str(r.get("header", {}).get("实验名称") or "")
                      for r in records if r.get("header", {}).get("实验ID")}
            wanted = wanted or None

        dry = bool(self.s.get("dry_run"))
        stats = {"ok": 0, "failed": 0, "skipped": 0, "dry": 0}
        results = []
        ctx = {"dry": dry, "stats": stats, "results": results, "i": 0, "seen": set()}

        try:
            with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
                self.ui.log(f"「{self.f['name']}」范围：{SCOPE_LABELS[self.scope]}" +
                            ("（试跑，不提交）" if dry else ""))
                if self.scope == SCOPE_ID_LIST:
                    self._run_by_list(b.page, wanted, ctx)
                else:
                    self._run_by_pages(b.page, wanted, ctx)
        except Stopped:
            self.ui.log("已停止", "warn")
            ctx["stopped"] = True
        finally:
            if not ctx.get("stopped"):
                self._account_unreached(wanted, ctx)
            self._write_results(results)
            self._report(stats, results, dry)
        return results

    def _planned(self, wanted) -> list[dict]:
        """这一轮「应该」走到的实验：预检列出的（去掉标红的），再按勾选过滤。

        没走过预检（只有 records）时，就按 records 里的实验 ID 算。
        """
        planned = [t for t in getattr(self, "_targets", None) or [] if not t.get("issues")]
        if wanted is None:
            return planned
        if planned:
            return [t for t in planned if t["id"] in wanted]
        return [{"key": k, "id": k, "name": v or k, "end_date": ""} for k, v in wanted.items()]

    def _account_unreached(self, wanted, ctx: dict):
        """预检列出来、这一轮却一次都没走到的实验，记成失败。

        ⚠ 这是兜底，不是正常路径：翻页/列表出了意外，宁可报「没走到」，
          也不能安安静静地「成功 6 个、失败 0 个」收工 —— 那种结果用户根本不会去查。
        """
        pending = [t for t in self._planned(wanted)
                   if t["key"] not in ctx["seen"] and not self.state.is_done(t["key"])]
        if not pending:
            return
        names = "、".join(t["name"] for t in pending[:5]) + ("…" if len(pending) > 5 else "")
        self.ui.log(f"有 {len(pending)} 个实验这一轮没走到：{names}（重跑会接着做）", "error")
        for t in pending:
            ctx["i"] += 1
            ctx["stats"]["failed"] += 1
            ctx["results"].append(self._result(ctx["i"], t, "failed",
                                               "这一轮没走到（列表翻页中断），重跑会接着做", ""))

    @staticmethod
    def _keep_wanted(targets: list[dict], wanted) -> list[dict]:
        if wanted is None:
            return targets
        return [t for t in targets if t["id"] in wanted]

    # ---------------- 两种走法 ----------------
    def _run_by_list(self, page, wanted, ctx: dict):
        """清单模式：逐个 ID 用搜索框直达。"""
        self._open_list(page)
        targets = self._collect_by_search(page, self._load_wanted())
        for t in [x for x in targets if x.get("issues")]:
            self.ui.log(f"跳过「{t['name']}」：{t['issues'][0]}", "warn")
            ctx["seen"].add(t["key"])
        targets = self._keep_wanted([t for t in targets if not t.get("issues")], wanted)

        total = len(targets)
        self.ui.log(f"待处理 {total} 个实验")
        self.ui.progress(0, total, ctx["stats"])
        try:
            for target in targets:
                # 每条开头重新搜一次（在 _locate_row 里）：上一条处理完，
                # 搜索框还停在上一个 ID 上
                if self._process_one(page, target, total, ctx) == "stop":
                    return
        finally:
            # 别把页面留在「只剩最后搜的那一条」的状态
            self._clear_search(page)

    def _run_by_pages(self, page, wanted, ctx: dict):
        """我的实验：顺着列表一页一页往下续，处理完当前页再翻页。

        列表按状态分组排，「实验中」全部排在最前面，扫到第一个非「实验中」就收工。
        ⚠ 「最后一页」按接口的 totalPageSize 算；翻页失败不当成「到底了」。
        """
        self._open_list(page)
        self._select_my_experiments(page)

        total = len(self._planned(wanted))
        self.ui.log(f"待处理 {total} 个实验" if total else "顺着列表逐页处理")
        self.ui.progress(0, total, ctx["stats"])

        page_no = 1
        for _ in range(1000):
            found, hit_inactive = self._scan_page(page)
            batch = self._keep_wanted(found, wanted)
            for t in batch:
                t["page"] = page_no
            self.ui.log(f"第 {page_no} 页：{len(batch)} 个实验中")
            for target in batch:
                if self._process_one(page, target, total, ctx) == "stop":
                    return
            if hit_inactive:
                self.ui.log(f"第 {page_no} 页已经出现非「{self._active_text()}」的实验，"
                            f"后面都不会再有了，停止翻页")
                return
            planned = self._planned(wanted)
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
        raise FillError("翻了 1000 页还没到头，列表不对劲")

    # ---------------- 单条处理 ----------------
    def _process_one(self, page, target: dict, total: int, ctx: dict) -> str:
        """处理一个实验。返回 'next' 继续，'stop' 中止整轮。"""
        stats, results, dry = ctx["stats"], ctx["results"], ctx["dry"]
        ctx["seen"].add(target["key"])

        if self.state.is_done(target["key"]):
            self.ui.log(f"{target['name']} 已完成过，跳过")
            return "next"

        self.ui.checkpoint()
        ctx["i"] += 1
        i = ctx["i"]
        label = f"[{i}/{total}]" if total else f"[{i}]"
        try:
            self._open_extension(page, target)
            picked = self._pick_date(page, target)

            # 选出来的日期并不比现在的到期日晚：提交等于空操作（平台多半还会报错），
            # 清单里写了个更早的日期甚至会把实验改短 —— 直接跳过
            if self._no_gain(target["end_date"], picked):
                self._close_dialog(page)
                stats["skipped"] += 1
                why = ("已经是最晚可选日期，无需续期" if not target.get("want_date")
                       or picked == target["end_date"]
                       else f"现在的到期日 {target['end_date']} 已不早于 {picked}，不缩短")
                results.append(self._result(i, target, "no_change", why, picked))
                self.ui.log(f"{label} {target['name']} {why}（{picked}），跳过", "warn")
                return "next"

            self._screenshot(page, i, "ready")
            self.ui.log(f"{label} {target['name']} 到期日 {target['end_date']} "
                        f"→ 已选：{picked}", "ok")

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
                results.append(self._result(i, target, "dry_run", "未提交", picked))
                return "next"

            self._submit(page)
            self.state.mark_done(target["key"])
            stats["ok"] += 1
            results.append(self._result(i, target, "ok", "", picked))
            self.ui.log(f"{label} {target['name']} 已续期到 {picked}", "ok")
            return "next"
        except Stopped:
            raise
        except NotExtendable as e:
            # 平台不让续了，不是脚本的错，跳过就好，别中断整批
            self._close_dialog(page)
            stats["skipped"] += 1
            results.append(self._result(i, target, "not_extendable", str(e), ""))
            self.ui.log(f"{label} {target['name']} {e}，跳过", "warn")
            return "next"
        except Exception as e:
            msg = str(e)
            log.exception("AB 续期失败：%s", target["name"])
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

    @staticmethod
    def _no_gain(old: str, picked: str) -> bool:
        """选出来的日期并不比现在的到期日更晚。

        两边都必须是 YYYY-MM-DD 才比较；格式对不上时一律当作「有变化」，
        宁可多提交一次，也不要因为解析失败把该续的实验静默跳过。
        """
        pat = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        if not (pat.match(old or "") and pat.match(picked or "")):
            return False
        return picked <= old

    # ---------------- 列表：打开 / 刷新 / 翻页 ----------------
    def _watch_on(self, page) -> _ListWatch:
        if self._watch is None or self._watch.page is not page:
            self._watch = _ListWatch(page, self.f.get("list_api", r"/ab/v3/experiment/list\?"))
            # detail_api 写成空串 = 不核对弹窗是哪个实验的（详情接口改名时的应急开关）
            detail = self.f.get("detail_api", r"/ab/v3/experiment/(\d+)(?:[?#]|$)")
            self._detail = _ReqLog(page, detail) if detail else None
        return self._watch

    def _set_meta(self, body: dict):
        if not body:
            return
        self._meta = body.get("pageVO") if isinstance(body.get("pageVO"), dict) else {}
        self._items = {str(it.get("id")): it for it in body.get("items") or []
                       if isinstance(it, dict)}

    def _refresh(self, page, action, what: str, accept=None) -> dict:
        """做一个会让列表重新拉数据的动作，等到新数据真的渲染出来。返回接口 body。

        accept(url)：最后发出去的那个列表请求的 URL 得满足它才算数。
        ⚠ 搜索时页面会连发两个请求（先按旧页码、再按第 1 页），第一个回来时
          第二个可能还没发出去 —— 光看「请求都回来了」会读到旧页码那一个。
        """
        w = self._watch_on(page)
        fence = self._list_fence()
        try:
            self._idle(page)        # 先让路上的请求落地，免得和这次的混在一起
        except FillError:
            log.info("动作之前那一次列表刷新是坏的，忽略，以这次为准", exc_info=True)
        since = w.sent
        action()
        keys = lambda: self._dom_keys(page)     # noqa: E731
        body = w.settle(since, fence, keys)
        if body is None:
            if w.sent == since:
                raise FillError(f"{what}之后页面没有去刷新实验列表（按钮没点上？）")
            raise FillError(f"{what}之后等了 {fence // 1000} 秒实验列表还没刷新出来"
                            f"（内网卡了？可以把 yaml 的 list_ready_timeout 调大）")
        for _ in range(3):
            if accept is None or accept(w.last_url):
                break
            # 等页面接着发的那一个；它是紧跟着第一个发的，没来就是真没有
            body = w.settle(w.sent, fence, keys,
                            start_timeout=int(self.f.get("follow_up_timeout", 3000)))
            if body is None:
                break
        if accept is not None and (body is None or not accept(w.last_url)):
            raise FillError(f"{what}之后列表没有按预期刷新（最后一次请求：…{w.last_url[-90:]}）")
        self._set_meta(body)
        return body

    def _idle(self, page):
        """如果页面自己在刷新列表（比如刚续期完），等它刷完再动。"""
        if self._watch is None:
            return
        body = self._watch.idle(self._list_fence(), lambda: self._dom_keys(page))
        if body:
            self._set_meta(body)

    def _open_list(self, page):
        """打开列表页并等到第一屏数据真的渲染出来。

        ⚠ SPA：DOM 里先有空的 el-table 骨架，行要等接口回来才渲染，所以等的是接口。
        ⚠ URL 是 # 路由：已经停在这个站点上时直接 goto 只改 # 后面，页面不重新加载、
          列表也不重新请求 —— 先跳 about:blank 再回来，保证是一次干净的加载
          （「我的实验」筛选也跟着清掉，下面重新点）。
        """
        url = self.f["form_url"]
        w = self._watch_on(page)
        fence = self._list_fence()
        for attempt in (1, 2):
            try:
                if attempt == 1:
                    if _same_doc(page.url, url):
                        page.goto("about:blank")
                    since = w.sent
                    page.goto(url, wait_until="domcontentloaded")
                else:
                    since = w.sent
                    page.reload(wait_until="domcontentloaded")
            except Exception as e:
                raise FillError(_nav_error(url, e)) from e
            try:
                body = w.settle(since, fence, lambda: self._dom_keys(page), start_timeout=fence)
            except FillError as e:
                if attempt == 2:
                    raise
                self.ui.log(f"{e}，刷新重试一次", "warn")
                continue
            if body is not None:
                self._set_meta(body)
                if self._dom_keys(page):
                    return
                # 这时还没点「我的实验」，是全站列表，现实里不会是空的
                if attempt == 1:
                    self.ui.log("实验列表是空的，刷新再确认一次", "warn")
                    continue
                self.ui.log("列表是空的（页面显示无数据）", "warn")
                return
            if _host(page.url) != _host(url):
                break           # 被跳去登录页了，刷新没用
            if attempt == 1:
                self.ui.log(f"实验列表 {fence // 1000} 秒没加载出来，刷新重试一次", "warn")

        if _host(page.url) != _host(url):
            raise FillError(f"打开实验列表时页面跳到了 {_host(page.url)} —— 多半是 AB 平台的登录"
                            f"过期了。在 Chrome 里重新登录一下，再点「载入并检查」")
        raise FillError(f"实验列表等了 {fence // 1000} 秒（还刷新重试了一次）都没加载出来 —— "
                        f"内网/VPN 卡了？在 Chrome 里手动打开这个页面看看能不能出数据")

    def _select_my_experiments(self, page):
        """点「我的实验」把列表收敛到自己创建的实验。

        这个筛选不进页面 URL，页面一刷新就没了，所以每次打开列表都要点一次。
        它不是开关，重复点仍然是「我的实验」，不会被反选回全量。
        ⚠ 点完之后等的是「带 userId 的列表请求」回来、表格换成那一批 ——
          点下去到接口回来之间，表格上摆的还是全站那一屏。
        """
        param = self.f.get("my_experiment_param", "userId")
        if _param(self._watch_on(page).last_url, param):
            return          # 已经是「我的实验」了
        text = self.f.get("my_experiment_text", "我的实验")
        button = None
        for selector in self.f.get("my_experiment_selectors", []):
            loc = page.locator(f"{selector}:visible").first
            if loc.count():
                button = loc
                break
        if button is None:
            # 退回按文案找，页面改版换了 class 时还能撑住
            loc = page.get_by_text(text, exact=True)
            button = next((loc.nth(i) for i in range(loc.count()) if loc.nth(i).is_visible()),
                          None)
        if button is None:
            raise FillError(f"列表页找不到「{text}」筛选按钮；请更新 my_experiment_selectors")
        self._refresh(page, button.click, f"点「{text}」", accept=lambda u: bool(_param(u, param)))
        total = self._meta.get("totalSize")
        self.ui.log(f"已筛选到「{text}」" + (f"（共 {total} 个）" if total is not None else ""))

    def _cur_page(self, page) -> int:
        n = self._meta.get("currentSize")
        if n:
            return int(n)
        try:   # 接口响应体读不到时退回看分页器上高亮的那一页
            t = page.locator(".el-pagination:visible .el-pager li.is-active").first.inner_text(
                timeout=2000)
            return int(t.strip() or 1)
        except Exception:
            return 1

    def _page_count(self) -> int | None:
        """总页数；接口没给就返回 None（退回看「下一页」按钮是不是禁用）。"""
        pages = self._meta.get("totalPageSize")
        if pages is not None:
            return max(1, int(pages))
        total, size = self._meta.get("totalSize"), self._meta.get("perPageSize")
        if total is None or not size:
            return None
        return max(1, math.ceil(int(total) / int(size)))

    def _pager_button(self, page, forward: bool):
        """「下一页 / 上一页」里真正能点的那个；禁用或不存在返回 None。"""
        key = "next_page_selectors" if forward else "prev_page_selectors"
        for selector in self.f.get(key) or []:
            items = page.locator(selector)
            for i in range(items.count()):
                item = items.nth(i)
                try:
                    if not item.is_visible() or not item.is_enabled():
                        continue
                    if item.get_attribute("disabled") is not None:
                        continue
                    if item.get_attribute("aria-disabled") == "true":
                        continue
                    if "disabled" in (item.get_attribute("class") or ""):
                        continue
                    return item
                except Exception:
                    continue
        return None

    def _next_disabled(self, page) -> bool:
        """分页器还在、而且「下一页」是灰的 —— 页面自己说「到底了」。

        ⚠ 分页器整个不见了不算：接口报错时分页器可能一起消失，那不是到底，是出错了。
        """
        for selector in self.f.get("next_page_selectors") or []:
            try:
                items = page.locator(selector)
                for i in range(items.count()):
                    it = items.nth(i)
                    if it.is_visible() and (it.get_attribute("disabled") is not None
                                            or it.get_attribute("aria-disabled") == "true"
                                            or "disabled" in (it.get_attribute("class") or "")):
                        return True
            except Exception:
                continue
        return False

    def _reopen_list(self, page):
        """列表出错之后的恢复：重新打开列表、再点一次「我的实验」，回到第 1 页。"""
        self._open_list(page)
        self._select_my_experiments(page)

    def _goto_page(self, page, n: int) -> bool:
        """把列表翻到第 n 页。n 超过总页数返回 False（这才是「到底了」）。

        能前能后：续期之后页面若自己把列表刷回了第 1 页，这里会翻回来。
        翻页时接口出错/超时：重新打开列表再翻过去，最多 page_retries 次；
        还不行就抛 FillError —— **任何时候都不把「翻不动」当成最后一页**。
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

    # ---------------- 列表：读 ----------------
    ROW_JS = """els => els.map(e => ({
        cells: [...e.querySelectorAll('td')].map(td => (td.innerText || '').trim()),
        shown: e.offsetParent !== null,
    }))"""

    def _rows_loc(self, page):
        """页面里有三张表（实验管理 / 联调实验 / 发布管理），只认当前可见那张的行。"""
        return page.locator(f"{self.f.get('table_selector', '.el-table')}:visible "
                            f"{self.f.get('row_selector', 'tbody tr')}")

    def _rows_of(self, page) -> list[dict]:
        """把当前这一屏读成结构化数据。

        ⚠ 一次 evaluate 取回整张表，不要 rows.nth(i) 逐行读：列表随时会异步重渲染，
          中途行数变少就会卡在「等 nth(7) 出现」直到超时。
        """
        try:
            raw = self._rows_loc(page).evaluate_all(self.ROW_JS)
        except Exception:
            return []
        return self._parse_rows(raw)

    def _parse_rows(self, raw: list[dict]) -> list[dict]:
        name_col = int(self.f.get("name_column", 0))
        status_col = int(self.f.get("status_column", 1))
        date_col = int(self.f.get("end_date_column", 3))
        id_rx = re.compile(self.f.get("id_pattern", r"ID[:：]\s*(\d+)"))
        out = []
        for r in raw:
            cells = r.get("cells") or []
            if not r.get("shown", True) or len(cells) <= status_col:
                continue
            raw_name = cells[name_col] if len(cells) > name_col else ""
            found = id_rx.search(raw_name)
            parts = ([x.strip() for x in cells[date_col].splitlines() if x.strip()]
                     if len(cells) > date_col else [])
            out.append({
                "id": found.group(1) if found else "",
                "name": (raw_name.splitlines() or [""])[0].strip(),
                "status": cells[status_col].strip(),
                "end_date": parts[-1] if parts else "",
            })
        return out

    def _dom_keys(self, page) -> list[str]:
        """表格里当前这一批行的实验 ID，按显示顺序（和接口 items 的 id 顺序对比）。"""
        try:
            return [r["id"] for r in self._rows_of(page)]
        except Exception:
            return ["<读不到>"]

    def _active_text(self) -> str:
        return self.f.get("active_status", "实验中")

    def _as_target(self, r: dict, i: int) -> dict:
        exp_id = r["id"]
        # 到期日优先用接口给的 expirationTime：表格那一列有时渲染不全
        api_end = str((self._items.get(exp_id) or {}).get("expirationTime") or "")[:10]
        return {"key": exp_id or f"{r['name']}|{i}", "id": exp_id, "name": r["name"],
                "status": r["status"], "end_date": api_end or r["end_date"]}

    def _scan_page(self, page) -> tuple[list[dict], bool]:
        """扫当前页，返回（本页的「实验中」记录，本页是否出现了非「实验中」的行）。

        列表是按状态分组排的，「实验中」全部排在最前面（实测接口 runStatus 2 在前、
        3 在后），所以一旦扫到一行不是「实验中」，后面的行和后面所有页就都不会再有了。
        """
        out = []
        for i, r in enumerate(self._rows_of(page)):
            # 按列精确匹配，不用整行包含：实验名里带「实验中」不该被选进来
            if r["status"] != self._active_text():
                return out, True
            out.append(self._as_target(r, i))
        return out, False

    def _all_active_targets(self, page) -> list[dict]:
        """逐页扫描，扫到第一个非「实验中」就收工。

        翻页失败直接抛出去（预检报错），不能拿半截名单当全部。
        """
        targets, seen, page_no = [], set(), 1
        for _ in range(1000):
            found, hit_inactive = self._scan_page(page)
            for target in found:
                if target["key"] in seen:
                    continue
                seen.add(target["key"])
                target["page"] = page_no
                targets.append(target)
            if hit_inactive:
                self.ui.log(f"第 {page_no} 页已经出现非「{self._active_text()}」的实验，"
                            f"后面都不会再有了，停止翻页")
                return targets
            if not self._goto_page(page, page_no + 1):
                return targets
            page_no += 1
        raise FillError("翻了 1000 页还没到头，列表不对劲")

    # ---------------- 搜索 ----------------
    def _search_box(self, page):
        for selector in self.f.get("search_input_selectors", []):
            loc = page.locator(f"{selector}:visible").first
            if loc.count():
                return loc
        return None

    def _find_id(self, page, exp_id: str) -> dict | None:
        """当前这一屏里 ID 等于 exp_id 的那一行；状态不是「实验中」的带上 issues。

        清单模式是用户点名的，不该因为状态不符就当作「搜不到」而误导人；
        搜到了但状态不对，要明确告诉他这个实验不是实验中。
        """
        for i, r in enumerate(self._rows_of(page)):
            if r["id"] != exp_id:
                continue
            t = self._as_target(r, i)
            if r["status"] != self._active_text():
                t["end_date"] = ""
                t["issues"] = [f"这个实验现在是「{r['status']}」，不是「{self._active_text()}」，续不了"]
            return t
        return None

    def _search_for(self, page, keyword: str):
        """用搜索框按实验ID定位，命中就返回那一行的 target，搜不到返回 None。

        ⚠ 等的是「这次搜索发出的请求」回来并渲染完：搜索响应回来之前，
          旧那一屏本来就是安静的，一判就过（误判成搜不到）。
        ⚠ 搜索框里已经是这个词时再按回车，页面**不会重新请求**（实测）。
          眼前这一屏如果就是它的搜索结果，直接用；不是就先清空搜索框逼页面重新请求。
        """
        box = self._search_box(page)
        if box is None:
            raise FillError("列表页找不到搜索框；请更新 search_input_selectors")
        kw = str(keyword).strip()
        q = self.f.get("search_param", "queryParam")
        pg = self.f.get("page_param", "currentSize")
        self._idle(page)
        try:
            same = (box.input_value() or "").strip() == kw
        except Exception:
            same = False
        if same and _param(self._watch_on(page).last_url, q) == kw:
            return self._find_id(page, kw)

        def is_search(want):
            return lambda u: _param(u, q) == want and _param(u, pg) in ("", "1")

        last = None
        for attempt in range(int(self.f.get("search_attempts", 3))):
            if same or attempt:
                try:
                    self._refresh(page, lambda: (box.fill(""), box.press("Enter")),
                                  "清空搜索框", accept=is_search(""))
                except FillError:
                    log.info("清空搜索框没触发刷新，照样往下搜", exc_info=True)
                same = False
            try:
                self._refresh(page, lambda: (box.fill(kw), box.press("Enter")),
                              f"搜「{kw}」", accept=is_search(kw))
            except FillError as e:
                last = e
                log.info("第 %d 次搜「%s」没等到结果：%s", attempt + 1, kw, e)
                continue
            return self._find_id(page, kw)
        raise last or FillError(f"搜「{kw}」失败")

    def _clear_search(self, page):
        """收尾清空搜索框。

        不清的话页面会一直停在「只剩最后搜的那一条」，下次有人手动打开
        这个页面会以为实验没了。
        """
        try:
            box = self._search_box(page)
            if box is None or not (box.input_value() or "").strip():
                return
            q = self.f.get("search_param", "queryParam")
            self._refresh(page, lambda: (box.fill(""), box.press("Enter")), "清空搜索",
                          accept=lambda u: _param(u, q) == "")
        except Exception:
            log.info("收尾清空搜索框失败，忽略", exc_info=True)

    # ---------------- 定位一行 ----------------
    def _row_for(self, page, target: dict):
        """定位目标实验那一行。

        ⚠ 不用 rows.nth(i) 按下标定位：列表会异步重渲染，拿到下标之后再去点，
          中间只要刷新一次，下标 i 指向的就是另一个实验了。行上没有 data-row-key，
          所以按「ID:12345」这段文字过滤，Playwright 每次动作都会重新解析。
        """
        rows = self._rows_loc(page)
        exp_id = str(target.get("id") or "")
        pattern = self.f.get("id_pattern", r"ID[:：]\s*(\d+)")
        if exp_id and r"(\d+)" in pattern:
            rx = re.compile(pattern.replace(r"(\d+)", re.escape(exp_id)) + r"(?!\d)")
            row = rows.filter(has_text=rx).first
            if row.count():
                return row
        elif target.get("name"):
            row = rows.filter(has_text=target["name"]).first
            if row.count():
                return row
        raise FillError(f"列表中找不到实验「{target['name']}」(ID:{target.get('id')})")

    def _locate_row(self, page, target: dict):
        """找到目标那一行，找不到时尽量自己把列表拨回去再找。

        · 清单模式：每条都重新搜一次它的 ID
        · 逐页模式：先等页面自己的刷新（续期后）落定；行不在当前页时翻回它原来那一页
        """
        if self.scope == SCOPE_ID_LIST:
            if self._search_for(page, target["id"]) is None:
                raise FillError(f"搜不到实验ID「{target['id']}」了")
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
    def _dialog_loc(self, page):
        return page.locator(f"{self.f.get('dialog_selector', '.el-dialog')}:visible")

    def _dialog(self, page):
        loc = self._dialog_loc(page)
        if not wait_until(page, lambda: loc.count() > 0, self._ui_fence()):
            raise FillError(f"没等到「{self.f.get('dialog_title', '实验续期')}」弹窗")
        return loc.first

    def _open_extension(self, page, target: dict):
        """点开这一行操作列的「其他」，再点菜单里的「续期」，并核对弹窗是不是这个实验的。

        ⚠ 每一行在 DOM 里都有自己的菜单（实测 17 个），只有刚展开那个是可见的；
          只认可见的，再用详情接口的实验 ID 兜底核对。
        ⚠ 「其他」是 hover 展开的 el-dropdown：鼠标已经停在触发器上时再点同一处
          不会触发 mouseenter，所以每次先把鼠标挪开。
        """
        row = self._locate_row(page, target)
        fence = self._ui_fence()
        row.wait_for(state="visible", timeout=fence)
        row.scroll_into_view_if_needed()

        last_cell = row.locator("td").last
        more_text = self.f.get("more_menu_text", "其他")
        trigger = None
        for selector in self.f.get("more_menu_selectors", []):
            loc = last_cell.locator(f"{selector}:visible").first
            if loc.count():
                trigger = loc
                break
        if trigger is None:
            loc = last_cell.get_by_text(more_text, exact=True)
            if loc.count():
                trigger = loc.first
        if trigger is None:
            raise FillError(f"「{target['name']}」行找不到操作列的「{more_text}」入口")

        item_text = self.f.get("extension_menu_item", "续期")
        item_sel = self.f.get("menu_item_selector", ".el-dropdown-menu__item")
        # ⚠ 只认「这个触发器自己的」那个菜单（触发器 aria-controls = 菜单 ul 的 id）。
        #   只按 :visible 找的话，上一行正在淡出的菜单也算可见 —— 实测点到它身上，
        #   它一收起 click 就干等满 15 秒。
        try:
            menu_id = trigger.get_attribute("aria-controls") or ""
        except Exception:
            menu_id = ""
        scope = f'[id="{menu_id}"] ' if re.fullmatch(r"[\w-]+", menu_id) else ""
        item = page.locator(f"{scope}{item_sel}:visible").filter(has_text=_spaced(item_text)).first
        others = page.locator(f"{item_sel}:visible").filter(has_text=_spaced(item_text))
        for attempt in range(3):
            page.mouse.move(5, 5)
            # 上一个实验的菜单收起来了再展开，免得两个菜单同时可见
            wait_until(page, lambda: not others.count(), fence)
            try:
                trigger.click(timeout=fence // 3)
            except Exception:
                log.info("点「%s」失败，重试", more_text, exc_info=True)
                continue
            if not wait_until(page, lambda: item.count() > 0, fence // 3):
                log.info("第 %d 次没能展开「%s」菜单，重试", attempt + 1, more_text)
                continue
            if "is-disabled" in (item.get_attribute("class") or "") \
                    or item.get_attribute("aria-disabled") == "true":
                raise FillError(f"「{target['name']}」的「{item_text}」是禁用状态")
            self._watch_on(page)
            n = self._detail.sent if self._detail else 0
            done = self._detail.finished if self._detail else 0
            try:
                # 菜单是 hover 出来的，点的瞬间可能正好收起：短超时，点不上就重来一轮
                item.click(timeout=fence // 3)
            except Exception:
                log.info("第 %d 次点「%s」没点上（菜单刚好收起？），重试",
                         attempt + 1, item_text, exc_info=True)
                continue
            self._dialog(page)
            self._check_dialog_target(page, target, n, done)
            return
        raise FillError(f"「{target['name']}」的「{more_text}」菜单里找不到「{item_text}」")

    def _check_dialog_target(self, page, target: dict, sent_before: int, finished_before: int):
        """弹窗必须是这个实验的，而且它的到期日已经加载出来。

        弹窗上不显示实验名称，但打开时会去拉 /ab/v3/experiment/<ID>（实测）——
        拿这个 ID 和目标对，续错实验比续不到严重得多。
        ⚠ 输入框里的日期是详情接口回来之后才填的；接口没回来就开日期面板，
          读到的是空的/上一个实验的日期，可选范围也不对。
        """
        fence = self._ui_fence()
        dialog = self._dialog(page)
        title = self.f.get("dialog_title", "实验续期")
        try:
            shown = dialog.inner_text(timeout=fence)
        except Exception:
            shown = ""
        if title and shown and _squeeze(title) not in _squeeze(shown):
            self._close_dialog(page)
            raise FillError(f"打开的不是「{title}」弹窗")

        exp_id = str(target.get("id") or "")
        det = self._detail
        if exp_id and det is not None:
            if wait_until(page, lambda: det.sent > sent_before, fence):
                got = det.last_group()
                if got and got != exp_id:
                    self._close_dialog(page)
                    raise FillError(f"打开的续期弹窗是实验 {got} 的，不是 {exp_id}，"
                                    f"为防续错实验，这一条不做")
            else:
                log.info("打开续期弹窗时没看到详情请求，跳过 ID 核对（行本身是按 ID 定位的）")

        date_input = self._date_input(dialog)
        want = str((self._items.get(exp_id) or {}).get("expirationTime") or "")[:10]

        def loaded():
            v = (date_input.input_value() or "").strip()
            if not v:
                return False
            if want and v == want:
                return True
            return det is None or det.finished > finished_before
        if not wait_until(page, loaded, fence):
            self.ui.log(f"续期弹窗 {fence // 1000} 秒还没读出「{target['name']}」现在的到期日", "warn")

    def _date_input(self, dialog):
        for selector in self.f.get("date_input_selectors", ["input"]):
            loc = dialog.locator(f"{selector}:visible").first
            if loc.count():
                return loc
        raise FillError("续期弹窗里找不到到期日期输入框")

    # ---------------- 日期面板 ----------------
    PANEL_JS = """(p, s) => ({
        label: [...p.querySelectorAll(s.label)].map(e => (e.innerText || '').trim())
                 .filter(Boolean).join(' '),
        days: [...p.querySelectorAll(s.cells)].map(td => [(td.innerText || '').trim(),
                                                         td.matches(s.avail)]),
    })"""

    def _panel_loc(self, page):
        return page.locator(f"{self.f.get('panel_selector', '.el-picker-panel')}:visible").first

    def _panel_snap(self, page) -> dict | None:
        """当前可见日期面板的一次快照：{label: '2026 年 11 月', days: [[文字, 可选], …]}。"""
        panel = self._panel_loc(page)
        try:
            if not panel.count():
                return None
            return panel.evaluate(self.PANEL_JS, {
                "label": self.f.get("month_label_selector", ".el-date-picker__header-label"),
                "cells": self.f.get("month_cells_selector",
                                    ".el-date-table td:not(.prev-month):not(.next-month)"),
                "avail": self.f.get("date_available_selector",
                                    ".el-date-table td.available:not(.disabled)"
                                    ":not(.prev-month):not(.next-month)"),
            })
        except Exception:
            return None

    @staticmethod
    def _snap_ym(snap: dict | None) -> tuple[int, int] | None:
        m = re.search(r"(\d{4})\D+(\d{1,2})", (snap or {}).get("label") or "")
        return (int(m.group(1)), int(m.group(2))) if m else None

    @classmethod
    def _rendered(cls, snap: dict | None) -> bool:
        """面板渲染完了：标题读得出年月，本月格子齐了（一个月至少 28 天）。"""
        return bool(snap) and cls._snap_ym(snap) is not None and len(snap.get("days") or []) >= 28

    @staticmethod
    def _month_state(snap: dict) -> tuple[list[int], bool]:
        """返回（本月可选的日子，本月最后一天是否还可选）。

        本月最后一天已经变灰，就说明可选范围的上限落在本月，不用再往后翻月。
        ⚠ 不能只看「本月有没有灰格子」：当月今天之前的日期本来就是灰的，
          那种灰不代表到顶了，所以判据必须是「最后一天」而不是「有没有」。
        """
        days = snap.get("days") or []
        avail = [int(t) for t, ok in days if ok and str(t).isdigit()]
        return avail, bool(days) and bool(days[-1][1])

    def _read_month(self, page) -> dict:
        """读当前月。读到「一个可选日期都没有」时再确认一下，别把没渲染完当成不能续。"""
        snap = self._panel_snap(page)
        if snap and not self._month_state(snap)[0]:
            wait_until(page, lambda: self._month_state(self._panel_snap(page) or {})[0],
                       min(2000, self._ui_fence()))
            snap = self._panel_snap(page) or snap
        return snap or {}

    def _open_panel(self, page, date_input):
        date_input.click()
        if not wait_until(page, lambda: self._rendered(self._panel_snap(page)), self._ui_fence()):
            raise FillError("点了到期日期输入框，日期面板没出来")

    def _step_month(self, page, forward: bool) -> bool:
        """翻一个月，等标题真的变了、格子重新渲染完再返回。没有可点的按钮返回 False。"""
        before = (self._panel_snap(page) or {}).get("label")
        panel = self._panel_loc(page)
        key = "next_month_selectors" if forward else "prev_month_selectors"
        for selector in self.f.get(key, []):
            button = panel.locator(f"{selector}:visible").first
            if not button.count() or not button.is_enabled():
                continue
            button.click()

            def moved():
                snap = self._panel_snap(page)
                return self._rendered(snap) and snap["label"] != before
            if not wait_until(page, moved, self._ui_fence()):
                raise FillError(f"点了「{'下' if forward else '上'}个月」，日期面板还停在 {before}")
            return True
        return False

    def _goto_month(self, page, year: int, month: int) -> bool:
        """把面板翻到指定年月。"""
        for _ in range(int(self.f.get("max_month_lookahead", 12)) * 2 + 2):
            ym = self._snap_ym(self._panel_snap(page))
            if ym is None:
                raise FillError("读不出日期面板的年月")
            if ym == (year, month):
                return True
            delta = (year - ym[0]) * 12 + (month - ym[1])
            if not self._step_month(page, delta > 0):
                return False
        return False

    def _click_day(self, page, date_input, year: int, month: int, day: int) -> str:
        """点面板上的某一天，等输入框回填成这一天再返回（回填的才是真正生效的日期）。"""
        want = date(year, month, day)
        cell = self._panel_loc(page).locator(self.f.get(
            "date_available_selector",
            ".el-date-table td.available:not(.disabled):not(.prev-month):not(.next-month)")
        ).filter(has_text=re.compile(rf"^\s*{day}\s*$")).first
        if not cell.count():
            raise FillError(f"日期面板上 {fmt_date(want)} 不可选")
        cell.click()
        value = lambda: (date_input.input_value() or "").strip()   # noqa: E731
        if not wait_until(page, lambda: parse_date(value()) == want, self._ui_fence()):
            raise FillError(f"点了 {fmt_date(want)}，输入框里却是「{value()}」")
        return value()

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

        (cy, cm), days = self._seek_latest_month(page, date_input)
        latest = date(cy, cm, days[-1])
        want = target.get("want_date")
        if want is not None and want < latest:
            if self._goto_month(page, want.year, want.month) \
                    and want.day in self._month_state(self._read_month(page))[0]:
                return self._click_day(page, date_input, want.year, want.month, want.day)
            self.ui.log(f"清单指定的 {fmt_date(want)} 在平台上不可选，"
                        f"改用最晚可选日期 {fmt_date(latest)}", "warn")
            if not self._goto_month(page, latest.year, latest.month):
                raise FillError("日期面板翻回上限月失败")
        return self._click_day(page, date_input, latest.year, latest.month, latest.day)

    def _seek_latest_month(self, page, date_input) -> tuple[tuple[int, int], list[int]]:
        """把面板停在「上限所在月」，返回（年月，该月可选的日子）。

        可选范围有上限，而且上限可能落在当前显示月之后，所以先往后翻月探路，
        记住最后一个还有可选日期的月份，再退回那个月。
        """
        self._open_panel(page, date_input)

        max_fwd = int(self.f.get("max_month_lookahead", 12))
        stop_after = int(self.f.get("stop_after_empty_months", 1))

        # best = 最后一个「有可选日期」的月份距初始月的步数；-1 = 还没见过
        best, steps, empty_streak = -1, 0, 0
        while True:
            avail, last_open = self._month_state(self._read_month(page))
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
            if steps >= max_fwd or not self._step_month(page, True):
                break
            steps += 1

        if best < 0:
            raise NotExtendable("平台没给出任何可选到期日，该实验已不能再续期")

        for _ in range(steps - best):
            if not self._step_month(page, False):
                raise FillError("日期面板回退月份失败；请检查 prev_month_selectors")

        snap = self._read_month(page)
        avail = self._month_state(snap)[0]
        ym = self._snap_ym(snap)
        if not avail or ym is None:
            raise FillError(f"回退到 {snap.get('label')} 后反而没有可选日期了")
        return ym, avail

    # ---------------- 提交 / 关闭 ----------------
    def _first_visible(self, page, selectors: list[str]):
        for selector in selectors:
            loc = page.locator(f"{selector}:visible").first
            if loc.count():
                return loc
        return None

    def _page_errors(self, page) -> list[str]:
        errors = []
        for selector in self.f.get("error_selectors", []):
            try:
                errors += [x.strip() for x in page.locator(f"{selector}:visible").all_inner_texts()
                           if x.strip()]
            except Exception:
                continue
        return list(dict.fromkeys(errors))

    def _click_in_dialog(self, page, texts: list[str]) -> str:
        """按按钮文字点弹窗里的按钮。

        ⚠ 按钮文字两边带空格（「 续期 」），标题「实验续期」里也有这两个字 ——
          只在 button 里找，文字整串匹配、允许字间空白。
        """
        dialog = self._dialog_loc(page)
        root = dialog.first if dialog.count() else page
        cands = root.locator("button:visible")
        for t in texts:
            btn = cands.filter(has_text=_spaced(t)).first
            if btn.count():
                btn.click()
                return t
        raise FillError(f"续期弹窗里找不到按钮：{'、'.join(texts)}")

    def _submit(self, page):
        """点「续期」，再点二次确认气泡里的「确定」，等弹窗关掉。

        ⚠ 弹窗上的「续期」不是终点：点完平台还会在按钮边上弹一个
          「确定续期吗?」的小气泡（el-popconfirm），不点它这次续期根本不生效。
        """
        self._dialog(page)
        self._click_in_dialog(page, self.f.get("submit_texts", ["续期"]))
        fence = self._ui_fence()
        selectors = self.f.get("popconfirm_ok_selectors", [])
        gone = lambda: not self._dialog_loc(page).count()      # noqa: E731

        # 气泡出来、或者弹窗已经关了 / 报错了（有的版本没有这一步）
        wait_until(page, lambda: self._first_visible(page, selectors) is not None
                   or gone() or self._page_errors(page), fence)
        ok = self._first_visible(page, selectors)
        if ok is not None:
            ok.click()

        list_fence = self._list_fence()
        done = wait_until(page, lambda: gone() or self._page_errors(page), list_fence)
        errors = self._page_errors(page)
        if errors:
            raise FillError("续期被页面拒绝：" + "；".join(errors))
        if not done:
            raise FillError(f"点了「续期」，等了 {list_fence // 1000} 秒弹窗还没关闭，判定为未生效")

    def _close_dialog(self, page):
        """不提交地关掉弹窗。失败/跳过/试跑都会走到这里，必须尽力关干净，
        否则下一条会被上一条的遮罩挡住。

        ⚠ 日期面板开着时，第一下 Esc 只收掉面板（实测），第二下才关弹窗。
        ⚠ 弹窗关的时候有淡出动画：按完 Esc 立刻去点「取消」，点的是一个正在被移除的
          按钮，Playwright 会等它「稳定」到超时（DMP 实测干等 30 秒）。
          所以先等它自己消失，Esc 不管用才点「取消」。
        """
        fence = self._ui_fence()
        dialog_open = lambda: self._dialog_loc(page).count() > 0   # noqa: E731
        panel_open = lambda: self._panel_loc(page).count() > 0     # noqa: E731
        try:
            for _ in range(3):
                if panel_open():
                    page.keyboard.press("Escape")
                    wait_until(page, lambda: not panel_open(), min(2000, fence))
                    continue
                if not dialog_open():
                    return
                page.keyboard.press("Escape")
                if wait_until(page, lambda: not dialog_open(), min(2000, fence)):
                    return
            if not dialog_open():
                return
            try:
                self._click_in_dialog(page, self.f.get("cancel_texts") or ["取消"])
            except FillError:
                pass
            if not wait_until(page, lambda: not dialog_open(), fence):
                log.warning("续期弹窗 %d 毫秒内没关掉", fence)
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

    @staticmethod
    def _result(i, target, status, error, picked):
        return {"序号": i, "实验名称": target["name"], "实验ID": target["id"],
                "状态": status, "原到期日": target.get("end_date", ""),
                "续期至": picked, "错误": error}

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
        lines = [f"配置类型：{self.f['name']}", f"延期范围：{SCOPE_LABELS[self.scope]}"]
        lines.append(f"试跑 {stats['dry']} 个" if dry else f"成功 {stats['ok']} 个")
        no_change = sum(1 for r in results if r["状态"] == "no_change")
        if no_change:
            lines.append(f"已是最晚日期、无需续期 {no_change} 个")
        if stats["skipped"] - no_change:
            lines.append(f"跳过 {stats['skipped'] - no_change} 个")
        if stats["failed"]:
            lines.append(f"失败 {stats['failed']} 个")
        lines += ["", f"明细：{self.s['result_file']}", f"截图：{self.s['screenshot_dir']}"]
        self.ui.finished("AB实验延期完成" if not stats["failed"] else "AB实验延期完成（有失败）",
                         "\n".join(lines), not stats["failed"])


_GAP = "[\\s ​　]*"


def _spaced(text: str):
    """「续期」→ 能匹配「续期」「 续期 」「续 期」的整串正则。"""
    chars = [re.escape(c) for c in _squeeze(text)]
    return re.compile("^" + _GAP + _GAP.join(chars) + _GAP + "$")


def _squeeze(text: str) -> str:
    """剔掉所有空白，含全角空格 U+3000。"""
    return re.sub(r"[\s ​　]", "", text or "")


def _param(url: str, name: str) -> str:
    """URL 查询参数的值；没有这个参数返回 ''。"""
    try:
        return (parse_qs(urlsplit(url or "").query, keep_blank_values=True).get(name) or [""])[0]
    except ValueError:
        return ""


def _host(url: str) -> str:
    try:
        return urlsplit(url or "").netloc
    except ValueError:
        return ""


def _same_doc(a: str, b: str) -> bool:
    """两个 URL 只差 # 后面那段（同一个 SPA 文档）。"""
    return bool(a) and a.split("#", 1)[0] == (b or "").split("#", 1)[0]


# Chromium 的网络错误码 → 人话
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
        return f"打不开 AB 实验列表（{why}）。网络好了之后重新点「载入并检查」"
    return f"打不开 AB 实验列表：{raw.splitlines()[0] if raw else type(e).__name__}"
