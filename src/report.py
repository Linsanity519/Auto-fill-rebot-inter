"""统计回传：一个 HTTPS POST 发到企微群机器人，没了。

⚠ 这个文件替代了原来的 src/sheet.py（330 行浏览器自动化：开 Chrome、打开企微文档、
  点网格拿焦点、CDP 合成 Ctrl+A/Ctrl+C、读剪贴板）。那套东西每一环都要求
  **前端有人、有登录态、页面结构不变**，实测坏过：有人用了一整天，团队表里一条没有，
  而且失败是静默的，谁都不知道。原因和取舍见
  docs/界面方案/主页-使用统计调研.md §2.5。

为什么是群机器人而不是企微文档的 API：
  文档 MCP 机器人（能读能写、也不用浏览器）的 apikey **没法限权** —— 一把 key 覆盖
  持有人名下所有文档，能新建、能全量覆盖、能删子表。打进发给一百个人的 exe 里，
  等于把「以我的身份删改我所有文档」发出去。所以那把 key 只留在收集端
  （tools/collect_usage.py），分发出去的只有这个 webhook key ——
  它泄露的最坏情况是有人往那个统计群里发消息。

三条铁律照旧（见 src/usage.py 开头）：只发条数和耗时、身份只有匿名指纹、
**回传绝不能挡业务**（这里任何异常都只写日志，不往上抛）。

发什么（1.1.14 改的，别改回去）：**一次运行一条消息**，内容就是这一次运行本身 ——
什么时候跑的、什么版本、哪个配置类型、什么模式、几条、机器花了多久。
在这之前发的是「本机某一周的累计快照」，于是：
  · 想知道刚才那轮干了什么，只能拿两条消息相减；
  · 每轮的类型/模式/耗时/时刻全被压掉了（埋点文件里明明都有）；
  · 一轮全跳过或空跑，因为「数字没变」压根不发 —— 而那正是最该查的那种。
累计是**收集端**该干的活，不是本机该发的东西。

代价是：增量消息丢一条就永久少一条（累计消息天然幂等，丢了下一条自带全部历史）。
所以配一个发件箱（output/usage-outbox.jsonl）：跑完先落盘，发出去才划掉，
失败留着下次补。每条带 run（运行 id），收集端按它去重 —— 所以宁可多发一次。

发到哪儿（两个通道，都是一个 POST，没有服务端）：
  · 智能表格「接收外部数据」——主通道，一次运行一行，本身就是能打开看的看板；
    收集端用 wecom-cli 的 SQL 直接聚合（records query）。
  · 企微群机器人——留着当兜底，万一表被关了数据还有个落点。
两个通道**各记各的账**（发件箱条目里的 "s"）：表格写成功、群失败时下次只补群，
不然表里会多出一行重复记录。

⚠⚠ 收集端聚合时**必须按「运行ID」去重**（COUNT(DISTINCT `运行ID`)）。
  表格 webhook 没有幂等键：2026-09-09 实测，读超时那次其实服务端**已经写进去了**，
  客户端以为失败、下次补发，于是同一次运行在表里有两行。这不是 bug 是常态 ——
  只要聚合去重就没有任何影响，不去重就会把数字翻倍。
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

from .paths import FROZEN, user_path

log = logging.getLogger(__name__)

TIMEOUT = 3          # 内网偶尔抽风，三秒不通就算了，下次再补
SHEET_TIMEOUT = 12   # ⚠ 表格那个接口比群机器人慢得多：3 秒实测必超时（读超时，
                     #   不是连不上）。它也不在跑批的关键路径上（后台线程），等得起。
MAX_BYTES = 1800     # 企微 text 消息上限 2048 字节，留点余量
OUTBOX_FILE = "usage-outbox.jsonl"    # 还没发出去的运行，一行一条
OUTBOX_MAX = 2000    # 发件箱最多留这么多条。长期连不上网时别让它无限涨
                     # （一条约 250 字节，2000 条也就 500KB；升级那次要补历史，别卡太小）
BACKFILL_MARK = "usage-backfilled.txt"     # 补过历史了的标记，只补一次
SHEET_WEBHOOK_FILE = "sheet_webhook.txt"   # 智能表格「接收外部数据」的地址
SHEET_MAX_ROWS = 200                        # 单次 POST 的行数。官方上限 500，留余量

# 智能表格「运行流水」那张子表的字段 ID。
# ⚠ 表格 webhook 的 values **按字段 ID 取键**，不是字段名（和 CLI 那套不一样）。
#   这份映射来自表格「接收外部数据」页面给的 schema，2026-09-09 实测写入通过。
#   ⚠ 重建那张子表的话字段 ID 会变，得回来改这里 —— 所以别重建，加列就行。
SHEET_FIELDS = {
    "运行ID": "f6018f", "时间": "f2a459", "指纹": "fd54d5", "版本": "fd8f4f",
    "配置类型": "f285a3", "模式": "ff2a94", "总": "fe1c44", "成": "f0c37c",
    "败": "fc8440", "跳": "fcd05c", "机器秒": "fef939", "等人秒": "fff32c",
    "失败明细": "fa4270", "范围": "ftWp0x", "重跑": "fTM5FR", "来源": "fB2cMJ",
}

# 表格列名 → 消息里的字段名。⚠ 两边**不是全都同名**：表里叫「运行ID / 配置类型」，
# 消息里叫「run / 类型」（消息要短，一条要塞进企微 2048 字节）。
# 对不上的后果是那一列静默空着 —— 表里看着有数据，其实少了最关键的两列。
SHEET_KEY = {"运行ID": "run", "配置类型": "类型"}

# ── 存量迁移：换群时把旧 key 挂到这里 ──────────────────────────────
# config/webhook.txt 不在 300KB 代码包的更新范围内（见 tools/updater.py 的
# PAYLOAD_MEMBERS），只有跑完整安装包才会刷新，而绝大多数人只走代码包更新 ——
# 于是换了群，存量机器还一直往旧群回传。这个文件（src/）是跟代码包一起发的，
# 所以在这兜一层：本机 webhook.txt 里若还是下面这些废弃 key，就改用 BUNDLED_WEBHOOK。
# ⚠ 只在「文件存在且是废弃 key」时替换；文件缺失仍然静默不上报（别人 clone 打的包）。
BUNDLED_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=4e7859e7-43d5-4eee-bda4-501989499c87"
_RETIRED_WEBHOOK_KEYS = frozenset({
    "53d90b8b-f8f8-4c02-83bc-52ec1369ac29",   # 1.1.4 换群前的统计群
})


def _bundled_sheet_webhook() -> str:
    """随代码包发出来的表格写入地址（src/_bundled.py，打包时生成、不进仓库）。

    ⚠ 这一层不是可有可无的兜底，它是**存量用户唯一拿得到地址的途径**。
      1.1.15 把地址放在 config/sheet_webhook.txt，而 config/ 下只有 forms 和
      team.json 在代码包的投递范围里（见 tools/make_payload.py 的 MEMBERS）——
      于是走 300KB 代码包升级的人（也就是绝大多数人）**永远收不到这个文件**，
      表格通道从没打开过，而且完全静默：enabled() 只要群那条在就算开着，
      push() 里 chans 少一条谁也不会发现。1.1.16 实测就是这样，表里一条没有。
    ⚠ 那为什么不干脆把 config/sheet_webhook.txt 加进代码包？
      因为用户机上跑的是**旧 updater**（tools/ 不在投递范围里，见同一个 MEMBERS），
      而旧 updater 见到 PAYLOAD_MEMBERS 之外的成员会直接判包损坏、整包回滚。
      加一个文件进去 = 存量用户全都更新失败。所以只能走 src/。
    ⚠ 模块缺失是正常的：从源码跑、或者别人自己 clone 打的包都没有它。
    """
    try:
        from ._bundled import SHEET_WEBHOOK      # type: ignore[attr-defined]
    except Exception:
        return ""
    return (SHEET_WEBHOOK or "").strip()


def _webhook_key(url: str) -> str:
    """取 ?key=... 的值，用来判断是不是废弃地址。取不到返回空串。"""
    try:
        return (parse_qs(urlsplit(url).query).get("key") or [""])[0].strip()
    except ValueError:
        return ""


def _webhook_from_file() -> str:
    """从 config/webhook.txt 读回传地址。

    ⚠ 为什么单独一个文件、而不是写在 settings.yaml 里：
      安装包升级时**不覆盖 settings.yaml**（怕冲掉用户自己改的配置），
      而 webhook.txt 是 ignoreversion、每次升级都刷新。地址若只存在
      settings.yaml 里，从老版本升上来的人就永远是空的、统计静默失效 ——
      这正是 1.0.6 → 1.0.9 踩过的坑。
    ⚠ 这个文件是**故意提交进仓库**的：统计一旦失效是静默的，没人会发现自己
      那份没回传，所以任何环境打包都必须开箱可用，不能依赖「先配一次 Secret」。
      代价是 key 可见，最坏情况只是有人往统计群发消息；真出事就换一把。
      换群 / 临时改地址：设环境变量 USAGE_WEBHOOK_URL（优先，会写回这个文件，
      见 tools/inject_release_config.py）。
    ⚠ 文件缺失是正常情况（比如别人自己 clone 打的包），此时静默不上报。
    ⚠ 文件里若是 _RETIRED_WEBHOOK_KEYS 里的废弃 key，返回 BUNDLED_WEBHOOK ——
      存量机器换群迁移，见上面那段注释。
    """
    p = user_path("config", "webhook.txt")
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                if _webhook_key(line) in _RETIRED_WEBHOOK_KEYS:
                    return BUNDLED_WEBHOOK
                return line
    except OSError:
        pass
    return ""


def webhook_url(settings: dict) -> str:
    """settings.yaml 里显式填了就用它（本机自定义优先），否则用随包注入的那份。"""
    explicit = (((settings or {}).get("usage") or {}).get("webhook_url") or "").strip()
    return explicit or _webhook_from_file()


def sheet_webhook_url(settings: dict) -> str:
    """智能表格的写入地址。settings 里显式填了就用它，否则读 config/sheet_webhook.txt。

    ⚠ 为什么是**另一个**地址、不复用群那个：这是「接收外部数据」给的**表级**写入 key，
      权限比群机器人还小 —— 只能往那一张子表追加行，读不了、删不了、改不了结构，
      表主随时能在界面上关掉它。所以它可以随包发出去。
      （文档机器人那把 apikey 覆盖持有人名下所有文档，绝不能进分发包，见文件头。）
    ⚠ 单独一个文件的理由同 webhook.txt：安装包升级不覆盖 settings.yaml。
    ⚠ 文件缺失是**常态**，不是异常：它不在代码包的投递范围里，走代码包升级的人
      本来就没有。所以最后还要落到 _bundled_sheet_webhook() —— 那才是存量用户
      真正拿到地址的地方，别把它当成可选的兜底删掉。

    顺序：settings.yaml 显式配的 → config/sheet_webhook.txt → 随代码包发的常量。
    """
    explicit = (((settings or {}).get("usage") or {}).get("sheet_webhook_url") or "").strip()
    if explicit:
        return explicit
    p = user_path("config", SHEET_WEBHOOK_FILE)
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except OSError:
        pass
    return _bundled_sheet_webhook()


def enabled(settings: dict) -> bool:
    """两个通道有一个能发就算开着。"""
    return bool(webhook_url(settings) or sheet_webhook_url(settings))


# ⚠ 1.1.14 之前这里有个 _payload()：把「某人某周的累计」拼成一条群消息。
#   回传改成「一次运行一条」（run_payload）之后它就没人用了，1.1.16 删掉。
#   还没升级的人发上来的仍是老格式，但那是**收集端**解析的事
#   （tools/collect_usage.py --clipboard），客户端这边不再生产老格式。

def _post_json(url: str, body: dict, timeout: int = TIMEOUT) -> dict:
    """POST 一个 JSON，返回解析后的响应。连不上/超时直接抛。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def _post(url: str, text: str) -> bool:
    """往群里发一条文本消息。"""
    res = _post_json(url, {"msgtype": "text", "text": {"content": text}})
    if res.get("errcode") not in (0, None):
        raise RuntimeError(f"企微返回 {res.get('errcode')}：{res.get('errmsg')}")
    return True


