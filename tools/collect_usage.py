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
            # ⚠ v2 = 1.1.14 起的「一次运行一条」消息（见 src/report.py 文件头）。
            #   这个收集端还是按「每人每周一行」聚的，认不了它 —— 明着跳过，
            #   别让它掉进下面那句凭「指纹」认的口子里，攒出一堆周为空的脏行。
            #   ⚠ 单次运行的数据现在只在群里、没有进 team.json，
            #     等按 run 聚合那套做好了再接上（那是数据统计侧的事）。
            if isinstance(d, dict) and _num(d.get("v")) >= 2:
                pass
            elif isinstance(d, dict) and d.get("指纹") and _week(d):
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

    # 「分类型跑了」={类型: 跑了几次}，1.1.10 起才带。和「分类型」同为累计语义，
    # 逐键取最大。它记的是**跑没跑过**，不是成功条数 —— 全失败的类型只在这儿露面，
    # 正表不用它（表结构没动），维护者靠它看出「这周谁在试哪个类型、试崩了」。
    ran = dict(old.get("分类型跑了") or {})
    for name, cnt in (new.get("分类型跑了") or {}).items():
        ran[name] = max(_num(ran.get(name)), _num(cnt))
    if ran:
        out["分类型跑了"] = ran

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


# ---------------------------------------------------------------- 主通道：智能表格
# 1.1.15 起客户端一次运行往「配置助手 · 使用统计」写一行，收集端直接读那张表。
# 剪贴板那条路留着，只为收还没升级的人发到群里的老消息（见 main 的 --clipboard）。
#
# ⚠ 表 ID 放在 tools/.stats_docid，不进仓库（和 tools/.mcp_key 一个待遇）。
#   第一次用：把智能表格的链接丢进那个文件，或者设环境变量 STATS_DOCID。
RUNS_SHEET = "运行流水"          # 一行 = 一次运行
LEGACY_SHEET = "历史周汇总"      # 1.1.15 之前的老数据，一行 = 某人某周的累计

# ⚠ **必须按运行ID去重**：表格 webhook 没有幂等键，客户端读超时（服务端其实已经
#   写进去了）之后补发，同一次运行就留下两行。实测过一次，不去重那次运行的条数和
#   耗时全部翻倍。
# ⚠ 去重放在 Python 里做，不写成 GROUP BY `运行ID` + MAX(每一列)：
#   实测这个 SQL 引擎的 MAX() **对文本列返回空**（配置类型、指纹、模式全成了空串），
#   于是「空跑/重跑不进累计」这条过滤形同虚设，数字悄悄变大。数值列倒是正常，
#   所以这个坑特别隐蔽 —— 总数看着只多了一点点。
RUNS_LIMIT = 20000      # 防跑飞。到顶会警告，不静默截断
RUNS_SQL = """
SELECT `运行ID` AS `run`,
       DATE_FORMAT(`时间`, "%Y-%m-%d") AS `日`,
       `指纹`, `版本`, `配置类型` AS `类型`, `模式`, `重跑`,
       `成`, `败`, `跳`, `机器秒`, `等人秒`
FROM `运行流水`
LIMIT 20000
"""

LEGACY_SQL = """
SELECT `周`, `指纹`, `版本`, `次数`, `成功`, `失败`, `机器秒`, `最后活跃`, `分类型`
FROM `历史周汇总`
"""


def stats_docid() -> str:
    """统计表的文档 ID。环境变量 STATS_DOCID 优先，否则读 tools/.stats_docid。

    ⚠ 文件里可以直接粘表格链接，这里会把 /smartsheet/<id> 那段抠出来 ——
      让人去 URL 里数字符是最容易出错的一步。
    """
    raw = (os.environ.get("STATS_DOCID") or "").strip()
    if not raw:
        p = ROOT / "tools" / ".stats_docid"
        try:
            raw = next((ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
                        if ln.strip() and not ln.startswith("#")), "")
        except OSError:
            raw = ""
    m = re.search(r"/smartsheet/([A-Za-z0-9_\-]+)", raw)
    return m.group(1) if m else raw


