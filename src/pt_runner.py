"""价格策略批量开启 / 关闭。

只服务 mode: pt_toggle。操作的是策略编辑页底部「价格配置」表 ——
每行「操作」列有个裸 <a>开启</a> / <a>关闭</a>，**点一下直接生效、没有二次确认**
（删除才有确认）。状态有三个值：未开启 / 已开启 / 已关闭；判方向看操作链接的文字
（只有开启/关闭两种）最省事。详见 docs/价格策略批量开关-配置项抓取.md。

和别的 runner 的关键差别：
  · 不建东西、不提交表单，只翻转已有行的开关，所以没有断点（幂等，重跑无害）。
  · preview() 要读活的表格，所以会开浏览器（和 dmp/ab 一样）。

选哪些行 = 「范围」下拉：
  keyword  按名称关键词（子串，命中人群名称即算；留空 = 整页）
  ledger   本工具「价格策略配置」配过的（读 pt_ledger 台账，按策略筛）
  all      整页全部
已在目标态的行、以及「人群选组=不限」的行（开启方向）都自动跳过。

哪几条策略 = 「策略」文本框（`toggle_strategies`，一行一个，可混填 URL/路由ID/业务ID）：
  留空                → 只当前打开的策略页
  留空 + 范围=ledger  → 台账里出现过的所有策略，逐个开/关
  填了                → 逐条 goto 打开、开/关
"""
from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

from . import pt_ledger, pt_strategy
from .browser import Browser
from .fill_core import FillError, norm, split_multi
from .preview import PreviewRow
from .pt_filler import PtToggleFiller
from .runstate import StateMixin
from .ui import BaseUI, ConsoleUI, Stopped

log = logging.getLogger(__name__)

_STRATEGY_JS = r"""
() => {
  const m = location.href.match(/\/edit\/(\d+)/);
  let name = '';
  const nodes = [...document.querySelectorAll('label, span, div')].filter(
    e => e.childElementCount === 0 && e.textContent.trim() === '策略名称');
  for (const lb of nodes) {
    let s = lb.parentElement;
    for (let i = 0; i < 4 && s && !name; i++) {
      const inp = s.querySelector('input');
      if (inp && inp.value) name = inp.value.trim();
      s = s.parentElement;
    }
    if (name) break;
  }
  return { id: m ? m[1] : '', name: name, url: location.href };
}
"""

MAX_PAGES = 50
MAX_STRATEGIES = 50
_DATE_RE = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$")


