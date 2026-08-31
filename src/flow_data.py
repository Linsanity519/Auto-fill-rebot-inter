"""自制配置类型（mode: flow）的定义：一份录下来、可编辑的步骤图。

存 `config/flows/<名>.json`（是图不是字段表，所以不走 config/forms/*.yaml）。
录制器（src/flow_record.py）产出草稿，整理页让人编辑，本地跑通了才收编。

## 步骤词汇表

参照 Automa 的 block 集，砍到这个场景够用：

  goto        打开 / 跳转页面
  click       点一个元素（submit:true 的是提交动作，空跑时跳过）
  fill        往输入框写值
  select      选下拉
  press       敲键（Enter / Escape）
  wait_for    等某个元素出现
  wait_text   等某段文字出现
  assert      校验：某文字在 / 某元素没了 / URL 匹配
  screenshot  截一张进结果目录
  confirm     停下，等人在浏览器里核对后继续
  loop_rows   把 body 里的步骤按 Excel 每行跑一遍，{{列名}} 绑进去

## 选择器候选（pick）

一个 pick 是一串候选，跑的时候按顺序试，命中哪个记哪个。稳→脆：

  {"text": "新建"}                 元素可见文字（穿透子节点）
  {"role": "button", "name": "新建"}  无障碍名
  {"label": "单元名称"}            label 文字 → 它管的字段块 → 块里的控件
  {"attr": "data-testid=submit"}   稳定属性 / id
  {"css": "form > div:nth-child(3) input"}   ⚠ 只当最后兜底

⚠ 硬约定 #2：不用编译哈希类名。录制器只把 css 当兜底，validate 对
  「一步只有 css 候选」标黄。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from .paths import user_path

log = logging.getLogger(__name__)

OPS = {"goto", "click", "fill", "select", "press", "wait_for", "wait_text",
       "assert", "screenshot", "confirm", "loop_rows"}
PICK_KEYS = {"text", "role", "name", "label", "attr", "css"}

_VAR = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")
_BAD_CHARS = r':\/?*[]<>|' + '"'
GROUP = "自制配置类型"


def _safe_stem(name: str) -> str:
    return "".join(c for c in str(name) if c not in _BAD_CHARS).strip() or "未命名"


def path_for(name: str):
    return user_path("config", "flows", f"{_safe_stem(name)}.json")


def flows_dir():
    return user_path("config", "flows")


def exists(name: str) -> bool:
    return path_for(name).exists()


# ---------------------------------------------------------------- 读写
def _defaults(doc: dict) -> dict:
    doc = dict(doc or {})
    doc.setdefault("mode", "flow")
    doc.setdefault("version", 1)
    doc.setdefault("status", "draft")        # draft → tested → submitted → adopted
    doc.setdefault("source_url", "")
    doc.setdefault("created_by", "")
    doc.setdefault("created_at", "")
    doc.setdefault("steps", [])
    d = dict(doc.get("data") or {})
    d.setdefault("source", "none")            # none | excel
    d.setdefault("columns", [])
    d["columns"] = [str(c).strip() for c in (d.get("columns") or []) if str(c).strip()]
    doc["data"] = d
    return doc


def load(name: str) -> dict:
    p = path_for(name)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("自制工作流读不了：%s", p, exc_info=True)
        doc = {}
    doc = _defaults(doc)
    doc["name"] = doc.get("name") or name
    return doc


def save(doc: dict) -> str:
    doc = _defaults(doc)
    name = str(doc.get("name") or "").strip()
    if not name:
        raise ValueError("工作流没有名字")
    doc["name"] = name
    if not doc.get("created_at"):
        doc["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p = path_for(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


def list_all() -> list[tuple[str, dict]]:
    """[(名, synthetic_cfg), ...]，按文件名排序。webapp.list_forms() 会接上这份。"""
    out = []
    d = flows_dir()
    if not d.exists():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            out.append((p.stem, synthetic_cfg(load(p.stem))))
        except Exception:
            log.warning("自制工作流解析失败：%s", p, exc_info=True)
    return out


# ---------------------------------------------------------------- 给界面用的「假 cfg」
def has_loop(doc: dict) -> bool:
    return any(s.get("op") == "loop_rows" for s in (doc.get("steps") or []))


def synthetic_cfg(doc: dict) -> dict:
    """把 flow json 包装成 webapp/registry 认得的那种 cfg。

    ⚠ 它长得像 config/forms 的 cfg，但 mode 恒为 flow、多一个 _flow 原件。
      _caps() 给它算出来的能力是固定的一小套（见 webapp）。
    """
    doc = _defaults(doc)
    eats_excel = doc["data"]["source"] == "excel" or has_loop(doc)
    st = doc.get("status", "draft")
    tail = {"draft": "（草稿）", "tested": "（本地已跑通 · 待审核）",
            "submitted": "（已提交审核）", "adopted": "（已采纳到正式配置）"}.get(st, "")
    return {
        "name": doc["name"],
        "mode": "flow",
        "flow": True,               # 界面按这个（而不是 mode 名）显示自制配置那张卡
        "description": f"自己录的助手 {tail}".strip(),
        "data_source": "excel" if eats_excel else "none",
        "nav": {"group": GROUP, "group_order": 90,
                "label": doc["name"], "order": 1},
        "ui": {"run_kind": "fill"},
        "_flow": doc,
    }


# ---------------------------------------------------------------- 变量
def refs_in(text) -> list[str]:
    return _VAR.findall(str(text or ""))


def all_refs(doc: dict) -> set[str]:
    seen = set()

    def walk(steps):
        for s in steps or []:
            for k in ("url", "value", "text", "note"):
                seen.update(refs_in(s.get(k)))
            if s.get("op") == "loop_rows":
                walk(s.get("body"))

    walk(doc.get("steps"))
    return seen


def render(text: str, row: dict, source_url: str = "") -> str:
    """把 {{列名}} 换成这一行的值；{{source_url}} 是特殊变量。"""
    def sub(m):
        key = m.group(1).strip()
        if key == "source_url":
            return source_url
        return str(row.get(key, m.group(0)))
    return _VAR.sub(sub, str(text or ""))


def columns(doc: dict) -> list[str]:
    """这个工作流要 Excel 的哪几列：显式 data.columns 优先，否则从 {{}} 扫。"""
    cols = list((doc.get("data") or {}).get("columns") or [])
    if cols:
        return cols
    scanned = [r for r in sorted(all_refs(doc)) if r != "source_url"]
    return scanned


# ---------------------------------------------------------------- 校验（离线）
#
# 两档，别混：
#   validate() —— **硬伤**：结构上跑不起来（没选择器 / 没值 / 没 url / 循环空 …）。
#                 非空就不给「本地试跑」「送审」。
#   warnings() —— **提醒**：能跑，但脆或不够稳（某步只有 css 兜底 / 全程没 confirm）。
#                 只提示，绝不挡运行 —— 录下来的流程本来就该能复原、能重复，
#                 「改版可能失效」是所有前端自动化的通病，不是这条流程的结构问题。
def validate(doc: dict, rows: list[dict] | None = None) -> list[str]:
    """结构硬伤清单。空 = 至少能跑起来。"""
    doc = _defaults(doc)
    issues: list[str] = []
    steps = doc.get("steps") or []
    if not isinstance(steps, list) or not steps:
        return ["这个工作流一步都没有"]

    def check(steps, path=""):
        for i, s in enumerate(steps, 1):
            where = f"{path}第 {i} 步"
            op = s.get("op")
            if op not in OPS:
                issues.append(f"{where}：不认识的动作「{op}」")
                continue
            if op in ("click", "fill", "select", "wait_for"):
                pick = s.get("pick") or []
                if not isinstance(pick, list) or not pick:
                    issues.append(f"{where}：{op} 没有选择器（pick）")
                elif not any([k for k in c if k in PICK_KEYS] for c in pick):
                    issues.append(f"{where}：选择器候选都是空的 / 不认识")
            if op in ("fill", "select") and not str(s.get("value", "")):
                issues.append(f"{where}：{op} 没有要填的值")
            if op == "goto" and not str(s.get("url", "")):
                issues.append(f"{where}：goto 没有 url")
            if op == "wait_text" and not str(s.get("text", "")):
                issues.append(f"{where}：wait_text 没有要等的文字")
            if op == "assert" and not (s.get("text") or s.get("gone") or s.get("url_matches")):
                issues.append(f"{where}：assert 没说要校验什么")
            if op == "loop_rows":
                body = s.get("body") or []
                if not body:
                    issues.append(f"{where}：loop_rows 里面是空的")
                check(body, path=f"{where} 里 ")

    check(steps)

    biz_refs = {r for r in all_refs(doc) if r != "source_url"}
    cols = set((doc.get("data") or {}).get("columns") or []) or set(columns(doc))
    missing = biz_refs - cols
    if missing:
        issues.append(f"用到了列 {sorted(missing)}，但没在某一步标成「按表格」")
    if biz_refs and not has_loop(doc):
        issues.append("有格子标了「按表格」，但没勾「用 Excel 一行一遍地跑」")
    if doc["data"]["source"] == "none" and biz_refs:
        issues.append("绑了 Excel 列，但「数据来源」是「无」")
    if rows is not None and has_loop(doc):
        have = set(rows[0].keys()) if rows else set()
        lack = cols - have
        if lack:
            issues.append(f"Excel 里缺这几列：{sorted(lack)}")
    return issues


def warnings(doc: dict) -> list[str]:
    """提醒清单。**不挡运行** —— 只是让人知道哪里脆、哪里可能想补一手。"""
    doc = _defaults(doc)
    out: list[str] = []
    n_confirm = n_submit = 0

    def walk(steps, path=""):
        nonlocal n_confirm, n_submit
        for i, s in enumerate(steps, 1):
            where = f"{path}第 {i} 步"
            op = s.get("op")
            if op == "confirm":
                n_confirm += 1
            if op == "click" and s.get("submit"):
                n_submit += 1
            if op in ("click", "fill", "select", "wait_for"):
                pick = s.get("pick") or []
                kinds, anchored = [], False
                for c in pick:
                    kinds += [k for k in c if k in PICK_KEYS]
                    if "css" in c and c.get("anchored"):
                        anchored = True
                if kinds and set(kinds) <= {"css"} and not anchored:
                    out.append(f"{where}：选择器在页面上定位不唯一，跑的时候取第一个 —— 可能不是你要的")
            if op == "loop_rows":
                walk(s.get("body") or [], f"{where} 里 ")

    walk(doc.get("steps") or [])
    if n_confirm == 0 and n_submit > 0:
        out.append("全程没有「停下核对」——真正跑（非试跑）时不会给你确认的机会，"
                   "建议在提交动作前插一个「停下让人核对」")
    return out


def describe(doc: dict) -> str:
    doc = _defaults(doc)
    n = len(doc.get("steps") or [])
    loop = "，按 Excel 行循环" if has_loop(doc) else ""
    return f"{n} 步{loop}"
