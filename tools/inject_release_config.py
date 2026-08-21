"""打包前注入「不能进仓库、但必须进分发包」的配置。

现在只有一项：统计回传的企微群机器人地址。
  · 默认：用仓库自带的 config/webhook.txt，任何环境开箱即用，不必先配 Secret。
    （它是故意提交进仓库的，取舍见 .gitignore 里那段注释。）
  · 换群 / 临时改地址：设环境变量 USAGE_WEBHOOK_URL，会覆盖写回那个文件。
  · 两个都没有：跳过，打出来的包不上报 —— 但 CI 会直接拦下来，不让这种包发出去。

顺带把 config/settings.yaml 原样拷成 assets/settings.default.yaml，
给 src/settings.py 做「老版本配置缺字段」的兜底。两个文件由此永远同源。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def inject_webhook() -> str:
    target = ROOT / "config" / "webhook.txt"
    url = (os.environ.get("USAGE_WEBHOOK_URL") or "").strip()
    if url:
        if not url.startswith("https://"):
            raise SystemExit(f"USAGE_WEBHOOK_URL 必须是 https 地址：{url!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "# 统计回传地址。打包时由 tools/inject_release_config.py 生成，别手改。\n"
            f"{url}\n", encoding="utf-8")
        return "由 USAGE_WEBHOOK_URL 写入"
    if target.is_file():
        return "沿用已有的 config/webhook.txt"
    return "未配置，本次打出来的包不上报统计"


def sync_settings_default() -> str:
    src = ROOT / "config" / "settings.yaml"
    dst = ROOT / "assets" / "settings.default.yaml"
    if not src.is_file():
        raise SystemExit(f"缺少 {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return f"已同步 {dst.relative_to(ROOT)}"


def main() -> int:
    print(f"  统计回传：{inject_webhook()}")
    print(f"  默认配置：{sync_settings_default()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
