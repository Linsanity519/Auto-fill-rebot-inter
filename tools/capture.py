"""页面结构抓取器：把一页表单 dump 成 docs/xxx-配置项抓取.md 的草稿。

## 为什么有这个脚本

接一个新配置类型，最贵的一段不是写代码，是**搞清楚页面上到底有什么**：
字段原文叫什么、是下拉还是勾选框组、选项全集是哪些、同名 label 出现几次、
选了某个值之后哪些字段会冒出来 / 消失。
以前这一段靠人肉一个个点 + 来回问，几十轮对话；这个脚本一次跑完。

⚠ **它出的是草稿，不是结论。** 控件类型是**推断**的，下拉的选项要点开才读得到。
  拿到草稿之后仍然要人眼过一遍再落进 docs/ —— 但过一遍比从零抓便宜一个数量级。

## 怎么用

    # 0) 先让浏览器开着、登录好（程序里点「启动浏览器并登录」，或者：）
    python tools\\capture.py --open "https://要抓的页面"

    # 1) 抓当前页 -> markdown 草稿
    python tools\\capture.py --out docs\\XX-配置项抓取.md

    # 2) 抓「联动」—— 这一步最值钱
    python tools\\capture.py --snap before
    #    （人工在页面上把「生效渠道」切成「定向」）
    python tools\\capture.py --snap after --diff before

    # 3) 把某个下拉点开、读出全部选项（草稿里读不到的那部分）
    python tools\\capture.py --options "生效平台" --options "人群包"

快照存在 output/capture/ 下，随便删。

## 支持哪些前端

按「表单项容器」和「label 是父节点第一个子元素」两条路一起找，覆盖了这个项目
遇到过的全部几套：antd / Formily(ant-formily-item) / Element(el-form-item) /
iView(ivu-form-item) / Arco(arco-form-item) / 老后台的裸 Vue 结构。
**不依赖任何编译哈希类名**（tw-xxxx / css-xxxx），那些发版即失效。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src import chrome                        # noqa: E402
from src.browser import Browser               # noqa: E402
from src.paths import user_path               # noqa: E402

SNAP_DIR = user_path("output", "capture")


# ============================================================ 页面里跑的那段
#
# ⚠ 全部判据只用「结构」和「语义属性」（tagName / type / role / 是不是第一个子元素），
#   class 只用*包含*匹配那几个各家框架都稳定的词（form-item / select / upload…）。
#   绝不匹配完整 class 名 —— tw-6hmssk 这种编译哈希发版就变。
JS_FIELDS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const cls  = el => (typeof el.className === 'string' ? el.className : '') || '';
  const has  = (el, sel) => !!el.querySelector(sel);

  // ---------- 1. 找候选字段块 ----------
  const cand = new Set();

  // ⚠ 各家框架把字段块内部的「标签格」「控件格」也命名成 xxx-form-item-label /
  //   xxx-form-item-control，同样被 [class*="form-item"] 选中。不排掉的话，
  //   下面的「去嵌套只留最内层」会把真正的字段块（外层）丢掉，只剩两个半截：
  //   标签格有 label 没控件、控件格有控件没 label —— 出来的表整列都是「无控件」。
  //   （这一条是拿 antd / Element / iView / Arco 四套的样板页实测出来的。）
  //   ⚠ [-_]* 不是 [-_]?：B 站自研的 bd- 组件用 BEM 双下划线（bd-form-item__label），
  //     只放一个分隔符会漏掉它，真正的字段块 bd-form-item 反而被当成最内层丢掉。
  const isPart = el => /(item|field)[-_]*(label|control|explain|extra|message|tip|wrapper)/i
      .test(typeof el.className === 'string' ? el.className : '');

  // (a) 各家 UI 框架的表单项容器
  document.querySelectorAll(
    '[class*="form-item"],[class*="form_item"],[class*="formily-item"]'
  ).forEach(el => { if (!isPart(el) && el.tagName !== 'LABEL') cand.add(el); });

  // (b) 「第一个子元素就是 label」的裸结构（老后台 Vue 那套）
  //     ⚠ 每个单选/复选项本身也是 <label>，必须排掉：
  //       判据是这个 label 里面包着 input[type=radio|checkbox]。
  document.querySelectorAll('label').forEach(lb => {
    if (lb.querySelector('input[type=radio],input[type=checkbox]')) return;
    const p = lb.parentElement;
    if (!p) return;
    if (p.firstElementChild !== lb) return;
    if (p.children.length < 2) return;
    cand.add(p);
  });

  // ---------- 2. 去嵌套：只留最内层 ----------
  // Formily 的 FormItem 会互相嵌套，外层留下来就会出一堆「label 为空」的壳。
  const all = [...cand];
  const blocks = all.filter(el => !all.some(o => o !== el && el.contains(o)));

  // 按页面上的先后排序（document order），出来的表和页面顺序一致
  blocks.sort((a, b) =>
    (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);

  // ---------- 3. 区块标题（这个字段属于页面上哪一段）----------
  const heads = [...document.querySelectorAll(
    'h1,h2,h3,h4,h5,[class*="card-head"],[class*="panel-title"],[class*="collapse-header"]')]
    .map(h => ({ el: h, text: norm(h.innerText).slice(0, 40) }))
    .filter(h => h.text);

  const sectionOf = el => {
    let best = '';
    for (const h of heads) {
      // h 在 el 前面（h.compareDocumentPosition(el) 说 el 在 h 之后）
      if (h.el.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) best = h.text;
    }
    return best;
  };

  // ---------- 4. 一个块 -> 一条记录 ----------
  const visible = el => {
    const r = el.getBoundingClientRect();
    return !!(r.width || r.height) && getComputedStyle(el).visibility !== 'hidden';
  };

  const labelOf = el => {
    const dedicated = el.querySelector('[class*="form-item-label"],[class*="item-label"]');
    if (dedicated && norm(dedicated.innerText)) return norm(dedicated.innerText);
    const lb = [...el.children].find(c => c.tagName === 'LABEL');
    if (lb) return norm(lb.innerText);
    const any = el.querySelector('label');
    return (any && !any.querySelector('input')) ? norm(any.innerText) : '';
  };

  // 选项文字：radio / checkbox 能直接读全；下拉读不到（要点开）
  const optionsOf = el => {
    const boxes = [...el.querySelectorAll('input[type=radio],input[type=checkbox]')];
    if (!boxes.length) {
      const native = [...el.querySelectorAll('select option')];
      if (native.length) return native.map(o => norm(o.textContent)).filter(Boolean);
      return null;
    }
    const seen = [];
    for (const b of boxes) {
      const wrap = b.closest('label') || b.parentElement;
      const t = norm(wrap ? wrap.innerText : '') || b.value || '';
      if (t && !seen.includes(t)) seen.push(t);
    }
    return seen;
  };

  const typeOf = el => {
    if (has(el, 'input[type=file]') || el.querySelector('[class*="upload"]')) return '上传';
    if (has(el, 'input[type=radio]')) return '单选';
    if (has(el, 'input[type=checkbox]')) return '勾选框组';
    if (el.querySelector('[class*="switch"]')) return '开关';
    if (has(el, 'textarea')) return '多行文本';

    const inputs = [...el.querySelectorAll('input')];
    const isRange = !!el.querySelector('[class*="range"]') ||
      inputs.filter(i => /开始|结束|start|end/i.test(i.placeholder || '')).length >= 2;
    if (el.querySelector('[class*="picker"]') ||
        inputs.some(i => /日期|时间/.test(i.placeholder || '')))
      return isRange ? '日期区间' : '日期';

    const sel = el.querySelector(
      '[class*="select"],[class*="multiselect"],[role=combobox],select');
    if (sel) {
      const multi = /multiple|multiselect|tags/i.test(cls(sel)) ||
        !!el.querySelector('[class*="tag"]');
      return multi ? '多选下拉' : '下拉';
    }
    if (inputs.length) {
      return inputs[0].readOnly ? '只读输入框（多半是下拉，点开才可编辑）' : '输入框';
    }
    return '（无控件 / 展示项）';
  };

  const requiredOf = el => {
    if (/required/i.test(cls(el))) return true;
    if (el.querySelector('[class*="required"]')) return true;
    if (el.querySelector('input[required],[aria-required="true"]')) return true;
    const lb = el.querySelector('label');
    return !!(lb && /^[*＊]/.test(norm(lb.innerText)));
  };

  const rows = blocks.map((el, i) => {
    const inputs = [...el.querySelectorAll('input,textarea')];
    // 「当前值」只看文本类控件 —— 单选/复选的 .value 是 'on' 或后端码值
    // （生效平台读出来是 on/on/on/on，生效渠道是 normal/direct），对人没用，
    // 而且会盖掉真正有意义的输入内容。勾没勾上看 options 那一列。
    const texty = inputs.filter(x => !['radio', 'checkbox'].includes(x.type));
    return {
      order: i,
      label: labelOf(el).replace(/^[*＊]\s*/, ''),
      type: typeOf(el),
      required: requiredOf(el),
      options: optionsOf(el),
      placeholder: [...new Set(texty.map(x => x.placeholder).filter(Boolean))],
      readonly: texty.length ? texty.every(x => x.readOnly) : null,
      value: norm(texty.map(x => x.value).filter(Boolean).join(' / ')).slice(0, 60),
      section: sectionOf(el),
      visible: visible(el),
      inputs: inputs.length,
    };
  }).filter(r => r.label);            // 拿不到 label 的壳直接丢

  // ---------- 5. 同名 label 计数 ----------
  const tally = {};
  rows.forEach(r => { tally[r.label] = (tally[r.label] || 0) + 1; });
  const seen = {};
  rows.forEach(r => {
    seen[r.label] = (seen[r.label] || 0) + 1;
    r.dup_total = tally[r.label];
    r.dup_index = seen[r.label];
  });

  return { url: location.href, title: document.title, rows };
}
"""

