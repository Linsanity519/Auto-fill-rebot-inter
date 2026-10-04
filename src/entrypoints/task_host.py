"""Explicit local worker entry; no GUI or arbitrary RPC methods."""
from __future__ import annotations

import json
from pathlib import Path
from ..bootstrap import task_host


def run(config_path: str) -> None:
    settings = json.loads(Path(config_path).read_text(encoding="utf-8"))
    task_host(settings).serve()