def sheet_query(docid: str, sql: str) -> list[dict]:
    """跑一条只读 SQL，返回 rows。CLI 不在 / 没授权 / 查不动都抛，由调用方兜。

    ⚠ 输出必须按 UTF-8 解码：Windows 控制台默认 GBK，中文列名会被解成乱码，
      json.loads 直接报「Expecting ',' delimiter」—— 看着像接口坏了，其实是编码。
    """
    import shutil
    import subprocess

    # ⚠ Windows 上 wecom-cli 是个 .cmd / .exe 的壳，subprocess 不走 shell 时
    #   直接传 "wecom-cli" 会 WinError 2「找不到文件」—— 得先 which 一下。
    exe = shutil.which("wecom-cli") or shutil.which("wecom-cli.cmd")
    if not exe:
        raise RuntimeError("找不到 wecom-cli（npm install -g @wecom/cli）")
    r = subprocess.run([exe, "smartsheet", "records", "query",
                        "--docid", docid, "--sql", " ".join(sql.split())],
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b"").decode("utf-8", "replace")[:200] or "wecom-cli 执行失败")
    d = json.loads((r.stdout or b"").decode("utf-8", "replace"))
    if d.get("errcode") not in (0, None):
        raise RuntimeError(f"表格返回 {d.get('errcode')}：{d.get('errmsg')}")
    rows = []
    for v in d.get("values") or []:
        v = json.loads(v) if isinstance(v, str) else v
        rows.extend(v.get("rows") or [])
    return rows


def _monday(day: str) -> str:
    """'2026-09-09' → 那一周的周一。统计的最小粒度是周，和 usage.week_of 同源。"""
    return usage.week_of(str(day or "").strip()[:10])


def build_team(runs: list[dict], legacy: list[dict], conf: dict) -> dict:
    """把「一次运行一行」+「老的周累计」压成首页要的那份 team.json。

    ⚠ 形状必须和 usage.parse_report 的返回值一模一样 —— 首页读的就是它，
      少一个键那张卡就空着，而且不报错。
    ⚠ 口径只在这里算一次（人工基准 × 条数，见 usage.saved_seconds）。
      客户端不算、前端不算 —— 以前这个数在两处各实现一份，改一次要改两个地方，
      漏一个就出现「首页我的」和「首页团队」两个不同的数。
    ⚠ 重跑和空跑不进累计，和本机口径一致（见 usage.summarize 的注释）。
    """
    people, forms, form_saved, weeks, who = set(), {}, {}, {}, {}
    n_runs = ok = failed = seconds = 0
    human = saved = 0.0

    def bump(uid, wk, last, items, secs, r_human, r_saved, n=1):
        nonlocal n_runs, ok, seconds, human, saved
        n_runs += n
        ok += items
        seconds += secs
        human += r_human
        saved += r_saved
        b = weeks.setdefault(wk, {"items": 0, "seconds": 0, "saved": 0.0})
        b["items"] += items
        b["seconds"] += secs
        b["saved"] += r_saved
        w = who.setdefault(uid, {"uid": uid, "name": "", "last": "", "items": 0, "runs": 0})
        w["items"] += items
        w["runs"] += n
        w["last"] = max(w["last"], str(last or ""))

    for r in runs:
        if str(r.get("重跑") or "").strip() or str(r.get("模式") or "") == "空跑":
            continue          # 重跑和空跑单独看，不进累计
        uid = str(r.get("指纹") or "")
        name = str(r.get("类型") or "(未知)")
        items, secs = _num(r.get("成")), _num(r.get("机器秒"))
        r_human = usage.human_seconds(conf, name, items, secs)
        r_saved = usage.saved_seconds(conf, name, items, secs)
        people.add(uid)
        failed += _num(r.get("败"))
        forms[name] = forms.get(name, 0) + items
        form_saved[name] = form_saved.get(name, 0.0) + r_saved
        bump(uid, _monday(r.get("日")), r.get("日"), items, secs, r_human, r_saved)

    for r in legacy:
        uid = str(r.get("指纹") or "")
        people.add(uid)
        failed += _num(r.get("失败"))
        secs = _num(r.get("机器秒"))
        try:
            by_form = json.loads(r.get("分类型") or "{}")
        except ValueError:
            by_form = {}
        # ⚠ 老数据只有「每个类型成功了几条」，没有「每个类型各花了多少秒」。
        #   人工基准按类型算得出来，机器秒只能整周挂着 —— 所以按条数摊。
        # ⚠ 总条数以「成功」那一列为准，**不能**拿分类型加出来：老归档的分类型是
        #   逐键取最大值合出来的，加起来可能比总数大（实测 22 加成了 34）。
        #   分类型只用来分摊，不参与总数。
        total_items = _num(r.get("成功"))
        split_base = sum(_num(v) for v in by_form.values()) or total_items
        r_human = r_saved = 0.0
        for name, cnt in by_form.items():
            cnt = _num(cnt)
            share = secs * cnt / split_base if split_base else 0
            r_human += usage.human_seconds(conf, name, cnt, share)
            s = usage.saved_seconds(conf, name, cnt, share)
            r_saved += s
            forms[name] = forms.get(name, 0) + cnt
            form_saved[name] = form_saved.get(name, 0.0) + s
        bump(uid, usage.norm_week(r.get("周")), r.get("最后活跃"),
             total_items, secs, r_human, r_saved, n=_num(r.get("次数")))

    return {
        "people": len(people),
        "totals": {"runs": n_runs, "items": ok, "failed": failed, "seconds": seconds,
                   "human": round(human, 1), "saved": round(saved, 1),
                   "ok_rate": (ok / (ok + failed)) if (ok + failed) else None},
        "forms": [{"name": n, "ok": v, "saved": round(form_saved.get(n, 0.0), 1)}
                  for n, v in sorted(forms.items(), key=lambda kv: -kv[1])],
        "weeks": {k: {"items": v["items"], "seconds": v["seconds"],
                      "saved": round(v["saved"], 1)} for k, v in weeks.items()},
        "actives": sorted(who.values(), key=lambda x: x["last"], reverse=True)[:12],
    }


