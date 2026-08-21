"""更新模块的离线冒烟测试：起一个本地 HTTP 服务当发布站，不碰真实网络和项目 output/。

跑法：python tools\\test_update.py
"""
from __future__ import annotations

import hashlib
import http.server
import json
import sys
import tempfile
import threading
import zipfile
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import update as update_mod  # noqa: E402
from src.update import UpdateService  # noqa: E402


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"OK  {label}")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_service(base: str, root: Path, version: str, name: str) -> UpdateService:
    service = UpdateService({"update": {
        "enabled": True, "manifest_url": f"{base}/latest.json",
        "check_interval_hours": 12,
    }}, version)
    service.cache_path = root / f"cache-{name}.json"
    service.download_dir = root / "downloads"
    return service


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="formbot-update-") as temp:
        root = Path(temp)
        downloads = root / "downloads"

        # ---- 两个包：300KB 的代码包 + 大得多的完整安装包 ----
        zip_path = root / "ConfigAssistant-1.0.9.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            z.writestr("main.py", "print('new')")
            z.writestr("src/__init__.py", "__version__='1.0.9'")
            z.writestr("assets/webui/index.html", "<html></html>")
        zip_bytes = zip_path.read_bytes()

        exe_bytes = b"config-assistant-installer-test\n" * 5000
        exe_path = root / "ConfigAssistant-Setup-1.0.9.exe"
        exe_path.write_bytes(exe_bytes)

        handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"

        payload_spec = {
            # 第一个地址是坏的：用来验「按顺序 fallback」确实生效
            "urls": [f"{base}/does-not-exist.zip", f"{base}/{zip_path.name}"],
            "sha256": sha256(zip_bytes), "size": len(zip_bytes), "min_runtime": 1,
        }
        installer_spec = {
            "urls": [f"{base}/{exe_path.name}"],
            "sha256": sha256(exe_bytes), "size": len(exe_bytes),
        }
        (root / "latest.json").write_text(json.dumps({
            "version": "1.0.9", "payload": payload_spec, "installer": installer_spec,
            "notes": "测试更新",
        }, ensure_ascii=False), encoding="utf-8")

        real_runtime = update_mod.installed_runtime
        try:
            # ---------- 1. 运行时够新 → 走 300KB 代码包 ----------
            update_mod.installed_runtime = lambda: 1
            svc = make_service(base, root, "1.0.8", "payload")
            found = svc.check(force=True)
            check("发现新版本", found["state"] == "available" and found["version"] == "1.0.9")
            check("默认选代码包", found["kind"] == "payload")
            check("告诉前端体积", found["size"] == len(zip_bytes))

            got = svc.download()
            check("首个地址失败后自动换下一个", got["ok"] and got["kind"] == "payload")
            check("下载内容正确", Path(got["path"]).read_bytes() == zip_bytes)
            check("交付前复验通过", svc.verify_downloaded(got["path"]) == "payload")

            Path(got["path"]).write_bytes(b"tampered")
            check("被换掉的包会被拒绝", svc.verify_downloaded(got["path"]) is None)
            outsider = root / "outsider.zip"
            outsider.write_bytes(zip_bytes)
            check("下载目录之外的路径会被拒绝", svc.verify_downloaded(str(outsider)) is None)

            # ---------- 2. 运行时过旧 → 自动改走完整安装包 ----------
            update_mod.installed_runtime = lambda: 0
            svc2 = make_service(base, root, "1.0.8", "installer")
            found2 = svc2.check(force=True)
            check("运行时过旧时改走完整安装包", found2["kind"] == "installer")
            got2 = svc2.download()
            check("安装包下载并校验成功",
                  got2["ok"] and Path(got2["path"]).read_bytes() == exe_bytes)
            check("安装包复验通过", svc2.verify_downloaded(got2["path"]) == "installer")

            # ---------- 3. 老 manifest（v1 平铺字段）仍然认得 ----------
            (root / "v1.json").write_text(json.dumps({
                "version": "1.0.9", "download_url": f"{base}/{exe_path.name}",
                "sha256": sha256(exe_bytes), "size": len(exe_bytes),
            }, ensure_ascii=False), encoding="utf-8")
            svc3 = UpdateService({"update": {
                "enabled": True, "manifest_url": f"{base}/v1.json"}}, "1.0.8")
            svc3.cache_path = root / "cache-v1.json"
            svc3.download_dir = downloads
            check("v1 老描述文件仍可用", svc3.check(force=True)["kind"] == "installer")

            # ---------- 4. 边界 ----------
            update_mod.installed_runtime = lambda: 1
            same = make_service(base, root, "1.0.9", "same")
            check("相同版本不提示", same.check(force=True)["state"] == "current")
            newer = make_service(base, root, "1.1.0", "newer")
            check("本机更新时不降级", newer.check(force=True)["state"] == "current")
            check("未配置地址时关闭", UpdateService({}, "1.0.8").check()["state"] == "disabled")

            bad = make_service(base, root, "1.0.8", "bad")
            bad.conf = dict(bad.conf, manifest_url=f"{base}/nope.json")
            check("发布站不可达只报错、不抛异常",
                  bad.check(force=True)["state"] == "error")
        finally:
            update_mod.installed_runtime = real_runtime
            server.shutdown()

    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