def send_line(settings: dict, text: str) -> bool:
    """发一句纯文本（告警用）。失败只记日志。"""
    url = webhook_url(settings)
    if not url:
        return False
    try:
        return _post(url, text)
    except Exception:
        log.warning("webhook 发送失败（不影响运行）", exc_info=True)
        return False


def feedback_webhook_url(settings: dict) -> str:
    """用户反馈发到哪儿。

    优先级：settings.yaml 的 usage.feedback_webhook_url → config/feedback_webhook.txt
    → 兜底并到统计群（webhook_url）。想让反馈单独进一个群，配前两者之一即可。
    """
    explicit = (((settings or {}).get("usage") or {}).get("feedback_webhook_url") or "").strip()
    if explicit:
        return explicit
    p = user_path("config", "feedback_webhook.txt")
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except OSError:
        pass
    return webhook_url(settings)


def send_feedback(settings: dict, text: str) -> bool:
    """发一条用户反馈。企微 text 上限 2048 字节，超了先砍日志、再砍正文。"""
    url = feedback_webhook_url(settings)
    if not url:
        return False
    if len(text.encode("utf-8")) > 2000:
        # 用两个「——」分隔的最后一段一般是日志，先砍它
        head, sep, _ = text.rpartition("\n——\n")
        text = (head if sep else text[:600]) + "\n——\n（内容过长，已截断）"
        text = text.encode("utf-8")[:2000].decode("utf-8", "ignore")
    try:
        return _post(url, text)
    except Exception:
        log.warning("反馈发送失败（不影响运行）", exc_info=True)
        return False


