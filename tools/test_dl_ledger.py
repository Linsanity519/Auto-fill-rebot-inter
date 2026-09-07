"""src/dl_ledger.py + 「本工具操作过的」范围的纯逻辑测试（不开浏览器）：

  记一批 / 按层级·方向·活动·日期挑 / 反方向取回 / 只记成功的

    python tools\test_dl_ledger.py
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

logging.disable(logging.CRITICAL)

from src import dl_ledger, paths                     # noqa: E402
from src.dl_runner import DlToggleRunner             # noqa: E402

PASS, FAIL = [], []
TMP = Path(tempfile.mkdtemp(prefix="dlledger_"))
LEDGER = "测试台账"


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("　" + str(detail)) if detail and not cond else ""))


# 台账写在 user_path("output", ...) 下 —— 测试里把它指到临时目录，别碰真的
_real_user_path = paths.user_path
paths.user_path = lambda *parts: TMP.joinpath(*parts)
dl_ledger.user_path = paths.user_path


def mk(direction="on", level="unit", activity="", scope="ledger",
       date_from="", date_to="", picked=None):
    s = {"screenshot_dir": str(TMP), "state_file": str(TMP / "state.json"),
         "result_file": str(TMP / "r.csv"), "cdp_url": "http://127.0.0.1:9222",
         "timeout": 15000, "resume": False, "toggle_scope": scope,
         "toggle_direction": direction, "toggle_level": level,
         "toggle_activity": activity, "toggle_params": "",
         "toggle_date_from": date_from, "toggle_date_to": date_to,
         "toggle_ledger_ids": picked if picked is not None else []}
    cfg = {"name": "常规资源位批量开关", "ledger": LEDGER, "ledger_kind": "delivery"}
    return DlToggleRunner(s, cfg, None)


def seed():
    dl_ledger.append(LEDGER, level="unit", level_label="单元", activity="708",
                     direction="off", verb="暂停",
                     items=[{"id": "41031", "name": "测试0831v2"},
                            {"id": "41030", "name": "测试v4"}])
    dl_ledger.append(LEDGER, level="unit", level_label="单元", activity="",
                     direction="on", verb="启动",
                     items=[{"id": "41029", "name": "测试v3"}])
    dl_ledger.append(LEDGER, level="creative", level_label="创意", activity="708",
                     direction="off", verb="暂停",
                     items=[{"id": "138073", "name": "测试测试"}])


def test_append_and_filter():
    print("\n[记一批 / 挑批次]")
    seed()
    ok("三批都记下了", len(dl_ledger.load(LEDGER)) == 3, len(dl_ledger.load(LEDGER)))
    ok("新的在前", dl_ledger.load(LEDGER)[0]["level"] == "creative")
    ok("空的一批不记", (dl_ledger.append(LEDGER, level="unit", level_label="单元",
                                       activity="", direction="on", verb="启动", items=[])
                       or len(dl_ledger.load(LEDGER)) == 3))
    ok("按层级挑", len(dl_ledger.batches_for(LEDGER, level="creative")) == 1)
    ok("按方向挑", len(dl_ledger.batches_for(LEDGER, direction="off")) == 2)
    ok("按活动挑", len(dl_ledger.batches_for(LEDGER, activity="708")) == 2)
    ok("层级+方向一起挑",
       len(dl_ledger.batches_for(LEDGER, level="unit", direction="off")) == 1)
    ids = [i["id"] for i in dl_ledger.items_for(LEDGER, level="unit")]
    ok("取行、按ID去重", ids == ["41029", "41031", "41030"], ids)


def test_reverse_pick():
    print("\n[「本工具操作过的」= 反方向那批]")
    r = mk(direction="on", level="unit")
    ids = [i["id"] for i in r._ledger_items()]
    ok("方向=启动 → 取上次被暂停的", ids == ["41031", "41030"], ids)
    r = mk(direction="off", level="unit")
    ok("方向=暂停 → 取上次被启动的",
       [i["id"] for i in r._ledger_items()] == ["41029"], r._ledger_items())
    r = mk(direction="on", level="creative")
    ok("层级=创意 不会串到单元的台账",
       [i["id"] for i in r._ledger_items()] == ["138073"], r._ledger_items())
    r = mk(direction="on", level="unit", activity="999")
    ok("填了活动ID → 只要那个活动的", r._ledger_items() == [], r._ledger_items())
    r = mk(direction="on", level="unit", scope="keyword")
    ok("范围不是 ledger → 不读台账", r._ledger_items() == [])
    r = mk(direction="on", level="unit", date_from="2099-01-01")
    ok("日期区间过滤得掉", r._ledger_items() == [], r._ledger_items())


def test_pick_and_ids():
    print("\n[怎么用到选行上]")
    r = mk(direction="on", level="unit")
    ok("活动ID 留空 → 走逐个ID查", r._id_list() == ["41031", "41030"], r._id_list())
    r = mk(direction="on", level="unit", activity="708")
    ok("填了活动ID → 不逐个查（整批筛完再挑）", r._id_list() == [])
    rows = [{"id": "41031", "name": "a", "state": "已暂停"},
            {"id": "41030", "name": "b", "state": "已暂停"},
            {"id": "40000", "name": "c", "state": "投放中"}]
    got, why = r._pick(rows)
    ok("按台账的ID挑出来", [x["id"] for x in got] == ["41031", "41030"], got)
    r2 = mk(direction="on", level="unit", activity="不存在")
    got, why = r2._pick(rows)
    ok("台账里没有 → 空 + 说清楚", got == [] and "台账里没有" in why, why)


def test_batch_picking():
    print("\n[界面勾了哪几批 → 只翻那几批]")
    # seed() 记的第二批（unit / on / activity=""）就一条 41029，
    # 拿它的批次 id 出来验证「勾一批」和「不勾（=全都要）」的差别
    all_on_unit = dl_ledger.batches_for(LEDGER, level="unit", direction="on")
    ok("先确认台账里有这一批能测", len(all_on_unit) == 1, all_on_unit)
    only_id = dl_ledger.bid(all_on_unit[0])

    r = mk(direction="off", level="unit")           # 方向=关闭 → 反方向是 on
    ok("没勾任何一批 → 符合条件的全都要",
       [i["id"] for i in r._ledger_items()] == ["41029"])

    r = mk(direction="off", level="unit", picked=[only_id])
    ok("勾了这一批的 id → 就是它", [i["id"] for i in r._ledger_items()] == ["41029"])

    r = mk(direction="off", level="unit", picked=["压根不存在的id"])
    ok("勾的 id 在台账里找不到 → 空，不会偷偷退回全量", r._ledger_items() == [])

    # _ledger_picked：字符串（命令行/手写配置可能传逗号串）也要认得出来
    r = mk(direction="off", level="unit")
    r.s["toggle_ledger_ids"] = f"{only_id}, 另一个id"
    ok("传字符串（逗号分隔）也解析得出来",
       r._ledger_picked() == [only_id, "另一个id"], r._ledger_picked())


def test_only_ok_rows():
    print("\n[只记真的翻转成功的]")
    r = mk(direction="off", level="unit", activity="708")
    records = [{"key": "1", "id": "1", "name": "成功的"},
               {"key": "2", "id": "2", "name": "跳过的"},
               {"key": "3", "id": "3", "name": "失败的"}]
    results = {"1": {"状态": "ok"}, "2": {"状态": "skipped"}, "3": {"状态": "failed"}}
    before = len(dl_ledger.load(LEDGER))
    r._write_ledger(records, results)
    latest = dl_ledger.load(LEDGER)[0]
    ok("多了一批", len(dl_ledger.load(LEDGER)) == before + 1)
    ok("只有 ok 的那条进了台账",
       [i["id"] for i in latest["items"]] == ["1"], latest["items"])
    ok("方向 / 层级 / 活动都记下了",
       (latest["direction"], latest["level"], latest["activity"]) == ("off", "unit", "708"),
       latest)
    r._write_ledger(records, {"1": {"状态": "skipped"}})
    ok("一条都没成功就不记", len(dl_ledger.load(LEDGER)) == before + 1)


def main():
    print("=" * 56)
    print("dl_ledger 纯逻辑测试")
    print("=" * 56)
    try:
        for fn in (test_append_and_filter, test_reverse_pick, test_pick_and_ids,
                   test_batch_picking, test_only_ok_rows):
            fn()
    finally:
        paths.user_path = _real_user_path
        shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗ " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
