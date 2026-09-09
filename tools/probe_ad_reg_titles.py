r"""线上实测：拿 yaml 里「素材标题」那一段的选择器，到**真页面**上核一遍。

    python tools\probe_ad_reg_titles.py

⚠ 只读 —— 不点、不填、不提交。要求那个已登录的调试 Chrome 开着（端口 9222），
  并且当前停在常规商广的投放页（创意块要在页面上，弹窗开不开都行）。

为什么单独有这一份：这一段的文案后台改过三次（「批量添加」→「+ 添加标题」、
「保存」→「确认」、「已添加」→「已选」），每次都是发出去、用户跑到第 1 条才发现。
离线测试测的是「几种叫法都认得」，测不了「页面现在到底叫什么」——只有这个能。
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

from src import formcfg                                   # noqa: E402
from src.ad_reg_creative import AdRegCreative             # noqa: E402
from src.browser import Browser                           # noqa: E402

CDP = "http://127.0.0.1:9222"      # 和 settings.yaml 的默认值一致

PASS = FAIL = WARN = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}" + (f"　{detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  \u2717 {name}" + (f"　{detail}" if detail else ""))


def warn(msg):
    global WARN
    WARN += 1
    print(f"  ~ {msg}")


# 页面上「批量填标题」那个弹窗现在长什么样，只读地问一遍
SCAN = r"""() => {
  const out = [];
  for (const el of document.querySelectorAll('[role=dialog], .ivu-drawer, .ivu-modal, .batch-title-drawer')) {
    const t = el.innerText || '';
    if (!t.includes('标题')) continue;
    out.push({
      tag: el.tagName.toLowerCase(), cls: el.className, role: el.getAttribute('role') || '',
      visible: !!el.offsetParent, textareas: el.querySelectorAll('textarea').length,
      buttons: [...el.querySelectorAll('button, .ivu-btn, [role=button]')]
                 .map(b => (b.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean),
      counter: (t.match(/(已\S{0,2})\s*\d+\s*\/\s*\d+/) || [])[0] || '',
    });
  }
  return out;
}"""


def main() -> int:
    cfg = formcfg.load("常规商广")
    tcfg = ((cfg.get("creative") or {}).get("titles")) or {}

    openers = AdRegCreative._opener_names(tcfg)
    saves = AdRegCreative._save_names(tcfg)
    counters = AdRegCreative._counter_texts(tcfg)
    print(f"yaml 里配的别名：打开={openers}　落地={saves}　计数={counters}")

    try:
        with Browser(CDP, 20000) as b:
            page = b.page
            print(f"当前页：{page.url}\n")

            print("[创意块] 打开弹窗的那个按钮")
            w = page.locator(".single-creative-wrapper").filter(visible=True).first
            if not w.count():
                warn("页面上没有可见的创意块（.single-creative-wrapper）——"
                     " 先在浏览器里点到有创意的那一步再跑我")
            else:
                f = AdRegCreative.__new__(AdRegCreative)
                f.page = page
                btn, txt = f._find_button(w, openers)
                check("找得到，且不是「新增标题」",
                      btn is not None and "新增" not in (txt or ""),
                      f"页面上写的是「{txt}」" if btn is not None else "一个别名都没命中")

            print("\n[弹窗] 落地按钮 / 计数文案 / 文本框")
            found = page.evaluate(SCAN)
            if not found:
                warn("页面上现在没有那个弹窗的 DOM —— 这一段核不了。"
                     "在浏览器里点一次「+ 添加标题」再跑我")
            for d in found:
                print(f"    {d['tag']}.{d['cls'] or '(无class)'}"
                      f"　可见={d['visible']}　textarea={d['textareas']}"
                      f"　按钮={d['buttons']}　计数=「{d['counter']}」")
                check("落地按钮的名字在别名表里",
                      any(any(n in b for n in saves) for b in d["buttons"]),
                      f"页面按钮：{d['buttons']}")
                if d["counter"]:
                    check("计数前缀在别名表里",
                          any(d["counter"].startswith(n) for n in counters),
                          f"页面写的是「{d['counter']}」")
                else:
                    warn("这个弹窗里没读到「x/y」形式的计数（关着的时候读不到很正常）")
                check("弹窗里有文本框", d["textareas"] > 0)
    except Exception as e:
        print(f"\n连不上调试 Chrome 或页面不对：{e}")
        print("先「启动浏览器并登录」，把页面停在常规商广的投放页，再跑我。")
        return 0

    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项，没核到 {WARN} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
