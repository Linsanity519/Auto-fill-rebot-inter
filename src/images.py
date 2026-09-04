"""图片素材：网址下载 + 本地缓存。

Excel 的图片列有三种填法，都支持：
  · 本地路径              → 直接用
  · 图片贴在单元格里      → 读取阶段就抽成文件了，见 wizard_data._extract_images
  · http(s) 网址          → 这里下到 output/_images/ 再当本地文件用

素材本来就都在 CDN 上（i0.hdslb.com/bfs/vip/...），让人先另存到本地再填路径
纯属多此一举。同一个网址只下一次，重跑不重复下载。
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import urllib.error
import urllib.request
from pathlib import Path

from .paths import user_path

log = logging.getLogger(__name__)

IMG_DIR = "_images"
TIMEOUT = 20
MAX_BYTES = 20 * 1024 * 1024        # 20MB，创意素材远到不了这个量级
# 内网机器上 urllib 默认不带 UA 会被一些 CDN 挡掉。
# ⚠ HTTP 头只能是 latin-1，这里千万别写中文（写了会 UnicodeEncodeError，实测踩过）
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FormBot"}


class ImageError(Exception):
    pass


def normalize_url(value: str) -> str:
    """把「像网址的字符串」补成能下载的 http(s) 网址，不是网址就返回 ""。

    认这几种写法（素材列里常见的都在）：
      · https://i0.hdslb.com/...        → 原样
      · //i0.hdslb.com/...              → 协议相对写法，补 https:
        （从浏览器/HTML 里直接拷出来的链接常长这样，实测踩过）
    """
    v = str(value or "").strip()
    low = v.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return v
    if v.startswith("//"):
        return "https:" + v
    return ""


def is_url(value: str) -> bool:
    return bool(normalize_url(value))


def _suffix(url: str, content_type: str | None) -> str:
    """扩展名：先信网址，再信 Content-Type，都没有就当 png。

    ⚠ 后台的上传框按扩展名判类型，没扩展名的文件会被拒，所以必须给一个。
    """
    tail = url.split("?")[0].split("#")[0].rsplit("/", 1)[-1]
    if "." in tail:
        ext = "." + tail.rsplit(".", 1)[-1].lower()
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svga", ".mp4", ".json"):
            return ext
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            return ".jpg" if ext == ".jpe" else ext
    return ".png"


def fetch_image(url: str) -> Path:
    """把网址下成本地文件，返回路径。已经下过的直接复用。"""
    url = normalize_url(url) or str(url).strip()
    key = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]

    img_dir = user_path("output", IMG_DIR)
    img_dir.mkdir(parents=True, exist_ok=True)

    hit = next((p for p in img_dir.glob(f"url_{key}.*") if p.stat().st_size), None)
    if hit:
        log.info("图片用缓存：%s → %s", url, hit.name)
        return hit

    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = resp.read(MAX_BYTES + 1)
            ctype = resp.headers.get("Content-Type")
    except urllib.error.HTTPError as e:
        raise ImageError(f"下载图片失败（HTTP {e.code}）：{url}") from e
    except Exception as e:
        raise ImageError(f"下载图片失败：{url}　{e}") from e

    if not data:
        raise ImageError(f"下载到的图片是空的：{url}")
    if len(data) > MAX_BYTES:
        raise ImageError(f"图片超过 {MAX_BYTES // 1024 // 1024}MB，太大了：{url}")

    dst = img_dir / f"url_{key}{_suffix(url, ctype)}"
    dst.write_bytes(data)
    log.info("图片已下载：%s → %s（%d KB）", url, dst.name, len(data) // 1024)
    return dst


def prefetch(urls, workers: int = 8) -> tuple[int, list[str]]:
    """把这些网址并发下到缓存里，返回（成功几张，下不下来的网址）。

    ⚠ 为什么要单独预取：内网下 CDN 只有十几 KB/s，一张 240KB 的底图要 16 秒
      （实测）。而下载是插在填表中间做的 —— 用户看到的就是「填到图片那一行卡住」。
      图片本来就有磁盘缓存，问题只是「串行 + 挡在关键路径上」：23 张不同的图
      串下来 6 分钟，并发 8 路压到 1 分钟，而且是在第一个单元开填之前一次做完。

    ⚠ 这里不抛错：某张图下不下来，等真填到它时 _upload_by_label 会报
      带字段名的准确错误。预取阶段报错只会把「哪一行哪一列」这个信息丢掉。
      但**要把下不来的网址报出来** —— 素材列填错网址（改过文件名、贴了过期链接）
      是常事，早说一句就不用等跑到第 40 行才发现。
    """
    from concurrent.futures import ThreadPoolExecutor

    todo, seen = [], set()
    for u in urls:
        u = str(u or "").strip()
        if is_url(u) and u not in seen:
            seen.add(u)
            todo.append(u)
    if not todo:
        return 0, []

    def one(u):
        try:
            fetch_image(u)
            return True
        except Exception as e:
            log.info("预取图片失败（先跳过，填到再报）：%s %s", u, e)
            return False

    with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as pool:
        got = list(pool.map(one, todo))
    bad = [u for u, r in zip(todo, got) if not r]
    return len(todo) - len(bad), bad
