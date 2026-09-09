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
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

from .paths import user_path

log = logging.getLogger(__name__)

TIMEOUT = 3          # 内网偶尔抽风，三秒不通就算了，下次再补
MAX_BYTES = 1800     # 企微 text 消息上限 2048 字节，留点余量
OUTBOX_FILE = "usage-outbox.jsonl"    # 还没发出去的运行，一行一条
OUTBOX_MAX = 500     # 发件箱最多留这么多条。长期连不上网时别让它无限涨

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


def enabled(settings: dict) -> bool:
    return bool(webhook_url(settings))


def _payload(header: list, row: list, form_names, extra: dict | None = None,
             runs_by_form: dict | None = None) -> dict:
    """一行（列顺序见 usage.report_header）→ 发出去的那个 JSON。

    ⚠ 发的是「本机到目前为止的累计」，不是增量。收集端只取每人最近一条就够，
      重复发同一周无害（幂等）。这是整条链路能容忍丢消息的根本原因。

    ⚠ **不发「周」**：它是可推的 —— 「最后活跃」就是那一周桶里最大的那个
      时间戳（见 usage.weekly_buckets），收集端 week_of 一下就还原了。
      群里那条消息本来就短，少一个能算出来的字段就少一份噪音。
      收集端两种消息都认（老消息还带着「周」），见 tools/collect_usage.py 的 _week。
    ⚠ **不发「花名」**：实际上没人填，发出去的一直是空字符串；而且它是真人名字，
      少发一处就少一处露出。本机那一列还留着（表结构没动），只是不出机器。
    """
    d = dict(zip(header, row))
    forms = {n: d[n] for n in form_names if str(d.get(n, "")).strip() not in ("", "0")}
    out = {
        "指纹": d.get("指纹", ""),
        "版本": d.get("版本", ""),
        "次数": d.get("运行次数", 0),
        "成功": d.get("成功", 0),
        "失败": d.get("失败", 0),
        "机器秒": d.get("机器代劳秒", 0),
        "最后活跃": d.get("最后活跃", ""),
        "分类型": forms,
    }
    # ⚠ 「分类型」记的是**成功条数**，全失败的那个类型在里面是 0、上面那行就把它
    #   丢掉了 —— 结果是「今天跑的是资源位投放、一条没成」回传上来还是只有昨天的
    #   「常规商广 27」，看着像回传坏了、每次都发同一份。所以再带一段「分类型跑了」
    #   ={类型: 跑了几次}，跟成功与否无关，专门回答「这周动过哪几个配置类型」。
    #   收集端不认这个键也无害（正表不受影响），见 tools/collect_usage.py 的 _merge。
    if runs_by_form:
        out["分类型跑了"] = {k: v for k, v in runs_by_form.items() if _num_ok(v)}

    # 失败明细（fail_kinds / fail_fields，全是定长枚举 + 字段名，无业务值）。
    # 有就带上，没有就不占位。
    if extra:
        out["失败明细"] = extra

    def _too_big() -> bool:
        return len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > MAX_BYTES

    # ⚠ 顶到长度上限时按重要性依次丢：失败明细 < 分类型明细 < 总数。
    #   总数（次数/成功/失败/秒）永远发得出去。
    if _too_big() and out.get("分类型跑了"):
        out.pop("分类型跑了", None)
        log.warning("上报内容超长，这一条不带「分类型跑了」")
    if _too_big() and out.get("失败明细"):
        out["失败明细"] = {}
        log.warning("上报内容超长，这一条不带失败明细")
    if _too_big():
        out["分类型"] = {}
        log.warning("上报内容超长，这一条只发总数不发分类型明细")
    return out


def _num_ok(v) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


def _post(url: str, text: str) -> bool:
    body = json.dumps({"msgtype": "text", "text": {"content": text}}).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        res = json.loads(r.read().decode("utf-8", "replace") or "{}")
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
MODE_TEXT = {"dry": "空跑", "step": "逐条确认", "sample": "抽样确认", "auto": "全自动"}


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
            fh.write(json.dumps(run_payload(row), ensure_ascii=False) + "\n")
        return True
    except Exception:
        log.warning("回传发件箱写不进去（这一次运行的统计会丢）", exc_info=True)
        return False


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
            if isinstance(d, dict):
                out.append(d)
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


def push(settings: dict, form_names=None, nickname: str = "") -> dict:
    """把发件箱里还没发出去的运行发掉，返回 {sent, failed, error}。

    ⚠ 发成功的才划掉。划早了就等于把失败的那几条当成已上报、下次不再补 ——
      那正是老方案静默丢数据的成因。
    ⚠ 一批里有一条失败就整批留着下次重发：收集端按 run 去重，重发无害。
    form_names / nickname 是老签名留下的，现在用不上（回传里不带花名，
    分类型由收集端按「类型」自己聚）。
    """
    url = webhook_url(settings)
    if not url:
        return {"sent": 0, "failed": 0, "error": "没配 usage.webhook_url"}

    rows = _read_outbox()
    if not rows:
        return {"sent": 0, "failed": 0, "error": ""}

    left, sent, bad, first_err = [], 0, 0, ""
    for batch in _batches(rows):
        body = batch[0] if len(batch) == 1 else batch
        try:
            _post(url, json.dumps(body, ensure_ascii=False))
            sent += len(batch)
        except Exception as e:
            bad += len(batch)
            first_err = first_err or str(e)
            left.extend(batch)
            log.warning("回传失败，%d 条留着下次补", len(batch), exc_info=True)
    _write_outbox(left)
    if sent:
        log.info("回传已发 %d 次运行", sent)
    return {"sent": sent, "failed": bad, "error": first_err}