def collect_from_sheet() -> dict:
    """从统计表读全量 → team.json 的内容。表读不了就抛，由 main 决定退不退。"""
    docid = stats_docid()
    if not docid:
        raise RuntimeError("不知道统计表是哪一张：把表格链接写进 tools/.stats_docid，"
                           "或者设环境变量 STATS_DOCID")
    raw = sheet_query(docid, RUNS_SQL)
    if len(raw) >= RUNS_LIMIT:
        print(f"  ⚠ 取到了 {RUNS_LIMIT} 行上限，可能还有没读到的 —— 该给这张表分表了")
    # 同一个运行ID 只留一行（重复来自客户端超时补发，见 RUNS_SQL 上面那段）
    runs, seen = [], set()
    for r in raw:
        rid = str(r.get("run") or "")
        if rid and rid in seen:
            continue
        seen.add(rid)
        runs.append(r)
    if len(raw) != len(runs):
        print(f"  （表里有 {len(raw) - len(runs)} 行是超时补发留下的重复，已按运行ID去掉）")
    try:
        legacy = sheet_query(docid, LEGACY_SQL)
    except Exception as e:
        legacy = []
        print(f"  （历史周汇总那张子表读不到，先只算新数据：{e}）")
    team = build_team(runs, legacy, usage.saving_conf(_settings()))
    print(f"表里读到 {len(runs)} 次运行（已按运行ID去重）+ {len(legacy)} 行历史周汇总")
    return team


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
    ap = argparse.ArgumentParser(
        description="整理 config/team.json。默认从企微智能表格读（1.1.15 起客户端直接写那张表）")
    ap.add_argument("--clipboard", action="store_true",
                    help="老路子：从剪贴板里的群聊记录抠上报消息（收还没升级的人发的）")
    ap.add_argument("--file", help="从文件读群聊记录，不读剪贴板（调试用）")
    ap.add_argument("--sheet", action="store_true", help="顺便写一份进企微智能表格看板")
    ap.add_argument("--dry", action="store_true", help="只看结果，不写 team.json")
    ap.add_argument("--rebuild", action="store_true",
                    help="不读剪贴板，只用归档里已有的老数据重算（不碰表格）")
    args = ap.parse_args()

    # 默认走表格。这是 1.1.15 之后的主通道：客户端一次运行写一行，
    # 这里几条 SQL 就够 —— 不用复制聊天记录、不用正则找 JSON、不用「取最大值」合并。
    if not (args.clipboard or args.file or args.rebuild):
        try:
            team = collect_from_sheet()
        except Exception as e:
            print(f"读统计表失败：{e}")
            print()
            print("对照检查：")
            print("  · wecom-cli 装了吗、授权了吗（wecom-cli auth show --status）")
            print("  · tools/.stats_docid 里是不是那张统计表的链接")
            print("  · 想收还没升级的人发到群里的老消息，用 --clipboard")
            return 1
        t = team.get("totals", {})
        print(f"  全团队：{team.get('people')} 人 · 累计 {t.get('items')} 条 · "
              f"省下 {int(t.get('saved', 0)) // 3600} 小时 · 失败 {t.get('failed')} 条")
        if args.dry:
            print("\n--dry：没有写文件")
            return 0
        usage.save_team(team)
        print("  同步：" + push_team(ROOT))
        print(f"\n已写入 {usage.team_path()}")
        print("  同事下次打开就能看到（首页运行时会去拉这份）。")
        return 0

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
