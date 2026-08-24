"""从历史上的 team.json 快照，把原始上报行还原进归档。

⚠ 一次性的抢救工具，正常发版流程用不到它。
  写它的起因：team.json 只存聚合、不存原始行，而某次收集只复制到了最近一段
  聊天记录，就把早几周的数据整个冲掉了（累计 38 条 → 1 条），
  而企微群里那几条原始上报也已经不在了。

能还原的前提是 **people == 1**：只有一个人的时候，「按周的聚合值」就等于
「那个人那一周的原始行」，没有任何歧义，不需要猜。people > 1 时聚合无法拆回
个人，这个脚本会拒绝处理。

用法（快照来自 git 历史里的某个提交）：
    git show <commit>:config/team.json > snap.json
    python tools/recover_from_snapshot.py snap.json
    python tools/collect_usage.py --rebuild      # 重算 team.json 并推送
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import collect_usage as cu  # noqa: E402


def rows_from_snapshot(snap: dict) -> list[dict]:
    people = snap.get("people")
    if people != 1:
        raise SystemExit(
            f"这份快照有 {people} 个人。聚合值没法拆回每个人的原始行，"
            "不能还原（硬拆就是编数据）。")

    actives = snap.get("actives") or []
    if len(actives) != 1:
        raise SystemExit(f"actives 里有 {len(actives)} 条，和 people=1 对不上，不敢还原。")
    who = actives[0]

    weeks = snap.get("weeks") or {}
    if len(weeks) != 1:
        raise SystemExit(
            f"这份快照跨了 {len(weeks)} 周，而 totals 里的次数/失败数是所有周的合计，"
            "没法按周拆开。只还原单周的快照。")
    week, wk = next(iter(weeks.items()))

    totals = snap.get("totals") or {}
    forms = {f["name"]: f.get("ok", 0) for f in (snap.get("forms") or [])
             if f.get("ok")}

    return [{
        "周": week,
        "指纹": who.get("uid", ""),
        "花名": who.get("name", ""),
        "版本": "",                       # 快照里没有，留空（不参与任何汇总）
        "次数": totals.get("runs", 0),
        "成功": wk.get("items", 0),
        "失败": totals.get("failed", 0),
        "机器秒": wk.get("seconds", 0),
        "最后活跃": who.get("last", ""),
        "分类型": forms,
    }]


def main() -> int:
    ap = argparse.ArgumentParser(description="从 team.json 快照还原原始上报行")
    ap.add_argument("snapshot", help="team.json 快照文件")
    ap.add_argument("--dry", action="store_true", help="只打印要还原的行，不写归档")
    args = ap.parse_args()

    snap = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    rows = rows_from_snapshot(snap)

    print(f"从快照（synced_at={snap.get('synced_at')}）还原出 {len(rows)} 行：")
    for r in rows:
        print("  " + json.dumps(r, ensure_ascii=False))

    if args.dry:
        print("\n--dry：没有写归档")
        return 0

    merged, added, updated = cu.merge_archive(ROOT, rows)
    print(f"\n已并入归档：新增 {added}、更新 {updated}，归档现共 {len(merged)} 行")
    print(f"归档位置：{cu.archive_path(ROOT)}")
    print("\n接着跑：python tools/collect_usage.py --rebuild")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
