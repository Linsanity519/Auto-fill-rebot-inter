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

    ⚠ 为什么不直接写在 settings.yaml 里：仓库已经公开，那个 key 不能进 git。
      但它又必须跟着分发包发出去（不然同事那边统计就是死的）。所以拆成一个
      **不进仓库、打包时注入**的单独文件：
        · 本机打包 → build.bat 从环境变量 USAGE_WEBHOOK_URL 或已有文件生成
        · CI 打包   → GitHub Actions 从 Secret 注入
      安装包用 ignoreversion 发它，所以老用户升级时也会被刷新 —— 这点很关键：
      升级不覆盖 settings.yaml（怕冲掉用户改的配置），如果地址只存在
      settings.yaml 里，从 1.0.6 升上来的人就永远是空的。
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
    """
    d = dict(zip(header, row))
    forms = {n: d[n] for n in form_names if str(d.get(n, "")).strip() not in ("", "0")}
    out = {
        "周": d.get("周", ""),
        "指纹": d.get("指纹", ""),
        "花名": d.get("花名", ""),
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
