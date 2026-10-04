"""On-demand local worker process, using a durable SQLite mailbox (no listening port)."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from ..application.pricing import PricingService
from ..domain.pricing import PriceItem
from .task_store import TaskStore, now
from .locking import FileLock, ResourceBusy
from .data_paths import browser_lock

TERMINAL = {"completed", "blocked", "unknown", "cancelled", "interrupted", "reconciled", "failed"}


class StoreControl:
    def __init__(self, store: TaskStore):
        self.store = store
        self.count = 0
        self.auto = False
        self.waiter = threading.Event()

    def decide(self, task_id: str, item: PriceItem) -> str:
        task = self.store.task(task_id)
        mode = task["approval"]["mode"]
        self.count += 1
        needs_confirmation = not (self.auto or mode == "auto" or (mode == "sample" and self.count > 3))
        if needs_confirmation:
            self.store.control(task_id, decision="")
            self.store.finish(task_id, "awaiting_confirmation",
                              {"item_id": item.item_id, "name": item.name,
                               "before": item.before, "after": item.after,
                               "strategy_id": item.strategy_id, "object_id": item.object_id})
        # Wait on a persisted condition; the frontend may disconnect and reconnect.
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            value = self.store.control(task_id)
            if value["cancelled"]:
                return "stop"
            if not value["paused"]:
                decision = value["decision"] if needs_confirmation else "submit"
                if decision:
                    self.store.control(task_id, decision="")
                    self.store.finish(task_id, "running", {})
                    if decision == "auto":
                        self.auto = True
                        return "submit"
                    return decision
            self.waiter.wait(.2)
        return "stop"


class TaskHost:
    def __init__(self, store: TaskStore, platform_factory, settings: dict):
        self.store, self.platform_factory, self.settings = store, platform_factory, settings

    def dispatch(self, command: dict) -> dict:
        operation, body = command["operation"], command["body"]
        with browser_lock(self.settings):
            with self.platform_factory() as platform:
                service = PricingService(self.store, platform, now)
                if operation == "prepare":
                    return {"plan": service.prepare(body["inputs"]).to_dict()}
                if operation == "reverse":
                    return {"plan": service.reverse(body["task_id"]).to_dict()}
                if operation == "reconcile":
                    return {"task": service.reconcile(body["task_id"])}
                if operation == "execute":
                    self.store.finish(body["task_id"], "running", {})
                    service.execute(body["task_id"], StoreControl(self.store))
                    return {"task": self.store.task(body["task_id"])}
        raise ValueError("未知命令")

    def serve(self, idle_seconds: float = 15) -> None:
        try:
            with FileLock(self.store.root / "host.lock"):
                self.store.recover_host()
                deadline = time.monotonic() + idle_seconds
                waiter = threading.Event()
                while time.monotonic() < deadline:
                    command = self.store.claim_command()
                    if command is None:
                        waiter.wait(.2)
                        continue
                    try:
                        result = self.dispatch(command)
                        self.store.complete_command(command["id"], result, True)
                    except Exception as exc:
                        task_id = command["body"].get("task_id")
                        if command["operation"] == "execute" and task_id:
                            rows = self.store.results(task_id)
                            status = "unknown" if any(x["status"] in ("submitted", "unknown") for x in rows) else "failed"
                            self.store.finish(task_id, status, {"message": "执行中断，请查看事件并重新预检或核验"})
                        self.store.complete_command(command["id"], {"error": str(exc)}, False)
                    deadline = time.monotonic() + idle_seconds
        except ResourceBusy:
            return  # Another host already owns the queue.


class TaskClient:
    def __init__(self, store: TaskStore, settings: dict, start_host=None):
        self.store, self.settings = store, settings
        self.start_host = start_host or self._spawn

    def _spawn(self) -> None:
        from .locking import atomic_json
        # Allowlist only runtime locations/settings. No cookies/webhooks in host config.
        keys = ("cdp_url", "timeout", "state_file", "result_file", "screenshot_dir",
                "task_data_dir", "platform_page_url", "task_lock_dir")
        config = {k: self.settings[k] for k in keys if k in self.settings}
        config["task_data_dir"] = str(self.store.root)
        path = self.store.root / "host-settings.json"
        import json
        with FileLock(self.store.root / "profile.lock", timeout=3):
            if path.exists():
                old = json.loads(path.read_text(encoding="utf-8"))
                if old != config:
                    raise ValueError("此任务目录已绑定另一套执行设置；请保持原设置或选择新的 task_data_dir")
            else:
                atomic_json(path, config)
        root = Path(__file__).resolve().parents[2]
        executable = Path(sys.executable)
        if not getattr(sys, "frozen", False) and os.name == "nt" and executable.with_name("pythonw.exe").exists():
            executable = executable.with_name("pythonw.exe")
        args = [str(executable)]
        if not getattr(sys, "frozen", False):
            args.append(str(root / "main.py"))
        args.extend(["--task-host", str(path)])
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
        with (self.store.root / "host.log").open("ab") as log:
            subprocess.Popen(args, cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             close_fds=True, creationflags=flags, start_new_session=os.name != "nt")

    def request(self, operation: str, body: dict, timeout: float = 180) -> dict:
        self.start_host()
        command_id = self.store.command(operation, body)
        self.start_host()  # Close the idle-exit race after the durable enqueue.
        deadline = time.monotonic() + timeout
        restart_at = time.monotonic() + 5
        waiter = threading.Event()
        while time.monotonic() < deadline:
            value = self.store.command_result(command_id)
            if value["status"] == "queued" and time.monotonic() >= restart_at:
                self.start_host()
                restart_at = time.monotonic() + 5
            if value["status"] in ("completed", "failed"):
                if value["status"] == "failed":
                    raise RuntimeError(value.get("error", "任务宿主请求失败"))
                return value
            waiter.wait(.2)
        raise TimeoutError(f"等待宿主超时；命令 {command_id} 保留在任务库中，不要重复提交")

    def execute(self, plan_id: str, selected: list[str], mode: str, entry: str) -> str:
        self.start_host()
        task_id = self.store.create_task(plan_id, selected, mode, entry)
        self.start_host()
        return task_id