# ── 一次运行一条：发件箱 ──────────────────────────────────────────
# 界面上那排「运行模式」的取值 → 人话。⚠ 少一个就等于那一档在表里显示成英文原文。
# ⚠ 这张表 1.1.16 修过一次，两处都错着：
#     · 漏了 confirm —— 而它是**默认那一档**，也就是最常见的一种，表里一直写着 "confirm"
#     · step 当时标成「逐条确认」，其实界面上 step 是「逐步试跑」（只有自制配置类型有），
#       真正的「逐条确认」是 confirm
#   取值的唯一出处是 assets/webui/app.js 的 state.runMode（dry/confirm/sample/auto/step），
#   那边加一档，这里也要加一条。
MODE_TEXT = {"dry": "空跑", "confirm": "逐条确认", "sample": "抽样确认",
             "auto": "全自动", "step": "逐步试跑"}


def outbox_path():
    return user_path("output", OUTBOX_FILE)


def run_payload(row: dict) -> dict:
    """把一条 run_finished 埋点，变成要发出去的那个 JSON。

    ⚠ 这里**只挑**要发的字段，不是把埋点整行发出去 —— 埋点里以后可能加别的东西，
      发出去的内容必须是这一处说了算的（三条铁律见文件头）。
    ⚠ 「机器秒」是净时长（墙钟 − 等人点确认），「等人秒」单独给：逐条确认模式下
      人就坐在旁边，那段时间不能算机器代劳。两个混成一个数就再也分不开了。
    """
    from . import usage

    row = row or {}
    ts = str(row.get("ts") or "")
    out = {
        "v": 2,
        "run": row.get("run_id", ""),
        "指纹": row.get("uid", ""),
        "版本": row.get("ver", ""),
        "时间": ts[:19].replace("T", " "),
        "类型": row.get("form") or "(未知)",
        "模式": MODE_TEXT.get(str(row.get("mode") or ""), str(row.get("mode") or "")),
        "总": _int(row.get("total")),
        "成": _int(row.get("ok")),
        "败": _int(row.get("failed")),
        "跳": _int(row.get("skipped")),
        "机器秒": int(round(usage._net_seconds(row))),
        "等人秒": _int(row.get("wait_seconds")),
    }
    if row.get("scope"):
        out["范围"] = str(row["scope"])
    if row.get("retry_of"):
        out["重跑"] = str(row["retry_of"])
    if row.get("stopped"):
        out["中途停止"] = True
    out["来源"] = "实时"
    # 失败明细：定长枚举（selector_miss / page_rejected…）+ 字段 label，无业务值。
    # 有就带上，没有不占位 —— 一条消息里多一个空对象就是多一分噪音。
    if row.get("fail_kinds"):
        out["失败明细"] = row["fail_kinds"]
    if row.get("fail_fields"):
        out["失败字段"] = row["fail_fields"]
    return out


