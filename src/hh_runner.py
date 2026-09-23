"""花火稿件授权 主流程。

⚠ 只服务 mode: hh。和别的配置类型的 runner 互不调用 ——
  各家后台的 DOM 栈完全不同，共用只会互相踩。

和别人的关键差别：

  · **只有一层**：一条数据 = 打开一次弹窗、填一个 bvid、点一次确定。
    没有活动/单元/创意那种嵌套，所以没有策略中心、没有准备页。
  · **预检走接口，不开弹窗**：输入 bvid 时页面自己会打
    `commercial_order/archive/status`，返回 fail +「该稿件不是花火稿件」的那些，
    在这一页永远授不了权（要走隔壁「普通内容授权」）。预检直接批量打这个接口，
    39 条几秒钟出结果，不用开 39 次弹窗才发现。
  · **发出去撤不回，而且有 3 次上限**：同一个视频 7 天内只有 3 次申请机会。
    所以清单里重复的 bvid 在 hh_data 里就拦掉，预检标红的默认不跑。
  · **成功 ≠ 授权成功**：这里只是把申请发出去，还要 UP 主 / MCN 去花火平台确认。
    结果里的「成功」= 申请已发出，措辞别改成「已授权」。
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path

from .browser import Browser
from .hh_filler import HhFiller
from .preview import PreviewRow
from .runstate import StateMixin
from .ui import BaseUI, ConsoleUI, Stopped

log = logging.getLogger(__name__)

STATUS_API = "/platform/api/web_api/v6/resource/sanlian/commercial_order/archive/status?archive_id="

# 页面上那句原话，预检标红时原样带出去，别自己改写
NOT_HUOHUO = "该稿件不是花火稿件"


def _key(item: dict) -> str:
    """断点的 key：重跑时还认得出是同一条。bvid 天然唯一，比行号稳。"""
    return str(item.get("bvid") or "")


class HhRunner(StateMixin):
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
        """读清单 → 逐条打平台的校验接口 → 非花火稿件当场标红。

        ⚠ 这里会连浏览器（要登录态才能打接口）。纯文件层面的错（bvid 空/重复/
          选项写错）在 hh_data.load 里就出了，不连浏览器也能用
          `tools\\check_mode.py --data` 验。
        """
        from .hh_data import load

        items = load(self.s.get("data_file"), self.f.get("defaults") or {})

        checkable = [it for it in items if it["bvid"] and not it["issues"]]
        if checkable:
            try:
                status = self._check_status([it["bvid"] for it in checkable])
            except Exception as e:                  # 接口挂了不该把预检整个堵死
                log.warning("预检打 status 接口失败", exc_info=True)
                self.ui.log(f"预检没能连上平台校验接口（{e}），"
                            f"这一轮不做「是不是花火稿件」的预判", "warn")
                status = {}
            for it in checkable:
                st = status.get(it["bvid"])
                if st is None:
                    continue
                if st.get("ok"):
                    it["title"] = st.get("title", "")
                    it["up"] = st.get("up", "")
                    if st.get("auth_times"):
                        # 7 天 3 次上限，快用完了要让人看见，但不拦（见 PreviewRow.warnings）
                        it.setdefault("warnings", []).append(
                            f"7 天内已申请 {st['auth_times']} 次（上限 3 次）")
                else:
                    it["issues"].append(st.get("msg") or "平台校验没过")

        self._items = items
        rows = []
        for i, it in enumerate(items, 1):
            header = {"稿件bvid": it["bvid"],
                      "授权时间": it["time"],
                      "共享范围": it["range"]}
            if it.get("title"):
                header["稿件标题"] = it["title"]
            if it.get("up") or it.get("name"):
                header["UP主"] = it.get("up") or it["name"]
            rows.append(PreviewRow(
                index=i,
                name=it.get("title") or it["bvid"],
                kind="稿件授权",
                detail_count=0,
                issues=list(it["issues"]),
                warnings=list(it.get("warnings") or []),
                done=self.state.is_done(_key(it)),
                payload={"header": header, "items": [], "_hh": it},
            ))
        return rows

    def _check_status(self, bvids: list[str]) -> dict:
        """在页面里批量打校验接口。一次开浏览器，比一条条开弹窗快两个数量级。"""
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

        import json
        got = {}
        for bvid, text in raw.items():
            try:
                j = json.loads(text)
            except Exception:
                got[bvid] = {"ok": False, "msg": f"校验接口返回看不懂：{str(text)[:80]}"}
                continue
            if j.get("status") == "success":
                r = (j.get("result") or {})
                info = (r.get("archive_info") or {})
                got[bvid] = {"ok": True,
                             "title": info.get("title") or "",
                             "up": (info.get("up_name") or info.get("author") or ""),
                             "auth_times": r.get("auth_times") or 0}
            else:
                got[bvid] = {"ok": False, "msg": j.get("error_msg") or "平台校验没过"}
        return got

    def _goto(self, b: Browser):
        url = self.f["form_url"]
        page = b.page
        # hash 路由：同一个文档换 tab 不会重新加载，所以先看当前 url 对不对
        if "subTab=video_auth" not in (page.url or ""):
            page.goto(url)
        ready = self.f.get("ready_selector")
        if ready:
            page.wait_for_selector(ready, timeout=self.s["timeout"])

    # ---------------- 主流程 ----------------
    def run(self, items: list[dict] | None = None):
        if items is None:
            src = self._items if self._items is not None else [
                r.payload["_hh"] for r in self.preview()]
            items = [it for it in src if not it["issues"]]
        else:
            # 界面传进来的是 PreviewRow.payload
            items = [it.get("_hh", it) for it in items]

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
                        b.front()               # ⚠ 后台标签会被 Chrome 降频 15 倍
                        filler.fill(bvid, it["time"], it["range"])
                        shot = self._shot(b.page, i + 1, "filled")

                        if dry:
                            filler.close_dialog()
                            stats["dry"] += 1
                            results.append(self._row(i + 1, it, "dry_run", "未提交"))
                            self.ui.log(f"{label} {bvid} 已填好（试跑，没提交）")
                            self.ui.progress(i + 1, total, stats)
                            continue

                        # ⚠ 提交前停下来给人看截图，除非用户选了全自动。
                        #   这一条尤其不能省：申请发出去撤不回，同一视频 7 天只有 3 次。
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

                        msg = filler.submit()       # 弹窗关掉才算发出去
                        stats["ok"] += 1
                        self.state.mark_done(_key(it))
                        self.created.append(bvid)
                        results.append(self._row(i + 1, it, "ok", msg or "申请已发出"))
                        self.ui.log(f"{label} {bvid} 申请已发出"
                                    + (f"：{msg}" if msg else ""), "ok")
                        _ = shot

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
                            filler.close_dialog()   # 不收掉的话下一条会填到这个弹窗上
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
            f"⚠ 申请还要 UP 主 / MCN 去花火平台确认，确认了才算授权成功。", ok)
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
            f"花火稿件授权_结果_{datetime.now():%Y%m%d_%H%M%S}.csv")
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
