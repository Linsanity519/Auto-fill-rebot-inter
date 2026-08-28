"""把一份「本地已跑通」的自制工作流送去审核。

两条渠道：

  1. 企微 webhook（默认，零配置）—— 发一条：谁、工具版本、几步、跑通摘要。
     完整 json 若塞得下就一并发；塞不下就只发摘要 + 本地包路径。
  2. 推 GitHub 分支 `selfmade/<名>`（配了 FLOW_REVIEW_GITHUB / FLOW_REVIEW_TOKEN 才走）
     —— 走 GitHub Contents API，不需要客户端装 git。
     ⚠ 只往 selfmade/* 分支写，从不碰主干。把 main 设成受保护分支，
       token 就算泄露也顶多多几个垃圾分支，能撤、能回滚。

不管哪条，都先在本地留一份 output/flow-review/<名>-<时间>.json。
审核不成功绝不能挡住「收编」——这里任何异常都只记日志、返回 False。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime

from . import report
from .paths import user_path

log = logging.getLogger(__name__)

TIMEOUT = 6


def _pack(flow: dict, result_csv: str, version: str, uid: str) -> dict:
    return {
        "name": flow.get("name", ""),
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "by": uid,
        "tool_version": version,
        "status": flow.get("status", ""),
        "steps": len(flow.get("steps") or []),
        "flow": flow,
        "last_run_result": result_csv[:4000] if result_csv else "",
    }


def _save_local(pack: dict) -> str:
    d = user_path("output", "flow-review")
    d.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in pack["name"] if c not in r':\/?*[]<>|"').strip() or "flow"
    p = d / f"{safe}-{datetime.now():%Y%m%d-%H%M%S}.json"
    p.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------- 企微
def _to_wecom(settings: dict, pack: dict, local_path: str) -> bool:
    body = json.dumps(pack["flow"], ensure_ascii=False, indent=2)
    lines = [
        "【配置助手 · 自制工作流待审核】",
        f"名称 {pack['name']}　{pack['steps']} 步　by {pack['by']}　工具 {pack['tool_version']}",
        f"本地包 {local_path}",
    ]
    if len((body + "\n".join(lines)).encode("utf-8")) < 1600:
        lines += ["——", body]
    else:
        lines += ["——", "（工作流较长，完整内容见本地包 / GitHub 分支）"]
    return report.send_feedback(settings, "\n".join(lines))


# ---------------------------------------------------------------- GitHub
def _gh_conf(settings: dict) -> tuple[str, str]:
    repo = (os.environ.get("FLOW_REVIEW_GITHUB")
            or ((settings.get("usage") or {}).get("flow_review_github") or "")).strip()
    token = (os.environ.get("FLOW_REVIEW_TOKEN")
             or ((settings.get("usage") or {}).get("flow_review_token") or "")).strip()
    return repo, token


def _gh(url: str, token: str, method: str = "GET", payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ConfigAssistant-FlowReview",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _to_github(repo: str, token: str, pack: dict) -> str:
    """在 selfmade/<名> 分支上写 config/flows/<名>.json，返回分支的网页地址。"""
    safe = "".join(c for c in pack["name"] if c.isalnum() or c in "-_") or "flow"
    branch = f"selfmade/{safe}"
    base = f"https://api.github.com/repos/{repo}"

    head = _gh(f"{base}", token)                       # 拿默认分支名
    default = head.get("default_branch", "main")
    ref = _gh(f"{base}/git/ref/heads/{default}", token)
    sha = ref["object"]["sha"]

    try:
        _gh(f"{base}/git/refs", token, "POST",
            {"ref": f"refs/heads/{branch}", "sha": sha})
    except urllib.error.HTTPError as e:
        if e.code != 422:                              # 422 = 分支已存在，覆盖写就行
            raise

    path = f"config/flows/{safe}.json"
    content = base64.b64encode(
        json.dumps(pack["flow"], ensure_ascii=False, indent=2).encode("utf-8")).decode()
    existing = ""
    try:
        cur = _gh(f"{base}/contents/{path}?ref={branch}", token)
        existing = cur.get("sha", "")
    except urllib.error.HTTPError:
        pass
    put = {"message": f"自制工作流：{pack['name']}（{pack['by']} @ {pack['tool_version']}）",
           "content": content, "branch": branch}
    if existing:
        put["sha"] = existing
    _gh(f"{base}/contents/{path}", token, "PUT", put)
    return f"https://github.com/{repo}/tree/{branch}"


# ---------------------------------------------------------------- 对外
def submit(settings: dict, flow: dict, result_csv: str = "") -> dict:
    """→ {ok, where, local, url, error}。"""
    from . import usage
    pack = _pack(flow, result_csv, usage._app_version(), usage._uid())
    local = ""
    try:
        local = _save_local(pack)
    except Exception:
        log.warning("自制工作流本地留档失败", exc_info=True)

    repo, token = _gh_conf(settings)
    if repo and token:
        try:
            url = _to_github(repo, token, pack)
            report.send_feedback(settings, "【配置助手 · 自制工作流待审核】\n"
                                 f"{pack['name']}　{pack['steps']} 步\n{url}")
            return {"ok": True, "where": "github", "local": local, "url": url}
        except Exception as e:
            log.warning("推 GitHub 分支失败，退回企微", exc_info=True)
            err = str(e)

    try:
        ok = _to_wecom(settings, pack, local)
        return {"ok": bool(ok), "where": "wecom", "local": local, "url": "",
                "error": "" if ok else "企微没发出去"}
    except Exception as e:
        log.exception("送审失败")
        return {"ok": False, "where": "", "local": local, "url": "", "error": str(e)}
