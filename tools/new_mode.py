"""Create an unregistered, layered capability scaffold. No unfinished feature is enabled.

python tools/new_mode.py 优惠券配置 --prefix coupon --dry-run
"""
from __future__ import annotations

import argparse
import keyword
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent


def scaffold(name: str, prefix: str, mode: str | None = None) -> dict[str, str]:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", prefix) or keyword.iskeyword(prefix):
        raise ValueError("prefix 必须是有效的小写 Python 标识符")
    if not name.strip() or any(c in name for c in '/\\:*?"<>|'):
        raise ValueError("功能名称不能含路径字符")
    capability = mode or prefix
    if not re.fullmatch(r"[a-z][a-z0-9_.]*", capability):
        raise ValueError("mode 只能包含小写字母、数字、点和下划线")
    return {
        f"src/domain/{prefix}.py": (
            '"""' + name + '：纯业务契约。补齐字段、单位、范围与业务规则。"""\n'
            'from dataclasses import dataclass\n\n'
            '@dataclass(frozen=True)\nclass Request:\n    object_id: str\n'
            '    # Replace with the reviewed capability fields before registration.\n'),
        f"src/application/{prefix}.py": (
            '"""' + name + '：通过端口读取平台，保存计划后再执行。"""\n'
            'from typing import Protocol\n'
            f'from ..domain.{prefix} import Request\n\n'
            'class Platform(Protocol):\n    def read(self, request: Request) -> dict: ...\n\n'
            'def prepare(request: Request, platform: Platform) -> dict:\n'
            '    raise NotImplementedError("先补齐业务校验、只读预检和计划契约")\n'),
        f"src/adapters/{prefix}.py": (
            '"""' + name + '：实现应用层端口；平台对象不得进入核心。"""\n'),
        f"src/entrypoints/{prefix}.py": (
            '"""' + name + '：输入/输出转换；从 bootstrap 获得用例。"""\n'),
        f"docs/engineering/capabilities/{prefix}.md": (
            f'# {name}\n\n能力 ID：{capability}。状态：未注册、不可执行。\n\n'
            '补齐字段与单位、平台事实来源、只读预检、计划前后差异、授权边界、未知结果恢复、'
            '附件与归集要求、正常与失败路径测试。经验证后在 bootstrap 组装，'
            '通过 registry 接入 GUI/CLI；不要给空实现添加可执行入口。\n'
            '\n运行 python tools/check_project.py，保留原功能基线。\n')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--mode")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    files = scaffold(args.name, args.prefix, args.mode)
    if any((ROOT / name).exists() for name in files):
        raise SystemExit("目标模块已存在，不覆盖已有实现")
    for name, content in files.items():
        print(("计划创建：" if args.dry_run else "创建：") + name)
        if not args.dry_run:
            path = ROOT / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    print("尚未注册到配置类型；请完成业务契约和测试后接入。")


if __name__ == "__main__":
    main()
