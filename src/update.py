"""程序自更新：检查版本、下载并校验更新包。

真正的文件替换由 ``tools/updater.py`` 的独立进程完成 —— 运行中的 Windows EXE
不能覆盖自己，正在被 import 的 src/ 也不该在自己脚下换掉。本模块只负责在主程序
还活着时做完所有网络工作，并把**校验通过**的包交给更新器。

━━ 两种更新包 ━━
整个程序 98MB，其中 src/ + assets/（真正每次发版会变的东西）只有 1.6MB，
压缩后不到 300KB。剩下 98% 是 playwright / Pillow / CPython 这些几乎从不变的
运行时。所以打包时把两者拆开（见 build.bat 的 --onedir + 外置 src/assets）：

  · 代码包 payload（~300KB）：src/ + assets/ + main.py 的 zip。日常发版走这个。
    GitHub 在国内实测只有 20~40KB/s，300KB 约 8 秒，98MB 要 40 分钟 ——
    这就是拆包的全部理由。
  · 完整安装包 installer（~45MB）：只有动了 requirements.txt（运行时变了）
    才需要。用 runtime 代号判断：代码包声明 min_runtime，本机 runtime.txt
    达不到就自动改走完整安装包。

━━ 下载地址是个列表 ━━
manifest 里每个包都给一组地址，按顺序试到通为止（国内镜像在前、GitHub 兜底）。
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

MAX_MANIFEST_BYTES = 256 * 1024
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024        # 代码包再大也不该有这个量级
MAX_INSTALLER_BYTES = 400 * 1024 * 1024


def _version_key(value: str) -> tuple[int, int, int]:
    """当前项目使用三段数字版本；不接受含糊的远端版本号。"""
    match = _VERSION_RE.fullmatch(str(value).strip())
    if not match:
        raise ValueError(f"版本号格式不正确：{value!r}（应为 X.Y.Z）")
    return tuple(int(item) for item in match.groups())


def _http_url(value: str) -> str:
    """URL 中的中文文件名需要编码；保留发布方已有的百分号编码。"""
    parts = urlsplit(str(value).strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"下载地址必须是 HTTP(S) 地址：{value!r}")
    return urlunsplit((parts.scheme, parts.netloc, quote(parts.path, safe="/%:@"),
                       parts.query, parts.fragment))


def installed_runtime() -> int:
    """本机运行时代号。

    由安装包写在 exe 旁边的 runtime.txt 里 —— 必须放在**代码包之外**：
    代码包会被更新覆盖，把代号放进去就等于让它自己声称自己是新的。
    读不到当 0：老的 onefile 安装（没有外置 src/）只能走完整安装包。
    """
    try:
        return int(user_path("runtime.txt").read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


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

    # ---------------- 缓存 ----------------
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
        """远端地址/校验值是内部实现细节，不交给前端。"""
        allowed = ("state", "version", "notes", "published_at", "mandatory",
                   "message", "kind", "size")
        return {key: result[key] for key in allowed if key in result}

    # ---------------- manifest ----------------
    def _fetch_manifest(self) -> dict:
        url = _http_url(self.conf.get("manifest_url", ""))
        request = urllib.request.Request(
            url, headers={"User-Agent": "ConfigAssistant-Updater/2"})
        with urllib.request.urlopen(request, timeout=self._timeout()) as response:
            raw = response.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError("更新描述文件过大")
        doc = json.loads(raw.decode("utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("更新描述文件必须是 JSON 对象")
        return doc

    @staticmethod
    def _spec(raw, key: str, limit: int) -> dict | None:
        """解析一个下载包的描述。地址可以是单个字符串，也可以是按序 fallback 的列表。"""
        if not isinstance(raw, dict):
            return None
        urls = raw.get("urls") or raw.get("url") or []
        if isinstance(urls, str):
            urls = [urls]
        urls = [_http_url(u) for u in urls if str(u).strip()]
        if not urls:
            raise ValueError(f"{key} 没有可用的下载地址")

        sha256 = str(raw.get("sha256", "")).strip().lower()
        if not _SHA256_RE.fullmatch(sha256):
            raise ValueError(f"{key}.sha256 必须是 64 位十六进制字符")
        try:
            size = int(raw.get("size", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key}.size 必须是正整数") from exc
        if not 0 < size <= limit:
            raise ValueError(f"{key}.size 超出合理范围：{size}")

        spec = {"urls": urls, "sha256": sha256, "size": size}
        if key == "payload":
            try:
                spec["min_runtime"] = int(raw.get("min_runtime", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("payload.min_runtime 必须是整数") from exc
        return spec

    def _validate_manifest(self, doc: dict) -> dict:
        version = str(doc.get("version", "")).strip()
        _version_key(version)

        payload = self._spec(doc.get("payload"), "payload", MAX_PAYLOAD_BYTES)
        installer = self._spec(doc.get("installer"), "installer", MAX_INSTALLER_BYTES)

        # v1 兼容：老 manifest 只有平铺的 download_url/sha256/size，指的是安装包。
        if installer is None and doc.get("download_url"):
            installer = self._spec(
                {"url": doc.get("download_url"), "sha256": doc.get("sha256"),
                 "size": doc.get("size")},
                "installer", MAX_INSTALLER_BYTES)
        if payload is None and installer is None:
            raise ValueError("更新描述文件里既没有 payload 也没有 installer")

        return {
            "version": version, "payload": payload, "installer": installer,
            "notes": str(doc.get("notes", "")).strip()[:2000],
            "published_at": str(doc.get("published_at", "")).strip()[:80],
            "mandatory": bool(doc.get("mandatory", False)),
        }

    @staticmethod
    def choose(manifest: dict) -> tuple[str, dict]:
        """决定这次下代码包还是完整安装包。

        代码包只有在本机运行时够新时才敢用 —— 否则新代码可能 import 到一个
        本机根本没有的库，装完直接起不来。
        """
        payload = manifest.get("payload")
        if payload and installed_runtime() >= payload.get("min_runtime", 0):
            return "payload", payload
        installer = manifest.get("installer")
        if not installer:
            raise ValueError("本机运行时过旧，而这个版本没有提供完整安装包")
        return "installer", installer

    # ---------------- 检查 ----------------
    def _evaluate(self, manifest: dict, checked_at: float) -> dict:
        """由 manifest 算出给界面看的结果。

        ⚠ 这一步必须**每次重新算**，不能把上次算好的 state 存起来直接用：
          state 是「manifest 版本 vs 本机版本」的比较结果，而本机版本会因为
          更新而改变。缓存 state 的后果实测过 —— 用户更新到 1.0.10 之后，
          缓存里还留着「有 1.0.10 可用」，12 小时内不再检查，于是每次打开都
          提示更新到自己已经在用的版本。而 output/ 是故意不被更新覆盖的
          （用户数据），这份缓存正好活了下来，更新越成功误报越准时。
        """
        newer = _version_key(manifest["version"]) > _version_key(self.current_version)
        result = {"state": "available" if newer else "current", "checked_at": checked_at,
                  "manifest": manifest, "version": manifest["version"],
                  "notes": manifest["notes"], "published_at": manifest["published_at"],
                  "mandatory": manifest["mandatory"]}
        if newer:
            kind, spec = self.choose(manifest)
            result["kind"] = kind
            result["size"] = spec["size"]
        self._latest = manifest if newer else None
        return result

    def check(self, force: bool = False) -> dict:
        if not self.enabled:
            return {"state": "disabled", "message": "未配置更新地址"}

        cached = self._read_cache()
        now = time.time()
        if not force and cached and isinstance(cached.get("manifest"), dict):
            try:
                fresh = now - float(cached.get("checked_at", 0)) < self._interval_seconds()
            except (TypeError, ValueError):
                fresh = False
            if fresh:
                try:
                    # 只复用「省下一次网络请求」，结论重新算
                    return self._public(self._evaluate(cached["manifest"],
                                                       float(cached["checked_at"])))
                except (KeyError, TypeError, ValueError):
                    pass        # 缓存坏了就当没有，往下走真检查

        try:
            manifest = self._validate_manifest(self._fetch_manifest())
            result = self._evaluate(manifest, now)
            self._write_cache(result)
            return self._public(result)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            log.info("检查更新失败：%s", exc)
            self._latest = None
            return {"state": "error", "message": f"检查更新失败：{exc}"}

    # ---------------- 下载 ----------------
    def _fetch_to(self, spec: dict, target: Path) -> None:
        """按顺序试每个地址，第一个完整下完并校验通过的算数。"""
        part = target.with_name(target.name + ".part")
        errors = []
        for url in spec["urls"]:
            host = urlsplit(url).netloc
            try:
                hasher = hashlib.sha256()
                received = 0
                request = urllib.request.Request(
                    url, headers={"User-Agent": "ConfigAssistant-Updater/2"})
                with urllib.request.urlopen(request, timeout=self._timeout()) as response, \
                        part.open("wb") as output:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        received += len(block)
                        if received > spec["size"]:
                            raise ValueError("下载文件大于描述中声明的大小")
                        hasher.update(block)
                        output.write(block)
                if received != spec["size"]:
                    raise ValueError(f"文件大小不匹配（收到 {received}，应为 {spec['size']}）")
                if hasher.hexdigest().lower() != spec["sha256"]:
                    raise ValueError("SHA-256 校验失败")
                os.replace(part, target)
                return
            except (OSError, ValueError, urllib.error.URLError) as exc:
                log.warning("从 %s 下载失败：%s", host, exc)
                errors.append(f"{host}: {exc}")
                try:
                    part.unlink(missing_ok=True)
                except OSError:
                    pass
        raise ValueError("所有下载地址都失败了 —— " + "；".join(errors))

    def download(self) -> dict:
        """下载到 output/updates；仅在 SHA-256、大小都对时返回路径。"""
        if not self._latest:
            checked = self.check(force=True)
            if checked.get("state") != "available":
                return {"ok": False, "error": checked.get("message", "没有可下载的新版本")}

        manifest = self._latest
        assert manifest is not None
        try:
            kind, spec = self.choose(manifest)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        self.download_dir.mkdir(parents=True, exist_ok=True)
        # 发布文件名固定用 ASCII：GitHub Release / Inno Setup 在不同 Windows 代码页下
        # 对中文附件名的处理并不一致，曾导致 manifest URL 与实际附件名不一致。
        name = (f"ConfigAssistant-{manifest['version']}.zip" if kind == "payload"
                else f"ConfigAssistant-Setup-{manifest['version']}.exe")
        target = self.download_dir / name

        # 已下好的也重验一遍，避免拿到上次中断或被换掉的文件。
        if target.exists() and self._matches(target, spec):
            return {"ok": True, "path": str(target), "kind": kind,
                    "version": manifest["version"]}
        try:
            self._fetch_to(spec, target)
        except (OSError, ValueError) as exc:
            log.warning("下载更新失败：%s", exc)
            return {"ok": False, "error": f"下载更新失败：{exc}"}
        return {"ok": True, "path": str(target), "kind": kind,
                "version": manifest["version"]}

    # ---------------- 交付前复核 ----------------
    @staticmethod
    def _matches(path: Path, spec: dict) -> bool:
        try:
            if path.stat().st_size != spec["size"]:
                return False
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            return digest.hexdigest().lower() == spec["sha256"]
        except OSError:
            return False

    def verify_downloaded(self, path: str) -> str | None:
        """交给更新器之前再校验一次，返回 'payload' / 'installer'，不合格返回 None。

        ⚠ 路径是前端传回来的，必须锁死在 output/updates 里并重算摘要 ——
          否则等于让页面指定「用哪个文件覆盖程序」。
        """
        if not self._latest:
            return None
        try:
            kind, spec = self.choose(self._latest)
            candidate = Path(path).resolve()
            if candidate.parent != self.download_dir.resolve() or not candidate.is_file():
                return None
            return kind if self._matches(candidate, spec) else None
        except (OSError, ValueError):
            return None
