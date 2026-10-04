"""Shared prepare/execute/reconcile/reverse use cases for GUI and CLI."""
from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4
from ..domain.pricing import PriceItem, PricePlan, check_selection, plan_fingerprint
from .ports import ExecutionControl, PricePlatform, TaskRepository


class PricingService:
    def __init__(self, repository: TaskRepository, platform: PricePlatform,
                 clock: Callable[[], str]):
        self.repo, self.platform, self.clock = repository, platform, clock

    def prepare(self, inputs: dict) -> PricePlan:
        direction = inputs.get("toggle_direction", "on")
        if direction not in ("on", "off"):
            raise ValueError("目标状态必须是 on 或 off")
        context = self.platform.context()
        if not context.get("session_id") or not context.get("environment"):
            raise ValueError("浏览器会话或环境无法识别")
        rows = self.platform.discover(inputs)
        items = tuple(PriceItem.from_row(x, direction) for x in rows)
        if len({x.item_id for x in items}) != len(items):
            raise ValueError("重复的平台对象 ID，无法生成唯一计划")
        plan = PricePlan("plan_" + uuid4().hex, self.clock(), context, inputs, items)
        self.repo.save_plan(plan)
        return plan

    def _check_context(self, plan: PricePlan) -> None:
        current = self.platform.context()
        if any(current.get(k) != plan.context.get(k) for k in ("environment", "session_id")):
            raise ValueError("账号会话或环境已变化，请重新生成计划")

    def execute(self, task_id: str, control: ExecutionControl) -> None:
        task = self.repo.task(task_id)
        plan = self.repo.plan(task["plan_id"])
        approval = task["approval"]
        selected = approval["selected"]
        check_selection(plan, selected)
        if approval.get("fingerprint") != plan_fingerprint(plan):
            raise ValueError("授权与存档计划不一致")
        self._check_context(plan)
        existing = {x["item_id"]: x["status"] for x in self.repo.results(task_id)}
        if existing:
            raise ValueError("任务已开始过；请查询或核验原任务，不能重复执行")
        statuses = []
        for item in plan.items:
            if item.item_id not in selected:
                continue
            decision = control.decide(task_id, item)
            if decision == "stop":
                self.repo.finish(task_id, "cancelled", {"message": "已停止后续操作"})
                return
            if decision == "skip":
                self.repo.record(task_id, item.item_id, "skipped", {"reason": "用户跳过"})
                statuses.append("skipped")
                continue
            # This record must be durable BEFORE any platform write.
            self._check_context(plan)
            before = self.platform.read(item)
            if before == item.after:
                self.repo.record(task_id, item.item_id, "skipped", {"current": before, "reason": "已是目标状态"})
                statuses.append("skipped")
                continue
            if before != item.before:
                self.repo.record(task_id, item.item_id, "blocked", {"current": before, "reason": "前置状态变化，请重新预检"})
                statuses.append("blocked")
                continue
            self.repo.record(task_id, item.item_id, "submitted",
                             {"before": before, "after": item.after, "phase": "intent"})
            try:
                self.platform.set_state(item)
                after = self.platform.read(item)
            except Exception:
                # Exception text may contain credentials; diagnostics live in local logs.
                self.repo.record(task_id, item.item_id, "unknown",
                                 {"reason": "提交或回读失败，请核验原任务；不会自动重提"})
                self.repo.finish(task_id, "unknown", {})
                return
            if after != item.after:
                self.repo.record(task_id, item.item_id, "unknown", {"current": after, "reason": "目标状态尚未证实"})
                self.repo.finish(task_id, "unknown", {})
                return
            self.repo.record(task_id, item.item_id, "verified_success", {"current": after})
            statuses.append("verified_success")
        self.repo.finish(task_id, "blocked" if "blocked" in statuses else "completed", {})

    def reconcile(self, task_id: str) -> dict:
        task = self.repo.task(task_id)
        if task["status"] not in ("unknown", "interrupted"):
            raise ValueError("只有结果未知或宿主中断的任务需要核验")
        plan = self.repo.plan(task["plan_id"])
        self._check_context(plan)
        uncertain = {x["item_id"] for x in self.repo.results(task_id)
                     if x["status"] in ("submitted", "unknown")}
        unresolved = False
        for item in plan.items:
            if item.item_id not in uncertain:
                continue
            current = self.platform.read(item)
            status = "verified_success" if current == item.after else "unknown"
            unresolved |= status == "unknown"
            self.repo.record(task_id, item.item_id, status,
                             {"current": current, "phase": "read_only_reconciliation",
                              "note": "仅证明回读时状态，不能证明由本任务独立导致"})
        self.repo.finish(task_id, "unknown" if unresolved else "reconciled", {})
        return self.repo.task(task_id)

    def reverse(self, task_id: str) -> PricePlan:
        task = self.repo.task(task_id)
        original = self.repo.plan(task["plan_id"])
        self._check_context(original)
        done = {x["item_id"] for x in self.repo.results(task_id)
                if x["status"] == "verified_success"}
        items = []
        for item in original.items:
            if item.item_id not in done:
                continue
            current = self.platform.read(item)
            if current != item.after:
                raise ValueError(f"「{item.name}」当前状态与原任务结果不同，不能自动翻回")
            items.append(PriceItem(item.item_id, item.strategy_id, item.object_id,
                                   item.name, item.after, item.before, item.group))
        if not items:
            raise ValueError("指定任务没有已核验且可翻回的项目")
        plan = PricePlan("plan_" + uuid4().hex, self.clock(), original.context,
                         {"reverse_of": task_id}, tuple(items), source_task=task_id)
        self.repo.save_plan(plan)
        return plan
