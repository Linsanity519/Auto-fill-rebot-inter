"""常规商广主流程：每 10 个视频一个单元，视频取自「我的视频」，
每条创意的 素材标题/描述/落地页 取自 Excel 对应行。

⚠ 和 ad_runner（原生商广）是两套。页面 DOM 一样（都用 ad_filler 填计划/单元层），
  但创意按「我的视频」列表位置取，逐行配 Excel。

流程和原生一样是「一页建三层」：
  第 1 个单元  → ?type=1            建计划 + 单元 + 创意，保存后回列表页捞计划ID
  第 2..N 个   → ?campaign_id=<id>  挂到刚才那个计划下，只建单元 + 创意
准备页填了「已有计划ID」的话，全部走 ?campaign_id=。
"""
from __future__ import annotations

import csv
import logging
import re
from datetime import datetime
from pathlib import Path

from . import ad_prep as P
from . import ad_reg_data as D
from .ad_filler import AdFiller
from .ad_reg_creative import AdRegCreative
from .browser import Browser
from .preview import PreviewRow
from .runstate import StateMixin
from .ui import BaseUI, ConsoleUI, Stopped

log = logging.getLogger(__name__)

CAMPAIGN_ID_RE = re.compile(r"ID[:：]\s*(\d+)")


def _accept_dialog(dialog):
    try:
        dialog.accept()
    except Exception:
        log.debug("关弹窗失败，多半是它自己已经没了", exc_info=True)


