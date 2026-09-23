"""普通内容授权 主流程。

⚠ 只服务 mode: nc。

和「花火稿件授权」（mode: hh）是**同一个页面的两个 tab**，弹窗 DOM 一模一样，
所以这里**复用** `src/hh_filler.HhFiller`（选择器全部由 yaml 的 dialog: 段喂进去）
和 `src/hh_data.load`（Excel 列完全相同）。按项目约定，DOM 栈相同就别新写一套 filler。

真正不一样的三处，全在这个文件里：

  1. **校验接口不同**：`general_video/authorize/state`。花火商单稿件在这里会返回
     「该稿件是商单稿件，请到花火平台进行绑定」—— 两个接口正好互补，
     一条稿件只会在其中一页能授权。
  2. **3 次/7 天的上限是按「同一账号（UP主）」算的**，不是按稿件算。
     所以预检要按 UP 主归并着数：同一个 UP 主在清单里超过 3 条，第 4 条起标红拦下
     —— 发出去平台也会拒，白烧机会。（花火那边是按视频算，不需要这一步。）
  3. 校验接口会回 `has_authorized`：已经授权过的直接标出来，不重复申请。

「成功」= 申请已发出，还要 UP 主到 bilibili 消息列表确认，措辞别改成「已授权」。
"""
from __future__ import annotations

import csv
import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .browser import Browser
from .hh_filler import HhFiller
from .preview import PreviewRow
from .runstate import StateMixin
from .ui import BaseUI, ConsoleUI, Stopped

log = logging.getLogger(__name__)

STATUS_API = "/platform/api/web_api/v6/resource/sanlian/general_video/authorize/state?archive_id="
QUOTA_LIMIT = 3          # 7 日内同一 UP 主的申请次数上限，yaml 的 quota: 段可以改


def _key(item: dict) -> str:
    return str(item.get("bvid") or "")


