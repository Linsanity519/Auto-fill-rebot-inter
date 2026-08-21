"""收集端：把统计群里的上报消息，整理成随包分发的 config/team.json。

    在统计群里全选复制 → 双击 tools\\收集统计.bat（或 python tools\\collect_usage.py）

⚠ 只在你自己的机器上跑，不进分发包。

怎么工作：
  1. 读系统剪贴板（ctypes 调 Windows API，不装依赖）
  2. 从里面抠出所有单行 JSON（企微聊天记录复制出来会夹着时间、人名、别的闲聊，
     所以是「大海捞针」式地找，不是按行严格解析）
  3. 同一个人同一周只留最后一条 —— 上报发的是**本机累计**不是增量，所以后来的那条
     天然覆盖前面的，重复发、乱序发都不影响结果
  4. 汇总成 config/team.json（首页读它）
  5. 顺手写一份进企微智能表格当看板 —— **这一步是可选的**，失败了不影响 team.json

⚠ 第 5 步现在还不通：文档机器人的 apikey 调任何 tools/call 都返回 850001
  （tools/list 正常），是机器人的文档权限没开。修好之前 --sheet 会自动跳过。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src import usage  # noqa: E402

# 上报消息长这样（src/report.py 拼的）：
#   {"周": "2026-08-17", "指纹": "16d69684", ..., "分类型": {"DMP延期": 38}}
# ⚠ 用「找 { 再配对括号」而不是正则一把梭：分类型是嵌套对象，正则配不平。
NEEDLE = '"指纹"'


def read_clipboard() -> str:
    """读剪贴板。用 ctypes 直接调 Win32，不引第三方库。"""
    if sys.platform != "win32":
        return ""
    import ctypes
    from ctypes import wintypes

    CF_UNICODETEXT = 13
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    u32.GetClipboardData.restype = wintypes.HANDLE
    k32.GlobalLock.restype = ctypes.c_void_p
    if not u32.OpenClipboard(None):
        return ""
    try:
        h = u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = k32.GlobalLock(h)
        try:
            return ctypes.c_wchar_p(p).value or ""
        finally:
            k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()


def extract(text: str) -> list[dict]:
    """从一坨聊天记录里把上报 JSON 都捞出来。看不懂的片段安静跳过。"""
    out, i = [], 0
    while True:
        k = text.find(NEEDLE, i)
        if k < 0:
            return out
        start = text.rfind("{", 0, k)
        if start < 0:
            i = k + 1
            continue
        depth, end = 0, -1
        for j in range(start, min(len(text), start + 4000)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        if end < 0:
            i = k + 1
            continue
        try:
            d = json.loads(text[start:end])
            if isinstance(d, dict) and d.get("指纹") and d.get("周"):
                out.append(d)
        except json.JSONDecodeError:
            pass
        i = end


def to_rows(msgs: list[dict], form_names: list[str]) -> list[list]:
    """每人每周留最后一条 → parse_report 认识的行。

    ⚠ 这套还原逻辑在 tools/test_usage.py 的 _from_line 里有一份测试替身，
      改这里记得改那边。
    """
    latest: dict[tuple, dict] = {}
    for d in msgs:
        latest[(str(d.get("指纹")), usage.norm_week(d.get("周")))] = d
    rows = []
    for (uid, wk), d in sorted(latest.items()):
        forms = d.get("分类型") or {}
        rows.append([wk, uid, d.get("花名", ""), d.get("版本", ""),
                     d.get("次数", 0), d.get("成功", 0), d.get("失败", 0), d.get("机器秒", 0)]
                    + [forms.get(n, 0) for n in form_names]
                    + [d.get("最后活跃", ""), ""])
    return rows


def form_names() -> list[str]:
    """配置类型清单 = config/forms/*.yaml 的文件名，和 webapp.list_forms 同源。"""
    return sorted(p.stem for p in (ROOT / "config" / "forms").glob("*.yaml"))


def push_to_sheet(header: list, rows: list) -> str:
    """可选：写一份进企微智能表格当看板。失败只返回原因，不抛。"""
    try:
        from mcp_doc import Doc, McpError
    except Exception as e:
        return f"MCP 客户端加载失败：{e}"
    docid = (ROOT / "tools" / ".mcp_docid")
    if not docid.exists():
        return ("还没有目标表格（tools/.mcp_docid 不存在）。"
                "机器人的文档权限开通后，跑一次 --create-sheet 建一张。")
    try:
        with Doc() as d:
            d.call("smartsheet_records_update", {
                "docid": docid.read_text(encoding="utf-8").strip(),
                "sheet_title": "每周上报",
                "type": "upsert",
                "records": [{"values": dict(zip(header, r))} for r in rows]})
        return ""
    except Exception as e:      # noqa: BLE001 —— 看板挂了不能挡住 team.json
        return f"写表格失败（不影响 team.json）：{str(e)[:200]}"


def main() -> int:
    ap = argparse.ArgumentParser(description="把统计群里的上报消息整理成 config/team.json")
    ap.add_argument("--file", help="从文件读，不读剪贴板（调试用）")
    ap.add_argument("--sheet", action="store_true", help="顺便写一份进企微智能表格")
    ap.add_argument("--dry", action="store_true", help="只看解析结果，不写 team.json")
    args = ap.parse_args()

    text = io.open(args.file, encoding="utf-8").read() if args.file else read_clipboard()
    if not text.strip():
        print("剪贴板是空的。先去统计群里全选复制（Ctrl+A、Ctrl+C），再跑这个。")
        return 1

    msgs = extract(text)
    if not msgs:
        print(f"这段文字里没找到上报消息（{len(text)} 字）。\n"
              f"确认复制的是「机器人统计」群的聊天记录，"
              f"里面应该有形如 {{\"周\": ..., \"指纹\": ...}} 的行。")
        return 1

    names = form_names()
    header = usage.report_header(names)
    rows = to_rows(msgs, names)
    team = usage.parse_report([header] + rows, names, usage.saving_conf(_settings()))

    weeks = sorted({r[0] for r in rows})
    print(f"捞到 {len(msgs)} 条上报，去重后 {len(rows)} 行"
          f"（{len({r[1] for r in rows})} 个人，{len(weeks)} 周：{weeks[0]} ~ {weeks[-1]}）")
    t = team.get("totals", {})
    print(f"  全团队：{team.get('people')} 人 · 累计 {t.get('items')} 条 · "
          f"省下 {int(t.get('saved', 0)) // 3600} 小时 · 失败 {t.get('failed')} 条")

    if args.dry:
        print("\n--dry：没有写文件")
        return 0

    usage.save_team(team)
    print(f"\n已写入 {usage.team_path()}")
    print("  下次打包会自动带进分发包，同事的 EXE 首页就能看到这个数。")

    if args.sheet:
        why = push_to_sheet(header, rows)
        print("  " + (why or "已同步到企微智能表格"))
    return 0


def _settings() -> dict:
    import yaml
    p = ROOT / "config" / "settings.yaml"
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


if __name__ == "__main__":
    sys.exit(main())