def _int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def enqueue(settings: dict, row: dict) -> bool:
    """把一次运行放进发件箱。发送是另一回事（push），这里只负责别丢。

    ⚠ 先落盘再发，不是先发再落盘：跑完那一下人常常直接叉掉窗口，
      后台线程跟着没了 —— 落了盘的下次开机会补上，没落盘的就真没了。
    """
    if not enabled(settings) or not row or row.get("event") != "run_finished":
        return False
    try:
        p = outbox_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"d": run_payload(row), "s": []},
                                ensure_ascii=False) + "\n")
        return True
    except Exception:
        log.warning("回传发件箱写不进去（这一次运行的统计会丢）", exc_info=True)
        return False


def _legacy_run_id(row: dict) -> str:
    """老埋点没有 run_id（1.0.x 那阵）。拿「谁 + 什么时候 + 哪个类型」凑一个稳定的，
    这样补历史重跑一次也不会在表里多出一行。"""
    import hashlib

    raw = f"{row.get('uid')}|{row.get('ts')}|{row.get('form')}"
    return "L" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:11]


def _channels(settings: dict) -> set:
    """这台机器现在开着哪几个回传通道。push() 里 chans 的取值来源同此。"""
    out = set()
    if sheet_webhook_url(settings):
        out.add("sheet")
    if webhook_url(settings):
        out.add("group")
    return out


