"""把 src/__init__.py 里的版本号末位加一，打印新版本号。

build.bat 每次打包会先调它，所以「打一次包 = 版本号 +1」：
1.0.0 → 1.0.1 → 1.0.2 …

⚠ 全项目只有 src/__init__.py 那一个版本号。前端侧栏、首页脚注、每条埋点
  都读它，改别处没用。
⚠ 只想看当前版本、不想加，用 --show。
"""
import io
import re
import sys
from pathlib import Path

INIT = Path(__file__).resolve().parent.parent / "src" / "__init__.py"
PATTERN = re.compile(r'^__version__\s*=\s*"([0-9]+)\.([0-9]+)\.([0-9]+)"', re.M)


def main() -> int:
    text = io.open(INIT, encoding="utf-8", newline="").read()
    m = PATTERN.search(text)
    if not m:
        print("找不到 __version__，请检查 src/__init__.py", file=sys.stderr)
        return 1

    major, minor, patch = (int(g) for g in m.groups())
    if "--show" in sys.argv:
        print(f"{major}.{minor}.{patch}")
        return 0

    new = f"{major}.{minor}.{patch + 1}"
    text = text[:m.start()] + f'__version__ = "{new}"' + text[m.end():]
    io.open(INIT, "w", encoding="utf-8", newline="").write(text)
    print(new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
