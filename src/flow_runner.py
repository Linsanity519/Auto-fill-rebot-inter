"""自制工作流（mode: flow）的执行器：按录下来的步骤图走。

和别的 runner 的差别：
  · 定义是 config/flows/<名>.json 的步骤图，不是 config/forms 的字段表
    （form_cfg 是 flow_data.synthetic_cfg 包出来的，真件在 form_cfg["_flow"]）。
  · 顶层 steps 跑一次（goto / 初始等待这类）；loop_rows 里的 body 按 Excel 每行跑一遍。
  · 没有 loop_rows（不吃 Excel）时，整份 steps 当「一条」跑一次。

复用：Browser / fill_core（经 FlowFiller）/ 预检行 / 断点 / 结果 CSV / 逐条确认。
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path

from . import flow_data as FD
from .browser import Browser
from .fill_core import FillError
from .flow_filler import FlowFiller
from .preview import PreviewRow
from .runstate import StateMixin
from .ui import BaseUI, ConsoleUI, Stopped

log = logging.getLogger(__name__)


def _row_key(i: int, row: dict) -> str:
    first = next((str(v) for v in row.values() if str(v).strip()), "")
    return f"{i}/{first}"


class FlowRunner(StateMixin):
    def __init__(self, settings: dict, form_cfg: dict, ui: BaseUI | None = None):
        self.s = settings
        self.f = form_cfg
        self.flow = FD._defaults(form_cfg.get("_flow") or {})
        self.ui = ui or ConsoleUI()
        self.shot_dir = Path(settings["screenshot_dir"])
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self.auto = False
        # 逐步试跑：每一步操作前高亮页面元素、停下等人点「下一步 / 跳过 / 全部自动 / 停」。
        # 由 webapp._run() 在 mode=="step" 时置上，同时 dry_run 也为真（只填不提交）。
        self._step_mode = bool(settings.get("flow_step"))
        self._step_auto = False
        self._init_state()

    # ---------------- 数据 ----------------
    def _rows(self) -> list[dict]:
        if not FD.has_loop(self.flow):
            return [{}]                        # 不吃 Excel：一条
        from .datasource import load_table
        path = self.s.get("data_file") or ""
        if not path:
            return []
        return load_table(path, self.s.get("sheet_name"))

    # ---------------- 预检 ----------------
    def preview(self) -> list[PreviewRow]:
        try:
            rows = self._rows()
        except Exception as e:
            return [PreviewRow(index=1, name="(读数据失败)", kind="",
                               detail_count=0, issues=[str(e)])]
        issues = FD.validate(self.flow, rows if FD.has_loop(self.flow) else None)
        kind = FD.describe(self.flow)

        if not FD.has_loop(self.flow):
            return [PreviewRow(index=1, name=self.flow["name"], kind=kind,
                               detail_count=len(self.flow.get("steps") or []),
                               issues=issues, done=False,
                               payload={"row": {}, "key": "single"})]
        out = []
        for i, r in enumerate(rows):
            name = next((str(v) for v in r.values() if str(v).strip()), f"第 {i + 1} 行")
            mine = [x for x in issues if not x.startswith("Excel 里缺")]  # 缺列是全局问题
            out.append(PreviewRow(
                index=i + 1, name=name, kind=kind, detail_count=0,
                issues=(issues if i == 0 else mine),
                done=self.state.is_done(_row_key(i, r)),
                payload={"row": r, "key": _row_key(i, r)}))
        if not rows:
            out.append(PreviewRow(index=1, name="(Excel 里没有数据行)", kind=kind,
                                  detail_count=0, issues=issues + ["数据文件是空的"]))
        return out

    # ---------------- 主流程 ----------------
    def run(self, records: list[dict] | None = None):
        if records is None:
            records = [r.payload for r in self.preview() if not r.issues]
        dry = bool(self.s.get("dry_run"))
        looped = FD.has_loop(self.flow)
        total = len(records)
        stats = {"ok": 0, "failed": 0, "skipped": 0, "dry": 0}
        results = []
        self._stop = False
        self._step_auto = False

        tail = ("（逐步试跑：每步停下核对，只填不提交）" if self._step_mode
                else "（试跑：填但不提交）" if dry else "")
        self.ui.log(f"「{self.flow['name']}」自制工作流，{FD.describe(self.flow)}"
                    + (f"，共 {total} 行" if looped else "") + tail)
        for w in FD.warnings(self.flow):
            self.ui.log(f"  提醒：{w}", "warn")
        self.ui.progress(0, total, stats)

        try:
            with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
                b.front()
                ff = FlowFiller(b.page, self.s["timeout"],
                                on_note=lambda m: self.ui.log(f"    {m}", "warn"))
                idx = {"n": 0}

                def run_body(steps, row, label, rec_i):
                    self.ui.checkpoint()
                    trace, err = [], ""
                    status = "ok"
                    try:
                        st = self._exec(ff, b, steps, row, label, trace, rec_i)
                        status = st or "ok"
                    except Stopped:
                        raise
                    except FillError as e:
                        status, err = "failed", str(e)
                    except Exception as e:
                        log.exception("%s 出错", label)
                        status, err = "failed", str(e)
                    return status, err, trace

                top_fail = []          # 顶层（非循环）步骤失败记这里 —— 别再当没发生
                for si, step in enumerate(self.flow.get("steps") or []):
                    if self._stop:
                        break
                    if step.get("op") == "loop_rows":
                        for rec_i, rec in enumerate(records):
                            if self._stop:
                                break
                            row = rec.get("row", rec) if isinstance(rec, dict) else {}
                            key = rec.get("key") if isinstance(rec, dict) else _row_key(rec_i, row)
                            if self.s.get("resume") and self.state.is_done(key):
                                self.ui.log(f"[{rec_i + 1}/{total}] 已完成过，跳过")
                                continue
                            idx["n"] += 1
                            label = f"[{rec_i + 1}/{total}]"
                            name = next((str(v) for v in row.values() if str(v).strip()),
                                        f"第 {rec_i + 1} 行")
                            self.ui.log(f"{label} {name} —— 走 {len(step.get('body') or [])} 步")
                            status, err, trace = run_body(step.get("body") or [], row, label, rec_i)
                            self._record(results, rec_i, row, status, err, trace, stats, dry, key)
                            self.ui.progress(min(idx["n"], total), total, stats)
                    else:
                        # 顶层非循环步骤：跑一次
                        status, err, trace = run_body([step], {}, "[setup]", -1)
                        if status == "failed":
                            top_fail.append(f"第 {si + 1} 步：{err}")
                            self.ui.log(f"[setup] 第 {si + 1} 步失败：{err}", "error")
                            # 试跑 / 逐步：失败就停，别硬着头皮往下走还报「跑通了」
                            if dry or self._step_mode:
                                break
                            if not self.ui.ask_continue(err):
                                break

                if not looped:
                    # 整份步骤当一条。顶层任何一步失败 = 这条没跑通。
                    ok = stats["failed"] == 0 and not top_fail
                    err = "；".join(top_fail[:3])
                    results.append(self._result(0, {}, "ok" if ok else "failed", err, []))
                    stats["ok" if ok else "failed"] += 1
                    self.ui.progress(1, 1, stats)

        except Stopped:
            self.ui.log("已停止", "warn")
        except Exception as e:
            log.exception("运行中断")
            self.ui.log(f"运行中断：{e}", "error")
        finally:
            self._write_results(results)

        ok = stats["failed"] == 0
        self.ui.finished(
            "跑完了" if ok else "跑完了，有失败",
            f"成功 {stats['ok']}　跳过 {stats['skipped']}　失败 {stats['failed']}　"
            f"试跑 {stats['dry']}\n\n明细：{self.s['result_file']}", ok)
        return results

    # ---------------- 走一段步骤 ----------------
    def _exec(self, ff: FlowFiller, b, steps, row, label, trace, rec_i) -> str:
        src = self.flow.get("source_url", "")
        for j, s in enumerate(steps, 1):
            if self._stop:
                return "stopped"
            self.ui.checkpoint()
            op = s.get("op")

            # 逐步试跑：操作类的每一步，先高亮、再停下等人。confirm 步本来就会停，不重复问。
            if (self._step_mode and not self._step_auto
                    and op in ("goto", "click", "fill", "select", "search_pick",
                               "pick_item", "check", "press", "wait_for")):
                act = self._step_prompt(ff, s, j, label, row, src)
                if act == "stop":
                    self._stop = True
                    return "stopped"
                if act == "skip":
                    self.ui.log(f"{label} 第 {j} 步（{op}）跳过", "warn")
                    trace.append((op, "跳过"))
                    continue
                if act == "auto":
                    self._step_auto = True

            if op == "goto":
                ff.goto(FD.render(s.get("url", ""), row, src))
            elif op == "click":
                if self.s.get("dry_run") and s.get("submit"):
                    self.ui.log(f"{label} 第 {j} 步：提交动作，空跑跳过")
                    trace.append((op, "skip"))
                    continue
                if s.get("submit"):
                    self._shot(b.page, rec_i, f"before_submit_{j}")
                trace.append((op, ff.click(s.get("pick") or [])))
                ff.settle(hard=bool(s.get("submit")))     # 点完等页面跟上，别急着下一步
            elif op == "fill":
                trace.append((op, ff.fill(s.get("pick") or [], FD.render(s.get("value", ""), row, src))))
            elif op == "select":
                trace.append((op, ff.select(s.get("pick") or [], FD.render(s.get("value", ""), row, src),
                                            field=s.get("field", ""))))
                ff.settle()
            elif op == "search_pick":
                trace.append((op, ff.search_pick(
                    s.get("pick") or [],
                    FD.render(s.get("query", "") or s.get("value", ""), row, src),
                    FD.render(s.get("value", ""), row, src), field=s.get("field", ""))))
                ff.settle()
            elif op == "pick_item":
                trace.append((op, ff.pick_item(s.get("pick") or [],
                                               FD.render(s.get("value", ""), row, src),
                                               field=s.get("field", ""))))
                ff.settle()
            elif op == "check":
                trace.append((op, ff.check(s.get("pick") or [],
                                           FD.render(s.get("value", ""), row, src),
                                           checked=s.get("checked", True),
                                           field=s.get("field", ""))))
                ff.settle()
            elif op == "press":
                ff.press(s.get("key", "Enter"), s.get("pick"))
            elif op == "wait_for":
                ff.wait_for(s.get("pick") or [], s.get("timeout"))
            elif op == "wait_text":
                ff.wait_text(FD.render(s.get("text", ""), row, src), s.get("timeout"))
            elif op == "assert":
                ff.do_assert(s)
            elif op == "screenshot":
                self._shot(b.page, rec_i, s.get("tag") or f"step_{j}")
            elif op == "confirm":
                b.front()
                self._shot(b.page, rec_i, f"confirm_{j}")
                if self.s.get("dry_run"):
                    self.ui.log(f"{label} 停一下（{s.get('note') or '核对'}）—— 空跑不等")
                    continue
                action = "submit" if self.auto else self.ui.confirm(
                    label, s.get("note") or "在浏览器里核对一眼")
                if action == "auto":
                    self.auto, action = True, "submit"
                if action == "stop":
                    self._stop = True
                    return "stopped"
                if action == "skip":
                    return "skipped"
            elif op == "loop_rows":
                # 嵌套循环：v1 不支持，当普通一段跑（用当前 row）
                self._exec(ff, b, s.get("body") or [], row, label, trace, rec_i)
            else:
                self._note_bad(op)
        return "ok"

    def _note_bad(self, op):
        self.ui.log(f"    不认识的步骤「{op}」，跳过", "warn")

    # ---------------- 逐步试跑 ----------------
    def _step_prompt(self, ff: FlowFiller, s: dict, j: int, label: str, row: dict, src: str) -> str:
        """高亮这一步要碰的元素，停下等人。返回 submit / skip / auto / stop。"""
        pick = s.get("pick") or []
        try:
            ff.highlight(pick, True)
        except Exception:
            pass
        try:
            return self.ui.confirm(f"{label} 逐步", self._step_desc(s, j, row, src))
        finally:
            try:
                ff.highlight(pick, False)
            except Exception:
                pass

    _VERB = {"goto": "打开", "click": "点击", "fill": "填写", "select": "选择",
             "search_pick": "搜索并选", "pick_item": "选中行", "check": "勾选",
             "press": "按键", "wait_for": "等元素"}

    @classmethod
    def _step_desc(cls, s: dict, j: int, row: dict, src: str) -> str:
        op = s.get("op")
        field = s.get("field") or ""
        val = FD.render(s.get("value", ""), row, src)
        if op == "goto":
            what = FD.render(s.get("url", ""), row, src)
        elif op in ("select", "pick_item"):
            what = (f"在「{field}」里 " if field else "") + f"选「{val}」"
        elif op == "check":
            what = (f"「{field}」" if field else "") + ("勾" if s.get("checked", True) else "取消勾") + f"「{val}」"
        elif op == "search_pick":
            q = FD.render(s.get("query", "") or val, row, src)
            what = (f"在「{field}」里 " if field else "") + f"搜「{q}」→ 选「{val}」"
        elif op == "fill":
            what = (f"「{field}」← " if field else "") + f"「{val}」"
        elif op == "press":
            what = f"{s.get('key', 'Enter')}"
        else:
            pick = s.get("pick") or []
            c = pick[0] if pick else {}
            k = next((x for x in ("text", "role", "label", "attr", "css") if x in c), "")
            what = f"{c.get(k)}" if k else (s.get("seen") or "")
        return f"第 {j} 步：{cls._VERB.get(op, op)}　{what}"

    # ---------------- 记录 ----------------
    def _record(self, results, i, row, status, err, trace, stats, dry, key):
        if dry and status == "ok":
            status = "dry_run"
        results.append(self._result(i, row, status, err, trace))
        bucket = {"ok": "ok", "dry_run": "dry", "skipped": "skipped",
                  "stopped": "skipped", "failed": "failed"}.get(status, "failed")
        stats[bucket] += 1
        if status == "ok":
            self.state.mark_done(key)
            self.ui.log(f"[{i + 1}] 完成", "ok")
        elif status == "failed":
            self.state.mark_failed(key, str(row)[:60], err)
            self.ui.log(f"[{i + 1}] 失败：{err}", "error")

    def _result(self, i, row, status, err, trace):
        how = "、".join(f"{op}:{h}" for op, h in trace) if trace else ""
        return {"序号": i + 1, "状态": status, "错误": err, "步数": len(trace),
                "命中方式": how, **{k: v for k, v in (row or {}).items()}}

    def _shot(self, page, rec_i, tag):
        ts = datetime.now().strftime("%H%M%S")
        p = self.shot_dir / f"{self.flow['name']}_{max(rec_i, 0) + 1:04d}_{tag}_{ts}.png"
        try:
            page.screenshot(path=str(p), full_page=True)
        except Exception:
            pass

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
            self.ui.log(f"{path} 被占用，结果没写进去", "warn")
