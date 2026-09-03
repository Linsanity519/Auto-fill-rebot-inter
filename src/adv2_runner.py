"""原生商广新（三连竞价推广 auto-v2）主流程：一次建 1 个项目。

⚠ 与 runner / dmp_runner / ab_runner / wizard_runner / ad_runner 都独立。
  老的「原生商广」是 ad_runner（1 计划 + N 单元）。

auto-v2 两层化之后：整批就一个项目，Excel 所有行的
  avid   → 稿件池
  素材标题 → 标题池
  封面    → 封面池
描述在准备阶段填一个固定值。所以 run() 不循环，一趟填完。

流程：
  打开 #/promote/auto-v2 → 填项目层字段 → 加稿件池 / 标题池 / 封面池 / 描述
  → 截图 → （试跑就停在这）→ 确认 → 点「保存」→ 等页面离开新建页
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path

from . import ad_prep as P
from . import adv2_data as D
from .adv2_filler import Adv2Filler
from .browser import Browser
from .preview import PreviewRow
from .runstate import StateMixin
from .ui import BaseUI, ConsoleUI, Stopped

log = logging.getLogger(__name__)


def _accept_dialog(dialog):
    """一律「确认离开」。表单填过之后 goto/reload 必弹 onbeforeunload。"""
    try:
        dialog.accept()
    except Exception:
        log.debug("关弹窗失败，多半是它自己已经没了", exc_info=True)


class Adv2Runner(StateMixin):
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
        issues = list(D.validate(self.f, data)) + list(P.validate(self.f, prep))

        name = str(prep.get("项目名称", "")).strip() or "（未填项目名称）"
        detail = [{"类型": "稿件", "值": a["value"]} for a in data["archives"]]
        detail += [{"类型": "标题", "值": t["value"]} for t in data["titles"]]
        detail += [{"类型": "封面", "值": c["value"]} for c in data["covers"]]

        row = PreviewRow(
            index=1, name=name, kind="项目",
            detail_count=len(data["archives"]),
            issues=issues, done=False,
            payload={
                "header": {k: v for k, v in P.summary(self.f, prep)},
                "items": detail,
                "_data": data,
            },
        )
        self._data = data
        self._prep = prep
        return [row]

    # ---------------- 主流程 ----------------
    def run(self, records: list[dict] | None = None):
        if records is not None and not records:
            self.ui.log("没有要跑的项目", "warn")
            return []

        prep = getattr(self, "_prep", None) or self.s.get("ad_prep") or P.load(self.f)
        data = getattr(self, "_data", None)
        if data is None and records:
            data = records[0].get("_data")
        if data is None:
            data = D.load(self.s["data_file"], self.f, self.s)

        prep = self._resolve_prep(prep)
        dry = self.s.get("dry_run")
        n_arch, n_title, n_cover = (len(data["archives"]), len(data["titles"]),
                                    len(data["covers"]))
        name = str(prep.get("项目名称", "")).strip()

        self.ui.log(f"「{self.f['name']}」建 1 个项目「{name}」："
                    f"{n_arch} 个稿件 / {n_title} 个标题 / {n_cover} 张封面"
                    + ("（试跑：只填不保存）" if dry else ""))
        self.ui.progress(0, 1, {"ok": 0, "failed": 0, "skipped": 0, "dry": 0})

        results = []
        page = None
        try:
            with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
                page = b.page
                page.on("dialog", _accept_dialog)
                af = Adv2Filler(page, self.s["timeout"])

                self.ui.checkpoint()
                self._open(page)

                values = dict(prep)
                af.fill(self.f.get("project_fields") or [], values, scope="项目层 ")
                self._fill_material(af, data, prep)
                self._shot(page, "filled")

                miss = getattr(self, "miss_archives", None)
                miss_note = (f"\n⚠ {len(miss)} 个 avid 搜不到已跳过：{'、'.join(miss)}"
                             if miss else "")
                if dry:
                    self.ui.log("已填好，试跑不保存", "ok")
                    results.append(self._row(name, "dry_run", "试跑未保存" + (
                        f"（{len(miss)} 个 avid 没加上）" if miss else "")))
                    self.ui.progress(1, 1, {"ok": 0, "failed": 0, "skipped": 0, "dry": 1})
                    self._write_results(results)
                    self.ui.finished("跑完了（试跑）",
                                     "已填好，未保存。到浏览器里核对。" + miss_note, True)
                    return results

                action = self._ask(name)
                if action == "stop":
                    self.ui.log("已停止", "warn")
                    self.ui.finished("已停止", "没有保存", True)
                    return results
                if action == "skip":
                    results.append(self._row(name, "skipped", "用户跳过"))
                    self.ui.finished("已跳过", "没有保存", True)
                    return results

                self._submit(af, page)
                self.state.mark_done(name)
                self.created.append((name, n_arch))
                results.append(self._row(name, "ok", ""))
                self.ui.log(f"项目「{name}」已保存", "ok")
                self.ui.progress(1, 1, {"ok": 1, "failed": 0, "skipped": 0, "dry": 0})

        except Stopped:
            self.ui.log("已停止", "warn")
        except Exception as e:
            msg = str(e)
            log.exception("原生商广新跑失败")
            if page is not None:
                self.ui.log(f"    截图：{self._shot(page, 'error')}")
            if results and results[-1]["状态"] == "ok":
                self.ui.log(f"收尾时出错（不影响已建好的项目）：{msg}", "warn")
            else:
                results.append(self._row(name, "failed", msg))
                self.ui.log(f"失败：{msg}", "error")
                self._write_results(results)
                self.ui.finished("没能建成", msg, False)
                return results

        self._write_results(results)
        ok = all(r["状态"] in ("ok", "dry_run", "skipped") for r in results)
        body = "\n".join(f"　{n}（{c} 个稿件）" for n, c in self.created) or "（未建）"
        self.ui.finished("跑完了" if ok else "跑完了，有失败", body, ok)
        return results

    # ---------------- 分步 ----------------
    def _resolve_prep(self, prep: dict) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        return {k: str(v).replace("{今天}", today) for k, v in prep.items()}

    def _open(self, page):
        urls = self.f.get("urls") or {}
        url = urls.get("new_project") or self.f.get("form_url")
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
                log.warning("打开 %s 后没等到 %s（现在 %s），重开一次", url, ready, page.url)
                page.wait_for_timeout(2000)
        # SPA 还在 hydrate：等推广目的那组卡片挂上来 + 网络静默，再动手
        try:
            page.wait_for_selector(".ppt-new-item", timeout=self.s["timeout"])
        except Exception:
            log.warning("没等到 .ppt-new-item（推广目的卡片），继续试")
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(2500)

    def _fill_material(self, af: Adv2Filler, data: dict, prep: dict):
        m = self.f.get("material") or {}
        avids = [a["value"] for a in data["archives"]]
        titles = [t["value"] for t in data["titles"]]
        covers = [c["value"] for c in data["covers"]]

        self.ui.log(f"素材层：加 {len(avids)} 个稿件…")
        af.add_archives(m.get("picker") or {}, avids)
        miss = getattr(af, "missing_archives", None)
        if miss:
            self.miss_archives = list(miss)
            self.ui.log(f"    ⚠ {len(miss)} 个 avid 在稿件库里搜不到，已跳过："
                        f"{'、'.join(miss[:10])}{'…' if len(miss) > 10 else ''}", "warn")
        self.ui.log(f"素材层：填 {len(titles)} 个标题…")
        af.add_titles(m.get("title") or {}, titles)
        if covers:
            self.ui.log(f"素材层：传 {len(covers)} 张封面…")
            af.add_covers(m.get("cover") or {}, covers)
        desc_cfg = m.get("description") or {}
        desc = str(prep.get(desc_cfg.get("from_prep", "素材描述"), "")).strip()
        if desc:
            af.set_description(desc_cfg, desc)

    def _submit(self, af: Adv2Filler, page):
        marker = self.f.get("create_url_marker", "promote/auto-v2")
        af.click_button(self.f.get("submit_button", "保存"), prefer_last=True)
        waited, step = 0, 500
        limit = int(self.s.get("timeout", 15000)) * 2
        while waited < limit:
            page.wait_for_timeout(step)
            waited += step
            if marker not in page.url:
                page.wait_for_timeout(int(self.s.get("after_submit_wait", 3000)))
                return
            err = page.locator(
                ".bd-form-item__error:visible, .bd-form-item.is-error:visible, "
                ".ivu-form-item-error-tip:visible").first
            if err.count():
                raise RuntimeError(f"页面报错，没保存成功：{(err.inner_text() or '').strip()}")
        raise RuntimeError(
            f"点了「保存」{limit // 1000} 秒后页面还停在新建页。到浏览器里看有没有红字或弹窗。")

    # ---------------- 杂 ----------------
    def _ask(self, name: str) -> str:
        if self.auto or not self.s.get("confirm_before_submit", True):
            return "submit"
        action = self.ui.confirm("[1/1]", name)
        if action == "auto":
            self.auto = True
            return "submit"
        return action

    def _shot(self, page, tag: str) -> str:
        path = self.shot_dir / f"adv2_{tag}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
        except Exception:
            log.warning("截图失败", exc_info=True)
        return str(path)

    @staticmethod
    def _row(name: str, status: str, note: str) -> dict:
        return {"序号": 1, "项目名称": name, "状态": status, "说明": note,
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
