"""settings.yaml 的默认值兜底。

⚠ 为什么需要这个：同事从老版本升上来时，他们机器上那份 settings.yaml 是老版本
  写的 —— 比如 1.0.6 的那份就没有 update: 段。而安装包为了不覆盖用户自己改过的
  配置，用的是 onlyifdoesntexist，压根不会替换它。结果会是最难查的那种故障：
  **升上来的人反而永远收不到下一次更新**，而且一点报错都没有。

  做法和 src/ad_prep.py 的 load() 一模一样：defaults 打底，再把用户的值盖上去。
  以后往 settings.yaml 里加任何字段，老用户都能自动补上，不用挨个让人重填。

默认值来自 assets/settings.default.yaml —— build.bat 打包时从 config/settings.yaml
原样拷过去，保证两边不会走样。开发时这个文件可能不存在，那就跳过兜底。
"""
from __future__ import annotations

import logging

import yaml

from .paths import resource

log = logging.getLogger(__name__)


def _merge(base: dict, over: dict) -> dict:
    """递归合并，用户的值优先。只有用户**没写这个键**时才用默认值。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def defaults() -> dict:
    p = resource("assets", "settings.default.yaml")
    if not p:
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        log.warning("默认配置读取失败，跳过兜底：%s", p, exc_info=True)
        return {}


def apply_defaults(s: dict | None) -> dict:
    """给用户的 settings 补上新版本才有的字段。兜底失败绝不能挡住程序启动。"""
    user = s or {}
    base = defaults()
    return _merge(base, user) if base else user
