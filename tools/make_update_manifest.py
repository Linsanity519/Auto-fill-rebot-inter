"""生成 latest.json（更新描述文件）。

描述两个包，客户端自己挑（见 src/update.py 的 choose）：
  · payload   代码包，~300KB，日常发版走它
  · installer 完整安装包，~45MB，只有运行时变了才需要

每个包都可以给**多个下载地址**，客户端按顺序试到通为止。把国内的镜像放前面、
GitHub 放最后 —— GitHub Release 在国内实测只有 20~40KB/s。

示例：
  python tools/make_update_manifest.py --version 1.0.9 ^
    --payload dist/ConfigAssistant-1.0.9.zip ^
    --installer dist/ConfigAssistant-Setup-1.0.9.exe ^
    --base-url https://gitee.com/xxx/yyy/releases/download/v1.0.9 ^
    --base-url https://github.com/xxx/yyy/releases/download/v1.0.9
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import sys

# ⚠ 这个脚本会往 stdout 打中文。英文 Windows / CI 上，输出被重定向时 Python 取的是
#   ANSI 代码页（cp1252）而不是 chcp 设的 65001，直接 UnicodeEncodeError 崩掉，
#   而且崩在打印那一行 —— 看起来像是功能出错，其实只是编码。实测在 GitHub
#   Actions 上炸过一次。main.py 开头也做了同样的事。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass



def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def spec(path: Path, bases: list[str]) -> dict:
    return {
        "urls": [f"{b.rstrip('/')}/{quote(path.name)}" for b in bases],
        "sha256": sha256(path),
        "size": path.stat().st_size,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="生成配置助手更新描述文件")
    ap.add_argument("--version", required=True)
    ap.add_argument("--payload", default="", help="代码包 zip")
    ap.add_argument("--installer", default="", help="完整安装包 exe")
    ap.add_argument("--runtime", type=int, default=0,
                    help="代码包要求的最低运行时代号，取自仓库根的 RUNTIME_ID")
    ap.add_argument("--base-url", action="append", required=True, dest="bases",
                    help="发布目录 URL，可给多次；靠前的优先")
    ap.add_argument("--notes", default="")
    # ⚠ notes 现在是多行的更新日志（tools/changelog.py 从 CHANGELOG.md 抠出来的）。
    #   多行文本走命令行参数，在 PowerShell / cmd 里的引号和换行处理各不相同，
    #   迟早会被截断或者拼歪 —— 走文件最稳，CI 就是这么传的。
    ap.add_argument("--notes-file", default="", dest="notes_file",
                    help="从文件读 notes（UTF-8）。给了就以它为准，覆盖 --notes")
    ap.add_argument("--output", default="dist/latest.json")
    args = ap.parse_args()

    if not args.payload and not args.installer:
        raise SystemExit("--payload 和 --installer 至少要给一个")

    notes = args.notes
    if args.notes_file:
        f = Path(args.notes_file)
        if not f.is_file():
            raise SystemExit(f"--notes-file 指的文件不存在：{f}")
        # ⚠ utf-8-sig 不是随手写的：CI 那边是 PowerShell 的 Out-File 生成这个文件，
        #   带不带 BOM 跟 PowerShell 版本有关（5.1 带、7 不带）。按 utf-8 读的话，
        #   BOM 会变成正文开头一个看不见的 ﻿，跟着进 latest.json、再进弹窗，
        #   .strip() 还清不掉它。utf-8-sig 两种情况都对。
        notes = f.read_text(encoding="utf-8-sig").strip()
        if not notes:
            raise SystemExit(f"--notes-file 是空的：{f}")

    doc: dict = {
        "version": args.version,
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "notes": notes,
        "mandatory": False,
    }

    if args.payload:
        p = Path(args.payload).resolve()
        if not p.is_file():
            raise SystemExit(f"代码包不存在：{p}")
        doc["payload"] = {**spec(p, args.bases), "min_runtime": args.runtime}

    if args.installer:
        p = Path(args.installer).resolve()
        if not p.is_file():
            raise SystemExit(f"安装包不存在：{p}")
        installer = spec(p, args.bases)
        doc["installer"] = installer
        # v1 兼容：1.0.8 的客户端只认平铺的这三个字段，指向完整安装包。
        # 等大家都升上来之后可以删掉。
        doc["download_url"] = installer["urls"][0]
        doc["sha256"] = installer["sha256"]
        doc["size"] = installer["size"]

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  {out.resolve()}")
    for kind in ("payload", "installer"):
        if kind in doc:
            print(f"  {kind:9} {doc[kind]['size'] / 1024:>9.0f} KB  {len(doc[kind]['urls'])} 个地址")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