class NcRunner(StateMixin):
    def __init__(self, settings: dict, form_cfg: dict, ui: BaseUI | None = None):
        self.s = settings
        self.f = form_cfg
        self.ui = ui or ConsoleUI()
        self.shot_dir = Path(settings["screenshot_dir"])
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self.auto = False
        self.created = []
        self._items = None
        self._init_state()

    # ---------------- 预检 ----------------
    def preview(self) -> list[PreviewRow]:
        from .hh_data import load

        items = load(self.s.get("data_file"), self.f.get("defaults") or {})

        checkable = [it for it in items if it["bvid"] and not it["issues"]]
        if checkable:
            try:
                status = self._check_status([it["bvid"] for it in checkable])
            except Exception as e:
                log.warning("预检打 state 接口失败", exc_info=True)
                self.ui.log(f"预检没能连上平台校验接口（{e}），这一轮不做可授权性预判", "warn")
                status = {}
            for it in checkable:
                st = status.get(it["bvid"])
                if st is None:
                    continue
                if not st.get("ok"):
                    it["issues"].append(st.get("msg") or "平台校验没过")
                    continue
                it["title"] = st.get("title", "")
                it["up"] = st.get("up", "")
                it["mid"] = st.get("mid", "")
                if st.get("has_authorized"):
                    it["issues"].append("这条已经授权过了，不用重复申请")
            self._apply_quota(checkable)

        self._items = items
        rows = []
        for i, it in enumerate(items, 1):
            header = {"稿件bvid": it["bvid"], "授权时间": it["time"], "共享范围": it["range"]}
            if it.get("title"):
                header["稿件标题"] = it["title"]
            if it.get("up") or it.get("name"):
                header["UP主"] = it.get("up") or it["name"]
            rows.append(PreviewRow(
                index=i, name=it.get("title") or it["bvid"], kind="内容授权",
                detail_count=0,
                issues=list(it["issues"]),
                warnings=list(it.get("warnings") or []),
                done=self.state.is_done(_key(it)),
                payload={"header": header, "items": [], "_nc": it},
            ))
        return rows

    def _apply_quota(self, items: list[dict]):
        """按 UP 主数申请数：超过 7 日内 3 次上限的，第 4 条起标红拦下。

        ⚠ 这是这个类型独有的一步。清单是按视频排的，人眼很难发现
          「这 5 条其实是同一个 UP 主」—— 不拦的话后两条发出去必被平台拒。
        """
        q = self.f.get("quota") or {}
        limit = int(q.get("limit") or QUOTA_LIMIT)
        by_up = defaultdict(list)
        for it in items:
            if it["issues"]:          # 已经有别的问题的不占名额
                continue
            by_up[it.get("mid") or it.get("up") or it["bvid"]].append(it)
        for _mid, group in by_up.items():
            if len(group) <= limit:
                continue
            who = group[0].get("up") or "这个 UP 主"
            for n, it in enumerate(group[limit:], limit + 1):
                it["issues"].append(
                    f"同一 UP 主（{who}）7 日内只有 {limit} 次申请机会，"
                    f"这是清单里的第 {n} 条，本批发不出去")

    def _check_status(self, bvids: list[str]) -> dict:
        api = self.f.get("status_api") or STATUS_API
        with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
            self._goto(b)
            raw = b.page.evaluate(
                """async ([api, ids]) => {
                    const out = {};
                    for (const id of ids) {
                      try {
                        const res = await fetch(api + encodeURIComponent(id), {credentials: 'include'});
                        out[id] = await res.text();
                      } catch (e) { out[id] = 'ERR ' + e; }
                      await new Promise(s => setTimeout(s, 120));
                    }
                    return out;
                }""", [api, bvids])

        got = {}
        for bvid, text in raw.items():
            try:
                j = json.loads(text)
            except Exception:
                got[bvid] = {"ok": False, "msg": f"校验接口返回看不懂：{str(text)[:80]}"}
                continue
            if j.get("status") != "success":
                got[bvid] = {"ok": False, "msg": j.get("error_msg") or "平台校验没过"}
                continue
            r = j.get("result") or {}
            info = r.get("archive_info") or {}
            got[bvid] = {"ok": True,
                         "title": info.get("title") or "",
                         "up": info.get("nick_name") or "",
                         "mid": str(info.get("mid") or ""),
                         "has_authorized": bool(r.get("has_authorized")),
                         "auth_times": r.get("auth_times") or 0}
        return got

    def _goto(self, b: Browser):
        page = b.page
        # hash 路由：换 tab 不重新加载文档，所以先看当前 url 在不在这个 tab 上
        if "subTab=normal_video_auth" not in (page.url or ""):
            page.goto(self.f["form_url"])
        ready = self.f.get("ready_selector")
        if ready:
            page.wait_for_selector(ready, timeout=self.s["timeout"])

    # ---------------- 主流程 ----------------
    def run(self, items: list[dict] | None = None):
        if items is None:
            src = self._items if self._items is not None else [
                r.payload["_nc"] for r in self.preview()]
            items = [it for it in src if not it["issues"]]
        else:
            items = [it.get("_nc", it) for it in items]

        dry = self.s.get("dry_run")
        total = len(items)
        stats = {"ok": 0, "failed": 0, "skipped": 0, "dry": 0}
        results = []

        self.ui.log(f"「{self.f['name']}」共 {total} 条" + ("（试跑：只填不提交）" if dry else ""))
        self.ui.progress(0, total, stats)

        try:
            with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
                self._goto(b)
                filler = HhFiller(b.page, self.s["timeout"],
                                  on_note=lambda m: self.ui.log(f"    {m}", "warn"),
                                  dialog=self.f.get("dialog") or {})

                for i, it in enumerate(items):
                    self.ui.checkpoint()
                    label = f"[{i + 1}/{total}]"
                    bvid = it["bvid"]
                    name = it.get("title") or it.get("name") or bvid

                    if self.state.is_done(_key(it)):
                        self.ui.log(f"{label} {bvid} 已申请过，跳过")
                        stats["skipped"] += 1
                        results.append(self._row(i + 1, it, "skipped", "断点里已完成"))
                        self.ui.progress(i + 1, total, stats)
                        continue

                    try:
                        b.front()
                        filler.fill(bvid, it["time"], it["range"])
                        self._shot(b.page, i + 1, "filled")

                        if dry:
                            filler.close_dialog()
                            stats["dry"] += 1
                            results.append(self._row(i + 1, it, "dry_run", "未提交"))
                            self.ui.log(f"{label} {bvid} 已填好（试跑，没提交）")
                            self.ui.progress(i + 1, total, stats)
                            continue

                        action = "submit" if self.auto else self.ui.confirm(
                            label, f"{name}（{bvid}） {it['time']} / {it['range']}")
                        if action == "auto":
                            self.auto, action = True, "submit"
                        if action == "stop":
                            filler.close_dialog()
                            break
                        if action == "skip":
                            filler.close_dialog()
                            stats["skipped"] += 1
                            results.append(self._row(i + 1, it, "skipped", "用户跳过"))
                            self.ui.progress(i + 1, total, stats)
                            continue

                        msg = filler.submit()
                        stats["ok"] += 1
                        self.state.mark_done(_key(it))
                        self.created.append(bvid)
                        results.append(self._row(i + 1, it, "ok", msg or "申请已发出"))
                        self.ui.log(f"{label} {bvid} 申请已发出" + (f"：{msg}" if msg else ""), "ok")

                    except Stopped:
                        raise
                    except Exception as e:
                        msg = str(e)
                        log.exception("%s 失败", label)
                        shot = self._shot(b.page, i + 1, "error")
                        stats["failed"] += 1
                        self.state.mark_failed(_key(it), bvid, msg)
                        results.append(self._row(i + 1, it, "failed", msg))
                        self.ui.log(f"{label} {bvid} 失败：{msg}", "error")
                        self.ui.log(f"    截图：{shot}")
                        try:
                            filler.close_dialog()
                        except Exception:
                            log.warning("收拾弹窗失败", exc_info=True)
                        if not self.ui.ask_continue(msg):
                            break

                    self.ui.progress(i + 1, total, stats)

        except Stopped:
            self.ui.log("已停止", "warn")

        self._write_results(results)
        ok = stats["failed"] == 0
        self.ui.finished(
            "跑完了" if ok else "跑完了，有失败",
            f"申请已发出 {stats['ok']}　失败 {stats['failed']}　"
            f"跳过 {stats['skipped']}　试跑 {stats['dry']}\n"
            f"⚠ 申请还要 UP 主到 bilibili 消息列表里确认，确认了才算授权成功。", ok)
        return results

    # ---------------- 结果 ----------------
    def _row(self, idx: int, it: dict, status: str, msg: str) -> dict:
        return {"序号": idx, "稿件bvid": it["bvid"], "UP主": it.get("up") or it.get("name", ""),
                "稿件标题": it.get("title", ""), "授权时间": it["time"],
                "共享范围": it["range"], "状态": status, "说明": msg}

    def _write_results(self, results):
        if not results:
            return
        p = Path(self.s["screenshot_dir"]).parent / (
            f"普通内容授权_结果_{datetime.now():%Y%m%d_%H%M%S}.csv")
        try:
            with p.open("w", encoding="utf-8-sig", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(results[0]))
                w.writeheader()
                w.writerows(results)
            self.ui.log(f"结果已写到：{p}")
        except Exception:
            log.warning("写结果 csv 失败", exc_info=True)

    def _shot(self, page, idx: int, tag: str) -> str:
        p = self.shot_dir / f"{self.f['name']}_{idx}_{tag}.png"
        try:
            page.screenshot(path=str(p))
        except Exception:
            log.warning("截图失败", exc_info=True)
        return str(p)
