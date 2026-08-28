"""src/pt_strategy.py 的纯逻辑测试（token 解析，不联网）。

    python tools\\test_pt_strategy.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src import pt_strategy as S      # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("　" + str(detail)) if detail and not cond else ""))


def test_classify():
    print("\n[classify]")
    cases = [
        ("https://rich-vip.bilibili.co/manage/v/experiment-manage/strategy-center/list/edit/186",
         ("route", "186")),
        (".../list/edit/192?x=1", ("route", "192")),
        ("186", ("route", "186")),
        ("2001234", ("route", "2001234")),           # 7 位还算路由ID
        ("07135930239440", ("biz", "07135930239440")),  # 14 位 → 业务ID
        ("26011902989580", ("biz", "26011902989580")),
        ("", ("bad", "")),
        ("子凡测试", ("bad", "子凡测试")),
        ("abc123", ("bad", "abc123")),
    ]
    for tok, want in cases:
        got = S.classify(tok)
        ok(f"{tok[:40] or '(空)'} → {want[0]}", got == want, got)


def test_parse_tokens():
    print("\n[parse_tokens]")
    ok("按行拆", S.parse_tokens("186\n190\n") == ["186", "190"])
    ok("逗号/空格/分号都当分隔",
       S.parse_tokens("186, 190; 192\t193") == ["186", "190", "192", "193"])
    ok("空行忽略", S.parse_tokens("\n\n186\n\n") == ["186"])
    ok("留空 → 空列表", S.parse_tokens("   \n  ") == [])
    ok("None 不炸", S.parse_tokens(None) == [])
    ok("混填 URL + ID",
       S.parse_tokens(".../edit/186\n07135930239440") == [".../edit/186", "07135930239440"])


def main():
    print("=" * 56)
    print("pt_strategy 纯逻辑测试")
    print("=" * 56)
    test_classify()
    test_parse_tokens()
    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗ " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
