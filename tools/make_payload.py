"""打「代码包」：src/ + assets/ + main.py 的 zip。

这是日常发版真正要发出去的东西 —— 300KB 上下，而不是 45MB 的完整安装包。
同事那边由 tools/updater.py --payload 解开覆盖。

⚠ 不要把 config/ 打进来：那是用户自己的配置（策略中心、准备参数、settings.yaml），
  代码包覆盖它等于把人家配了一下午的东西清掉。config 只由完整安装包按
  onlyifdoesntexist 铺一次默认值。
⚠ 也不要打 __pycache__：里面是上一版的字节码，跟着发出去只会添乱。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEMBERS = ("main.py", "src", "assets")
SKIP_DIRS = {"__pycache__", ".git"}
SKIP_SUFFIX = {".pyc", ".pyo"}


def _files() -> list[Path]:
    out = []
    for member in MEMBERS:
        p = ROOT / member
        if not p.exists():
            raise SystemExit(f"缺少 {p}")
        if p.is_file():
            out.append(p)
            continue
        for f in sorted(p.rglob("*")):
            if not f.is_file():
                continue
            if SKIP_DIRS & set(f.relative_to(ROOT).parts):
                continue
            if f.suffix.lower() in SKIP_SUFFIX:
                continue
            out.append(f)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="打配置助手的代码包")
    ap.add_argument("--version", required=True)
    ap.add_argument("--runtime", type=int, required=True,
                    help="这份代码要求的最低运行时代号，取自仓库根的 RUNTIME_ID")
    ap.add_argument("--output", default="")
    args = ap.parse_args()

    out = Path(args.output) if args.output else ROOT / "dist" / f"ConfigAssistant-{args.version}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)

    files = _files()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in files:
            z.write(f, f.relative_to(ROOT).as_posix())
        z.writestr("payload.json", json.dumps(
            {"version": args.version, "min_runtime": args.runtime},
            ensure_ascii=False, indent=2))

    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"  代码包：{out}")
    print(f"  文件数：{len(files)}　大小：{out.stat().st_size / 1024:.0f} KB")
    print(f"  sha256：{digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
