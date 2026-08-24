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
import time
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
    """读剪贴板。用 ctypes 直接调 Win32，不引第三方库。

    ⚠ 每个函数的 argtypes/restype 都必须显式声明，一个都不能省。
      ctypes 默认把参数按 32 位 c_int 转换，而 64 位 Windows 上的句柄是
      64 位指针 —— 地址一旦超过 2GB 就是
        ctypes.ArgumentError: argument 1: OverflowError: int too long to convert
      而地址落在哪儿是随机的，所以这个 bug 会「时好时坏」，实测就这么炸过一次
      （只声明了 restype 忘了 argtypes）。
    """
    if sys.platform != "win32":
        return ""
    import ctypes
    from ctypes import wintypes

    CF_UNICODETEXT = 13
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32

    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.OpenClipboard.restype = wintypes.BOOL
    u32.CloseClipboard.argtypes = []
    u32.CloseClipboard.restype = wintypes.BOOL
    u32.GetClipboardData.argtypes = [wintypes.UINT]
    u32.GetClipboardData.restype = wintypes.HANDLE
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalLock.restype = wintypes.LPVOID
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.restype = wintypes.BOOL

    # 剪贴板同一时刻只能被一个进程打开。企微/浏览器可能正好占着，等一下再试。
    for attempt in range(10):
        if u32.OpenClipboard(None):
            break
        time.sleep(0.1)
    else:
        print("剪贴板被别的程序占着，打不开。关掉正在读写剪贴板的程序再试一次。")
        return ""

    try:
        h = u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""            # 剪贴板里不是文本（比如复制的是图片）
        p = k32.GlobalLock(h)
        if not p:
            return ""
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


def push_team(root) -> str:
    """把 config/team.json 提交并推上去，让同事下次打开就能看到新数字。

    ⚠ 这一步以前是「等下次发版打进安装包」，同事看到的团队数据最新只到上次发版。
      客户端改成运行时从 raw.githubusercontent 拉之后（见 src/usage.py fetch_team），
      推一次就够了，几分钟内全员可见。

    ⚠ 只 add team.json 这一个文件 —— 绝不 `git add -A`：这台机器上可能正改着别的
      东西，甚至有不该进仓库的 data/、.chrome-profile/。收集统计不该顺手替人提交。
    ⚠ 全程失败只提示、不抛：拿不到网、没配 remote 都不影响 team.json 已经写好了，
      大不了下次发版时带出去（老行为）。
    """
    import subprocess

    def run(*args):
        return subprocess.run(("git",) + args, cwd=str(root),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")

    if run("rev-parse", "--git-dir").returncode != 0:
        return "不是 git 仓库，跳过推送（team.json 已写好，下次发版会带出去）"
    if not (run("remote").stdout or "").strip():
        return "没有配 git remote，跳过推送"

    if run("diff", "--quiet", "--", "config/team.json").returncode == 0:
        return "team.json 没有变化，不用推"

    if run("add", "--", "config/team.json").returncode != 0:
        return "git add 失败，跳过推送"
    r = run("commit", "-m", "chore: 更新团队使用统计快照", "--", "config/team.json")
    if r.returncode != 0:
        return f"git commit 失败：{(r.stderr or r.stdout).strip()[:120]}"

    # 推到远端默认分支；失败就把提交留在本地，下次再推
    head = (run("rev-parse", "HEAD").stdout or "").strip()
    r = run("push", "origin", f"{head}:main")
    if r.returncode != 0:
        return ("已提交到本地，但推送失败（下次再推或手动 git push）："
                + (r.stderr or r.stdout).strip()[:160])
    return "已推送，同事下次打开就能看到（raw 有几分钟 CDN 缓存）"


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
        # 把实际拿到的东西回显出来。只说「没找到」的话，人判断不了到底是
        # 「复制错群了」还是「群里本来就没有上报」—— 这两种处理完全不同。
        head = text.strip().replace("\r", "")[:300].replace("\n", " / ")
        print(f"这段文字里没找到上报消息（剪贴板里有 {len(text)} 字）。")
        print()
        print("剪贴板开头是这样的：")
        print(f"  {head}")
        print()
        print("要找的是形如下面这样的整行 JSON：")
        print('  {"周": "2026-W34", "指纹": "16d69684", "次数": 3, ...}')
        print()
        print("对照检查：")
        print("  · 复制的是不是「机器人统计」那个群？别的群里没有上报")
        print("  · 企微里 Ctrl+A 有时只选中了输入框 —— 先在聊天记录区点一下再全选")
        print("  · 群里确实有人跑过任务吗？没人跑就没有上报可收")
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
    print("  同步：" + push_team(ROOT))
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
