"""src/pt_ledger.py 的场景测试（不联网、不开浏览器）。

    python tools\\test_pt_ledger.py

全在临时目录里跑完就删。
"""
import logging
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.disable(logging.CRITICAL)   # 「文件坏了」那几条会 warn，是预期

from src import pt_ledger            # noqa: E402
from src import paths as _paths      # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("　" + str(detail)) if detail and not cond else ""))


class TmpRoot:
    """把 pt_ledger 的落盘目录劫持到临时目录。"""

    def __enter__(self):
        self.d = Path(tempfile.mkdtemp(prefix="ptledger_"))
        self._orig = _paths.user_path
        _paths.user_path = lambda *p: self.d.joinpath(*p)
        pt_ledger.user_path = _paths.user_path
        return self

    def __exit__(self, *a):
        _paths.user_path = self._orig
        pt_ledger.user_path = self._orig
        shutil.rmtree(self.d, ignore_errors=True)


L = "价格策略开关"


def test_append_and_read():
    print("\n[记一批 / 读回来]")
    with TmpRoot():
        ok("没文件时 load 是空", pt_ledger.load(L) == [])
        ok("没文件时 names_for 是空", pt_ledger.names_for(L, "186") == [])

        pt_ledger.append(L, strategy_id="186", strategy_name="子凡测试",
                         strategy_url="u1", run_id="r1", names=["A", "B"])
        pt_ledger.append(L, strategy_id="186", strategy_name="子凡测试",
                         strategy_url="u1", run_id="r2", names=["B", "C"])
        pt_ledger.append(L, strategy_id="190", strategy_name="新客对照",
                         strategy_url="u2", run_id="r3", names=["X"])

        batches = pt_ledger.load(L)
        ok("三批都在", len(batches) == 3)
        ok("load 新的在前（最后记的 190 排第一）", batches[0]["strategy_id"] == "190")
        ok("每批带时间戳", all(b.get("at") for b in batches))

        # 新批次整批排在前，批内按写入顺序；B 在 r2 里先出现所以压过 r1 里的 B
        ok("names_for(186) 去重 + 批次新→旧", pt_ledger.names_for(L, "186") == ["B", "C", "A"],
           pt_ledger.names_for(L, "186"))
        ok("names_for(190) 只有那条策略的", pt_ledger.names_for(L, "190") == ["X"])
        ok("names_for(空策略) 不过滤、汇总全部",
           set(pt_ledger.names_for(L, "")) == {"A", "B", "C", "X"})


def test_empty_names_not_recorded():
    print("\n[空名单不记]")
    with TmpRoot():
        pt_ledger.append(L, strategy_id="1", strategy_name="", strategy_url="",
                         run_id="", names=[])
        pt_ledger.append(L, strategy_id="1", strategy_name="", strategy_url="",
                         run_id="", names=["  ", ""])
        ok("一批都没记（跑了但没成功）", pt_ledger.load(L) == [])


def test_batches_for_and_since():
    print("\n[按策略 / 按日期筛]")
    with TmpRoot():
        # 手写一份带不同日期的台账
        p = _paths.user_path("output", f"{L}台账.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        import json
        p.write_text(json.dumps({"batches": [
            {"at": "2026-08-25 10:00:00", "strategy_id": "186", "strategy_name": "S",
             "strategy_url": "", "run_id": "", "names": ["old1", "old2"]},
            {"at": "2026-08-28 09:00:00", "strategy_id": "186", "strategy_name": "S",
             "strategy_url": "", "run_id": "", "names": ["new1"]},
        ]}, ensure_ascii=False), encoding="utf-8")

        ok("batches_for(186) 两批", len(pt_ledger.batches_for(L, "186")) == 2)
        ok("batches_for(999) 空", pt_ledger.batches_for(L, "999") == [])
        ok("since=2026-08-27 只留新那批",
           pt_ledger.names_for(L, "186", since="2026-08-27") == ["new1"],
           pt_ledger.names_for(L, "186", since="2026-08-27"))
        ok("since=2026-08-01 全都留",
           set(pt_ledger.names_for(L, "186", since="2026-08-01")) == {"old1", "old2", "new1"})


def test_strategies_index():
    print("\n[台账里出现过的策略]")
    with TmpRoot():
        pt_ledger.append(L, strategy_id="186", strategy_name="子凡测试",
                         strategy_url="u1", run_id="", names=["A"])
        pt_ledger.append(L, strategy_id="190", strategy_name="新客对照",
                         strategy_url="u2", run_id="", names=["X"])
        pt_ledger.append(L, strategy_id="186", strategy_name="子凡测试",
                         strategy_url="u1", run_id="", names=["B"])

        ss = pt_ledger.strategies(L)
        ok("两条不同策略", len(ss) == 2)
        ok("最近配过的在前（最后是 186）", ss[0]["id"] == "186", [s["id"] for s in ss])
        ok("186 计到 2 批", next(s for s in ss if s["id"] == "186")["batches"] == 2)
        ok("带上名字和 url", ss[0]["name"] == "子凡测试" and ss[0]["url"] == "u1")


def test_broken_file():
    print("\n[文件坏了]")
    with TmpRoot():
        p = _paths.user_path("output", f"{L}台账.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{这不是 json", encoding="utf-8")
        ok("坏文件 load 当空", pt_ledger.load(L) == [])
        pt_ledger.append(L, strategy_id="1", strategy_name="x", strategy_url="",
                         run_id="", names=["A"])
        ok("坏文件会被重写、还能记进去", pt_ledger.names_for(L, "1") == ["A"])


def main():
    print("=" * 56)
    print("pt_ledger 场景测试")
    print("=" * 56)
    for fn in (test_append_and_read, test_empty_names_not_recorded,
               test_batches_for_and_since, test_strategies_index, test_broken_file):
        fn()
    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗ " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
