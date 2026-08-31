"""把「策略中心」/「准备页」配好的那份参数分享给同事,或拉同事分享的过来。

策略配置存 `config/strategies/<配置类型>.json`,准备页存 `config/prep/<配置类型>.json`,
都是每台机器各存一份 —— 想给同事只能发文件。这里走和「自制工作流送审」同一条
GitHub 通道:写到分支 `shared-config/<kind>/<配置类型>`,**从不碰主干**。

没配 GitHub token 时降级:发一条企微,只说「谁分享了什么 + 本地文件路径」。

⚠ 拉取会**先把本地那份备份到 output/config-backup/** 再覆盖。
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime

from . import gh, report
from .paths import user_path

log = logging.getLogger(__name__)

# kind -> (取本地路径的函数, 仓库里的目录)
_KINDS = {
    "strategy": ("wizard_strategy", "config/strategies"),
    "prep": ("ad_prep", "config/prep"),
}
_KIND_CN = {"strategy": "策略中心", "prep": "准备页参数"}


def _safe(name: str) -> str:
    return "".join(c for c in str(name) if c.isalnum() or c in "-_") or "cfg"


def _local_path(kind: str, cfg: dict):
    mod_name, _ = _KINDS[kind]
    from importlib import import_module
    return import_module(f".{mod_name}", __package__).path_for(cfg)


def _branch(kind: str, form_name: str) -> str:
    return f"shared-config/{kind}/{_safe(form_name)}"


def _repo_path(kind: str, form_name: str) -> str:
    return f"{_KINDS[kind][1]}/{_safe(form_name)}.json"


def push(settings: dict, kind: str, cfg: dict, form_name: str) -> dict:
    """把本地这份分享出去。→ {ok, where, url, error}。"""
    if kind not in _KINDS:
        return {"ok": False, "error": f"不认识的类型：{kind}"}
    p = _local_path(kind, cfg)
    if not p.exists():
        return {"ok": False, "error": f"本地还没有{_KIND_CN[kind]}配置，先配一份再分享"}
    content = p.read_bytes()

    repo, token = gh.conf(settings)
    if repo and token:
        try:
            from . import usage
            branch = _branch(kind, form_name)
            gh.ensure_branch(repo, token, branch)
            url = gh.put_file(
                repo, token, branch, _repo_path(kind, form_name), content,
                f"分享{_KIND_CN[kind]}：{form_name}（{usage._uid()} @ {usage._app_version()}）")
            report.send_feedback(settings, "【配置助手 · 配置分享】\n"
                                 f"{_KIND_CN[kind]}：{form_name}\n{url}")
            return {"ok": True, "where": "github", "url": url}
        except Exception as e:
            log.warning("推 GitHub 分支失败，退回企微", exc_info=True)
            err = str(e)
    else:
        err = ""

    ok = report.send_feedback(settings, "\n".join([
        "【配置助手 · 配置分享】", f"{_KIND_CN[kind]}：{form_name}",
        f"本地文件：{p}", "（没配 GitHub token，只发了通知；文件需要手动给同事）"]))
    return {"ok": bool(ok), "where": "wecom", "url": "",
            "error": "" if ok else (err or "企微没发出去")}


def list_remote(settings: dict) -> dict:
    """同事分享出来的那些。→ {ok, items:[{kind, name, branch}], error}。"""
    repo, token = gh.conf(settings)
    if not (repo and token):
        return {"ok": False, "error": "没配 GitHub（CONFIG_SYNC_GITHUB / _TOKEN），列不了"}
    try:
        items = []
        for b in gh.list_branches(repo, token, "shared-config/"):
            parts = b["name"].split("/", 2)      # shared-config / kind / name
            if len(parts) == 3 and parts[1] in _KINDS:
                items.append({"kind": parts[1], "name": parts[2], "branch": b["name"]})
        return {"ok": True, "items": items}
    except Exception as e:
        log.exception("列远端配置失败")
        return {"ok": False, "error": str(e)}


def pull(settings: dict, kind: str, cfg: dict, form_name: str) -> dict:
    """拉同事分享的那份下来（覆盖前先备份本地）。→ {ok, backup, error}。"""
    if kind not in _KINDS:
        return {"ok": False, "error": f"不认识的类型：{kind}"}
    repo, token = gh.conf(settings)
    if not (repo and token):
        return {"ok": False, "error": "没配 GitHub，拉不了"}
    try:
        data = gh.get_file(repo, token, _branch(kind, form_name), _repo_path(kind, form_name))
        if data is None:
            return {"ok": False, "error": "远端没有这份分享"}
        p = _local_path(kind, cfg)
        backup = ""
        if p.exists():
            bdir = user_path("output", "config-backup")
            bdir.mkdir(parents=True, exist_ok=True)
            backup = str(bdir / f"{_safe(form_name)}-{kind}-{datetime.now():%Y%m%d-%H%M%S}.json")
            shutil.copyfile(p, backup)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return {"ok": True, "backup": backup, "path": str(p)}
    except Exception as e:
        log.exception("拉取配置失败")
        return {"ok": False, "error": str(e)}
