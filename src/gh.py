"""GitHub Contents API 的最小封装。`flow_review`(送审自制工作流) 和
`config_sync`(分享策略/准备页配置) 共用。

⚠ 只往**非主干分支**写(`selfmade/*` / `shared-config/*`),从不碰 main。
  把 main 设成受保护分支,token 就算泄露也顶多多几个垃圾分支,能撤能回滚。
⚠ 走 Contents API 而不是要客户端装 git。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

TIMEOUT = 6
_UA = "ConfigAssistant"


def conf(settings: dict | None) -> tuple[str, str]:
    """(repo, token)。环境变量优先,再退到 settings.yaml 的 usage 段。

    `CONFIG_SYNC_*` 和 `FLOW_REVIEW_*` 都认(通常是同一个仓库、同一把 token)。
    """
    u = (settings or {}).get("usage") or {}
    repo = (os.environ.get("CONFIG_SYNC_GITHUB") or os.environ.get("FLOW_REVIEW_GITHUB")
            or u.get("config_sync_github") or u.get("flow_review_github") or "").strip()
    token = (os.environ.get("CONFIG_SYNC_TOKEN") or os.environ.get("FLOW_REVIEW_TOKEN")
             or u.get("config_sync_token") or u.get("flow_review_token") or "").strip()
    return repo, token


def request(url: str, token: str, method: str = "GET", payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": _UA,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def default_branch(repo: str, token: str) -> str:
    return request(f"https://api.github.com/repos/{repo}", token).get("default_branch", "main")


def ensure_branch(repo: str, token: str, branch: str, from_branch: str | None = None) -> None:
    """建分支;已存在(422)就当成功。"""
    base = f"https://api.github.com/repos/{repo}"
    src = from_branch or default_branch(repo, token)
    sha = request(f"{base}/git/ref/heads/{src}", token)["object"]["sha"]
    try:
        request(f"{base}/git/refs", token, "POST",
                {"ref": f"refs/heads/{branch}", "sha": sha})
    except urllib.error.HTTPError as e:
        if e.code != 422:
            raise


def put_file(repo: str, token: str, branch: str, path: str, content: bytes,
             message: str) -> str:
    """在 branch 上写 path;返回分支网页地址。"""
    base = f"https://api.github.com/repos/{repo}"
    existing = ""
    try:
        existing = request(f"{base}/contents/{path}?ref={branch}", token).get("sha", "")
    except urllib.error.HTTPError:
        pass
    body = {"message": message, "branch": branch,
            "content": base64.b64encode(content).decode()}
    if existing:
        body["sha"] = existing
    request(f"{base}/contents/{path}", token, "PUT", body)
    return f"https://github.com/{repo}/tree/{branch}"


def get_file(repo: str, token: str, ref: str, path: str) -> bytes | None:
    """读 ref(分支/tag/sha) 上的 path;不存在返回 None。"""
    base = f"https://api.github.com/repos/{repo}"
    try:
        doc = request(f"{base}/contents/{path}?ref={ref}", token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    if doc.get("encoding") == "base64" and doc.get("content"):
        return base64.b64decode(doc["content"])
    return None


def list_branches(repo: str, token: str, prefix: str = "") -> list[dict]:
    """分支列表(可按前缀过滤)。每项 {name, commit_sha}。翻页取前 3 页够用。"""
    out = []
    base = f"https://api.github.com/repos/{repo}"
    for page in range(1, 4):
        rows = request(f"{base}/branches?per_page=100&page={page}", token)
        if not isinstance(rows, list) or not rows:
            break
        for b in rows:
            name = b.get("name", "")
            if not prefix or name.startswith(prefix):
                out.append({"name": name, "commit_sha": (b.get("commit") or {}).get("sha", "")})
        if len(rows) < 100:
            break
    return out
