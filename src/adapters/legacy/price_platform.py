"""Reuse proven selectors while selecting objects by platform ID, never by name."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from ...domain.pricing import PriceItem
from ...pt_filler import PtToggleFiller
from ...pt_runner import PtToggleRunner, _STRATEGY_JS
from ...fill_core import wait_until
from ... import formcfg
from ...ui import ConsoleUI

ORIGIN = "https://rich-vip.bilibili.co"


class PricePlatform:
    def __init__(self, settings: dict):
        self.settings = settings
        self.pw = self.browser = self.page = self.ctx = None

    def __enter__(self):
        try:
            self.pw = sync_playwright().start()
            self.browser = self.pw.chromium.connect_over_cdp(
                self.settings["cdp_url"], timeout=self.settings["timeout"])
            candidates = [(ctx, page) for ctx in self.browser.contexts for page in ctx.pages
                          if page.url.startswith(ORIGIN + "/") and "/strategy-center/" in page.url]
            explicit = self.settings.get("platform_page_url")
            if explicit:
                candidates = [(ctx, page) for ctx, page in candidates if page.url == explicit]
            if len(candidates) != 1:
                raise ValueError("请在调试浏览器中保留一个价格策略页；多个候选页需设置 platform_page_url 精确选择")
            self.ctx, source = candidates[0]
            # An owned tab avoids navigating the user's arbitrary first tab.
            self.page = self.ctx.new_page()
            self.page.set_default_timeout(self.settings["timeout"])
            self.page.set_default_navigation_timeout(max(self.settings["timeout"] * 3, 45000))
            self.page.goto(source.url, wait_until="domcontentloaded")
            self.pf = PtToggleFiller(self.page, self.settings["timeout"])
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        # Only close the tab this adapter created, never the user's browser.
        try:
            if self.page:
                self.page.close()
        finally:
            if self.pw:
                self.pw.stop()

    def context(self) -> dict:
        if not self.page.url.startswith(ORIGIN + "/"):
            raise ValueError("页面离开指定平台，可能登录已失效")
        cookies = self.ctx.cookies([ORIGIN])
        # This identifies the authenticated session, NOT a verified human name.
        identity = sorted((x["name"], x["value"], x.get("domain", "")) for x in cookies
                          if x.get("httpOnly") or x["name"] in ("DedeUserID", "SESSDATA", "sid"))
        if not identity:
            raise ValueError("未识别到登录会话，请先登录平台")
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()
        return {"environment": urlsplit(ORIGIN).netloc, "session_id": digest,
                "identity_kind": "session_fingerprint",
                "identity_label": "浏览器会话 " + digest[:12] + "（非实名核验）"}

    def _scan(self) -> list[dict]:
        self.pf.wait_table()
        self.pf.first_page()
        out, seen = [], set()
        for _ in range(100):
            snap = self.pf.wait_table()
            for row in snap["rows"]:
                key = str(row.get("key") or "")
                if key in seen and key:
                    raise ValueError("分页返回重复对象 ID，结果不完整")
                seen.add(key)
                out.append(row)
            if snap["page"] >= snap["pages"]:
                if snap.get("total", 0) > len(out):
                    raise ValueError("读取条数小于平台总数，不能按部分数据生成计划")
                return out
            before = snap["page"]
            if not self.pf.next_page() or self.pf.snapshot()["page"] == before:
                raise ValueError("分页未前进，不能按部分数据执行")
        raise ValueError("超过 100 页读取上限，请缩小范围")

    def discover(self, inputs: dict) -> list[dict]:
        cfg = formcfg.load(inputs["form_name"])
        if cfg.get("mode") != "pt_toggle":
            raise ValueError("此用例仅支持价格策略批量开关")
        runner = PtToggleRunner({**self.settings, **inputs}, cfg, ConsoleUI())
        if len(runner._strategy_tokens()) > 50:
            raise ValueError("每份计划最多 50 条策略，请拆分输入")
        targets, bad = runner._targets(self.pf)
        rows = []
        for i, item in enumerate(bad):
            rows.append({"strategy_id": "invalid", "key": str(i), "name": item.name,
                         "issue": "；".join(item.issues)})
        for target in targets:
            if target.get("route_id"):
                self.pf.open_strategy(target["route_id"])
            self.pf.wait_table()
            meta = self.page.evaluate(_STRATEGY_JS)
            sid = str(meta.get("id") or "")
            found = self._scan()
            selected, reason = runner._pick(found, sid)
            if reason and not selected:
                rows.append({"strategy_id": sid, "key": "no_match", "issue": reason})
            rows.extend({**row, "strategy_id": sid, "strategy_name": meta.get("name", "")}
                        for row in selected)
        return rows

    def _locate(self, item: PriceItem) -> dict:
        self.pf.open_strategy(item.strategy_id)
        self.pf.first_page()
        for _ in range(100):
            snap = self.pf.wait_table()
            rows = [x for x in snap["rows"] if str(x["key"]) == item.object_id]
            if len(rows) > 1:
                raise ValueError("同页存在重复对象 ID")
            if rows:
                row = rows[0]
                if row["name"] != item.name or row.get("group", "") != item.group:
                    raise ValueError("对象名称或选组已变化，请重新生成计划")
                return row
            if snap["page"] >= snap["pages"] or not self.pf.next_page():
                break
        raise ValueError("指定策略中的对象 ID 已不存在或无法完整读取")

    def read(self, item: PriceItem) -> str:
        row = self._locate(item)
        return {"开启": "off", "关闭": "on"}.get(row["link"], "unknown")

    def set_state(self, item: PriceItem) -> None:
        row = self._locate(item)
        expected = "开启" if item.before == "off" else "关闭"
        if row["link"] != expected:
            raise ValueError("提交前状态已变化")
        target = self.page.locator("tr.ant-table-row[data-row-key=" + json.dumps(item.object_id) + "]")
        if target.count() != 1:
            raise ValueError("平台对象定位不唯一")
        link = target.locator("td").last.locator("a").filter(has_text=re.compile("^" + expected + "$"))
        if link.count() != 1:
            raise ValueError("操作按钮不唯一")
        link.click()
        desired = "关闭" if item.after == "on" else "开启"
        if not wait_until(self.page, lambda: any(str(x["key"]) == item.object_id and x["link"] == desired
                                              for x in self.pf.snapshot()["rows"]), self.settings["timeout"]):
            raise RuntimeError("提交后未核验目标状态")