def _read_backfill_mark() -> set | None:
    """补过历史的标记。返回「已经补进过哪几个通道」，None = 从没补过。

    ⚠ 1.1.15/1.1.16 的标记里只有一个条数（"23\n"），认不出通道。那两版
      **表格通道其实一次都没打开过**（地址收不到，见 _bundled_sheet_webhook），
      所以老标记一律当成「只补过群」—— 这样升上来会把历史往表格补一次，
      正是我们要的。个别用完整安装包、当时确实补进过表格的人会重复一次，
      收集端按运行ID去重（见文件头，那是硬要求），没有影响。
    """
    try:
        raw = user_path("output", BACKFILL_MARK).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return set()
    try:
        d = json.loads(raw)
    except ValueError:
        return {"group"}
    if isinstance(d, dict):
        return {str(c) for c in (d.get("chans") or [])}
    return {"group"}


def backfill(settings: dict) -> int:
    """把本机 usage.jsonl 里的历史运行补进发件箱，返回补了几条。

    ⚠ 为什么值得补：新表的粒度是「一次运行」，而收集端归档里的历史是**每人每周的
      累计**，粒度对不上，硬填进去只能编。但每台机器上的 usage.jsonl **本来就是
      一次运行一条**，是真的明细 —— 升级那一下把它补上去，新表就直接有了完整历史，
      不用拿周累计凑数。
    ⚠ 补的单位是**通道**，不是「一台机器只补一次」。1.1.16 踩过：那时候标记一落，
      backfill 就永远返回 0；而当时表格通道压根没开，那 23 条历史只进了群，
      发件箱随即被清空（push 里 want 只有 group，发完就划掉）—— 于是等表格通道
      修好，历史已经永久地捞不回来了。所以这里记的是「补进过哪些通道」，
      新开一个通道就为它再补一趟，已经补过的通道预先标成已发、不会重发。
    """
    if not enabled(settings):
        return 0
    want = _channels(settings)
    done = _read_backfill_mark()
    already = set() if done is None else done
    if done is not None and not (want - already):
        return 0
    try:
        from . import usage

        rows = [r for r in usage._read_file(usage.local_path())
                if isinstance(r, dict) and r.get("event") == "run_finished"]
        have = {(e.get("d") or {}).get("run") for e in _read_outbox()}
        # 已经补进过的通道预先记成「发过了」，push 只会补新开的那个通道 ——
        # 不这么做的话，群里会把全部历史再刷一遍。
        sent_already = sorted(already & want)
        added = []
        for r in rows:
            d = run_payload(r)
            d["run"] = d.get("run") or _legacy_run_id(r)
            d["来源"] = "补历史"
            if d["run"] in have:
                continue
            have.add(d["run"])
            added.append({"d": d, "s": list(sent_already)})
        if added:
            p = outbox_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                for e in added:
                    fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        mark = user_path("output", BACKFILL_MARK)
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(json.dumps({"n": len(added), "chans": sorted(already | want)},
                                   ensure_ascii=False) + "\n", encoding="utf-8")
        if added:
            log.info("补历史：把本机 %d 次历史运行放进了发件箱（这趟补的通道：%s）",
                     len(added), "、".join(sorted(want - already)) or "全部")
        return len(added)
    except Exception:
        log.warning("补历史失败（不影响运行，下次开程序再试）", exc_info=True)
        return 0


