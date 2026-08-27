"""原生商广主流程：一个内容一个单元，每个单元最多 10 条创意。

⚠ 与 runner / dmp_runner / ab_runner / wizard_runner 都独立。

和资源位投放（wizard）最大的不同是「没有分步跳转」：计划、单元、创意在同一页，
只有一个「保存」按钮，一次提交同时建出三层。所以流程是：

  第 1 个单元  → ?type=1            建计划 + 单元 + 创意，保存后回列表页把计划ID捞出来
  第 2..N 个   → ?campaign_id=<id>  挂到刚才那个计划下，只建单元 + 创意

准备阶段填了「已有计划ID」的话，第一步也省了，全部走 ?campaign_id=。

⚠ ?campaign_id= 那个页面不会继承任何上一单元的设置（定向不限、转化目标空），
  所以每个单元都是从零填一遍，不能只填「变化的那几项」。
"""
from __future__ import annotations

import csv
import logging
import re
from datetime import datetime
from pathlib import Path

from . import ad_data as D
from . import ad_prep as P
from .ad_filler import AdFiller
from .browser import Browser
from .preview import PreviewRow
from .runstate import StateMixin
from .ui import BaseUI, ConsoleUI, Stopped

log = logging.getLogger(__name__)

# 计划列表里一行的「ID:54534700」
CAMPAIGN_ID_RE = re.compile(r"ID[:：]\s*(\d+)")


def _accept_dialog(dialog):
    """一律「确认离开」。

    ⚠ 表单填过之后页面会挂 onbeforeunload，下一趟 goto/reload 必然弹「离开吗」。
      不接管的话 Playwright 走它自己的默认 dismiss 分支，在这套页面上会撞出
      「Protocol error (Page.handleJavaScriptDialog): No dialog is showing」
      直接把 driver 打挂，整轮跑不下去。这里显式接管，顺手兜住重复关闭的异常。
    """
    try:
        dialog.accept()
    except Exception:
        log.debug("关弹窗失败，多半是它自己已经没了", exc_info=True)


def _key(unit: dict) -> str:
    """断点的 key：单元名。这套后台里单元名本来就唯一（重名建不出来）。"""
    return str(unit.get("name") or unit.get("key") or "")


