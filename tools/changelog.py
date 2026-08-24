"""从 CHANGELOG.md 里抠出某个版本那一节。

发版时用两次（见 .github/workflows/release.yml）：
  · --plain  给 latest.json 的 notes 用 —— 程序里那个更新弹窗直接显示这段文本，
             所以要把 markdown 记号去掉，只留人话。
  · 默认     给 GitHub Release 的正文用 —— 那边认 markdown，原样给。

⚠ 抠不到就以非 0 退出，让发版当场失败。notes 缺失是**静默**的：发出去了才发现
  同事在更新提示里只看到一个版本号，而那时候包已经在 Release 上了。
  宁可卡住发版，也不要发一版没人知道改了什么的东西。

用法：
    python tools/changelog.py 1.0.13            # markdown 原文
    python tools/changelog.py 1.0.13 --plain    # 去掉记号的纯文本
    python tools/changelog.py --latest --plain  # 文件里最靠上的那一节
"""
import argparse
import io
import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

# 一节的开头：## 1.0.13
HEAD = re.compile(r"^##\s+(\d+\.\d+\.\d+)\s*$", re.M)


def sections(text: str) -> list[tuple[str, str]]:
    """[(版本号, 正文), …]，按文件里的先后顺序。"""
    out = []
    marks = list(HEAD.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((m.group(1), text[m.end():end].strip()))
    return out


def to_plain(body: str) -> str:
    """去掉 markdown 记号，留给程序里那个更新弹窗。

    ⚠ 弹窗的 CSS 是 white-space: pre-line，换行有效但不认 markdown；
      侧栏那行小字则只取第一行（见 assets/webui/app.js）。所以这里保留换行、
      把「- 」换成「· 」，其余记号一律抹掉。
    """
    lines = []
    for raw in body.splitlines():
        s = raw.rstrip()
        if not s.strip():
            # 段落之间留一个空行，但不要连着好几个
            if lines and lines[-1] != "":
                lines.append("")
            continue
        s = re.sub(r"^\s*[-*]\s+", "· ", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)     # 粗体
        s = re.sub(r"`(.+?)`", r"\1", s)           # 行内代码
        lines.append(s)
    return "\n".join(lines).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("version", nargs="?", help="要抠的版本号，例如 1.0.13")
    ap.add_argument("--latest", action="store_true", help="取文件里最靠上的那一节")
    ap.add_argument("--plain", action="store_true", help="去掉 markdown 记号")
    args = ap.parse_args()

    if not CHANGELOG.exists():
        print(f"找不到 {CHANGELOG}", file=sys.stderr)
        return 1

    found = sections(io.open(CHANGELOG, encoding="utf-8").read())
    if not found:
        print("CHANGELOG.md 里一节都没有（小节标题要写成 '## X.Y.Z'）", file=sys.stderr)
        return 1

    if args.latest:
        version, body = found[0]
    else:
        if not args.version:
            print("要么给版本号，要么加 --latest", file=sys.stderr)
            return 2
        hit = [b for v, b in found if v == args.version]
        if not hit:
            print(f"CHANGELOG.md 里没有 {args.version} 这一节 —— "
                  f"发版前先补上，别让同事只看到一个版本号。\n"
                  f"现有的：{', '.join(v for v, _ in found[:8])}", file=sys.stderr)
            return 1
        version, body = args.version, hit[0]

    if not body.strip():
        print(f"{version} 那一节是空的", file=sys.stderr)
        return 1

    sys.stdout.reconfigure(encoding="utf-8")
    print(to_plain(body) if args.plain else body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
