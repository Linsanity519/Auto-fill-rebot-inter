"""主流程：开弹窗 → 填一条 → 等确认 → 提交 → 回列表 → 下一条。

界面交互全部走 ui（BaseUI 的实现），Runner 本身不知道是命令行还是图形界面。
"""
import csv
import logging
from datetime import datetime
from pathlib import Path

from .browser import Browser
from .datasource import load_table, build_records
from .filler import Filler
from .preview import PreviewRow
from .runstate import StateMixin
from .ui import BaseUI, ConsoleUI, Stopped
from .validate import validate_all, summarize

log = logging.getLogger(__name__)


class Runner(StateMixin):
    def __init__(self, settings: dict, form_cfg: dict, ui: BaseUI | None = None):
        self.s = settings
        self.f = form_cfg
        self.ui = ui or ConsoleUI()
        self.shot_dir = Path(settings["screenshot_dir"])
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self.auto = False
        # 断点走 src/runstate.py，六个执行器同一套（原来这里各写了一份）
        self._init_state()

    # ---------- 预检 ----------
    def preview(self) -> list[PreviewRow]:
        """跑之前把数据解析 + 校验一遍，给界面显示。不碰浏览器。"""
        rows = load_table(self.s["data_file"], self.s.get("sheet_name"))
        records = build_records(rows, self.f, self.s.get("group_key", "分组"))
        issues = validate_all(self.f, records)
        out = []
        for i, r in enumerate(records):
            s = summarize(r, self.f)
            out.append(PreviewRow(
                index=i + 1, name=s["名称"], kind=s["类型"], detail_count=s["明细"],
                issues=issues[i], done=self.state.is_done(i), payload=r,
            ))
        return out

    # ---------- 主循环 ----------
    def run(self, records: list[dict] | None = None):
        if records is None:
            rows = load_table(self.s["data_file"], self.s.get("sheet_name"))
            records = build_records(rows, self.f, self.s.get("group_key", "分组"))

        total = len(records)
        dry = self.s.get("dry_run")
        stats = {"ok": 0, "failed": 0, "skipped": 0, "dry": 0}
        results = []

        self.ui.log(f"表单「{self.f['name']}」，共 {total} 条配置" + ("（空跑，不提交）" if dry else ""))
        self.ui.progress(0, total, stats)

        try:
            with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
                filler = Filler(b.page, self.f)

                for i, rec in enumerate(records):
                    if self.state.is_done(i):
                        self.ui.log(f"[{i + 1}] 已完成过，跳过")
                        continue

                    self.ui.checkpoint()          # 暂停在这里阻塞，停止抛 Stopped

                    name = summarize(rec, self.f)["名称"]
                    label = f"[{i + 1}/{total}]"
                    self.ui.log(f"{label} {name} —— 填写中（{len(rec['items'])} 个明细项）")

                    try:
                        self._open_form(b.page)
                        filler.fill_record(rec)
                        shot = self._screenshot(b.page, i)
                        self.ui.log(f"{label} 已填写完成，请在浏览器里核对", "ok")

                        if dry:
                            stats["dry"] += 1
                            results.append(self._result(i, rec, "dry_run", ""))
                            self._cancel(b.page)
                        else:
                            action = "submit" if self.auto else self.ui.confirm(label, name)
                            if action == "auto":
                                self.auto, action = True, "submit"
                            if action == "stop":
                                self.ui.log("已停止", "warn")
                                break
                            if action == "skip":
                                stats["skipped"] += 1
                                results.append(self._result(i, rec, "skipped", "用户跳过"))
                                self._cancel(b.page)
                            else:
                                self._submit(b.page)
                                self.state.mark_done(i)
                                stats["ok"] += 1
                                results.append(self._result(i, rec, "ok", ""))
                                self.ui.log(f"{label} 提交成功", "ok")

                    except Stopped:
                        raise
                    except Exception as e:
                        msg = str(e)
                        log.exception("%s 失败", label)
                        shot = self._screenshot(b.page, i, tag="error")
                        self.state.mark_failed(i, name, msg)
                        stats["failed"] += 1
                        results.append(self._result(i, rec, "failed", msg))
                        self.ui.log(f"{label} 失败：{msg}", "error")
                        self.ui.log(f"    错误截图：{shot}")
                        self._cancel(b.page)
                        if not self.ui.ask_continue(msg):
                            break

                    self.ui.progress(i + 1, total, stats)

        except Stopped:
            self.ui.log("已停止", "warn")
        finally:
            self._write_results(results)
            self._report(results, total, stats)

        return results

    # ---------- 步骤 ----------
    def _open_form(self, page):
        url = self.f.get("form_url")
        if url and (self.f.get("reset_between_rows", True) or page.url != url):
            page.goto(url, wait_until="domcontentloaded")

        # open_dialog 是单步；open_steps 支持多步（如 DMP 要先「新建人群」再选「临时表创建」）
        steps = self.f.get("open_steps") or ([self.f["open_dialog"]] if self.f.get("open_dialog") else [])
        ready = self.f.get("ready_selector")
        if steps:
            already = ready and page.locator(ready).count() and page.locator(ready).first.is_visible()
            if not already:
                for sel in steps:
                    page.wait_for_selector(sel, state="visible")
                    page.click(sel)
        if ready:
            page.wait_for_selector(ready, state="visible")

    def _cancel(self, page):
        """关掉弹窗，避免脏状态带到下一条。"""
        sel = self.f.get("cancel_selector")
        if not sel:
            return
        try:
            if page.locator(sel).count():
                page.click(sel)
                page.wait_for_timeout(600)
        except Exception:
            log.warning("关闭弹窗失败，下一条可能受影响")

    def _submit(self, page):
        """点确定 → 等弹窗关闭。

        不能"点了确定就当成功"：后端校验不过时弹窗会留在原地并标红。
        必须以「弹窗消失」为判据，否则失败会被记成成功，
        而且下一条会在没关的弹窗里继续填，越错越离谱。
        """
        page.click(self.f["submit_selector"])
        ready = self.f.get("ready_selector")

        if ready:
            try:
                page.wait_for_selector(ready, state="hidden", timeout=self.s["timeout"])
            except Exception:
                raise RuntimeError(f"点了确定但弹窗没关闭，提交被拒。{self._form_errors(page)}")
        else:
            page.wait_for_timeout(self.s.get("after_submit_wait", 3000))

        success = self.f.get("success_selector")
        if success:
            try:
                page.wait_for_selector(success, timeout=2000)
            except Exception:
                log.info("没捕捉到成功提示，但弹窗已关闭，按成功处理")

    def _form_errors(self, page) -> str:
        # antd 的 class 前缀可以在构建期改（DMP 那套是 full_ogv_data_antd-），
        # 写死 ant- 的话页面明明标红了却读不到，错误信息会变成一句"看截图"。
        p = self.f.get("antd_prefix") or "ant"
        msgs = []
        for sel in (f".{p}-form-item-explain-error", f".{p}-message-error",
                    f".{p}-notification-notice-description"):
            try:
                msgs += [t.strip() for t in page.locator(sel).all_inner_texts() if t.strip()]
            except Exception:
                pass
        uniq = list(dict.fromkeys(msgs))
        return f"页面报错：{uniq}" if uniq else "页面上没找到明确的错误提示，看截图。"

    def _screenshot(self, page, idx, tag="filled"):
        ts = datetime.now().strftime("%H%M%S")
        path = self.shot_dir / f"{self.f['name']}_{idx + 1:04d}_{tag}_{ts}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
        except Exception:
            return "(截图失败)"
        return str(path)

    # ---------- 输出 ----------
    def _result(self, idx, rec, status, error):
        return {
            "序号": idx + 1,
            "状态": status,
            "错误": error,
            "明细项数": len(rec.get("items", [])),
            **rec["header"],
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
        lines = [f"表单：{self.f['name']}", f"共 {total} 条配置"]
        if stats["dry"]:
            lines.append(f"空跑 {stats['dry']} 条（未提交）")
        else:
            lines.append(f"成功 {stats['ok']} 条")
            if stats["skipped"]:
                lines.append(f"跳过 {stats['skipped']} 条")
            if stats["failed"]:
                lines.append(f"失败 {stats['failed']} 条 ← 看结果表的「错误」列")
        lines += ["", f"明细：{self.s['result_file']}", f"截图：{self.s['screenshot_dir']}"]

        bad = stats["failed"] > 0
        self.ui.finished("配置完成" if not bad else "配置完成（有失败）", "\n".join(lines), not bad)
