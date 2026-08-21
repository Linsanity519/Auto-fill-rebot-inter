"""把打好的包发布为 GitHub Release（本机手动发版用；CI 走 .github/workflows/release.yml）。

完整流程：
  1) build.bat                      产出安装包 + 代码包
  2) python tools/make_update_manifest.py --version 1.0.9 --runtime 1 ^
       --payload dist/ConfigAssistant-1.0.9.zip ^
       --installer dist/ConfigAssistant-Setup-1.0.9.exe ^
       --base-url https://github.com/Linsanity519/Auto-fill-rebot-inter/releases/download/v1.0.9
  3) python tools/publish_github_release.py --version 1.0.9 --notes "修了 xxx"

要求本机已安装并登录 GitHub CLI（gh auth login）。

⚠ 三个文件要一起传：代码包、安装包、latest.json。少传代码包的话，所有人都会被
  推去下几十 MB 的完整安装包（GitHub 在内网 20~40KB/s，那是 40 分钟）。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

REPO = "Linsanity519/Auto-fill-rebot-inter"
ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="发布配置助手 GitHub Release")
    parser.add_argument("--version", required=True, help="例如 1.0.9（不带 v）")
    parser.add_argument("--notes", default="", help="本次更新说明")
    parser.add_argument("--repo", default=REPO)
    args = parser.parse_args()

    gh = shutil.which("gh")
    if not gh:
        raise SystemExit("未找到 GitHub CLI（gh）。请先安装并执行 gh auth login。")

    dist = ROOT / "dist"
    assets = [
        dist / f"ConfigAssistant-{args.version}.zip",          # 代码包：日常更新下这个
        dist / f"ConfigAssistant-Setup-{args.version}.exe",    # 完整安装包
        dist / "latest.json",                                  # 更新描述
    ]
    missing = [str(p) for p in assets if not p.is_file()]
    if missing:
        raise SystemExit("缺少发布文件（先跑 build.bat 和 make_update_manifest.py）：\n"
                         + "\n".join(missing))

    command = [
        gh, "release", "create", f"v{args.version}", *[str(p) for p in assets],
        "--repo", args.repo, "--title", f"配置助手 v{args.version}",
        "--notes", args.notes or f"配置助手 v{args.version}",
    ]
    subprocess.run(command, check=True)
    print(f"已发布：https://github.com/{args.repo}/releases/tag/v{args.version}")
    for p in assets:
        print(f"  {p.name:42} {p.stat().st_size / 1024:>9.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
