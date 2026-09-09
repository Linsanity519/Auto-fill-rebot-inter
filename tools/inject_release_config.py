"""打包前注入「不能进仓库、但必须进分发包」的配置。

两项：统计回传的两个地址（企微群机器人 / 智能表格「接收外部数据」）。
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

# ⚠ 这个脚本会往 stdout 打中文。英文 Windows / CI 上，输出被重定向时 Python 取的是
#   ANSI 代码页（cp1252）而不是 chcp 设的 65001，直接 UnicodeEncodeError 崩掉，
#   而且崩在打印那一行 —— 看起来像是功能出错，其实只是编码。实测在 GitHub
#   Actions 上炸过一次。main.py 开头也做了同样的事。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


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


def inject_sheet_webhook() -> str:
    """智能表格「接收外部数据」的写入地址。

    ⚠ 和群那个不一样，这个**不提交进仓库**：群 key 泄露顶多有人往统计群发消息，
      而这把能往那张统计表里灌行 —— 虽然也只能灌那一张表（读不了、删不了、
      改不了结构，表主随时能关），但没必要白白公开。所以：只认环境变量，
      没配就跳过（打出来的包只发群，不写表）。
    """
    target = ROOT / "config" / "sheet_webhook.txt"
    url = (os.environ.get("USAGE_SHEET_WEBHOOK_URL") or "").strip()
    if url:
        if not url.startswith("https://"):
            raise SystemExit(f"USAGE_SHEET_WEBHOOK_URL 必须是 https 地址：{url!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "# 智能表格回传地址。打包时由 tools/inject_release_config.py 生成，别手改。\n"
            f"{url}\n", encoding="utf-8")
        return "由 USAGE_SHEET_WEBHOOK_URL 写入"
    if target.is_file():
        return "沿用已有的 config/sheet_webhook.txt"
    return "未配置，本次打出来的包只发群、不写表格"


def inject_bundled_module() -> str:
    """把表格地址同时写进 src/_bundled.py。

    ⚠ 这一份才是**存量用户真正收得到**的那份。config/sheet_webhook.txt 只有
      45MB 完整安装包会铺，而日常升级走的是 300KB 代码包，它只投递
      main.py / src / assets / config/forms / config/team.json
      （见 tools/make_payload.py 的 MEMBERS）—— 所以 1.1.15 那版发出去之后，
      走代码包升级的人一个都没打开表格通道，而且完全没有报错。
    ⚠ 为什么不把 config/sheet_webhook.txt 加进代码包：用户机上跑的是**旧
      updater**（tools/ 也不在投递范围里），旧 updater 遇到 PAYLOAD_MEMBERS
      之外的成员会判包损坏、整包回滚 —— 等于存量用户全部更新失败。
    ⚠ 这个文件不进仓库（.gitignore），和 config/sheet_webhook.txt 同一个理由。
    """
    target = ROOT / "src" / "_bundled.py"
    url = ((os.environ.get("USAGE_SHEET_WEBHOOK_URL") or "").strip()
           or _read_url(ROOT / "config" / "sheet_webhook.txt"))
    if not url:
        # 没有地址时**删掉**旧的，别让上一次构建的残留混进这次的包
        if target.is_file():
            target.unlink()
        return "未配置，本次打出来的包只发群、不写表格"
    target.write_text(
        '"""打包时生成的地址表，别手改、也别提交（见 tools/inject_release_config.py）。"""\n'
        f'SHEET_WEBHOOK = {url!r}\n', encoding="utf-8")
    return "已写入 src/_bundled.py"


def _read_url(path: Path) -> str:
    """从 webhook 文本文件里读出第一条非注释行。读不到返回空串。"""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except OSError:
        pass
    return ""


def sync_settings_default() -> str:
    src = ROOT / "config" / "settings.yaml"
    dst = ROOT / "assets" / "settings.default.yaml"
    if not src.is_file():
        raise SystemExit(f"缺少 {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return f"已同步 {dst.relative_to(ROOT)}"


def main() -> int:
    print(f"  统计回传（群）：{inject_webhook()}")
    print(f"  统计回传（表格）：{inject_sheet_webhook()}")
    print(f"  统计回传（表格·随代码包）：{inject_bundled_module()}")
    print(f"  默认配置：{sync_settings_default()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
