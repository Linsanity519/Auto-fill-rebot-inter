"""src/fill_core.py 的场景测试。改那个文件之后跑一遍：

    python tools\test_fill_core.py

不联网、不碰浏览器 —— page 用一个假的替身，只记「被叫去等了几次」。
往 fill_core 里加原语时请在这里补一条场景。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src import fill_core as F   # noqa: E402

# note() 会 log.warning —— 那是预期行为，别让它在 GBK 控制台上刷出乱码
import logging  # noqa: E402
logging.disable(logging.CRITICAL)

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("　" + detail) if detail and not cond else ""))


class FakePage:
    """只实现 wait_for_timeout：记下总共"睡"了多少毫秒，不真睡。"""

    def __init__(self):
        self.slept = 0
        self.calls = 0

    def wait_for_timeout(self, ms):
        self.slept += ms
        self.calls += 1


# ------------------------------------------------------------ wait_until
def test_wait_until():
    print("\n[wait_until]")
    p = FakePage()
    ok("条件当场成立 → 一次都不等", F.wait_until(p, lambda: True, 5000) and p.slept == 0)

    p = FakePage()
    n = {"i": 0}

    def third_time():
        n["i"] += 1
        return n["i"] >= 3

    ok("第三次才成立 → 返回 True", F.wait_until(p, third_time, 5000, step=100))
    ok("第三次才成立 → 只等了两轮", p.slept == 200, f"实际 {p.slept}ms")

    p = FakePage()
    ok("永不成立 → 返回 False", F.wait_until(p, lambda: False, 500, step=100) is False)
    ok("永不成立 → 等到上限就收手", p.slept == 500, f"实际 {p.slept}ms")

    p = FakePage()
    ok("cond 抛异常算「还没成立」，不往外抛",
       F.wait_until(p, lambda: (_ for _ in ()).throw(RuntimeError("元素还没渲染")),
                    200, step=100) is False)

    p = FakePage()
    ok("timeout=0 也要先试一次条件", F.wait_until(p, lambda: True, 0) is True)


# ------------------------------------------------------------ wait_stable
def test_wait_stable():
    print("\n[wait_stable]")
    p = FakePage()
    seq = [["a"], ["a", "b"], ["a", "b"], ["a", "b"], ["a", "b"], ["a", "b"]]
    it = iter(seq)
    got = F.wait_stable(p, lambda: next(it, ["a", "b"]), quiet_ms=300, step=100, timeout=3000)
    ok("变完之后稳住 → 返回稳定值", got == ["a", "b"], f"实际 {got}")

    p = FakePage()
    n = {"i": 0}

    def always_changing():
        n["i"] += 1
        return [n["i"]]

    F.wait_stable(p, always_changing, quiet_ms=300, step=100, timeout=600)
    ok("一直在变 → 到 timeout 就收手，不死循环", p.slept <= 600, f"实际 {p.slept}ms")

    p = FakePage()
    got = F.wait_stable(p, lambda: (_ for _ in ()).throw(RuntimeError()),
                        quiet_ms=200, step=100, timeout=400)
    ok("read 一直抛 → 返回 None 而不是崩", got is None)


# ------------------------------------------------------------ norm / pick
def test_norm():
    print("\n[norm]")
    ok("去首尾空白", F.norm("  年卡  ") == "年卡")
    ok("去换行", F.norm("年卡\n优先") == "年卡优先")
    ok("None → 空串", F.norm(None) == "")
    ok("⚠ 中间的空格要留着（有的 SKU 靠它区分）",
       F.norm(" 连续包月 首月优惠 ") == "连续包月 首月优惠")


def test_pick():
    print("\n[pick]")
    texts = ["年卡", "  季卡  ", "月卡\n"]
    ok("完全相等", F.pick(texts, "年卡") == "年卡")
    ok("候选带空白也能对上，且返回的是页面原文", F.pick(texts, "季卡") == "  季卡  ")
    ok("对不上 → None", F.pick(texts, "周卡") is None)
    ok("默认不模糊：子串不算命中", F.pick(["年卡优先·双面板"], "年卡") is None)
    ok("空值 → None，不要瞎猜一条", F.pick(texts, "") is None)

    pids = ["11439(normal,ipad,连续包年,148.00元)",
            "1143(normal,pc,月度,25.00元)",
            "114(旧)"]
    ok("contains：按「值+左括号」认，1143 不会命中 11439",
       F.pick(pids, "1143", contains=True) == "1143(normal,pc,月度,25.00元)")
    ok("contains：11439 命中自己",
       F.pick(pids, "11439", contains=True) == pids[0])
    ok("contains：短 pid 也不会串到长的那条",
       F.pick(pids, "114", contains=True) == "114(旧)")
    ok("contains：真找不到才走纯子串兜底",
       F.pick(["前缀-年卡-后缀"], "年卡", contains=True) == "前缀-年卡-后缀")


def test_pick_all():
    print("\n[pick_all]")
    texts = ["iPhone", "Android", "PC"]
    hit, missing = F.pick_all(texts, ["iPhone", "PC"])
    ok("全中 → missing 为空", hit == {"iPhone": "iPhone", "PC": "PC"} and missing == [])

    hit, missing = F.pick_all(texts, ["iPhone", "HD", "TV"])
    ok("缺的一次全报出来，不是报第一个就停", missing == ["HD", "TV"], f"实际 {missing}")
    ok("缺了也要把命中的那部分给出来", hit == {"iPhone": "iPhone"})


# ------------------------------------------------------------ value_matches
def test_value_matches():
    print("\n[value_matches]")
    ok("默认完全相等", F.value_matches("在期大会员", "在期大会员"))
    ok("默认下子串不算", not F.value_matches("在期大会员", "在期"))
    ok("contains：以选项开头算", F.value_matches("在期大会员", "在期", "contains"))
    ok("multi：逗号分隔命中其中一项",
       F.value_matches("iPhone,Android", "Android", "multi"))
    ok("multi：中文逗号/顿号也认",
       F.value_matches("iPhone，Android、PC", "PC", "multi"))
    ok("multi：没命中就是没命中",
       not F.value_matches("iPhone,Android", "HD", "multi"))


def test_opt_regex():
    print("\n[opt_regex]")
    r = F.opt_regex("年卡")
    ok("默认锚定首尾：不会把「年卡优先·双面板」也选上", r.search("年卡优先·双面板") is None)
    ok("默认锚定首尾：本体能中", r.search("  年卡  ") is not None)
    ok("特殊字符要转义，不能当正则解释",
       F.opt_regex("面板(新)").search("面板(新)") is not None)
    ok("contains=True 时才放开", F.opt_regex("年卡", True).search("年卡优先") is not None)


# ------------------------------------------------------------ 报错措辞
def test_errors():
    print("\n[报错措辞]")
    e = F.option_error("生效平台", "HD", ["iPhone", "Android"])
    ok("option_error 是 FillError（runner 才接得住）", isinstance(e, F.FillError))
    ok("option_error 说清了字段名", "生效平台" in str(e))
    ok("option_error 说清了想填什么", "HD" in str(e))
    ok("option_error 说清了页面上有什么", "iPhone" in str(e))

    e = F.option_error("价格面板pid", "134", [])
    ok("候选为空时换一种说法（这是「没展开」不是「写错了」）",
       "一条候选都没有" in str(e) and "没真展开" in str(e))

    e = F.option_error("人群包", "x", [f"选项{i}" for i in range(50)], limit=5)
    ok("候选很多时截断，并说明总共几条", "共 50 条" in str(e) and "选项7" not in str(e))

    e = F.missing_error("生效平台", ["HD", "TV"], ["iPhone", "Android"])
    ok("missing_error 把缺的一起列出来", "HD" in str(e) and "TV" in str(e))

    ok("field_error 默认给出最可能的原因",
       "还没渲染" in str(F.field_error("面板个数")))
    ok("field_error 可以换成场景专属的原因",
       "资源位" in str(F.field_error("面板个数", "「其他设置」要选完资源位才出现")))

    e = F.verify_error("套餐排列", ["b", "a"], ["a", "b"])
    ok("verify_error 同时给出实际和期望", "['b', 'a']" in str(e) and "['a', 'b']" in str(e))


def test_note():
    print("\n[note]")
    got = []
    F.note(got.append, "候选是空的，重选一次 panel_type")
    ok("回调收到消息", got == ["候选是空的，重选一次 panel_type"])

    def boom(_):
        raise RuntimeError("界面已经关了")

    try:
        F.note(boom, "x")
        ok("回调抛异常不能打断主流程", True)
    except Exception as e:
        ok("回调抛异常不能打断主流程", False, repr(e))

    try:
        F.note(None, "x")
        ok("没有回调时也能用（命令行模式）", True)
    except Exception as e:
        ok("没有回调时也能用（命令行模式）", False, repr(e))


def test_reexport():
    print("\n[转出]")
    from src.filler import FillError as Origin
    ok("⚠ FillError 必须和 filler.py 是同一个类（runner 全靠 except 它）",
       F.FillError is Origin)
    ok("split_multi 也转出来了", F.split_multi("a,b、c") == ["a", "b", "c"])


def main():
    print("=" * 56)
    print("fill_core 场景测试")
    print("=" * 56)
    for fn in (test_wait_until, test_wait_stable, test_norm, test_pick, test_pick_all,
               test_value_matches, test_opt_regex, test_errors, test_note, test_reexport):
        fn()
    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗ " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
