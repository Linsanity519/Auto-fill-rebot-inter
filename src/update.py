"""程序自更新：检查版本、下载并校验安装包。

真正的文件替换由 ``tools/updater.py`` 的独立进程完成。运行中的 Windows
EXE 不能覆盖自己；本模块只负责在主程序还活着时完成所有网络工作，并把经过
校验的安装包交给更新器。

更新服务默认关闭。发布时在 ``config/settings.yaml`` 填入 update.manifest_url
即可启用，不依赖任何特定的内网产品或地址。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit, urlunsplit
from pathlib import Path

from .paths import user_path

log = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _version_key(value: str) -> tuple[int, int, int]:
    """当前项目使用三段数字版本；不接受含糊的远端版本号。"""
    match = _VERSION_RE.fullmatch(str(value).strip())
    if not match:
        raise ValueError(f"版本号格式不正确：{value!r}（应为 X.Y.Z）")
    return tuple(int(item) for item in match.groups())


def _http_url(value: str) -> str:
    """URL 中的中文安装包名需要编码；保留发布方已有的百分号编码。"""
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("下载地址必须是 HTTP(S) 地址")
    return urlunsplit((parts.scheme, parts.netloc, quote(parts.path, safe="/%:@"),
                       parts.query, parts.fragment))


class UpdateService:
    """无 UI、可单测的更新服务。所有异常都转成给界面展示的结果。"""

    def __init__(self, settings: dict, current_version: str):
        self.settings = settings or {}
        self.current_version = current_version
        self.conf = self.settings.get("update") or {}
        self.cache_path = user_path("output", "update-status.json")
        self.download_dir = user_path("output", "updates")
        self._latest: dict | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.conf.get("enabled")) and bool(self.conf.get("manifest_url"))

    def _timeout(self) -> float:
        try:
            return max(2.0, min(float(self.conf.get("timeout_seconds", 10)), 60.0))
        except (TypeError, ValueError):
            return 10.0

    def _interval_seconds(self) -> float:
        try:
            return max(0.0, float(self.conf.get("check_interval_hours", 12)) * 3600)
        except (TypeError, ValueError):
            return 12 * 3600

    def _read_cache(self) -> dict | None:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write_cache(self, result: dict) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.cache_path)
        except OSError:
            log.warning("写更新状态缓存失败", exc_info=True)

    @staticmethod
    def _public(result: dict) -> dict:
        """远端 URL/校验值是内部实现细节，不交给前端。"""
        allowed = ("state", "version", "notes", "published_at", "mandatory", "message")
        return {key: result[key] for key in allowed if key in result}

    def _fetch_manifest(self) -> dict:
        url = _http_url(str(self.conf.get("manifest_url", "")).strip())
        request = urllib.request.Request(url, headers={"User-Agent": "ConfigAssistant-Updater/1"})
        with urllib.request.urlopen(request, timeout=self._timeout()) as response:
            raw = response.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError("更新描述文件过大")
        doc = json.loads(raw.decode("utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("更新描述文件必须是 JSON 对象")
        return doc

    def _validate_manifest(self, doc: dict) -> dict:
        version = str(doc.get("version", "")).strip()
        _version_key(version)
        url = str(doc.get("download_url", "")).strip()
        sha256 = str(doc.get("sha256", "")).strip().lower()
        url = _http_url(url)
        if not _SHA256_RE.fullmatch(sha256):
            raise ValueError("sha256 必须是 64 位十六进制字符")
        try:
            size = int(doc.get("size", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("size 必须是正整数") from exc
        if size <= 0:
            raise ValueError("size 必须是正整数")
        return {
            "version": version, "download_url": url, "sha256": sha256, "size": size,
            "notes": str(doc.get("notes", "")).strip()[:2000],
            "published_at": str(doc.get("published_at", "")).strip()[:80],
            "mandatory": bool(doc.get("mandatory", False)),
        }

    def check(self, force: bool = False) -> dict:
        if not self.enabled:
            return {"state": "disabled", "message": "未配置更新地址"}

        cached = self._read_cache()
        now = time.time()
        if not force and cached:
            try:
                fresh = now - float(cached.get("checked_at", 0)) < self._interval_seconds()
            except (TypeError, ValueError):
                fresh = False
            if fresh:
                self._latest = cached.get("manifest") if cached.get("state") == "available" else None
                return self._public(cached)

        try:
            manifest = self._validate_manifest(self._fetch_manifest())
            state = "available" if _version_key(manifest["version"]) > _version_key(self.current_version) else "current"
            result = {"state": state, "checked_at": now, "manifest": manifest,
                      "version": manifest["version"], "notes": manifest["notes"],
                      "published_at": manifest["published_at"], "mandatory": manifest["mandatory"]}
            self._latest = manifest if state == "available" else None
            self._write_cache(result)
            return self._public(result)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            log.info("检查更新失败：%s", exc)
            self._latest = None
            return {"state": "error", "message": f"检查更新失败：{exc}"}

    def download(self) -> dict:
        """下载到 output/updates；仅在 SHA-256、大小均正确时返回路径。"""
        if not self._latest:
            checked = self.check(force=True)
            if checked.get("state") != "available":
                return {"ok": False, "error": checked.get("message", "没有可下载的新版本")}

        manifest = self._latest
        assert manifest is not None
        self.download_dir.mkdir(parents=True, exist_ok=True)
        target = self.download_dir / f"配置助手-Setup-{manifest['version']}.exe"
        part = target.with_suffix(".exe.part")

        # 已完成的文件仍重新验一遍，避免拿到上次中断或被误替换的文件。
        if target.exists() and self._matches(target, manifest):
            return {"ok": True, "path": str(target), "version": manifest["version"]}

        try:
            hasher = hashlib.sha256()
            received = 0
            request = urllib.request.Request(manifest["download_url"], headers={"User-Agent": "ConfigAssistant-Updater/1"})
            with urllib.request.urlopen(request, timeout=self._timeout()) as response, part.open("wb") as output:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    received += len(block)
                    if received > manifest["size"]:
                        raise ValueError("下载文件大于版本描述中声明的大小")
                    hasher.update(block)
                    output.write(block)
            if received != manifest["size"]:
                raise ValueError(f"文件大小不匹配（收到 {received}，应为 {manifest['size']}）")
            if hasher.hexdigest().lower() != manifest["sha256"]:
                raise ValueError("SHA-256 校验失败，安装包已丢弃")
            os.replace(part, target)
            return {"ok": True, "path": str(target), "version": manifest["version"]}
        except (OSError, ValueError, urllib.error.URLError) as exc:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            log.warning("下载更新失败：%s", exc)
            return {"ok": False, "error": f"下载更新失败：{exc}"}

    @staticmethod
    def _matches(path: Path, manifest: dict) -> bool:
        if path.stat().st_size != manifest["size"]:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest().lower() == manifest["sha256"]

    def is_verified_installer(self, path: str) -> bool:
        """安装前再校验一次，避免前端传来的路径被替换。"""
        if not self._latest:
            return False
        try:
            candidate = Path(path).resolve()
            return (candidate.parent == self.download_dir.resolve()
                    and candidate.is_file() and self._matches(candidate, self._latest))
        except OSError:
            return False