def _read_outbox() -> list[dict]:
    out = []
    try:
        p = outbox_path()
        if not p.exists():
            return out
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue          # 半行/乱码，跳过就是了
            if not isinstance(d, dict):
                continue
            # ⚠ 1.1.14 的发件箱里存的是**裸 payload**，没有 {"d":…,"s":[…]} 这层壳。
            #   升级上来时文件里可能两种混着，都得认 —— 认不出来就等于把那几条丢了。
            out.append(d if "d" in d else {"d": d, "s": []})
    except OSError:
        log.warning("回传发件箱读不了", exc_info=True)
    return out[-OUTBOX_MAX:]


def _write_outbox(rows: list[dict]):
    try:
        p = outbox_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                               for r in rows[-OUTBOX_MAX:]), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        log.warning("回传发件箱写不回去（下次会重发，不丢数据）", exc_info=True)


def pending(settings: dict) -> int:
    """还有几次运行没发出去。首页拿它提醒人 —— 静默失败是最难发现的那种坏。"""
    if not enabled(settings):
        return 0
    try:
        return len(_read_outbox())
    except Exception:
        return 0


def _batches(rows: list[dict]) -> list[list[dict]]:
    """把待发的按字节数打包。一条一条发的话，攒了几十条时会把群刷屏、
    还可能撞上群机器人的频率限制。"""
    out, cur = [], []
    for r in rows:
        cand = cur + [r]
        text = json.dumps(cand[0] if len(cand) == 1 else cand, ensure_ascii=False)
        if cur and len(text.encode("utf-8")) > MAX_BYTES:
            out.append(cur)
            cur = [r]
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


def _ms(text: str) -> str:
    """「2026-09-09 14:30:00」→ 毫秒时间戳字符串。表格的日期字段只认这个。

    ⚠ 和 CLI 那套不一样（CLI 收的是可读日期串），别混用。解析不了就返回空串，
      让那一格空着 —— 为一个时间戳把整条统计丢掉不值当。
    """
    from datetime import datetime

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return str(int(datetime.strptime(str(text or "").strip(), fmt).timestamp() * 1000))
        except ValueError:
            continue
    return ""


def _sheet_body(payloads: list[dict]) -> dict:
    """一批运行 → 智能表格 webhook 的报文。

    ⚠ 只发 SHEET_FIELDS 里认得的列。表里没有的字段（比如「范围」「重跑」）直接丢掉，
      不是错 —— 表结构是收集侧的事，客户端不该因为多一个字段就发不出去。
    """
    recs = []
    for d in payloads:
        vals = {}
        for name, fid in SHEET_FIELDS.items():
            key = SHEET_KEY.get(name, name)
            if name == "时间":
                v = _ms(d.get(key))
            elif name == "失败明细":
                v = json.dumps(d.get(key), ensure_ascii=False) if d.get(key) else ""
            else:
                v = d.get(key)
            if v is None or v == "":
                continue
            vals[fid] = v
        if vals:
            recs.append({"values": vals})
    return {"add_records": recs}


