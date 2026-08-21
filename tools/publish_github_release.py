"""把已打好的安装包和更新描述文件发布为 GitHub Release。

先执行 build.bat，再生成 latest.json：
  python tools/make_update_manifest.py --version 1.0.7 ^
    --installer dist/配置助手-Setup-1.0.7.exe ^
    --base-url https://github.com/Linsanity519/Auto-fill-rebot-inter/releases/download/v1.0.7

最后执行：
  python tools/publish_github_release.py --version 1.0.7 --notes "修复更新功能"

要求本机已安装并登录 GitHub CLI（gh auth login）。
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
    parser.add_argument("--version", required=True, help="例如 1.0.7（不带 v）")
    parser.add_argument("--notes", default="", help="本次更新说明")
    parser.add_argument("--repo", default=REPO)
    args = parser.parse_args()

    gh = shutil.which("gh")
    if not gh:
        raise SystemExit("未找到 GitHub CLI（gh）。请先安装并执行 gh auth login。")
    installer = ROOT / "dist" / f"配置助手-Setup-{args.version}.exe"
    manifest = ROOT / "dist" / "latest.json"
    missing = [str(path) for path in (installer, manifest) if not path.is_file()]
    if missing:
        raise SystemExit("缺少发布文件：\n" + "\n".join(missing))

    command = [
        gh, "release", "create", f"v{args.version}", str(installer), str(manifest),
        "--repo", args.repo, "--title", f"配置助手 v{args.version}",
        "--notes", args.notes or f"配置助手 v{args.version}",
    ]
    subprocess.run(command, check=True)
    print(f"已发布：https://github.com/{args.repo}/releases/tag/v{args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
