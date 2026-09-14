"""每天探测一次：统计表的文档授权过期了没有，过期了往统计群里喊一声。

    python tools\\check_doc_auth.py            # 探测，过期就发群
    python tools\\check_doc_auth.py --dry      # 只探测，不发群

⚠ 只在收集端（装了 wecom-cli、授权过的那台机器）跑，不进分发包。
  由 Windows 计划任务每天调一次（任务名「配置助手-文档授权检查」）。

为什么要它：
  文档机器人的「文档使用权限」会过期，而且续不了期 —— 只能机器人创建者在企业微信里
  重新点一次授权。`wecom-cli auth show --status` 过期了照样显示 authorized，
  于是过期是**静默**的：要等哪天跑 collect_usage.py 读不了表才发现，
  首页「全团队」的数字早就停了好几天。这里每天真读一行，读不了就喊。

⚠ 过期期间不丢数据（见 collect_usage.DocAuthExpired），所以这里只提醒，不补救。
⚠ 一天最多喊一句（output/doc-auth-alert.txt 记着日期）：手动多跑几次不会刷群。
  授权恢复后标记自动删掉，下次再过期会重新喊。
⚠ 其它失败（CLI 没装、断网、表 ID 没配）只写日志不发群：那是这台机器自己的事，
  跟「要创建者去授权」不是一回事，发群只会让人白跑一趟。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import collect_usage as C   # noqa: E402

PROBE_SQL = "SELECT `运行ID` FROM `运行流水` LIMIT 1"
MARK = ROOT / "output" / "doc-auth-alert.txt"
LOG = ROOT / "output" / "doc-auth-check.log"


def probe(docid: str) -> tuple[str, str]:
    """读一行。返回 ("ok", "") / ("expired", 授权链接) / ("error", 原因)。"""
    try:
        C.sheet_query(docid, PROBE_SQL)
        return "ok", ""
    except C.DocAuthExpired as e:
        return "expired", e.link
    except Exception as e:
        return "error", str(e)[:200]


def alert_text(link: str) -> str:
    return ("【配置助手·统计】企微文档机器人的「文档使用权限」过期了，"
            "统计表读不了，首页「全团队」的数字会停在上一次收集。\n"
            "请机器人创建者在企业微信里重新授权"
            + (f"（在企业微信里打开）：\n{link}" if link else
               "：工作台 → 智能机器人 → 找到这个机器人 → 授权文档使用权限")
            + "\n同事那边数据照常在写，不会丢；授权后重新收集一次即可。")


def run_check(docid: str, send, *, mark: Path = MARK, today: str = "",
              do_probe=probe, dry: bool = False) -> tuple[int, str]:
    """探测 + 决定发不发。返回 (退出码, 一句说明)。send(text)->bool 由调用方给。

    退出码：0 正常 / 2 过期 / 1 探测不了（CLI、网络、配置）。
    """
    today = today or date.today().isoformat()
    if not docid:
        return 1, "不知道统计表是哪一张（tools/.stats_docid 或环境变量 STATS_DOCID）"
    state, info = do_probe(docid)
    if state == "ok":
        mark.unlink(missing_ok=True)
        return 0, "授权正常"
    if state == "error":
        return 1, f"探测失败（不是授权过期，不发群）：{info}"
    try:
        already = mark.read_text(encoding="utf-8").strip() == today
    except OSError:
        already = False
    if already:
        return 2, "授权过期（今天已经在群里提醒过了）"
    if dry:
        return 2, "授权过期（--dry，没发群）：\n" + alert_text(info)
    if not send(alert_text(info)):
        # 没发出去就不落标记，下次跑还会再试
        return 2, "授权过期，但群消息没发出去（看 config/webhook.txt）"
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(today, encoding="utf-8")
    return 2, "授权过期，已在统计群提醒"


def _log(line: str):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="探测统计表的企微文档授权是否过期")
    ap.add_argument("--dry", action="store_true", help="只探测，不往群里发")
    args = ap.parse_args()

    from src import report
    settings = C._settings()
    code, msg = run_check(C.stats_docid(), lambda t: report.send_line(settings, t),
                          dry=args.dry)
    print(msg)
    _log(f"[{code}] {msg.splitlines()[0]}")
    return code


if __name__ == "__main__":
    sys.exit(main())