# 当前展开着的浮层里的选项。各家的浮层都 teleport 到 body 底下，
# 而且**关掉之后不从 DOM 里删**（只是 display:none），所以必须挑显示着的那个。
JS_POPUP_OPTIONS = r"""
() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return !!(r.width || r.height) && cs.display !== 'none' && cs.visibility !== 'hidden';
  };
  const pools = [...document.querySelectorAll(
    '[class*="dropdown"],[class*="popper"],[class*="popup"],[class*="select-menu"],' +
    '[role=listbox],ul[class*="multiselect__content"]')].filter(vis);
  if (!pools.length) return [];
  // 取最靠后出现的那个（后开的盖在前面的上头）
  const box = pools[pools.length - 1];
  const items = [...box.querySelectorAll('li,[role=option],[class*="option"]')];
  const out = [];
  for (const it of items) {
    const t = (it.innerText || '').replace(/\s+/g, ' ').trim();
    if (t && !out.includes(t) && !/没有找到|暂无数据|选项列表为空|no data/i.test(t))
      out.push(t);
  }
  return out;
}
"""

# 按 label 找到「这个字段里可以点开下拉的那个元素」，交回给 Python 去点。
#
# ⚠ 为什么要分 which=0 / which=1 两个候选：同一个下拉，有的框架把点击监听挂在
#   里面那个 <input> 上，有的挂在外面的 selector 容器上。点错那个就一点反应没有
#   —— 而且**不报错**，表现成「这个下拉是空的」。所以两个都要能试。
JS_CONTROL = r"""
([label, which]) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().replace(/^[*＊]\s*/, '');
  // ⚠ 和 JS_FIELDS 用同一条排除规则：xxx-form-item-label 那种「标签格」
  //   也会被 [class*="form-item"] 选中，它里面没有控件。
  const isPart = el => /(item|field)[-_]*(label|control|explain|extra|message|tip|wrapper)/i
      .test(typeof el.className === 'string' ? el.className : '');

  const blocks = [...document.querySelectorAll(
    '[class*="form-item"],[class*="form_item"],[class*="formily-item"]')]
    .filter(el => !isPart(el) && el.tagName !== 'LABEL');
  document.querySelectorAll('label').forEach(lb => {
    const p = lb.parentElement;
    if (p && p.firstElementChild === lb &&
        !lb.querySelector('input[type=radio],input[type=checkbox]')) blocks.push(p);
  });

  const hit = blocks.filter(b => {
    const l = b.querySelector('[class*="item-label"]') ||
              [...b.children].find(c => c.tagName === 'LABEL');
    return l && norm(l.innerText) === label;
  });
  if (!hit.length) return null;

  for (const b of hit.reverse()) {                 // 外层壳排在前面，从最内层试起
    const cands = [
      b.querySelector('input:not([type=radio]):not([type=checkbox]),textarea'),
      b.querySelector('[class*="selector"],[class*="select"],[role=combobox]'),
    ].filter(Boolean);
    if (cands.length > which) {
      cands[which].scrollIntoView({ block: 'center' });
      return cands[which];
    }
  }
  return null;
}
"""