def _post_sheet(url: str, payloads: list[dict]):
    """往智能表格写一批。失败抛异常，由 push 决定留不留。"""
    body = _sheet_body(payloads)
    if not body["add_records"]:
        return
    res = _post_json(url, body, SHEET_TIMEOUT)
    if res.get("errcode") not in (0, None):
        raise RuntimeError(f"表格返回 {res.get('errcode')}：{res.get('errmsg')}")


SHEET_DIAG_MARK = "sheet-diag.txt"   # 今天已经往群里报过「表格通道有问题」了


def _scrub(text: str, limit: int = 120) -> str:
    """错误原文进群之前去掉地址和 key —— 表格写入 key 不能出现在群消息里。"""
    s = re.sub(r"https?://\S+", "<地址>", str(text or ""))
    s = re.sub(r"key=[\w\-]+", "key=***", s)
    return s.replace("\n", " ")[:limit]


def _sheet_diag(settings: dict, group_url: str, why: str, left: int):
    """表格通道不通时，往群里补一句「为什么」，每台机器每天最多一句。

    ⚠ 为什么需要：两个通道各记各的账，群那条发成功、表格那条失败时，
      群里照常看得到这次运行，表格里却一直没有 —— 失败原因只写在那台机器自己的
      output/run.log 里，谁也看不到。1.1.18 线上就是这样：一台机器 4 次运行进了群、
      表里一条没有，查不出原因。
    ⚠ v=2 的消息收集端（tools/collect_usage.py）会整条跳过，不会被当成运行数据。
    """
    if not group_url:
        return
    from datetime import date

    today = date.today().isoformat()
    mark = user_path("output", SHEET_DIAG_MARK)
    try:
        if mark.read_text(encoding="utf-8").strip() == today:
            return
    except OSError:
        pass
    try:
        from . import __version__, usage

        msg = {"v": 2, "类型": "(回传诊断)", "指纹": usage._uid(), "版本": __version__,
               "表格": _scrub(why), "表格待补": left}
        _post(group_url, json.dumps(msg, ensure_ascii=False))
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(today, encoding="utf-8")
    except Exception:
        log.info("回传诊断没发出去", exc_info=True)


def _send_sheet_batch(sheet_url: str, batch: list[dict]) -> tuple[int, str]:
    """发一批到表格，返回 (被表格拒掉而放弃的条数, 错误)。整批都没发出去就抛。

    ⚠ 一条坏数据不能把整个发件箱卡死：原来一批里只要有一条被表格拒掉，
      整批就留着下次重发，下次还是这一批、还是被拒 —— 这台机器之后的
      所有运行都永远进不了表。所以整批被拒时拆开一条一条发：
        · 有的成、有的被拒 → 被拒的那几条是坏数据，放弃表格这一路（群里已经有），
          标上 sheet_rejected 留个记录；
        · 一条都发不出去 → 不是数据的问题（key 失效、限流、断网），整批留着下次补。
    """
    try:
        _post_sheet(sheet_url, [e["d"] for e in batch])
        for e in batch:
            e.setdefault("s", []).append("sheet")
        return 0, ""
    except Exception as ex:
        if len(batch) == 1 or not str(ex).startswith("表格返回"):
            raise
        whole = ex

    ok, bad = [], []
    for e in batch:
        try:
            _post_sheet(sheet_url, [e["d"]])
            ok.append(e)
        except Exception as ex:
            bad.append((e, ex))
    if not ok:
        raise whole
    for e in ok:
        e.setdefault("s", []).append("sheet")
    for e, ex in bad:
        e.setdefault("s", []).append("sheet")
        e["sheet_rejected"] = _scrub(ex)
        log.warning("表格拒收了运行 %s，放弃这一条的表格回传（群里有）：%s",
                    (e.get("d") or {}).get("run"), ex)
    return len(bad), f"表格拒收 {len(bad)} 条：{_scrub(bad[0][1])}"


