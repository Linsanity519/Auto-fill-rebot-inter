"""重新生成 assets/webui/app.js 里的 STUB_FORMS。

    python tools\\gen_stub_forms.py

## 这东西是干嘛的

app.js 在没有后端时（`hasBackend()===false`，比如直接拿普通浏览器打开
`assets/webui/index.html` 核对样式）会走一份假数据。其中 STUB_FORMS 就是
`webapp.Api.list_forms()` 返回值的样子货。

⚠ **它必须和真实返回同构**，尤其是 `caps` / `ui` 这两段 —— 界面上哪张卡显示、
  哪一行藏掉，全看 caps。假数据缺了 caps，浏览器里看到的布局就和真机不是一回事，
  而这份假数据存在的全部意义就是"不启动 pywebview 也能核对布局"。

所以：**加了配置类型、或者改了 `Api._caps()` / `Api._ui_text()` 之后，回来跑一次。**
它直接调那两个函数，不会走样。

不带参数就地改写 app.js；`--print` 只打到屏幕上不动文件。
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

import yaml                                    # noqa: E402

from src import registry                       # noqa: E402
from src.paths import user_path                # noqa: E402
from src.webapp import Api                     # noqa: E402

APP_JS = Path(__file__).resolve().parent.parent / "assets" / "webui" / "app.js"
BEGIN = "  const STUB_FORMS = ["
END = "  ];"


def build_items() -> list[dict]:
    items = []
    for p in sorted(user_path("config", "forms").glob("*.yaml")):
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        nav = cfg.get("nav") or {}
        caps = Api._caps(cfg)
        items.append({
            "name": p.stem,
            "mode": cfg.get("mode"),
            "caps": caps,
            "ui": Api._ui_text(cfg, caps),
            "group": nav.get("group") or "其他",
            "group_order": nav.get("group_order", 99),
            "label": nav.get("label") or p.stem,
            "order": nav.get("order", 99),
            "desc": cfg.get("description") or "",
            "scopes": [list(x) for x in registry.scopes_for(cfg)],
        })
    # 和侧栏一样按 nav 排，读起来跟界面对得上
    items.sort(key=lambda d: (d["group_order"], d["order"], d["name"]))
    return items


def render(items: list[dict]) -> str:
    return "\n".join("    " + json.dumps(d, ensure_ascii=False) + "," for d in items)


def main() -> int:
    body = render(build_items())
    if "--print" in sys.argv:
        print(body)
        return 0

    text = APP_JS.read_text(encoding="utf-8")
    try:
        start = text.index(BEGIN)
        end = text.index(END, start) + len(END)
    except ValueError:
        print(f"在 {APP_JS} 里找不到 STUB_FORMS 那一段，没改动。")
        return 1

    new = text[:start] + BEGIN + "\n" + body + "\n" + END + text[end:]
    if new == text:
        print("STUB_FORMS 已经是最新的，没改动。")
        return 0
    APP_JS.write_text(new, encoding="utf-8")
    print(f"已更新 {APP_JS}（{len(build_items())} 个配置类型）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
