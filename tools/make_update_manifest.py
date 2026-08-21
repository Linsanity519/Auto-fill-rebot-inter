"""为已打好的 Inno Setup 安装包生成 latest.json。

示例：
  python tools/make_update_manifest.py --version 1.0.7 ^
    --installer dist/ConfigAssistant-Setup-1.0.7.exe ^
    --base-url https://download.example.com/config-assistant
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="生成配置助手更新描述文件")
    parser.add_argument("--version", required=True)
    parser.add_argument("--installer", required=True)
    parser.add_argument("--base-url", required=True, help="不含文件名的发布目录 URL")
    parser.add_argument("--notes", default="")
    parser.add_argument("--output", default="dist/latest.json")
    args = parser.parse_args()

    installer = Path(args.installer).resolve()
    if not installer.is_file():
        raise SystemExit(f"安装包不存在：{installer}")
    base = args.base_url.rstrip("/")
    doc = {
        "version": args.version,
        "download_url": f"{base}/{quote(installer.name)}",
        "sha256": sha256(installer),
        "size": installer.stat().st_size,
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "notes": args.notes,
        "mandatory": False,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