class PtToggleRunner(StateMixin):
    def __init__(self, settings: dict, form_cfg: dict, ui: BaseUI | None = None):
        self.s = settings
        self.f = form_cfg
        self.ui = ui or ConsoleUI()
        self.shot_dir = Path(settings["screenshot_dir"])
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self.auto = False
        # 方向：界面上切的（settings["toggle_direction"]），命令行退回 yaml 的 direction
        self.direction = str(settings.get("toggle_direction")
                             or form_cfg.get("direction") or "on").lower()   # on / off
        self.ledger_name = form_cfg.get("ledger") or ""
        self._init_state()          # 只为给 webapp 提供 clear_state，本 runner 不记断点

    # ---------------- 文案 ----------------
    @property
    def _verb(self) -> str:
        return "开启" if self.direction == "on" else "关闭"

    @property
    def _target_link(self) -> str:
        return "关闭" if self.direction == "on" else "开启"

    @property
    def _click_link(self) -> str:
        return "开启" if self.direction == "on" else "关闭"

    # ---------------- 参数 ----------------
    def _scope(self) -> str:
        v = str(self.s.get("pt_scope") or "keyword").lower()
        return v if v in ("keyword", "ledger", "list") else "keyword"

    def _tokens(self) -> list[str]:
        """`toggle_params` 拆成词：按行 + 逗号/分号/顿号。keyword 和 list 都用它。"""
        raw = str(self.s.get("toggle_params") or "")
        out: list[str] = []
        for line in raw.replace("\r", "\n").split("\n"):
            line = line.strip()
            if not line:
                continue
            out.extend(split_multi(line) if any(c in line for c in ",，、;；") else [line])
        return [k for k in (x.strip() for x in out) if k]

    @staticmethod
    def _norm_date(v) -> str | None:
        v = str(v or "").strip()
        m = _DATE_RE.match(v)
        if not m:
            return None
        y, mo, d = re.split(r"[-/.]", v)
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"

    def _since(self) -> str | None:
        return self._norm_date(self.s.get("toggle_date_from"))

    def _until(self) -> str | None:
        return self._norm_date(self.s.get("toggle_date_to"))

    def _strategy_tokens(self) -> list[str]:
        return pt_strategy.parse_tokens(self.s.get("toggle_strategies") or "")

    # ---------------- 选行 ----------------
    def _pick(self, rows: list[dict], strategy_id: str) -> tuple[list[dict], str]:
        """→ (选中的行, 没选中时的一句原因)。"""
        scope = self._scope()

        if scope == "ledger":
            names = pt_ledger.names_for(self.ledger_name, strategy_id,
                                        since=self._since(), until=self._until())
            wanted = {norm(n) for n in names}
            got = [r for r in rows if norm(r["name"]) in wanted]
            if got:
                return got, ""
            nb = len(pt_ledger.batches_for(self.ledger_name, strategy_id))
            rng = self._date_desc()
            return [], (f"本工具在这条策略下配过 {nb} 批{rng}，"
                        f"但那些人群名称在当前表里都找不到" if nb else
                        f"本工具在这条策略下没有配置记录{rng}")

        if scope == "list":
            toks = self._tokens()
            if not toks:
                return [], "「按清单」但一个人群名称都没填"
            want = {norm(t) for t in toks}
            got = [r for r in rows if norm(r["name"]) in want
                   or any(t in r["name"] for t in toks)]
            return got, ("" if got else f"清单里的 {len(toks)} 个名称在 {len(rows)} 行里一个都没对上")

        # keyword
        kws = self._tokens()
        if not kws:
            return rows, ("这条策略下一条价格配置都没有" if not rows else "")
        got = [r for r in rows if any(k in r["name"] for k in kws)]
        return got, ("" if got else f"关键词 {kws} 在 {len(rows)} 行里一个都没命中")

    def _date_desc(self) -> str:
        a, b = self._since(), self._until()
        if a and b:
            return f"（{a} ~ {b}）"
        if a:
            return f"（{a} 之后）"
        if b:
            return f"（{b} 之前）"
        return ""

    # ---------------- 分类 ----------------
    def _classify(self, r: dict) -> tuple[str, str, str]:
        """→ (act, kind, reason)。act ∈ {toggle, done, block}。"""
        link = norm(r.get("link", ""))
        if link == self._target_link:
            return "done", f"已{'开启' if self.direction == 'on' else '关闭/未开启'}", ""
        if self.direction == "on" and norm(r.get("group", "")) == "不限":
            return "block", "不限·跳过", "人群选组=不限，不可开启"
        if link == self._click_link:
            return "toggle", f"将{self._verb}", ""
        return "block", "状态异常", f"操作列文字是「{r.get('link') or '(空)'}」，既不是开启也不是关闭"

    # ---------------- 扫全表 ----------------
    def _scan_all(self, pf: PtToggleFiller) -> list[dict]:
        pf.first_page()
        seen: set = set()
        rows: list[dict] = []
        for pageno in range(1, MAX_PAGES + 1):
            snap = pf.snapshot()
            for x in snap.get("rows", []):
                k = x.get("key") or f"{pageno}:{x['name']}"
                if k in seen:
                    continue
                seen.add(k)
                rows.append({**x, "page": snap.get("page", pageno)})
            if snap.get("page", pageno) >= snap.get("pages", pageno):
                break
            if not pf.next_page():
                break
        pf.first_page()
        return rows

    # ---------------- 要跑哪几条策略 ----------------
    def _targets(self, pf: PtToggleFiller) -> tuple[list[dict], list[PreviewRow]]:
        """→ ([{route_id, name}], 解析不出来的当预检问题行)。

        route_id 为 None 表示「就用当前打开这一页」。
        """
        tokens = self._strategy_tokens()
        bad_rows: list[PreviewRow] = []

        if tokens:
            res = pt_strategy.StrategyResolver(
                pf.page, self.s["timeout"],
                on_note=lambda m: self.ui.log(f"    {m}")).resolve(tokens[:MAX_STRATEGIES])
            good = [{"route_id": x["route_id"], "name": x["name"]} for x in res if x["ok"]]
            for x in res:
                if not x["ok"]:
                    bad_rows.append(PreviewRow(index=len(bad_rows) + 1,
                                               name=f"(策略 {x['token']})", kind="",
                                               detail_count=0, issues=[x["error"]]))
            if len(tokens) > MAX_STRATEGIES:
                self.ui.log(f"策略清单超过 {MAX_STRATEGIES} 条，只取前 {MAX_STRATEGIES} 条", "warn")
            return good, bad_rows

        if self._scope() == "ledger":
            saved = pt_ledger.strategies(self.ledger_name)
            if saved:
                self.ui.log(f"范围=本工具配置过的、策略框留空 → 台账里这 {len(saved)} 条策略挨个过："
                            + "、".join(s["name"] or s["id"] for s in saved))
                return [{"route_id": s["id"], "name": s["name"]} for s in saved], bad_rows

        return [{"route_id": None, "name": ""}], bad_rows

    # ---------------- 预检 ----------------
    def preview(self) -> list[PreviewRow]:
        with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
            b.front()
            pf = PtToggleFiller(b.page, self.s["timeout"],
                                on_note=lambda m: self.ui.log(f"    {m}", "warn"))
            targets, out = self._targets(pf)
            multi = len([t for t in targets if t["route_id"]]) > 1 or bool(out)

            for t in targets:
                rid = t.get("route_id")
                try:
                    if rid:
                        pf.open_strategy(rid)
                    else:
                        pf.wait_table()
                    meta = {}
                    try:
                        meta = b.page.evaluate(_STRATEGY_JS) or {}
                    except Exception:
                        pass
                    sid = meta.get("id") or (rid or "")
                    sname = t.get("name") or meta.get("name") or (f"策略{sid}" if sid else "当前页")
                except Exception as e:
                    out.append(PreviewRow(index=len(out) + 1, name=f"(策略 {t.get('name') or rid})",
                                          kind="", detail_count=0,
                                          issues=[f"打开这条策略失败：{e}"]))
                    continue

                rows = self._scan_all(pf)
                picked, why = self._pick(rows, sid)
                if not picked:
                    out.append(PreviewRow(index=len(out) + 1, name=f"[{sname}] (没有命中的行)",
                                          kind="", detail_count=0,
                                          issues=[why or "没有可操作的行"]))
                    continue
                for r in picked:
                    act, kind, reason = self._classify(r)
                    disp = f"[{sname}] {r['name']}" if multi else r["name"]
                    out.append(PreviewRow(
                        index=len(out) + 1, name=disp,
                        kind=f"第{r.get('page', 1)}页 · {kind}",
                        detail_count=0,
                        issues=[reason] if act == "block" else [],
                        done=(act == "done"),
                        payload={"header": {"策略": sname, "人群名称": r["name"],
                                            "人群选组": r.get("group", ""),
                                            "当前状态": r.get("state", ""), "本次动作": kind},
                                 "name": r["name"], "act": act,
                                 "strategy": {"route_id": rid, "name": sname}},
                    ))

        if not out:
            out.append(PreviewRow(index=1, name="(没有命中的行)", kind="", detail_count=0,
                                  issues=["按当前的策略 + 范围，找不到任何要操作的行"]))
        return out

    # ---------------- 主流程 ----------------
    def run(self, records: list[dict] | None = None):
        if records is None:
            records = [r.payload for r in self.preview() if not r.issues]
        dry = bool(self.s.get("dry_run"))
        total = len(records)
        stats = {"ok": 0, "failed": 0, "skipped": 0, "dry": 0}

        # 按策略分组（保持首次出现顺序）；route_id=None 就是当前页
        groups: list[tuple[tuple, list[dict]]] = []
        index: dict[tuple, list[dict]] = {}
        for rec in records:
            st = rec.get("strategy") or {}
            key = (st.get("route_id"), st.get("name") or "")
            if key not in index:
                index[key] = []
                groups.append((key, index[key]))
            index[key].append(rec)

        def rkey(route_id, name):
            return f"{route_id}\x00{norm(name)}"

        pending = {rkey((rec.get("strategy") or {}).get("route_id"), rec["name"]): rec
                   for rec in records if rec.get("name")}
        results_by: dict[str, dict] = {}

        self.ui.log(f"「{self.f['name']}」共 {total} 行待{self._verb}"
                    + (f"（跨 {len(groups)} 条策略）" if len(groups) > 1 else "")
                    + ("（试跑：只看不点）" if dry else ""))
        self.ui.progress(0, total, stats)

        try:
            with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
                b.front()
                pf = PtToggleFiller(b.page, self.s["timeout"],
                                    on_note=lambda m: self.ui.log(f"    {m}", "warn"))
                stop = False

                for (route_id, sname), recs in groups:
                    if stop:
                        break
                    try:
                        if route_id:
                            self.ui.log(f"→ 策略「{sname or route_id}」")
                            pf.open_strategy(route_id)
                        else:
                            pf.wait_table()
                        pf.first_page()
                    except Stopped:
                        raise
                    except Exception as e:
                        for rec in recs:
                            k = rkey(route_id, rec["name"])
                            results_by[k] = self._res(rec["name"], "failed",
                                                      f"打开策略失败：{e}", "", sname)
                            stats["failed"] += 1
                            pending.pop(k, None)
                        self.ui.log(f"  策略「{sname or route_id}」打不开：{e}", "error")
                        self.ui.progress(len(results_by), total, stats)
                        continue

                    want_here = {rkey(route_id, r["name"]) for r in recs}
                    stop = self._toggle_page_by_page(
                        pf, b, want_here, pending, results_by, stats, total, sname, dry)

        except Stopped:
            self.ui.log("已停止", "warn")
        except Exception as e:
            log.exception("运行中断")
            self.ui.log(f"运行中断：{e}", "error")

        for k, rec in list(pending.items()):
            results_by[k] = self._res(rec.get("name", ""), "failed",
                                      "跑的时候这一行在表里找不到了", "",
                                      (rec.get("strategy") or {}).get("name", ""))
            stats["failed"] += 1

        results = [results_by.get(
            rkey((r.get("strategy") or {}).get("route_id"), r.get("name", "")),
            self._res(r.get("name", ""), "skipped", "没处理", "", "")) for r in records]
        self._write_results(results)
        ok = stats["failed"] == 0
        self.ui.finished(
            f"批量{self._verb}完成" if ok else f"批量{self._verb}完成（有失败）",
            f"成功 {stats['ok']}　跳过 {stats['skipped']}　"
            f"失败 {stats['failed']}　试跑 {stats['dry']}\n\n明细：{self.s['result_file']}",
            ok)
        return results

    def _toggle_page_by_page(self, pf, b, want_here: set, pending: dict, results_by: dict,
                             stats: dict, total: int, sname: str, dry: bool) -> bool:
        """把 want_here 里的行在当前这条策略页上逐页开/关。返回 True = 用户要停。"""
        def rkey(name):
            # 当前策略的 route_id：从 pending 里任一条同策略记录拿
            return next((k for k in want_here), None)

        for pageno in range(1, MAX_PAGES + 1):
            snap = pf.snapshot()
            here = [x for x in snap.get("rows", [])
                    if any(norm(x["name"]) == norm(pending[k]["name"]) and k in want_here
                           for k in list(pending))]
            for x in here:
                self.ui.checkpoint()
                # 找到这一行对应的 pending key（同策略 + 同名）
                key = next((k for k in list(pending)
                            if k in want_here and norm(pending[k]["name"]) == norm(x["name"])), None)
                if key is None:
                    continue
                name = pending[key]["name"]
                idx = len(results_by) + 1
                label = f"[{idx}/{total}]"
                act, kind, reason = self._classify(x)

                if act == "block":
                    results_by[key] = self._res(name, "skipped", reason, kind, sname)
                    stats["skipped"] += 1
                    pending.pop(key, None)
                    self.ui.log(f"{label} {name} —— 跳过（{reason}）", "warn")
                    self.ui.progress(len(results_by), total, stats)
                    continue
                if act == "done":
                    results_by[key] = self._res(name, "skipped", "已是目标状态", kind, sname)
                    stats["skipped"] += 1
                    pending.pop(key, None)
                    self.ui.log(f"{label} {name} —— 已{self._verb}，跳过")
                    self.ui.progress(len(results_by), total, stats)
                    continue
                if dry:
                    results_by[key] = self._res(name, "dry_run", "", kind, sname)
                    stats["dry"] += 1
                    pending.pop(key, None)
                    self.ui.log(f"{label} {name} —— 试跑：会{self._verb}")
                    self.ui.progress(len(results_by), total, stats)
                    continue

                action = "submit" if self.auto else self.ui.confirm(
                    label, f"{('[' + sname + '] ') if sname else ''}{name} —— {self._verb}？")
                if action == "auto":
                    self.auto, action = True, "submit"
                if action == "stop":
                    self.ui.log("已停止", "warn")
                    return True
                if action == "skip":
                    results_by[key] = self._res(name, "skipped", "用户跳过", kind, sname)
                    stats["skipped"] += 1
                    pending.pop(key, None)
                    self.ui.progress(len(results_by), total, stats)
                    continue

                try:
                    b.front()
                    r = pf.toggle(name, self.direction)
                    results_by[key] = self._res(
                        name, "ok" if r == "ok" else "skipped",
                        "" if r == "ok" else "已是目标状态", kind, sname)
                    stats["ok" if r == "ok" else "skipped"] += 1
                    pending.pop(key, None)
                    self.ui.log(f"{label} {name} —— 已{self._verb}", "ok")
                except Stopped:
                    raise
                except Exception as e:
                    msg = str(e)
                    log.exception("%s 失败", label)
                    results_by[key] = self._res(name, "failed", msg, kind, sname)
                    stats["failed"] += 1
                    pending.pop(key, None)
                    self.ui.log(f"{label} {name} —— 失败：{msg}", "error")
                    if not self.ui.ask_continue(msg):
                        return True
                self.ui.progress(len(results_by), total, stats)

            # 这条策略的行都处理完了？
            if not any(k in want_here for k in pending):
                break
            if snap.get("page", pageno) >= snap.get("pages", pageno):
                break
            if not pf.next_page():
                break
        return False

    # ---------------- 输出 ----------------
    def _res(self, name: str, status: str, error: str, kind: str, strategy: str) -> dict:
        return {"策略": strategy, "名称": name, "状态": status, "错误": error,
                "计划动作": kind, "方向": self._verb}

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
            self.ui.log(f"{path} 被占用（用 Excel 开着？），结果没写进去", "warn")
