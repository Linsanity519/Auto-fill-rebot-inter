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
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from .paths import user_path

log = logging.getLogger(__name__)

TIMEOUT = 3          # 内网偶尔抽风，三秒不通就算了，下次再补
MAX_BYTES = 1800     # 企微 text 消息上限 2048 字节，留点余量


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
    """
    p = user_path("config", "webhook.txt")
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
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


def _payload(header: list, row: list, form_names) -> dict:
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
    # ⚠ 配置类型多了会顶到长度上限。宁可丢掉明细也要把总数发出去。
    if len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
        out["分类型"] = {}
        log.warning("上报内容超长，这一条只发总数不发分类型明细")
    return out


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


def push(settings: dict, form_names, nickname: str = "") -> dict:
    """把「还没成功发出去的那几周」发一遍，返回 {sent, failed, error}。

    ⚠ 只有真发出去了才记账（usage.mark_reported）。记早了就会把失败的周
      当成已上报、下次不再补 —— 那正是老方案静默丢数据的成因。
    ⚠ 一周一条，分开发：其中一条失败不连累其它周。
    """
    from . import usage

    url = webhook_url(settings)
    if not url:
        return {"sent": 0, "failed": 0, "error": "没配 usage.webhook_url"}

    header = usage.report_header(form_names)
    rows = usage.report_rows(settings, form_names, nickname=nickname)
    if not rows:
        return {"sent": 0, "failed": 0, "error": ""}

    ok, bad, first_err = [], 0, ""
    for row in rows:
        line = json.dumps(_payload(header, row, form_names), ensure_ascii=False)
        try:
            _post(url, line)
            ok.append(row)
        except Exception as e:
            bad += 1
            first_err = first_err or str(e)
            log.warning("第 %s 周上报失败（下次会补）", row[0], exc_info=True)

    if ok:
        usage.mark_reported(ok)
        log.info("统计已上报 %d 周（%s）", len(ok), "、".join(r[0] for r in ok))
    return {"sent": len(ok), "failed": bad, "error": first_err}
