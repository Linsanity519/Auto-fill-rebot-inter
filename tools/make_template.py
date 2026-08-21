"""命令行生成 Excel 模板。逻辑在 src/template.py，这里只是个壳。

用法：python tools/make_template.py 价格配置
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.template import build  # noqa: E402


def main():
    forms = sorted(p.stem for p in (ROOT / "config" / "forms").glob("*.yaml"))
    if not forms:
        raise SystemExit("config/forms/ 下没有任何表单配置")

    name = sys.argv[1] if len(sys.argv) > 1 else None
    if not name:
        name = forms[0] if len(forms) == 1 else input(f"表单名 {forms}：").strip()

    print("已生成：" + build(name))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