class AdRegRunner(StateMixin):
    def __init__(self, settings: dict, form_cfg: dict, ui: BaseUI | None = None):
        self.s = settings
        self.f = form_cfg
        self.ui = ui or ConsoleUI()
        self.shot_dir = Path(settings["screenshot_dir"])
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self.auto = False
        self.created = []
        self._init_state()

    # ---------------- 预检 ----------------
    def preview(self) -> list[PreviewRow]:
        prep = self.s.get("ad_prep") or P.load(self.f)
        data = D.load(self.s["data_file"], self.f, self.s)
        issues = D.validate(self.f, data, prep)
        head = P.validate(self.f, prep)

        rows = []
        for i, u in enumerate(data["units"]):
            cs = u["creatives"]
            idx = [c["video_index"] for c in cs]
            span = f"第{idx[0] + 1}~{idx[-1] + 1}个" if len(idx) > 1 else f"第{idx[0] + 1}个"
            rows.append(PreviewRow(
                index=i + 1, name=u["name"], kind=f"我的视频 {span}（{len(cs)} 条创意）",
                detail_count=len(cs), issues=[],
                done=self.state.is_done(u["name"]), payload=u,
            ))
        if rows:
            rows[0].issues = head + issues + rows[0].issues
        elif head or issues:
            rows.append(PreviewRow(index=1, name="（准备参数）", kind="", detail_count=0,
                                   issues=head + issues, done=False, payload={}))

        self._data = data
        self._prep = prep
        return rows

    # ---------------- 主流程 ----------------
    def run(self, units: list[dict] | None = None):
        prep = getattr(self, "_prep", None) or self.s.get("ad_prep") or P.load(self.f)
        data = getattr(self, "_data", None) or D.load(self.s["data_file"], self.f, self.s)
        units = units if units is not None else data["units"]

        dry = self.s.get("dry_run")
        total = len(units)
        n_vid = sum(len(u["creatives"]) for u in units)
        stats = {"ok": 0, "failed": 0, "skipped": 0, "dry": 0}
        results = []

        prep = self._resolve_prep(prep)
        campaign_id = str(prep.get("已有计划ID", "")).strip()
        plan_name = str(prep.get("计划名称", "")).strip()

        self.ui.log(f"「{self.f['name']}」共 {total} 个单元、{n_vid} 个视频（≤10/单元）"
                    + ("（试跑：只填不保存）" if dry else ""))
        if campaign_id:
            self.ui.log(f"挂到已有计划（ID {campaign_id}），不新建计划")
        else:
            self.ui.log(f"第一个单元会连着把计划「{plan_name}」一起建出来")
        self.ui.progress(0, total, stats)

        try:
            with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
                b.page.on("dialog", _accept_dialog)
                af = AdFiller(b.page, self.s["timeout"])
                cr = AdRegCreative(b.page, self.s["timeout"])

                for i, u in enumerate(units):
                    self.ui.checkpoint()
                    label = f"[{i + 1}/{total}]"
                    name = u["name"]
                    new_plan = not campaign_id
                    cs = list(u["creatives"])
                    vids = [c["video_index"] for c in cs]
                    self.ui.log(f"{label} {name} —— 填写中（{len(vids)} 个视频，"
                                f"第 {vids[0] + 1}~{vids[-1] + 1} 个）"
                                + ("，并新建计划" if new_plan else ""))
                    try:
                        self._open(b.page, campaign_id)
                        values = dict(prep)
                        values["单元名称"] = name

                        if new_plan:
                            af.fill(self.f.get("plan_fields") or [], values, scope="计划层 ")
                        af.fill(self.f.get("unit_fields") or [], values, scope="单元层 ")

                        creative = self.f.get("creative") or {}
                        got = cr.add_videos(creative.get("video_picker") or {}, vids)
                        cr.fill_creatives(creative, cs)
                        note = f"{got} 条创意"
                        self._shot(b.page, i + 1, "filled")

                        if dry:
                            stats["dry"] += 1
                            results.append(self._row(i, name, "dry_run", f"试跑未保存（{note}）"))
                            self.ui.log(f"{label} 已填好，试跑不保存", "ok")
                            self.ui.progress(i + 1, total, stats)
                            continue

                        action = self._ask(label, name)
                        if action == "stop":
                            break
                        if action == "skip":
                            stats["skipped"] += 1
                            results.append(self._row(i, name, "skipped", "用户跳过"))
                            self.ui.progress(i + 1, total, stats)
                            continue

                        self._submit(af, b.page)
                        stats["ok"] += 1
                        results.append(self._row(i, name, "ok", note))
                        self.state.mark_done(name)
                        self.created.append((name, note))
                        self.ui.log(f"{label} 已保存", "ok")

                        if new_plan:
                            campaign_id = self._find_campaign_id(b.page, plan_name)
                            if not campaign_id:
                                raise RuntimeError(
                                    f"计划「{plan_name}」建出来了，但在计划列表里没找到它的ID。\n"
                                    f"到页面上查一下计划ID，填进准备页的「已有计划ID」再重跑。")
                            self.ui.log(f"    计划ID {campaign_id}，后面的单元都挂它下面", "ok")

                    except Stopped:
                        raise
                    except Exception as e:
                        msg = str(e)
                        log.exception("%s 失败", label)
                        shot = self._shot(b.page, i + 1, "error")
                        stats["failed"] += 1
                        results.append(self._row(i, name, "failed", msg))
                        self.state.mark_failed(name, name, msg)
                        self.ui.log(f"{label} 失败：{msg}", "error")
                        self.ui.log(f"    截图：{shot}")
                        if not self.ui.ask_continue(msg):
                            break

                    self.ui.progress(i + 1, total, stats)

        except Stopped:
            self.ui.log("已停止", "warn")
        except Exception as e:
            if results:
                log.exception("跑完之后收尾出错")
                self.ui.log(f"收尾时出错（不影响已经建好的单元）：{e}", "warn")
            else:
                log.exception("跑不起来")
                self.ui.log(str(e), "error")
                self.ui.finished("没能开始", str(e), False)
                return results

        self._write_results(results)
        ok = stats["failed"] == 0
        body = (f"成功 {stats['ok']}　失败 {stats['failed']}　"
                f"跳过 {stats['skipped']}　试跑 {stats['dry']}")
        if self.created:
            body += "\n\n建好的单元：\n" + "\n".join(
                f"　{n}（{t}）" for n, t in self.created)
        self.ui.finished("跑完了" if ok else "跑完了，有失败", body, ok)
        return results

    # ---------------- 分步（和 ad_runner 同款）----------------
    def _resolve_prep(self, prep: dict) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        return {k: str(v).replace("{今天}", today) for k, v in prep.items()}

    def _open(self, page, campaign_id: str):
        urls = self.f.get("urls") or {}
        url = (urls["under_plan"].format(campaign_id=campaign_id) if campaign_id
               else urls["new_plan"])
        ready = self.f.get("ready_selector")
        for attempt in (1, 2):
            same_doc = page.url.split("#")[0] == url.split("#")[0]
            page.goto(url, wait_until="domcontentloaded")
            if same_doc:
                page.reload(wait_until="domcontentloaded")
            if not ready:
                break
            try:
                page.wait_for_selector(ready, timeout=self.s["timeout"])
                break
            except Exception:
                if attempt == 2:
                    raise
                log.warning("打开 %s 后没等到 %s，页面在 %s，重开一次", url, ready, page.url)
                page.wait_for_timeout(2000)
        page.wait_for_timeout(2000)
        self._dismiss_info_modals(page)

    @staticmethod
    def _dismiss_info_modals(page):
        """新页面加载后会弹一两个「新增XX功能！…我知道了」的说明弹窗，盖住表单。
        全关掉，不然点「推广目的」会点到遮罩上、下游联动也不触发。"""
        for _ in range(4):
            try:
                btn = page.locator('.ivu-modal-wrap:visible').get_by_text(
                    "我知道了", exact=True).first
                if not btn.count():
                    break
                btn.click()
                page.wait_for_timeout(500)
            except Exception:
                break

    def _submit(self, af: AdFiller, page):
        create_marker = self.f.get("create_url_marker", "promote/auto")
        af.click_button(self.f.get("submit_button", "保存"), prefer_last=True)
        waited, step = 0, 500
        limit = int(self.s.get("timeout", 15000)) * 2
        while waited < limit:
            page.wait_for_timeout(step)
            waited += step
            if create_marker not in page.url:
                page.wait_for_timeout(int(self.s.get("after_submit_wait", 3000)))
                return
            err = page.locator(".ivu-form-item-error-tip:visible").first
            if err.count():
                raise RuntimeError(f"页面报错，没保存成功：{(err.inner_text() or '').strip()}")
        raise RuntimeError(
            f"点了「保存」{limit // 1000} 秒后页面还停在新建页，没保存成功。"
            f"到浏览器里看一眼是不是有弹窗或红字提示。")

    def _find_campaign_id(self, page, plan_name: str) -> str:
        urls = self.f.get("urls") or {}
        list_url = urls.get("campaign_list") or "https://ad.bilibili.co/#/promote/campaign"
        same_doc = page.url.split("#")[0] == list_url.split("#")[0]
        page.goto(list_url, wait_until="domcontentloaded")
        if same_doc:
            page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        row = page.locator("tr").filter(has_text=plan_name).first
        if not row.count():
            return ""
        m = CAMPAIGN_ID_RE.search(row.inner_text() or "")
        return m.group(1) if m else ""

    # ---------------- 杂 ----------------
    def _ask(self, label: str, name: str) -> str:
        if self.auto or not self.s.get("confirm_before_submit", True):
            return "submit"
        action = self.ui.confirm(label, f"{name}")
        if action == "auto":
            self.auto = True
            return "submit"
        return action

    def _shot(self, page, idx: int, tag: str) -> str:
        path = self.shot_dir / f"adreg_{idx:03d}_{tag}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
        except Exception:
            log.warning("截图失败", exc_info=True)
        return str(path)

    @staticmethod
    def _row(i: int, name: str, status: str, note: str) -> dict:
        return {"序号": i + 1, "单元名称": name, "状态": status, "说明": note,
                "时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    def _write_results(self, results: list[dict]):
        if not results:
            return
        path = Path(self.s["result_file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("w", newline="", encoding="utf-8-sig") as fh:
                w = csv.DictWriter(fh, fieldnames=list(results[0].keys()))
                w.writeheader()
                w.writerows(results)
        except OSError:
            log.warning("结果写不进 %s", path, exc_info=True)
