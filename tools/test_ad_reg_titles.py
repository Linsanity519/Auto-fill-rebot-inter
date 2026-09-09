"""常规商广「素材标题」那个按钮的定位回归：headless 起一个假创意块，
确认几种叫法都点得到、而且不会误点「新增标题」。

    python tools\test_ad_reg_titles.py

浏览器按 test_filler_locate 同样的顺序找：playwright 自带的 chromium →
本机 Chrome/Edge。都没有就整体 skip（返回 0），不让它变成 CI 硬失败。

⚠ 它只测「按文字找按钮」这一步。抽屉里怎么逐条打字、上限是不是 6，
  那些只有实跑能验。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        print(f"  \u2717 {name}" + (f"  {detail}" if detail else ""))


# 一个创意块该有的几个按钮。「一键填充」「一键清空」「新增标题」都不是我们要的那个。
BLOCK = """
<div class="single-creative-wrapper">
  <button class="ivu-btn"><i class="ai"></i>一键填充</button>
  {opener}
  <span class="clear">一键清空</span>
  <input placeholder="请输入2~40个字（移动场景建议18字以内）">
  <button class="ivu-btn">+ 新增标题</button>
</div>
"""

CASES = [
    ("带「+」图标的「添加标题」（2026-09-09 页面上就是这个）",
     '<button class="ivu-btn"><span>+</span> 添加标题</button>', "添加标题"),
    ("光秃秃的「批量添加」（抓取记录里的老叫法）",
     '<button class="ivu-btn">批量添加</button>', "批量添加"),
    ("「批量添加标题」（抽屉 aria-label 那个叫法）",
     '<button class="ivu-btn">+ 批量添加标题</button>', "批量添加标题"),
]

NAMES = ["添加标题", "批量添加", "批量添加标题"]


def _launch(p):
    """能起哪个起哪个，都不行返回 None。"""
    try:
        return p.chromium.launch()
    except Exception:
        pass
    try:
        from src import chrome
        exe = chrome.find_browser()
        if exe:
            return p.chromium.launch(executable_path=exe)
    except Exception:
        pass
    return None


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("没装 playwright，跳过")
        return 0

    from src.ad_reg_creative import AdRegCreative

    with sync_playwright() as p:
        browser = _launch(p)
        if browser is None:
            print("本机既没有 playwright 自带的 chromium，也没找到 Chrome/Edge，跳过")
            return 0
        page = browser.new_page()
        filler = AdRegCreative.__new__(AdRegCreative)
        filler.page = page

        print("\n[找按钮] 同一个抽屉，页面上叫过好几个名字")
        for title, opener_html, want in CASES:
            page.set_content(BLOCK.format(opener=opener_html))
            w = page.locator(".single-creative-wrapper").filter(visible=True).first
            btn, got = filler._find_button(w, NAMES)
            check(title, btn is not None and want in (got or ""), f"找到的是「{got}」")
            check("  没误点「新增标题」", btn is not None and "新增" not in (got or ""),
                  f"找到的是「{got}」")

        print("\n[找不到] 一个都没有时要明确报错，不能默默点别的")
        page.set_content(BLOCK.format(opener=""))
        w = page.locator(".single-creative-wrapper").filter(visible=True).first
        btn, got = filler._find_button(w, NAMES)
        check("一个别名都命不中时返回 None", btn is None, f"找到的是「{got}」")

        print("\n[慢渲染] 按钮晚一点才挂上来，也得等到（1.1.16 的线上故障）")
        filler.timeout = 6000
        # 复现现场：刚传完 10 个视频，创意表单还在渲染，按钮此刻**还不在 DOM 里**。
        # 老代码 _find_button 是一次性快照，这里直接返回 None → 报「没找到按钮」，
        # 而人去页面上一看按钮明明在，于是一路怀疑是不是又改名了。
        page.set_content(BLOCK.format(opener=""))
        page.evaluate("""() => setTimeout(() => {
            const b = document.createElement('button');
            b.className = 'ivu-btn';
            b.textContent = '+ 添加标题';
            document.querySelector('.single-creative-wrapper').appendChild(b);
        }, 900)""")
        w = page.locator(".single-creative-wrapper").filter(visible=True).first
        btn, got = filler._find_title_opener(w, {"open_buttons": NAMES})
        check("晚 900ms 才渲染出来的按钮，等得到", btn is not None, f"找到的是「{got}」")
        check("  等到的是对的那个，没误点「新增标题」",
              btn is not None and "新增" not in (got or ""), f"找到的是「{got}」")

        # 真的没有时不能死等到超时之后还假装找到了
        page.set_content(BLOCK.format(opener=""))
        filler.timeout = 600
        w = page.locator(".single-creative-wrapper").filter(visible=True).first
        btn, got = filler._find_title_opener(w, {"open_buttons": NAMES})
        check("真的没有时，等满超时后老老实实返回 None", btn is None, f"找到的是「{got}」")

        print("\n[切换中] 两条创意块同时可见时必须报错，不能填到上一条去")
        filler.timeout = 600
        page.set_content(BLOCK.format(opener="") + BLOCK.format(opener=""))
        try:
            filler._wrapper()
            check("同时可见两条创意块时报错", False, "居然没报错，会静默填到上一条上")
        except Exception as ex:
            check("同时可见两条创意块时报错", "分不清该填哪条" in str(ex), str(ex))

        page.set_content(BLOCK.format(opener='<button class="ivu-btn">批量添加</button>'))
        try:
            check("正常只有一条时照常返回", filler._wrapper().count() == 1)
        except Exception as ex:
            check("正常只有一条时照常返回", False, str(ex))

        print("\n[别名表] yaml 里配的那几个名字都读得出来")
        cfg = {"open_buttons": ["添加标题", "批量添加"]}
        check("open_buttons 优先", AdRegCreative._opener_names(cfg) == ["添加标题", "批量添加"])
        check("只配了老的 open_button 也认",
              AdRegCreative._opener_names({"open_button": "批量添加"}) == ["批量添加"])

        browser.close()

    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
