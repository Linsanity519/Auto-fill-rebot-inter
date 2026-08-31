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
import os
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
#   {"指纹": "16d69684", "版本": "1.0.20", "次数": 3, ..., "分类型": {"DMP延期": 38},
#    "失败明细": {"fail_kinds": {"selector_miss": 3}, "fail_fields": {"selector_miss@pid": 3}}}
#   （失败明细是 1.1.2 起才带的，老消息没有；全是定长枚举 + 字段名，无业务值）
# ⚠ 用「找 { 再配对括号」而不是正则一把梭：分类型是嵌套对象，正则配不平。
NEEDLE = '"指纹"'


def _week(d: dict) -> str:
    """这条上报属于哪一周。

    ⚠ 1.0.20 起上报里**不再带「周」**——它是可推的：「最后活跃」就是那一周桶里
      最大的那个时间戳（见 src/usage.py 的 weekly_buckets），week_of 一下就还原了。
    ⚠ 老消息还带着「周」，优先用它 —— 群里两种消息会长期混在一起，
      只认新的等于把历史全丢了。
    """
    wk = usage.norm_week(d.get("周"))
    return wk or usage.week_of(str(d.get("最后活跃") or ""))


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
            if isinstance(d, dict) and d.get("指纹") and _week(d):
                out.append(d)
        except json.JSONDecodeError:
            pass
        i = end


ARCHIVE = "usage-archive.json"


def archive_path(root) -> Path:
    """原始上报行的归档。

    ⚠ 为什么必须有它：team.json 里只有**聚合结果**（总条数、各周汇总），
      没有原始行，所以聚合完就再也回不去了。而每次收集都是「拿剪贴板里有的行
      重算一遍、整个覆盖 team.json」—— 只要这次复制少捞了几周，那几周的数据
      就永久消失。实测踩过：一次只复制了最近的聊天，累计条数从 38 掉到 1。

    ⚠ 不进仓库（output/ 已在 .gitignore）：行里带花名，那是真人名字，
      而发布仓库是公开的。丢了也不要紧 —— 企微群里的消息是永久的，
      重新捞一遍就回来了。
    """
    return Path(root) / "output" / ARCHIVE


def _key(d: dict) -> str:
    return f"{d.get('指纹', '')}|{_week(d)}"


def _num(x) -> int:
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def _fold(old: dict, new: dict) -> dict:
    """同一个 (指纹, 周) 的两条上报，逐字段取最大。

    ⚠ 为什么是「取最大」而不是「取最后发的那条」：上报的是本机对该周的**累计值**，
      正常只增不减。但同事重装程序、或程序换了目录之后，本机 output/usage.jsonl
      会从头开始，于是后发的那条反而更小 —— 实测遇到过：同一周的累计条数从 38
      变成 0。按「取最后一条」就会把真实的历史冲掉，按「取最大」才对。
    ⚠ 花名/版本这类非累计字段取后来的（人可能改了花名、升级了版本）。
    """
    out = dict(old)
    for k in ("次数", "成功", "失败", "机器秒"):
        out[k] = max(_num(old.get(k)), _num(new.get(k)))
    for k in ("花名", "版本"):
        if str(new.get(k) or "").strip():
            out[k] = new[k]
    out["最后活跃"] = max(str(old.get("最后活跃") or ""), str(new.get("最后活跃") or ""))

    forms = dict(old.get("分类型") or {})
    for name, cnt in (new.get("分类型") or {}).items():
        forms[name] = max(_num(forms.get(name)), _num(cnt))
    out["分类型"] = forms

    # 失败明细（fail_kinds / fail_fields）：同「分类型」，逐叶子键取最大（累计语义）
    fd = {}
    for sect in ("fail_kinds", "fail_fields"):
        cur = dict((old.get("失败明细") or {}).get(sect) or {})
        for k, v in ((new.get("失败明细") or {}).get(sect) or {}).items():
            cur[k] = max(_num(cur.get(k)), _num(v))
        if cur:
            fd[sect] = cur
    if fd:
        out["失败明细"] = fd
    return out


