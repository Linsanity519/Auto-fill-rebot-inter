"""Separate process acceptance using a fake platform; no browser/network/credentials.

Checks client disconnect, competing hosts, OS lock release, crash recovery and
read-only reconciliation. Run separately from the no-subprocess offline suite.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.adapters.host import TaskHost
from src.adapters.task_store import TaskStore, now
from src.application.pricing import PricingService


class FixturePlatform:
    def __init__(self, root: Path):
        self.root = root
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def context(self):
        return {"session_id": "fixture", "environment": "offline", "identity_label": "fixture"}
    def discover(self, inputs):
        return [{"strategy_id": "1", "key": "2", "name": "fixture", "link": "开启", "group": "A"}]
    def read(self, item):
        return (self.root / "platform.txt").read_text() if (self.root / "platform.txt").exists() else "off"
    def set_state(self, item):
        with (self.root / "writes.txt").open("a") as output:
            output.write("write\n")
        (self.root / "platform.txt").write_text(item.after)
        if (self.root / "crash").exists():
            os._exit(37)  # Deliberate process death after actual fake-platform state changed.


def wait_for(condition, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition(): return
        time.sleep(.05)
    raise AssertionError("Timed out waiting for test condition")


def worker(root: Path):
    TaskHost(TaskStore(root), lambda: FixturePlatform(root),
             {"cdp_url": "http://127.0.0.1:19999", "task_lock_dir": str(root / "locks")}
             ).serve(idle_seconds=.5)


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--worker":
        worker(Path(sys.argv[2]))
        return 0
    with tempfile.TemporaryDirectory(prefix="task-host-") as temp:
        root = Path(temp)
        store = TaskStore(root)
        service = PricingService(store, FixturePlatform(root), now)
        plan = service.prepare({"toggle_direction": "on"})
        task = store.create_task(plan.plan_id, [plan.items[0].item_id], "auto", "process-test")
        children = []
        def spawn():
            process = subprocess.Popen([sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                                        "--worker", str(root)], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            children.append(process)
            return process
        try:
            # No live client keeps the task running. Both workers race for the same queue.
            spawn(); spawn()
            wait_for(lambda: store.task(task)["status"] == "completed")
            for process in children:
                stdout, stderr = process.communicate(timeout=10)
                assert process.returncode == 0, stderr.decode("utf-8", "replace")
            assert (root / "writes.txt").read_text().splitlines() == ["write"]
            (root / "platform.txt").write_text("off")
            (root / "crash").write_text("crash after write")
            plan2 = service.prepare({"toggle_direction": "on"})
            task2 = store.create_task(plan2.plan_id, [plan2.items[0].item_id], "auto", "process-test")
            process = spawn()
            process.communicate(timeout=10)
            assert process.returncode == 37
            assert store.results(task2)[0]["status"] == "submitted"
            (root / "crash").unlink()
            command = store.command("reconcile", {"task_id": task2})
            process = spawn()
            process.communicate(timeout=10)
            assert process.returncode == 0
            assert store.task(task2)["status"] == "reconciled"
            assert store.command_result(command)["status"] == "completed"
            assert (root / "writes.txt").read_text().splitlines() == ["write", "write"]
            print("PASS process ownership, client disconnect, competing hosts, crash recovery, read-only reconciliation")
        finally:
            for process in children:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
