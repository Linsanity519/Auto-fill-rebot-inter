"""Thin compatibility bridge for the existing desktop/CLI runner interface."""
from __future__ import annotations

import threading
import time

from ...domain.pricing import PricePlan, describe
from ...preview import PreviewRow
from ...ui import ConsoleUI, Stopped
from ..host import TERMINAL


class PriceTaskRunner:
    managed_host = True

    def __init__(self, settings: dict, cfg: dict, ui, client):
        self.s, self.f, self.ui = settings, cfg, ui or ConsoleUI()
        self._client_factory, self._client = client, None
        self.auto = False
        self.task_id = ""
        self.plan = None

    @property
    def client(self):
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def clear_state(self):
        raise ValueError("价格任务保留完整历史；请重新预检生成新计划，不清除历史来重放任务")

    def preview(self) -> list[PreviewRow]:
        keys = ("pt_scope", "toggle_direction", "toggle_params", "toggle_strategies",
                "toggle_date_from", "toggle_date_to")
        inputs = {k: self.s[k] for k in keys if k in self.s}
        inputs["form_name"] = self.f["name"]
        response = self.client.request("prepare", {"inputs": inputs})
        self.plan = PricePlan.from_dict(response["plan"])
        return self.preview_plan()

    def preview_plan(self) -> list[PreviewRow]:
        self.ui.log(describe(self.plan))
        rows = []
        for index, item in enumerate(self.plan.items, 1):
            header = {"策略ID": item.strategy_id, "对象ID": item.object_id, "人群名称": item.name,
                      "变更前": item.before, "变更后": item.after,
                      "环境": self.plan.context["environment"],
                      "身份依据": self.plan.context["identity_label"]}
            rows.append(PreviewRow(index, item.name, item.before + " → " + item.after, 0,
                                   issues=[item.issue] if item.issue else [],
                                   done=item.before == item.after,
                                   payload={"plan_id": self.plan.plan_id, "item_id": item.item_id,
                                            "header": header}))
        return rows

    def run(self, records=None):
        if self.plan is None:
            raise ValueError("请先生成计划")
        records = records if records is not None else [x.payload for x in self.preview_plan() if not x.issues]
        if any(x.get("plan_id") != self.plan.plan_id for x in records):
            raise ValueError("条目属于另一份计划，请重新预检")
        if self.s.get("dry_run"):
            self.ui.log(describe(self.plan) + " 本次只读，没有提交。")
            return [{"名称": x["header"]["人群名称"], "状态": "dry_run", "错误": "",
                     "item_id": x["item_id"]} for x in records]
        mode = "auto" if self.auto else "confirm"
        # Existing UI performs sample-N confirmation; host persists each decision.
        self.task_id = self.client.execute(self.plan.plan_id, [x["item_id"] for x in records],
                                           mode, self.s.get("entry", "desktop"))
        self.ui.log("任务 " + self.task_id + " 已交给本机宿主；可在任务档案中重新连接。")
        return self.watch([x["item_id"] for x in records])

    def control(self, **changes):
        if self.task_id:
            return self.client.store.control(self.task_id, **changes)
        return {}

    def watch(self, selected=None):
        store = self.client.store
        waiter = threading.Event()
        answered = set()
        restart_at = time.monotonic() + 5
        while True:
            task = store.task(self.task_id)
            if task["status"] == "queued" and time.monotonic() >= restart_at:
                self.client.start_host()
                restart_at = time.monotonic() + 5
            if task["status"] in TERMINAL:
                break
            try:
                self.ui.checkpoint()
            except Stopped:
                self.control(cancelled=1)
            detail = task.get("detail") or {}
            token = detail.get("item_id")
            if task["status"] == "awaiting_confirmation" and token not in answered:
                value = self.ui.confirm(detail.get("name", ""),
                                        f"策略 {detail.get('strategy_id')} / 对象 {detail.get('object_id')}："
                                        f"{detail.get('before')} → {detail.get('after')}")
                store.answer(self.task_id, token, value)
                answered.add(token)
            items = store.results(self.task_id)
            stats = {"ok": sum(x["status"] == "verified_success" for x in items),
                     "failed": sum(x["status"] in ("blocked", "unknown") for x in items),
                     "skipped": sum(x["status"] == "skipped" for x in items), "dry": 0}
            self.ui.progress(len(items), len(task["approval"]["selected"]), stats)
            waiter.wait(.3)
        plan = store.plan(task["plan_id"])
        by_id = {x["item_id"]: x for x in store.results(self.task_id)}
        selected = selected or task["approval"]["selected"]
        output = []
        for item in plan.items:
            if item.item_id not in selected:
                continue
            result = by_id.get(item.item_id, {"status": "not_started", "detail": {}})
            status = {"verified_success": "ok", "skipped": "skipped"}.get(result["status"], "failed")
            output.append({"名称": item.name, "状态": status, "业务状态": result["status"],
                           "错误": result["detail"].get("reason", "") if status != "failed" else
                           result["detail"].get("reason", "尚未核验成功，请查询任务档案"),
                           "item_id": item.item_id, "task_id": self.task_id,
                           "策略ID": item.strategy_id, "对象ID": item.object_id})
        self.ui.finished("任务已结束：" + task["status"], "任务档案：" + self.task_id,
                         task["status"] in ("completed", "reconciled"))
        return output
