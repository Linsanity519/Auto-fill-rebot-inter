"""选择器体检：把 form yaml 里声明的定位目标,拿到真实页面上点一遍名。

    python tools\\health_check.py                    体检当前页(对所有 form 试一遍)
    python tools\\health_check.py 价格策略批量开关     只体检一个,针对当前页
    python tools\\health_check.py --goto              逐个 goto 各 form 的 form_url 再体检
    python tools\\health_check.py --json              机读输出,给发版流程用

## 和 check_mode.py 的分工

`check_mode.py` 查**接线**(yaml 键、registry、caps),不开浏览器。
这个查**选择器**:label 文字还在不在、按钮结构变没变 —— 要连着已登录的 Chrome。
发版流程:后台一有改版风声,先跑这个,别等同事批量跑到一半才发现。

## 会漏报 / 误报

- 藏在弹窗/二级 tab 里的字段,当前页没展开时会记 `missing` / `hidden` —— 正常。
  要准,把浏览器点到那一屏再针对单个 form 跑。
- `--goto` 只是 `page.goto(form_url)`,进不了需要点「新建」才出现的弹窗。
- 命中 = 能定位,**不代表点得中、填得进** —— 那只有实跑能验。

## 退出码

全绿 0；有 `missing` / `error` 返回 1(可挂 CI,但要有已登录的 Chrome)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import yaml                                     # noqa: E402

from src import chrome, formcfg, health         # noqa: E402
from src.paths import user_path                 # noqa: E402

FORMS = user_path("config", "forms")

MARK = {"ok": "✓", "missing": "✗", "error": "✗", "ambiguous": "⚠",
        "hidden": "·", "closed": "·"}


def _forms(name: str | None) -> list[str]:
    if name:
        return [name]
    out = []
    for p in sorted(FORMS.glob("*.yaml")):
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if cfg.get("fields") or cfg.get("ready_selector"):
            out.append(p.stem)
    return out


def _check_one(page, name: str, goto: bool) -> dict:
    cfg = formcfg.load(name)
    if goto and cfg.get("form_url"):
        try:
            page.goto(cfg["form_url"])
        except Exception as e:
            return {"form": name, "error": f"打不开 {cfg['form_url']}：{e}", "rows": []}
    res = health.probe(cfg, page)
    res["form"] = name
    return res


def _print(res: dict) -> None:
    if res.get("error"):
        print(f"\n■ {res['form']}  —  {res['error']}")
        return
    rows = res["rows"]
    w = max((len(str(r["name"])) for r in rows), default=4)
    print(f"\n■ {res['form']}  检查 {res['checked']} 项,"
          f"{res['bad']} 项要看  {'[通过]' if res['ok'] else '[有问题]'}")
    for r in rows:
        m = MARK.get(r["status"], "?")
        line = f"  {m} {str(r['name']).ljust(w)}  [{r['where']}] "
        line += f"命中{r['count']}"
        if r["visible"] != r["count"]:
            line += f"/可见{r['visible']}"
        if r["note"]:
            line += f"  —— {r['note']}"
        print(line)


def main() -> int:
    ap = argparse.ArgumentParser(description="选择器体检:yaml 定位目标 vs 真实页面")
    ap.add_argument("form", nargs="?", help="只体检这一个;不给就全部")
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--goto", action="store_true",
                    help="逐个 goto 各 form 的 form_url 再体检(默认只针对当前页)")
    ap.add_argument("--json", action="store_true", help="机读输出")
    ap.add_argument("--timeout", type=int, default=15000)
    args = ap.parse_args()

    if not chrome.is_connected(args.cdp):
        info = chrome.diagnose(args.cdp)
        print(f"连不上 Chrome（{args.cdp}）。{info['hint']}")
        return 1

    names = _forms(args.form)
    if not names:
        print("没有可体检的配置类型(需要 yaml 里有 fields 或 ready_selector)。")
        return 1

    from src.browser import Browser
    results = []
    with Browser(args.cdp, args.timeout) as b:
        if not args.json:
            print(f"当前页面：{b.page.url}")
        for name in names:
            results.append(_check_one(b.page, name, args.goto))

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for res in results:
            _print(res)
        total_bad = sum(r.get("bad", 0) for r in results)
        print(f"\n共 {len(results)} 个配置类型,{total_bad} 项待看。")

    hard = any(r.get("error") or not r.get("ok", True) for r in results)
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
