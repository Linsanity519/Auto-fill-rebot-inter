"""DOM 定位回归：拿 tests/fixtures/ 下的真实页面快照，headless 跑一遍
「yaml 里的定位目标还在不在」。不连内网、不登录。

浏览器按这个顺序找，任一可用即跑：
  1. playwright 自带的 Chromium（`python -m playwright install chromium`，CI 上走这个）
  2. 本机装的 Chrome / Edge（`src/chrome.find_browser()` 定位，内网机器没法下 1 时走这个）
  3. 已经开着的调试 Chrome（端口 9222，`--cdp` 可改）
都没有就**整体 skip**（返回 0），不让它变成 CI 硬失败。

⚠ 它测的是通用探针（src/health.py）：label / css 选择器定位得到几个元素。
  各 filler 私有的「点开下拉读选项、联动」不在这里 —— 那只有实跑能验。
  fixture 怎么加见 tests/fixtures/README.md。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _skip(reason: str) -> int:
    print(f"SKIP：{reason}")
    return 0


def _get_browser(pw, cdp_url: str):
    """(browser, 用的是哪个, 用完要不要 close)。都拿不到 → (None, 原因, False)。"""
    # 1) playwright 自带 Chromium
    try:
        return pw.chromium.launch(headless=True), "playwright chromium", True
    except Exception:
        pass
    # 2) 本机装的 Chrome / Edge
    try:
        from src import chrome as _chrome
        exe = _chrome.find_browser()
        if exe:
            return (pw.chromium.launch(headless=True, executable_path=exe),
                    f"本机浏览器 {exe}", True)
    except Exception:
        pass
    # 3) 已经开着的调试 Chrome
    try:
        from src import chrome as _chrome
        if _chrome.is_connected(cdp_url):
            b = pw.chromium.connect_over_cdp(cdp_url)
            return b, f"调试 Chrome {cdp_url}", False   # 别 close 用户的浏览器
    except Exception:
        pass
    return None, ("三种都拿不到：playwright 没装 Chromium、本机没找到 Chrome/Edge、"
                  "9222 也没开着"), False


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return _skip("没装 playwright")

    cases = sorted(FIX.glob("*/*.html"))
    if not cases:
        return _skip("tests/fixtures/ 下还没有快照")

    from src import formcfg, health

    try:
        pw = sync_playwright().start()
    except Exception as e:
        return _skip(f"playwright 起不来：{e}")
    try:
        browser, how, do_close = _get_browser(pw, args.cdp)
        if browser is None:
            return _skip(how)
        print(f"用的浏览器：{how}")

        passed = failed = 0
        for html_path in cases:
            exp_path = html_path.with_suffix(".expect.json")
            if not exp_path.exists():
                print(f"  ⚠ {html_path.name} 没有 .expect.json，跳过")
                continue
            exp = json.loads(exp_path.read_text(encoding="utf-8"))
            form = exp["form"]
            allow_missing = set(exp.get("expect_missing") or [])

            ctx = browser.new_context()
            page = ctx.new_page()
            page.set_content(html_path.read_text(encoding="utf-8"))
            res = health.probe(formcfg.load(form), page)
            ctx.close()

            bad = [r for r in res["rows"]
                   if r["status"] in ("missing", "error") and r["name"] not in allow_missing]
            if bad:
                failed += 1
                print(f"  ✗ {form} / {html_path.name}：{len(bad)} 个定位目标丢了")
                for r in bad:
                    print(f"      - {r['name']} [{r['where']}] {r['target']}  {r['note']}")
            else:
                passed += 1
                print(f"  ✓ {form} / {html_path.name}：{res['checked']} 项定位都在")

        if do_close:
            browser.close()
        print("\n" + "=" * 48)
        print(f"通过 {passed} 份，失败 {failed} 份")
        return 1 if failed else 0
    finally:
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
