"""打「代码包」：src/ + assets/ + main.py 的 zip。

这是日常发版真正要发出去的东西 —— 300KB 上下，而不是 45MB 的完整安装包。
同事那边由 tools/updater.py --payload 解开覆盖。

⚠ config/ 下只带 forms 和 team.json 这两样「发布内容」。
  settings.yaml、strategies、prep 是用户自己的配置（策略中心配了一下午的东西），
  代码包绝不能碰 —— 它们只由完整安装包按 onlyifdoesntexist 铺一次默认值。
  webhook.txt 也不带：它由安装包负责刷新，少放一处就少一处泄漏面。
⚠ 也不要打 __pycache__：里面是上一版的字节码，跟着发出去只会添乱。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path
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


ROOT = Path(__file__).resolve().parent.parent
# ⚠ config/forms 和 config/team.json 是**随版本发布的内容**，不是用户数据，
#   必须跟着代码包走：几乎每次发版都会动表单定义，漏了就会出现
#   「新代码 + 旧表单定义」的错配，而且跨的版本越多越严重。
#   安装包那边对它们用的也是 ignoreversion，口径一致。
MEMBERS = ("main.py", "src", "assets", "config/forms", "config/team.json")
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