def merge_archive(root, msgs: list[dict]) -> tuple[list[dict], int, int]:
    """把这次捞到的合并进归档，返回 (合并后的全部行, 新增数, 更新数)。

    合并单位是 (指纹, 周)，和上报的单位一致，所以重复收集完全无害 ——
    这也意味着**你不必每次都复制整个群的历史**，只复制最近一段就行，
    老数据从归档来。群消息越攒越多之后，这是唯一还跑得动的做法。
    """
    p = archive_path(root)
    old: dict[str, dict] = {}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        for k, v in (doc.get("rows") or {}).items():
            if isinstance(v, dict):
                old[k] = v
    except (OSError, ValueError):
        pass            # 第一次跑，或者文件坏了：当空的重来

    added = updated = 0
    merged = dict(old)
    for d in msgs:
        k = _key(d)
        if k not in merged:
            merged[k] = d
            added += 1
        else:
            folded = _fold(merged[k], d)
            if folded != merged[k]:
                merged[k] = folded
                updated += 1

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"rows": merged}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        print("  ⚠ 归档写入失败，这次只按剪贴板里的数据算（老数据不会进 team.json）")
        return list(msgs), added, updated

    return list(merged.values()), added, updated


def to_rows(msgs: list[dict], form_names: list[str]) -> list[list]:
    """每人每周留最后一条 → parse_report 认识的行。

    ⚠ 这套还原逻辑在 tools/test_usage.py 的 _from_line 里有一份测试替身，
      改这里记得改那边。
    """
    latest: dict[tuple, dict] = {}
    for d in msgs:
        latest[(str(d.get("指纹")), _week(d))] = d
    rows = []
    for (uid, wk), d in sorted(latest.items()):
        forms = d.get("分类型") or {}
        # ⚠ 「花名」这一列还在表结构里，但 1.0.20 起上报不带它了，新消息一律是空。
        #   老归档里那些还有值，照旧填回去。
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
    """把 config/team.json 推上 GitHub，让同事下次打开就能看到新数字。

    ⚠ 做法是「在 origin/main 之上直接造一个提交」，而不是「本地 commit 再 push」。
      这个仓库有两条互不相干的历史：本地是完整开发史（早期提交里有 webhook key），
      公开仓库是 squash 过的干净快照。按常规做法：
        · 推送会被判非快进直接拒绝 —— 数据永远上不去（实测踩过）
        · 万一推成功了更糟，会把本地历史连同那把 key 带上公开仓库
      用 hash-object / read-tree / commit-tree 这套底层命令，既不碰你的工作区和
      本地分支，也保证每次都是快进。

    ⚠ 只替换 config/team.json 这一个文件，origin/main 上的其它内容原样保留。
    ⚠ 全程失败只提示、不抛：team.json 已经写好了，大不了下次再推。
    """
    import subprocess
    import tempfile

    root = Path(root)

    def git(*args, **kw):
        env = kw.pop("env", None)
        return subprocess.run(("git",) + args, cwd=str(root), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", env=env)

    if git("rev-parse", "--git-dir").returncode != 0:
        return "不是 git 仓库，跳过推送（team.json 已写好）"
    if not (git("remote").stdout or "").strip():
        return "没有配 git remote，跳过推送"

    if git("fetch", "origin", "main").returncode != 0:
        return "拉不到 origin/main（网络？），本次不推，下次再说"

    # 远端那份和本地一样就不用推
    remote_blob = (git("rev-parse", "origin/main:config/team.json").stdout or "").strip()
    local_blob = (git("hash-object", "-w", "--", "config/team.json").stdout or "").strip()
    if not local_blob:
        return "算不出 team.json 的对象哈希，跳过推送"
    if remote_blob == local_blob:
        return "远端已经是这份数据，不用推"

    # 用一个临时索引拼出「origin/main 的树 + 换掉 team.json」，不碰真正的索引
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        if git("read-tree", "origin/main", env=env).returncode != 0:
            return "读不出 origin/main 的目录树，跳过推送"
        if git("update-index", "--add", "--cacheinfo",
               f"100644,{local_blob},config/team.json", env=env).returncode != 0:
            return "写不进临时索引，跳过推送"
        tree = (git("write-tree", env=env).stdout or "").strip()
    if not tree:
        return "生成目录树失败，跳过推送"

    r = git("commit-tree", tree, "-p", "origin/main",
            "-m", "chore: 更新团队使用统计快照")
    commit = (r.stdout or "").strip()
    if r.returncode != 0 or not commit:
        return f"造提交失败：{(r.stderr or '').strip()[:120]}"

    r = git("push", "origin", f"{commit}:main")
    if r.returncode != 0:
        return "推送失败（下次再跑会自动重试）：" + (r.stderr or r.stdout).strip()[:160]
    return "已推送，同事下次打开就能看到（raw 有几分钟 CDN 缓存）"


def _print_fail_hotspots(msgs: list[dict], top: int = 12) -> None:
    """把所有上报里的「失败明细」汇总,按次数排出热点。

    这一段**只打给维护者看**,不进 team.json —— 目的是不等有人手动报,
    就知道「哪个配置类型的哪个字段这周崩得最多」。全是定长枚举 + 字段名,无业务值。
    """
    kinds: dict[str, int] = {}
    fields: dict[str, int] = {}
    for d in msgs:
        fd = d.get("失败明细") or {}
        for k, v in (fd.get("fail_kinds") or {}).items():
            kinds[k] = kinds.get(k, 0) + _num(v)
        for k, v in (fd.get("fail_fields") or {}).items():
            fields[k] = fields.get(k, 0) + _num(v)
    if not kinds and not fields:
        return
    print("\n  失败热点（来自上报的失败明细，仅本地展示）：")
    if kinds:
        top_k = sorted(kinds.items(), key=lambda x: -x[1])[:top]
        print("    按类别： " + "　".join(f"{k}×{v}" for k, v in top_k))
    if fields:
        top_f = sorted(fields.items(), key=lambda x: -x[1])[:top]
        for k, v in top_f:
            print(f"    {k}　×{v}")


def main() -> int:
    ap = argparse.ArgumentParser(description="把统计群里的上报消息整理成 config/team.json")
    ap.add_argument("--file", help="从文件读，不读剪贴板（调试用）")
    ap.add_argument("--sheet", action="store_true", help="顺便写一份进企微智能表格")
    ap.add_argument("--dry", action="store_true", help="只看解析结果，不写 team.json")
    ap.add_argument("--rebuild", action="store_true",
                    help="不读剪贴板，只用归档里已有的数据重算 team.json 并推送")
    args = ap.parse_args()

    if args.rebuild:
        # 只用归档重算：改过归档、或上次推送失败想重推时用
        msgs = []
        text = "(rebuild)"
    else:
        text = io.open(args.file, encoding="utf-8").read() if args.file else read_clipboard()
    if not args.rebuild and not text.strip():
        print("剪贴板是空的。先去统计群里全选复制（Ctrl+A、Ctrl+C），再跑这个。")
        return 1

    if not args.rebuild:
        msgs = extract(text)
    if not args.rebuild and not msgs:
        # 把实际拿到的东西回显出来。只说「没找到」的话，人判断不了到底是
        # 「复制错群了」还是「群里本来就没有上报」—— 这两种处理完全不同。
        head = text.strip().replace("\r", "")[:300].replace("\n", " / ")
        print(f"这段文字里没找到上报消息（剪贴板里有 {len(text)} 字）。")
        print()
        print("剪贴板开头是这样的：")
        print(f"  {head}")
        print()
        print("要找的是形如下面这样的整行 JSON：")
        print('  {"指纹": "16d69684", "版本": "1.0.20", "次数": 3, ...}')
        print()
        print("对照检查：")
        print("  · 复制的是不是「机器人统计」那个群？别的群里没有上报")
        print("  · 企微里 Ctrl+A 有时只选中了输入框 —— 先在聊天记录区点一下再全选")
        print("  · 群里确实有人跑过任务吗？没人跑就没有上报可收")
        return 1

    # 先并进归档再算：只复制了最近一段聊天时，老数据不会被冲掉
    all_msgs, added, updated = merge_archive(ROOT, msgs)
    print(f"本次捞到 {len(msgs)} 条上报"
          f"（新增 {added}、更新 {updated}）；归档里累计 {len(all_msgs)} 条")

    names = form_names()
    header = usage.report_header(names)
    rows = to_rows(all_msgs, names)
    team = usage.parse_report([header] + rows, names, usage.saving_conf(_settings()))

    weeks = sorted({r[0] for r in rows})
    print(f"去重后 {len(rows)} 行"
          f"（{len({r[1] for r in rows})} 个人，{len(weeks)} 周：{weeks[0]} ~ {weeks[-1]}）")
    t = team.get("totals", {})
    print(f"  全团队：{team.get('people')} 人 · 累计 {t.get('items')} 条 · "
          f"省下 {int(t.get('saved', 0)) // 3600} 小时 · 失败 {t.get('failed')} 条")
    _print_fail_hotspots(all_msgs)

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