def push(settings: dict, form_names=None, nickname: str = "") -> dict:
    """把发件箱里还没发出去的运行发掉，返回 {sent, failed, error}。

    ⚠ 两个通道**各记各的账**（条目里的 "s" 记着已经发成功的通道）：表格写成功、
      群发失败时，下次只补群那一条 —— 不然重发会在表里多出一行重复记录，
      而群那边重发是无害的（收集端按 run 去重）。两个都发成功了才划掉。
    ⚠ 一批里有一条失败就整批留着：宁可重发，不可丢。
    form_names / nickname 是老签名留下的，现在用不上。
    """
    group_url = webhook_url(settings)
    sheet_url = sheet_webhook_url(settings)
    if not group_url and not sheet_url:
        return {"sent": 0, "failed": 0, "error": "没配回传地址"}

    entries = _read_outbox()
    if not entries:
        return {"sent": 0, "failed": 0, "error": ""}

    chans = []
    rejected = {"n": 0, "why": ""}
    if sheet_url:
        # 表格是主通道，先发：它是有结构的那份，群里那条只是给人扫一眼
        def send_sheet(b):
            n, why = _send_sheet_batch(sheet_url, b)
            if n:
                rejected["n"] += n
                rejected["why"] = rejected["why"] or why

        chans.append(("sheet", send_sheet,
                      lambda rows: [rows[i:i + SHEET_MAX_ROWS]
                                    for i in range(0, len(rows), SHEET_MAX_ROWS)]))
    if group_url:
        chans.append(("group",
                      lambda b: _post(group_url, json.dumps(
                          b[0]["d"] if len(b) == 1 else [e["d"] for e in b],
                          ensure_ascii=False)),
                      lambda rows: _batches(rows)))

    bad, first_err, sheet_err = 0, "", ""
    for name, send, split in chans:
        todo = [e for e in entries if name not in (e.get("s") or [])]
        for batch in split(todo):
            if not batch:
                continue
            try:
                send(batch)
                for e in batch:
                    if name not in e.setdefault("s", []):
                        e["s"].append(name)
            except Exception as ex:
                bad += len(batch)
                first_err = first_err or f"{'表格' if name == 'sheet' else '群'}：{ex}"
                if name == "sheet":
                    sheet_err = sheet_err or f"{type(ex).__name__}：{ex}"
                log.warning("回传到%s失败，%d 条留着下次补",
                            "表格" if name == "sheet" else "群", len(batch), exc_info=True)

    want = {name for name, _, _ in chans}
    left = [e for e in entries if not want.issubset(set(e.get("s") or []))]
    sent = len(entries) - len(left)
    _write_outbox(left)
    if sent:
        log.info("回传已发 %d 次运行（通道：%s）", sent, "、".join(sorted(want)))

    # 表格这一路有问题：往群里补一句为什么（每天最多一句），不然只有本机日志知道
    sheet_left = sum(1 for e in left if "sheet" not in (e.get("s") or []))
    if not sheet_url:
        # ⚠ 从源码跑（git clone 下来 python main.py）是最常见的情形：群地址 config/webhook.txt
        #   在仓库里、表格地址 src/_bundled.py 故意不进仓库 —— 于是群里有、表里永远没有。
        _sheet_diag(settings, group_url,
                    "没有表格回传地址：" + ("安装目录里缺 src/_bundled.py" if FROZEN else
                                   "从源码运行（表格地址只在打包时生成），"
                                   "在 config/sheet_webhook.txt 里填上地址即可"), 0)
    elif sheet_err:
        _sheet_diag(settings, group_url, sheet_err, sheet_left)
    elif rejected["n"]:
        _sheet_diag(settings, group_url, rejected["why"], 0)
    return {"sent": sent, "failed": bad, "error": first_err}
