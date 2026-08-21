"""更新模块的离线冒烟测试：不访问真实发布站，也不写项目 output/。"""
from __future__ import annotations

import hashlib
import http.server
import json
import sys
import tempfile
import threading
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.update import UpdateService  # noqa: E402


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"OK  {label}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="formbot-update-") as temp:
        root = Path(temp)
        payload = b"config-assistant-installer-test\n" * 500
        installer = root / "配置助手-Setup-1.0.7.exe"
        installer.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()

        handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        (root / "latest.json").write_text(json.dumps({
            "version": "1.0.7", "download_url": f"{base}/{installer.name}",
            "sha256": digest, "size": len(payload), "notes": "测试更新",
        }, ensure_ascii=False), encoding="utf-8")

        try:
            service = UpdateService({"update": {
                "enabled": True, "manifest_url": f"{base}/latest.json", "check_interval_hours": 12,
            }}, "1.0.6")
            service.cache_path = root / "cache.json"
            service.download_dir = root / "downloads"
            found = service.check(force=True)
            check("发现更高版本", found["state"] == "available" and found["version"] == "1.0.7")
            downloaded = service.download()
            check("下载并校验成功", downloaded["ok"] and Path(downloaded["path"]).read_bytes() == payload)
            check("安装前复验成功", service.is_verified_installer(downloaded["path"]))
            Path(downloaded["path"]).write_bytes(b"tampered")
            check("篡改安装包会被拒绝", not service.is_verified_installer(downloaded["path"]))

            current = UpdateService({"update": {
                "enabled": True, "manifest_url": f"{base}/latest.json",
            }}, "1.0.7")
            current.cache_path = root / "current.json"
            check("相同版本不提示", current.check(force=True)["state"] == "current")
            check("未配置地址时关闭", UpdateService({}, "1.0.6").check()["state"] == "disabled")
        finally:
            server.shutdown()
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
