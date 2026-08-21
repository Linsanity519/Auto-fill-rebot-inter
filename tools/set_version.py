"""把 src/__init__.py 的正式版本号设为指定值（供 GitHub Actions 发布用）。"""
from __future__ import annotations

import re
import sys
from pathlib import Path


TARGET = Path(__file__).resolve().parent.parent / "src" / "__init__.py"
PATTERN = re.compile(r'^__version__\s*=\s*"[0-9]+\.[0-9]+\.[0-9]+"', re.M)


def main() -> int:
    if len(sys.argv) != 2 or not re.fullmatch(r"\d+\.\d+\.\d+", sys.argv[1]):
        raise SystemExit("用法：python tools/set_version.py X.Y.Z")
    text = TARGET.read_text(encoding="utf-8")
    updated, count = PATTERN.subn(f'__version__ = "{sys.argv[1]}"', text, count=1)
    if count != 1:
        raise SystemExit("找不到唯一的 __version__")
    TARGET.write_text(updated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