# ============================================================ Python 侧
def snapshot(page) -> dict:
    data = page.evaluate(JS_FIELDS)
    data["at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return data


def read_options(page, label: str, wait_ms: int = 2500) -> list:
    """把某个字段的下拉点开，读出候选。

    两个候选控件各试一遍（里面的 input / 外面的 selector 容器），
    先真实鼠标点、不行再退回 JS 合成点击 —— 三种组合都覆盖到才算试过。

    ⚠ 必须先真实鼠标点：「点开之前 readonly、点开才变可编辑」的远程搜索框
      （商品ID、组合价格那一族）合成 click 打不开这个状态，
      表现成"这个活动下一条商品都没有"。见 src/pp_filler._open_input。
    ⚠ 远程搜索的下拉要打字才拉数据，这里读到空是**正常**的。
      别当成"这个下拉是空的"，在草稿上补一句「远程搜索，要打字」。
    """
    found = False
    for which in (0, 1):
        h = page.evaluate_handle(JS_CONTROL, [label, which])
        el = h.as_element()
        if el is None:
            continue
        found = True

        # 先收掉上一个浮层，并**记下它现在的内容**。
        # ⚠ 这一步不是洁癖：浮层关掉之后不从 DOM 里删（只是 display:none），
        #   有的还压根收不掉。不比一下的话，点了个没反应的下拉，
        #   读回来的是上一个下拉的选项 —— 于是报出「组合价格里没有 134，
        #   能看到的是 1223450133(充电券…)」这种驴唇不对马嘴的话。
        _dismiss(page)
        before = page.evaluate(JS_POPUP_OPTIONS)

        try:
            el.click(timeout=2000)           # 真实鼠标
        except Exception:
            el.evaluate("el => { el.focus(); el.click(); }")   # 退回合成
        page.wait_for_timeout(wait_ms)
        opts = page.evaluate(JS_POPUP_OPTIONS)
        _dismiss(page)

        if opts and opts == before:
            continue                          # 没变 = 没打开，读到的是旧浮层，丢掉
        if opts:
            return opts
    if not found:
        return ["（打不开：页面上没有这个字段，或者字段块里没有可点的控件）"]
    return []


def _dismiss(page):
    """尽量把展开着的浮层收掉。收不掉也不报错 —— 由调用方用「内容没变」兜底。"""
    try:
        page.keyboard.press("Escape")
        # 「点了别处」是各家框架收浮层的主要途径，Escape 反而不是每家都听。
        # 往 body 上派发事件而不是真的挪鼠标去点 —— 真点会点到别的控件上。
        page.evaluate("""() => {
            if (document.activeElement && document.activeElement.blur)
                document.activeElement.blur();
            for (const t of ['mousedown', 'click', 'mouseup'])
                document.body.dispatchEvent(new MouseEvent(t, {bubbles: true}));
        }""")
        page.wait_for_timeout(250)
    except Exception:
        pass


def _key(r: dict) -> str:
    """字段的身份。同名 label 出现多次时带上序号，不然 diff 会互相抵消。"""
    return r["label"] if r.get("dup_total", 1) == 1 else f'{r["label"]} #{r["dup_index"]}'


def to_markdown(snap: dict, title: str = "") -> str:
    rows = [r for r in snap["rows"] if r["visible"]]
    hidden = [r for r in snap["rows"] if not r["visible"]]
    dups = sorted({r["label"] for r in snap["rows"] if r["dup_total"] > 1})

    L = []
    A = L.append
    A(f"# {title or snap.get('title') or '页面'} — 配置项抓取（脚本草稿）")
    A("")
    A(f"抓取来源：`{snap['url']}`，{snap['at']}。")
    A("由 `python tools\\capture.py` 自动 dump。")
    A("")
    A("> ⚠ **这是草稿，核对完请把这一段删掉。** 三件事脚本做不到，必须人工补：")
    A(">")
    A("> 1. **控件类型是推断的。** 多选下拉和勾选框组长得很像、填法完全不同，挨个确认。")
    A('> 2. **下拉的选项读不到**（要点开才有）。用 `--options "字段名"` 一个个抓；')
    A(">    远程搜索的那种还得打字才出得来。")
    A("> 3. **联动完全没体现**。用 `--snap a` / 人工改一个值 / `--snap b --diff a` 抓。")
    A("")
    A("## 概况")
    A("")
    A(f"- 可见字段 **{len(rows)}** 个"
      + (f"，另有 {len(hidden)} 个当前不可见（多半要联动才出现）" if hidden else ""))
    A(f"- 必填 **{sum(1 for r in rows if r['required'])}** 个")
    if dups:
        A("- ⚠ **同名 label 出现多次**：" + "、".join(f"「{d}」" for d in dups))
        A("  —— 定位时必须指定第几个。这是这个项目踩过最多次的坑。")
    A("")
    A("## 字段全表")

    cur = object()
    for r in rows:
        if r["section"] != cur:
            cur = r["section"]
            A("")
            A(f"### {cur or '（无区块标题）'}")
            A("")
            A("| label 原文 | 控件类型 | 必填 | 选项 / placeholder | 备注 |")
            A("|---|---|---|---|---|")
        name = r["label"]
        if r["dup_total"] > 1:
            name += f" **（第 {r['dup_index']}/{r['dup_total']} 个）**"
        if r["options"]:
            head = "、".join(r["options"][:12]) + ("…" if len(r["options"]) > 12 else "")
            detail = f"**{len(r['options'])} 项**：{head}"
        else:
            detail = " / ".join(r["placeholder"])
        note = []
        if "下拉" in r["type"] and not r["options"]:
            note.append("选项待 `--options` 抓")
        if r["readonly"]:
            note.append("readOnly")
        if r["value"]:
            note.append(f"当前值：{r['value']}")
        A(f"| {name} | {r['type']} | {'✔' if r['required'] else ''} "
          f"| {detail} | {'；'.join(note)} |")

    if hidden:
        A("")
        A("## 当前不可见的字段")
        A("")
        A("⚠ 这些块在 DOM 里但没渲染出来，**大概率是联动字段**。")
        A("去页面上把上游的值切一遍，用 `--diff` 确认它们各自是被什么触发的。")
        A("")
        for r in hidden:
            A(f"- {r['label']} — {r['type']}")

    A("")
    A("## 还没抓的")
    A("")
    A("- [ ] 各下拉的选项全集")
    A("- [ ] 联动关系（选了什么会出现 / 消失什么）")
    A("- [ ] 提交成功、失败的判据（URL 跳转？弹窗消失？绿条？错误显示在哪？）")
    A("- [ ] 有没有能直接取数的接口（有的话比翻 DOM 省事得多）")
    A("")
    return "\n".join(L)


def diff_markdown(a: dict, b: dict, name_a: str, name_b: str) -> str:
    va = {_key(r): r for r in a["rows"] if r["visible"]}
    vb = {_key(r): r for r in b["rows"] if r["visible"]}
    appeared = [k for k in vb if k not in va]
    gone = [k for k in va if k not in vb]
    changed = []
    for k, ra in va.items():
        rb = vb.get(k)
        if rb is None:
            continue
        d = []
        if ra["type"] != rb["type"]:
            d.append(f"控件类型 {ra['type']} → {rb['type']}")
        if ra["required"] != rb["required"]:
            d.append("变成必填" if rb["required"] else "不再必填")
        # ⚠ 只在两边都真的读到过选项时才比。下拉的选项默认是 None（要点开才有），
        #   一边跑过 --options 一边没跑，会报出「选项 3 → 0 项」这种假联动。
        oa, ob = ra["options"], rb["options"]
        if oa and ob and oa != ob:
            d.append(f"选项 {len(oa)} → {len(ob)} 项")
        if d:
            changed.append((k, "；".join(d)))

    L = [f"## 联动：{name_a} → {name_b}", ""]
    L.append(f"（{name_a} 可见 {len(va)} 个字段，{name_b} 可见 {len(vb)} 个）")
    L.append("")
    L.append("⚠ 这份差异直接决定 **Excel 模板出哪些列**、跑的时候填哪些字段。")
    L.append("模板列是在跑之前就定死的，这里漏一条，整个模板要重做。")
    L.append("")
    L.append(f"### 新出现的字段（{len(appeared)}）")
    L.append("")
    L += ([f"- {k}（{vb[k]['type']}{'，必填' if vb[k]['required'] else ''}）"
           for k in appeared] or ["（无）"])
    L.append("")
    L.append(f"### 消失的字段（{len(gone)}）")
    L.append("")
    L += ([f"- {k}（{va[k]['type']}）" for k in gone] or ["（无）"])
    L.append("")
    L.append(f"### 变了的字段（{len(changed)}）")
    L.append("")
    L += ([f"- {k}：{d}" for k, d in changed] or ["（无）"])
    L.append("")
    return "\n".join(L)


def load_snap(name: str) -> dict:
    p = SNAP_DIR / f"{name}.json"
    if not p.exists():
        raise SystemExit(f"没有这份快照：{p}\n先跑一次 `--snap {name}`")
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把当前页面的表单结构 dump 成抓取记录草稿")
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--open", metavar="URL", help="先在调试 Chrome 里打开这个地址")
    ap.add_argument("--out", metavar="FILE", help="markdown 写到哪；不给就打到屏幕上")
    ap.add_argument("--title", default="", help="草稿的标题")
    ap.add_argument("--snap", metavar="NAME", help="存一份快照，供 --diff 用")
    ap.add_argument("--diff", metavar="NAME", help="和这份快照比，输出联动差异")
    ap.add_argument("--options", metavar="LABEL", action="append", default=[],
                    help="把这个字段的下拉点开读选项，可重复")
    ap.add_argument("--timeout", type=int, default=30000)
    args = ap.parse_args()

    if args.open:
        from src.paths import app_dir
        print(chrome.launch(args.cdp, app_dir() / ".chrome-profile", args.open))
        print("在弹出的窗口里登录好、把页面点到要抓的那一屏，然后不带 --open 再跑一次。")
        return 0

    if not chrome.is_connected(args.cdp):
        print(f"连不上 Chrome（{args.cdp}）。\n"
              "先在程序里点「启动浏览器并登录」，或者：\n"
              '    python tools\\capture.py --open "要抓的页面URL"')
        return 1

    with Browser(args.cdp, args.timeout) as b:
        page = b.page
        print(f"当前页面：{page.url}")
        snap = snapshot(page)
        print(f"读到 {len(snap['rows'])} 个字段块"
              f"（可见 {sum(1 for r in snap['rows'] if r['visible'])} 个）")

        for label in args.options:
            opts = read_options(page, label)
            print(f"\n[{label}] {len(opts)} 项")
            for o in opts:
                print("   " + o)
            for r in snap["rows"]:
                if r["label"] == label and not r["options"]:
                    r["options"] = opts

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    if args.snap:
        p = SNAP_DIR / f"{args.snap}.json"
        p.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"快照已存：{p}")

    out = ""
    if args.diff:
        out = diff_markdown(load_snap(args.diff), snap, args.diff, args.snap or "当前")
    elif args.out or not args.snap:
        out = to_markdown(snap, args.title)

    if out:
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(out, encoding="utf-8")
            print(f"已写：{args.out}")
        else:
            print()
            print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
