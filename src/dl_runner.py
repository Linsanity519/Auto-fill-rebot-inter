"""常规资源位批量开关：把「投放列表」里的单元 / 创意批量启动 或 暂停。

只服务 mode: dl_toggle。操作的是大会员投放系统的投放列表页
（rich-vip.bilibili.co/.../delivery-list/delivery/unit | /originality）——
每行「操作」列的「更多」里有「启动投放」/「暂停投放」，**点完有一次二次确认**。
详见 docs/常规资源位批量开关-配置项抓取.md。

和价格策略那套（pt_*）的关键差别：

  · 多一个**层级**：单元 / 创意，两张表两套列，界面上切（`toggle_level`）
  · 多一个**活动ID**：这张列表全站四万条，不圈定一个活动就没法批量（`toggle_activity`）
  · 点完**有确认弹窗**，filler 会先核对弹窗文字再点「确 定」
  · **只认「启动投放 / 暂停投放」两项，永远不点「终止投放」** —— 那个不可逆

和 pt 一样的地方：不建东西、只翻转已有行的开关，所以没有断点（幂等，重跑无害）；
preview() 要读活的页面，所以会开浏览器。

选哪些行 = 「范围」：
  keyword  按名称关键词（子串命中单元名称；留空 = 这个活动下所有行）
  list     按清单（一行一个 ID 或名称：ID 精确匹配，非数字当名称子串）
  ledger   本工具操作过的 —— 台账里**反方向**动过的那批（见 src/dl_ledger.py）。
           「刚被我暂停的那批，现在恢复投放」就是它，不用自己留 ID 清单。

活动ID 可以**留空**，前提是范围=按清单、并且填的是纯数字 ID：那时不整批筛，
而是**一个 ID 查一次列表、查到就点**（筛选区的「单元ID」/「创意ID」一次只吃一个，
所以只能这么来）。查不到的 ID 会单独列一行说清楚，不会悄悄少几条。

⚠ 创意那张表**没有活动ID列、也没有活动ID筛选框**，只有活动名称。所以
  层级=创意 时先去单元表按活动ID查一遍，拿到活动名称 + 这个活动下的单元ID全集，
  再回创意表按活动名称筛、最后用单元ID全集把跨活动同名的行剔掉。见 _collect()。
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

from . import dl_filler as DF
from . import dl_ledger
from .browser import Browser
from .dl_filler import DlToggleFiller
from .fill_core import norm, split_multi
from .preview import PreviewRow
from .runstate import StateMixin
from .ui import BaseUI, ConsoleUI, Stopped

log = logging.getLogger(__name__)

# 100 条/页 × 40 页 = 4000 行封顶。真有活动比这还大，让人先用关键词收窄
MAX_PAGES = 40


class DlToggleRunner(StateMixin):
    def __init__(self, settings: dict, form_cfg: dict, ui: BaseUI | None = None):
        self.s = settings
        self.f = form_cfg
        self.ui = ui or ConsoleUI()
        self.shot_dir = Path(settings["screenshot_dir"])
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self.auto = False
        # 方向 / 层级：界面上切的，命令行退回 yaml 的默认值
        self.direction = str(settings.get("toggle_direction")
                             or form_cfg.get("direction") or "on").lower()      # on / off
        self.level = str(settings.get("toggle_level")
                         or form_cfg.get("level") or "unit").lower()             # unit / creative
        if self.level not in DF.LEVELS:
            self.level = "unit"
        self.activity = str(settings.get("toggle_activity") or "").strip()
        self.ledger_name = form_cfg.get("ledger") or ""
        self._init_state()      # 只为给 webapp 提供 clear_state，本 runner 不记断点

    # ---------------- 文案 ----------------
    @property
    def _verb(self) -> str:
        return "启动" if self.direction == "on" else "暂停"

    @property
    def _level_label(self) -> str:
        return DF.LEVELS[self.level]["label"]

    @property
    def _id_label(self) -> str:
        return DF.LEVELS[self.level]["id_col"]

    # ---------------- 参数 ----------------
    def _scope(self) -> str:
        v = str(self.s.get("toggle_scope") or self.s.get("pt_scope") or "keyword").lower()
        return v if v in ("keyword", "list", "ledger") else "keyword"

    @staticmethod
    def _date(v) -> str | None:
        """'2026-09-04' / '2026/9/4' → '2026-09-04'；填的不是日期就当没填。"""
        s = str(v or "").strip().replace("/", "-").replace(".", "-")
        parts = s.split("-")
        if len(parts) != 3 or not all(x.isdigit() for x in parts):
            return None
        y, m, d = (int(x) for x in parts)
        return f"{y:04d}-{m:02d}-{d:02d}"

    # ---------------- 「本工具操作过的」 ----------------
    def _ledger_picked(self) -> list[str]:
        """界面上勾了哪几批（`toggle_ledger_ids`）。没勾 = 空表 = 符合条件的都要。"""
        raw = self.s.get("toggle_ledger_ids") or []
        if isinstance(raw, str):          # 命令行/手写配置时可能是一串逗号分隔的
            raw = raw.replace("，", ",").split(",")
        return [str(x).strip() for x in raw if str(x).strip()]

    def _ledger_items(self) -> list[dict]:
        """要翻回去的那批：台账里**反方向**动过的那些行。

        现在方向=启动 → 找上次被本工具**暂停**的那些；反之亦然。
        再按「层级」卡一道（单元的台账不会串到创意），活动ID / 日期区间 /
        界面上勾了哪几批，填了就各再筛一层。
        """
        if not self.ledger_name or self._scope() != "ledger":
            return []
        return dl_ledger.items_for(
            self.ledger_name,
            level=self.level,
            direction=("off" if self.direction == "on" else "on"),
            activity=self.activity,
            since=self._date(self.s.get("toggle_date_from")),
            until=self._date(self.s.get("toggle_date_to")),
            ids=self._ledger_picked())

    def _tokens(self) -> list[str]:
        """`toggle_params` 拆成词：按行 + 逗号/顿号/分号。keyword 和 list 都用它。"""
        raw = str(self.s.get("toggle_params") or "")
        out: list[str] = []
        for line in raw.replace("\r", "\n").split("\n"):
            line = line.strip()
            if not line:
                continue
            out.extend(split_multi(line) if any(c in line for c in ",，、;；") else [line])
        return [k for k in (x.strip() for x in out) if k]

    # ---------------- 扫一页一页 ----------------
    def _scan_all(self, pf: DlToggleFiller) -> list[dict]:
        pf.big_page()
        pf.first_page()
        seen: set = set()
        rows: list[dict] = []
        for pageno in range(1, MAX_PAGES + 1):
            snap = pf.snapshot()
            for x in snap.get("rows", []):
                k = x.get("key") or f"{pageno}:{x.get('id')}"
                if k in seen:
                    continue
                seen.add(k)
                rows.append({**x, "page": snap.get("page", pageno)})
            if snap.get("page", pageno) >= snap.get("pages", pageno):
                break
            if not pf.next_page():
                break
        else:
            self.ui.log(f"这个活动下超过 {MAX_PAGES} 页，只看了前 {len(rows)} 行 —— "
                        f"用「按名称关键词」收窄一下", "warn")
        pf.first_page()
        return rows

    # ---------------- 逐个 ID 查（活动ID 留空时走这条） ----------------
    def _id_list(self) -> list[str]:
        """没填活动ID时能用的那批 ID：范围=按清单里那些**纯数字**。

        填了活动ID就不走这条（那时清单只是在活动内部再挑一遍，不用一个个查）。
        """
        if self.activity:
            return []
        if self._scope() == "ledger":
            return [it["id"] for it in self._ledger_items() if it["id"].isdigit()]
        if self._scope() != "list":
            return []
        seen, out = set(), []
        for t in self._tokens():
            if t.isdigit() and t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def _collect_by_id(self, pf: DlToggleFiller, ids: list[str]) -> list[dict]:
        """一个 ID 查一次列表（筛选区的「单元ID」/「创意ID」一次只吃一个）。

        查不到的也留一行（标 missing），预检里会写清楚是哪几个 ID 没找着 ——
        比直接少几行让人对不上数强。
        """
        col = self._id_label
        out: list[dict] = []
        for i, one in enumerate(ids, 1):
            self.ui.checkpoint()
            self.ui.log(f"  [{i}/{len(ids)}] 查 {col}={one}…")
            try:
                pf.query(self.level, {col: one})
                hit = next((r for r in pf.snapshot().get("rows", [])
                            if str(r.get("id")) == one), None)
            except Exception as e:
                out.append({"key": "", "id": one, "name": "", "state": "",
                            "missing": f"查这个{col}时出错：{e}"})
                continue
            out.append(hit if hit else
                       {"key": "", "id": one, "name": "", "state": "",
                        "missing": f"列表里查不到这个{col}"})
        return out

    # ---------------- 圈定「这个活动下的哪些行」 ----------------
    def _collect(self, pf: DlToggleFiller) -> tuple[list[dict], str]:
        """→ (要操作的全部行, 活动名称)。

        活动ID 留空 + 范围=按清单 ：逐个 ID 去列表里查（`_collect_by_id`）。
        填了活动ID —— 单元：直接按「活动ID」筛就完了。
                     创意：创意表没有活动ID，只能先在单元表把活动名称 + 单元ID全集
                           查出来，再按活动名称筛创意、用单元ID全集过滤（同名活动会串）。
        """
        ids = self._id_list()
        if ids:
            self.ui.log(f"没填活动ID → 按清单里的 {len(ids)} 个{self._id_label}逐个查")
            return self._collect_by_id(pf, ids), ""

        if not self.activity:
            if self._scope() == "ledger":
                # 走到这儿说明台账里一条都没挑出来，_pick 那句话最完整，借过来用
                raise ValueError(self._pick([])[1])
            raise ValueError(
                "没填「活动ID」—— 这张列表全站四万多行，得先圈定范围："
                f"要么填活动ID，要么把「选哪些行」切到「按清单」、填上{self._id_label}"
                "（纯数字，一行一个），工具会逐个去查")

        # 先查单元表：两个层级都要它（创意层要从这里拿活动名称和单元ID全集）
        pf.query("unit", {"活动ID": self.activity})
        units = self._scan_all(pf)
        if not units:
            raise ValueError(f"活动 {self.activity} 下一个单元都没查到 —— 活动ID 填对了吗？")
        act_name = next((u.get("act_name") for u in units if u.get("act_name")), "")

        if self.level == "unit":
            return units, act_name

        if not act_name:
            raise ValueError(f"活动 {self.activity} 的活动名称读不出来，"
                             f"创意表只能按活动名称筛，没法继续")
        unit_ids = {str(u.get("id")) for u in units if u.get("id")}
        self.ui.log(f"活动 {self.activity}「{act_name}」下 {len(unit_ids)} 个单元，"
                    f"去创意表按活动名称查…")

        pf.query("creative", {"活动名称": act_name})
        rows = self._scan_all(pf)
        keep = [r for r in rows if str(r.get("unit_id")) in unit_ids]
        dropped = len(rows) - len(keep)
        if dropped:
            self.ui.log(f"按活动名称查回 {len(rows)} 条创意，其中 {dropped} 条不属于"
                        f"活动 {self.activity}（同名活动），已剔除", "warn")
        return keep, act_name

    # ---------------- 选行 ----------------
    def _pick(self, rows: list[dict]) -> tuple[list[dict], str]:
        """→ (选中的行, 没选中时的一句原因)。"""
        if self._scope() == "ledger":
            items = self._ledger_items()
            if not items:
                picked = self._ledger_picked()
                return [], (f"勾的那 {len(picked)} 批里没有可翻回的{self._level_label}"
                            "（方向 / 层级 / 活动ID 和勾的那几批对不上？）" if picked else
                            (f"台账里没有「本工具{'暂停' if self.direction == 'on' else '启动'}"
                             f"过的{self._level_label}」可翻回"
                             + (f"（活动 {self.activity}）" if self.activity else "")
                             + " —— 先用这个工具跑一批，之后这里才有东西"))
            ids = {it["id"] for it in items}
            got = [r for r in rows if str(r.get("id")) in ids]
            return got, ("" if got else
                         f"台账里那 {len(ids)} 条在当前这批 {len(rows)} 行里一个都没对上")

        if self._scope() == "list":
            toks = self._tokens()
            if not toks:
                return [], "「按清单」但一个 ID / 名称都没填"
            ids = {t for t in toks if t.isdigit()}
            names = [t for t in toks if not t.isdigit()]
            got = [r for r in rows
                   if str(r.get("id")) in ids or any(n in (r.get("name") or "") for n in names)]
            return got, ("" if got else
                         f"清单里的 {len(toks)} 项在 {len(rows)} 行里一个都没对上")

        kws = self._tokens()
        if not kws:
            return rows, ("这个活动下一条都没有" if not rows else "")
        got = [r for r in rows if any(k in (r.get("name") or "") for k in kws)]
        return got, ("" if got else f"关键词 {kws} 在 {len(rows)} 行里一个都没命中")

    # ---------------- 分类 ----------------
    def _classify(self, r: dict) -> tuple[str, str, str]:
        """→ (act, kind, reason)。act ∈ {toggle, done, block}。"""
        if r.get("missing"):
            return "block", "没找到", str(r["missing"])
        state = norm(r.get("state", ""))
        target = DF.STATE_ON if self.direction == "on" else DF.STATE_OFF
        source = DF.STATE_OFF if self.direction == "on" else DF.STATE_ON
        if state == target:
            return "done", f"已{'投放中' if self.direction == 'on' else '暂停'}", ""
        if state == source:
            return "toggle", f"将{self._verb}投放", ""
        if state in DF.DEAD_STATES:
            return "block", f"{state}·跳过", (
                f"状态是「{state}」，「更多」里既没有启动也没有暂停 —— "
                f"这类要改投放时间才动得了")
        return "block", "状态异常", f"状态列写的是「{r.get('state') or '(空)'}」，没见过，不敢动"

    # ---------------- 预检 ----------------
    def preview(self) -> list[PreviewRow]:
        out: list[PreviewRow] = []
        with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
            b.front()
            pf = DlToggleFiller(b.page, self.s["timeout"],
                                on_note=lambda m: self.ui.log(f"    {m}", "warn"))
            rows, act_name = self._collect(pf)
            # 逐个 ID 查回来的行本来就是清单里那几条，不用再挑一遍
            picked, why = (rows, "") if self._id_list() else self._pick(rows)
            if not picked:
                return [PreviewRow(index=1, name="(没有命中的行)", kind="", detail_count=0,
                                   issues=[why or "没有可操作的行"])]
            for r in picked:
                act, kind, reason = self._classify(r)
                out.append(PreviewRow(
                    index=len(out) + 1,
                    name=f"[{r.get('id')}] {r.get('name') or ''}",
                    kind=f"{self._level_label} · {kind}",
                    detail_count=0,
                    issues=[reason] if act == "block" else [],
                    done=(act == "done"),
                    payload={"header": {"层级": self._level_label,
                                        self._id_label: r.get("id", ""),
                                        "名称": r.get("name", ""),
                                        "活动": f"{self.activity} {act_name}".strip(),
                                        "当前状态": r.get("state", ""),
                                        "本次动作": kind},
                             "key": r.get("key", ""), "id": r.get("id", ""),
                             "name": r.get("name", ""), "act": act, "kind": kind},
                ))
        return out

    # ---------------- 主流程 ----------------
    def run(self, records: list[dict] | None = None):
        if records is None:
            records = [r.payload for r in self.preview() if not r.issues]
        dry = bool(self.s.get("dry_run"))
        total = len(records)
        stats = {"ok": 0, "failed": 0, "skipped": 0, "dry": 0}
        pending = {str(rec.get("key")): rec for rec in records if rec.get("key")}
        results_by: dict[str, dict] = {}

        self.ui.log(f"「{self.f['name']}」共 {total} 个{self._level_label}待{self._verb}投放"
                    + (f"（活动 {self.activity}）" if self.activity else "（逐个 ID 查）")
                    + ("（试跑：只看不点）" if dry else ""))
        self.ui.progress(0, total, stats)

        try:
            with Browser(self.s["cdp_url"], self.s["timeout"]) as b:
                b.front()
                pf = DlToggleFiller(b.page, self.s["timeout"],
                                    on_note=lambda m: self.ui.log(f"    {m}", "warn"))
                if self._id_list():
                    # 活动ID 留空：一个 ID 查一次列表，查一条点一条
                    self._toggle_by_id(pf, b, records, pending, results_by, stats, total, dry)
                else:
                    # 重新筛一遍：预检和开跑之间可能隔了一会儿，页面也可能被人动过
                    self._collect(pf)
                    pf.big_page()
                    pf.first_page()
                    self._toggle_page_by_page(pf, b, pending, results_by, stats, total, dry)
        except Stopped:
            self.ui.log("已停止", "warn")
        except Exception as e:
            log.exception("运行中断")
            self.ui.log(f"运行中断：{e}", "error")

        for key, rec in list(pending.items()):
            results_by[key] = self._res(rec, "failed", "跑的时候这一行在列表里找不到了")
            stats["failed"] += 1

        results = [results_by.get(str(r.get("key")), self._res(r, "skipped", "没处理"))
                   for r in records]
        self._write_results(results)
        self._write_ledger(records, results_by)
        ok = stats["failed"] == 0
        self.ui.finished(
            f"批量{self._verb}完成" if ok else f"批量{self._verb}完成（有失败）",
            f"成功 {stats['ok']}　跳过 {stats['skipped']}　"
            f"失败 {stats['failed']}　试跑 {stats['dry']}\n\n明细：{self.s['result_file']}",
            ok)
        return results

    def _handle_row(self, pf, b, rec: dict, x: dict, key: str, pending: dict,
                    results_by: dict, stats: dict, total: int, dry: bool) -> bool:
        """处理一行（跳过 / 试跑 / 确认 / 真点）。返回 True = 用户要停。

        两条跑法（整页扫 / 逐个ID查）共用它 —— 中间那段「怎么算跳过、怎么记结果」
        写两遍一定会漂。
        """
        self.ui.checkpoint()
        name = f"[{rec.get('id')}] {rec.get('name') or ''}"
        label = f"[{len(results_by) + 1}/{total}]"
        act, kind, reason = self._classify(x)

        if act in ("block", "done"):
            why = reason or "已是目标状态"
            results_by[key] = self._res(rec, "skipped", why, kind)
            stats["skipped"] += 1
            pending.pop(key, None)
            self.ui.log(f"{label} {name} —— 跳过（{why}）",
                        "warn" if act == "block" else "info")
            self.ui.progress(len(results_by), total, stats)
            return False
        if dry:
            results_by[key] = self._res(rec, "dry_run", "", kind)
            stats["dry"] += 1
            pending.pop(key, None)
            self.ui.log(f"{label} {name} —— 试跑：会{self._verb}投放")
            self.ui.progress(len(results_by), total, stats)
            return False

        action = "submit" if self.auto else self.ui.confirm(
            label, f"{self._level_label}「{name}」—— {self._verb}投放？")
        if action == "auto":
            self.auto, action = True, "submit"
        if action == "stop":
            self.ui.log("已停止", "warn")
            return True
        if action == "skip":
            results_by[key] = self._res(rec, "skipped", "用户跳过", kind)
            stats["skipped"] += 1
            pending.pop(key, None)
            self.ui.progress(len(results_by), total, stats)
            return False

        try:
            b.front()
            # 点的是**页面上这一行**的 key；results_by 仍按记录自己的 key 记
            r = pf.toggle(str(x.get("key") or key), self.direction)
            results_by[key] = self._res(rec, "ok" if r == "ok" else "skipped",
                                        "" if r == "ok" else "已是目标状态", kind)
            stats["ok" if r == "ok" else "skipped"] += 1
            pending.pop(key, None)
            self.ui.log(f"{label} {name} —— 已{self._verb}投放", "ok")
        except Stopped:
            raise
        except Exception as e:
            msg = str(e)
            log.exception("%s 失败", label)
            results_by[key] = self._res(rec, "failed", msg, kind)
            stats["failed"] += 1
            pending.pop(key, None)
            self.ui.log(f"{label} {name} —— 失败：{msg}", "error")
            if not self.ui.ask_continue(msg):
                return True
        self.ui.progress(len(results_by), total, stats)
        return False

    def _toggle_page_by_page(self, pf, b, pending: dict, results_by: dict,
                             stats: dict, total: int, dry: bool):
        """填了活动ID：整个活动筛出来，一页一页扫过去，命中清单的就点。"""
        for pageno in range(1, MAX_PAGES + 1):
            snap = pf.snapshot()
            for x in list(snap.get("rows", [])):
                key = str(x.get("key"))
                if key not in pending:
                    continue
                if self._handle_row(pf, b, pending[key], x, key,
                                    pending, results_by, stats, total, dry):
                    return

            if not pending:
                break
            if snap.get("page", pageno) >= snap.get("pages", pageno):
                break
            if not pf.next_page():
                break

    def _toggle_by_id(self, pf, b, records: list[dict], pending: dict, results_by: dict,
                      stats: dict, total: int, dry: bool):
        """活动ID 留空：一个 ID 查一次列表，查到就点，一条一条来。

        ⚠ 每条都重新查一遍，而不是拿预检时那一屏 —— 预检和开跑之间隔着人点确认的
          时间，状态可能已经被别人改了。
        """
        col = self._id_label
        for rec in records:
            key = str(rec.get("key") or "")
            if key not in pending:
                continue
            one = str(rec.get("id") or "")
            self.ui.checkpoint()
            try:
                pf.query(self.level, {col: one})
                x = next((r for r in pf.snapshot().get("rows", [])
                          if str(r.get("id")) == one), None)
            except Stopped:
                raise
            except Exception as e:
                results_by[key] = self._res(rec, "failed", f"查这个{col}时出错：{e}")
                stats["failed"] += 1
                pending.pop(key, None)
                self.ui.log(f"[{len(results_by)}/{total}] {col}={one} —— 查不了：{e}", "error")
                self.ui.progress(len(results_by), total, stats)
                continue
            if not x:
                results_by[key] = self._res(rec, "failed", f"列表里查不到这个{col}")
                stats["failed"] += 1
                pending.pop(key, None)
                self.ui.log(f"[{len(results_by)}/{total}] {col}={one} —— 列表里查不到", "error")
                self.ui.progress(len(results_by), total, stats)
                continue
            if self._handle_row(pf, b, rec, x, key, pending, results_by,
                                stats, total, dry):
                return

    # ---------------- 台账 ----------------
    def _write_ledger(self, records: list[dict], results_by: dict):
        """把这一批**真的翻转成功的**记进台账，供下次「本工具操作过的」翻回去。

        ⚠ 只记 ok 的：跳过和失败的根本没动过，记进去会让"翻回去"翻到一堆无关的行。
        ⚠ 写台账失败绝不能影响本轮结果 —— 整段吞掉异常。
        """
        if not self.ledger_name:
            return
        try:
            ok_ids = {k for k, v in results_by.items() if v.get("状态") == "ok"}
            items = [{"id": r.get("id"), "name": r.get("name")}
                     for r in records if str(r.get("key")) in ok_ids]
            if not items:
                return
            dl_ledger.append(self.ledger_name, level=self.level,
                             level_label=self._level_label, activity=self.activity,
                             direction=self.direction, verb=self._verb, items=items)
            self.ui.log(f"已记入台账：{len(items)} 条 —— 想翻回去的话，"
                        f"方向切到「{'关闭' if self.direction == 'on' else '开启'}」、"
                        f"范围选「本工具操作过的」")
        except Exception:
            log.warning("写台账失败（不影响本轮）", exc_info=True)

    # ---------------- 输出 ----------------
    def _res(self, rec: dict, status: str, error: str, kind: str = "") -> dict:
        return {"层级": self._level_label, "活动ID": self.activity,
                self._id_label: rec.get("id", ""), "名称": rec.get("name", ""),
                "状态": status, "错误": error,
                "计划动作": kind or rec.get("kind", ""), "方向": f"{self._verb}投放"}

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