class AdRunner(StateMixin):
    def __init__(self, settings: dict, form_cfg: dict, ui: BaseUI | None = None):
        self.s = settings
        self.f = form_cfg
        self.ui = ui or ConsoleUI()
        self.shot_dir = Path(settings["screenshot_dir"])
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self.auto = False
        self.created = []          # 建出来的单元，跑完报给用户
        self._init_state()

    # ---------------- 预检 ----------------
    def preview(self) -> list[PreviewRow]:
        prep = self.s.get("ad_prep") or P.load(self.f)
        data = D.load(self.s["data_file"], self.f, self.s)
        issues = D.validate(self.f, data)

        rows = []
        for i, u in enumerate(data["units"]):
            mine = [x for x in issues if x.startswith(f"「{u['name']}」")]
            rows.append(PreviewRow(
                index=i + 1, name=u["name"], kind=u["key"],
                detail_count=len(u["creatives"]), issues=mine,
                done=self.state.is_done(_key(u)), payload=u,
            ))

        # 准备阶段的问题不属于任何一个单元，挂在第一行上，
        # 免得「校验通过」满屏绿、真正拦路的那条反而没人看见
        head = P.validate(self.f, prep)
        if head and rows:
            rows[0].issues = head + rows[0].issues

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
        stats = {"ok": 0, "failed": 0, "skipped": 0, "dry": 0}
        results = []

        prep = self._resolve_prep(prep)
        campaign_id = str(prep.get("已有计划ID", "")).strip()
        plan_name = str(prep.get("计划名称", "")).strip()

        self.ui.log(f"「{self.f['name']}」共 {total} 个单元、"
                    f"{sum(len(u['creatives']) for u in units)} 条创意"
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

                for i, u in enumerate(units):
                    self.ui.checkpoint()
                    label = f"[{i + 1}/{total}]"
                    name = u["name"]
                    new_plan = not campaign_id
                    self.ui.log(f"{label} {name} —— 填写中（{len(u['creatives'])} 条创意）"
                                + ("，并新建计划" if new_plan else ""))

                    try:
                        self._open(b.page, campaign_id)
                        values = dict(prep)
                        values["单元名称"] = name

                        if new_plan:
                            af.fill(self.f.get("plan_fields") or [], values, scope="计划层 ")
                        af.fill(self.f.get("unit_fields") or [], values, scope="单元层 ")
                        self._do_creatives(af, u)
                        self._shot(b.page, i + 1, "filled")

                        if dry:
                            stats["dry"] += 1
                            results.append(self._row(i, name, "dry_run", "试跑未保存"))
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
                        results.append(self._row(i, name, "ok", ""))
                        # ⚠ 建出去了就立刻记断点：这个单元在后台是真存在了，
                        #   重跑再建一遍就是重复的单元
                        self.state.mark_done(_key(u))
                        self.created.append((name, len(u["creatives"])))
                        self.ui.log(f"{label} 已保存", "ok")

                        if new_plan:
                            campaign_id = self._find_campaign_id(b.page, plan_name)
                            if not campaign_id:
                                raise RuntimeError(
                                    f"计划「{plan_name}」建出来了，但在计划列表里没找到它的ID，"
                                    f"后面的单元不知道该挂到哪。\n"
                                    f"请到页面上查一下计划ID，填进准备阶段的「已有计划ID」再重跑。")
                            self.ui.log(f"    计划ID {campaign_id}，后面的单元都挂它下面", "ok")

                    except Stopped:
                        raise
                    except Exception as e:
                        msg = str(e)
                        log.exception("%s 失败", label)
                        shot = self._shot(b.page, i + 1, "error")
                        stats["failed"] += 1
                        results.append(self._row(i, name, "failed", msg))
                        self.state.mark_failed(_key(u), name, msg)
                        self.ui.log(f"{label} 失败：{msg}", "error")
                        self.ui.log(f"    截图：{shot}")
                        if not self.ui.ask_continue(msg):
                            break

                    self.ui.progress(i + 1, total, stats)

        except Stopped:
            self.ui.log("已停止", "warn")
        except Exception as e:
            # ⚠ 分清「压根没开始」和「跑完了但收尾出岔子」：
            #   results 非空说明单元已经建出去了，这时候再弹「没能开始」
            #   会让人以为白跑一趟、回头重跑一遍 —— 那才是真的出事。
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
                f"　{n}（{c} 条创意）" for n, c in self.created)
        self.ui.finished("跑完了" if ok else "跑完了，有失败", body, ok)
        return results

    # ---------------- 分步 ----------------
    def _resolve_prep(self, prep: dict) -> dict:
        """把准备阶段里的 {今天} 替换成跑的当天。"""
        today = datetime.now().strftime("%Y-%m-%d")
        return {k: str(v).replace("{今天}", today) for k, v in prep.items()}

    def _open(self, page, campaign_id: str):
        """打开这一趟要用的页面。

        ⚠ 必须强制整页重载，不能只 goto。两个入口只差 hash 里的 query
          （#/promote/auto?type=1 ↔ #/promote/auto?campaign_id=123），
          浏览器认为是同一个文档，Vue router 不会重新挂载组件 ——
          表现是 URL 已经变成 ?type=1 了，但「计划信息」那一整块压根没渲染，
          填到「推广目的」就报「找不到表单项」。
          第一次导航（本来就是别的文档）不用多此一举，所以只在同文档时补一次 reload。
        """
        urls = self.f.get("urls") or {}
        url = (urls["under_plan"].format(campaign_id=campaign_id) if campaign_id
               else urls["new_plan"])
        ready = self.f.get("ready_selector")
        # ⚠ 重试一次：后台自己的跳转有可能正好在我们 goto 之后落地，把新建页顶掉。
        #   _submit 已经等过跳转，这里再兜一层 —— 顶掉最多发生一次，重开就好。
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
                log.warning("打开 %s 后没等到 %s，页面现在在 %s，重开一次",
                            url, ready, page.url)
                page.wait_for_timeout(2000)
        page.wait_for_timeout(2000)

    def _do_creatives(self, af: AdFiller, unit: dict):
        """先把这个单元的稿件全加进来，再逐条填标题/描述/封面。

        ⚠ 必须在单元层填完之后才做：抽屉里要先有「推广内容 = OGV推广」，
          才搜得到 OGV 稿件。
        ⚠ 同一个 avid 在一个单元里出现多次时要分多趟加（见 ad_data.add_passes），
          每趟一次「确定」，追加到已有创意后面。
        """
        creative = self.f.get("creative") or {}
        passes = [[str(c[D.AVID]).strip() for c in group]
                  for group in D.add_passes(unit["creatives"])]
        af.add_archives(creative.get("picker") or {}, passes)
        for c in unit["creatives"]:
            af.fill_creative(creative, str(c[D.AVID]).strip(), c, int(c.get("_seq", 0)))

    def _submit(self, af: AdFiller, page):
        """点保存，并且一定要等到「页面已经离开新建页」才返回。

        ⚠ 保存是异步的，成功之后后台会自己把页面跳到刚建好的单元详情页。
          原来这里只 sleep 3 秒就返回，下一个单元的 _open 马上 goto 新建页，
          结果后台那次跳转晚一步落地，把我们刚打开的新建页顶掉 ——
          下一轮死等「单元名称」输入框直到超时。日志里表现为
          「[3/41] 失败 … waiting for input[placeholder="请输入单元名称"]」，
          而单元 2 其实是建成功的。所以必须等跳转落地再走。

        ⚠ 顺带把「跳走了没」当成保存成功的判据。原来只看表单红字，
          后台用 toast 报的错（比如出价超限被拦）会被当成成功。
        """
        create_marker = self.f.get("create_url_marker", "promote/auto")
        af.click_button(self.f.get("submit_button", "保存"), prefer_last=True)

        waited = 0
        step = 500
        limit = int(self.s.get("timeout", 15000)) * 2
        while waited < limit:
            page.wait_for_timeout(step)
            waited += step
            if create_marker not in page.url:
                # 已经跳到详情页 = 存下来了。再缓一下让新页面稳定，
                # 免得下一轮 goto 撞上这次跳转的收尾
                page.wait_for_timeout(int(self.s.get("after_submit_wait", 3000)))
                return
            err = page.locator(".ivu-form-item-error-tip:visible").first
            if err.count():
                raise RuntimeError(f"页面报错，没保存成功：{(err.inner_text() or '').strip()}")

        raise RuntimeError(
            f"点了「保存」{limit // 1000} 秒后页面还停在新建页，没保存成功。"
            f"到浏览器里看一眼是不是有弹窗或红字提示。")

    def _find_campaign_id(self, page, plan_name: str) -> str:
        """建完计划后，去计划列表里按名字把 ID 捞回来。

        ⚠ 页面保存后会跳到哪一页并不稳定（有时是列表，有时留在原页），
          所以不从 URL 里猜，直接显式回列表页找。
        """
        urls = self.f.get("urls") or {}
        list_url = urls.get("campaign_list") or "https://ad.bilibili.co/#/promote/campaign"
        # 同一个文档内换 hash 路由，同样强制重载一次，理由见 _open
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
        path = self.shot_dir / f"ad_{idx:03d}_{tag}.png"
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
