"""Task/archive commands; all platform work remains in the shared task host."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from ..bootstrap import archives, task_client, sync_archives, collection


def run(arguments: list[str], settings: dict) -> None:
    parser = argparse.ArgumentParser(prog="main.py --tasks")
    parser.add_argument("action", choices=("list", "show", "plan", "reconcile", "reverse", "cancel",
                                         "export", "import", "import-legacy", "sync", "receive", "receipt", "imports"))
    parser.add_argument("value", nargs="?")
    parser.add_argument("--output")
    args = parser.parse_args(arguments)
    client = task_client(settings)
    if args.action == "list":
        result = client.store.tasks()
    elif args.action == "show":
        task = client.store.task(args.value)
        result = {"task": task, "items": client.store.results(args.value), "events": client.store.events(args.value)}
    elif args.action == "plan":
        result = client.store.plan(args.value).to_dict()
    elif args.action in ("reconcile", "reverse"):
        result = client.request(args.action, {"task_id": args.value})
    elif args.action == "cancel":
        result = client.store.control(args.value, cancelled=1, paused=0)
    elif args.action == "export":
        if not args.output:
            parser.error("export 需要 --output 指定新的档案包路径")
        result = archives(settings).export(args.value, Path(args.output))
    elif args.action == "import":
        result = archives(settings).import_bytes(Path(args.value).read_bytes())
    elif args.action == "import-legacy":
        result = archives(settings).import_legacy(Path(args.value))
    elif args.action == "imports":
        result = archives(settings).imported()
    elif args.action == "sync":
        result = sync_archives(settings)
    elif args.action == "receive":
        result = collection(settings).receive(json.loads(Path(args.value).read_text(encoding="utf-8")))
        if args.output:
            with Path(args.output).open("x", encoding="utf-8") as target:
                json.dump(result, target, ensure_ascii=False, indent=2)
    else:
        result = {"acknowledged_chunks": collection(settings).acknowledge(
            json.loads(Path(args.value).read_text(encoding="utf-8")))}
    print(json.dumps(result, ensure_ascii=False, indent=2))
