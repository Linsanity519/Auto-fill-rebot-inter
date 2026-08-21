"""封面图压缩：把图压到体积上限以内，尺寸一个像素都不动。

⚠ 商广后台对封面有硬限制：「推荐尺寸 4:3 或 16:9，不大于 700kb」。
  运营从剧集里截的图动辄 1~8MB，直接传会被后台打回。

⚠ 只压体积，绝不缩尺寸 —— 广告位对宽高比和分辨率有要求，
  改了尺寸等于换了一张素材。所以唯一能动的就是编码质量：
  统一重编码成 JPEG，质量从高往低试，第一个达标的就用。

  为什么不保 PNG：这些封面都是剧集截图（照片类），PNG 无损压缩对它们
  几乎没用，8MB 的图 optimize 完还是 7MB+；转 JPEG 才是数量级的差别。
  带透明通道的先合到白底上再转（封面不需要透明）。

压出来的文件放 output/_covers/，按「源文件 + 修改时间 + 目标大小」命名，
同一张图重复跑不会反复压。
"""
from __future__ import annotations

import logging
from pathlib import Path

from .paths import user_path

log = logging.getLogger(__name__)

CACHE_DIR = "_covers"
# 从高到低试的 JPEG 质量。步子前密后疏：大多数图在 85~70 就达标了，
# 真正的巨图才需要往下探，没必要在低质量区间磨。
QUALITY_STEPS = (92, 85, 78, 70, 62, 55, 48, 40, 32, 25, 20)


class ImageError(Exception):
    pass


def shrink(path: str | Path, max_bytes: int) -> str:
    """返回一个体积 <= max_bytes 的图片路径。本来就够小的原样返回。

    抛 ImageError 的情况：文件不存在、不是图片、压到最低质量还超标。
    """
    src = Path(path)
    if not src.exists():
        raise ImageError(f"图片不存在：{src}")

    size = src.stat().st_size
    if size <= max_bytes:
        return str(src)

    try:
        from PIL import Image
    except ImportError as e:
        raise ImageError(
            f"这张图 {size // 1024}KB，超过后台上限 {max_bytes // 1024}KB，"
            f"需要 Pillow 来压缩但没装：pip install Pillow") from e

    out_dir = user_path("output", CACHE_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{src.stem}_{int(src.stat().st_mtime)}_{max_bytes}.jpg"
    if dst.exists() and dst.stat().st_size <= max_bytes:
        return str(dst)

    try:
        im = Image.open(src)
        im.load()
    except Exception as e:
        raise ImageError(f"读不了这张图（{src.name}）：{e}") from e

    wh = im.size
    if im.mode in ("RGBA", "LA", "P"):
        # 透明通道合到白底上；JPEG 存不了 alpha
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", wh, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")

    for q in QUALITY_STEPS:
        im.save(dst, "JPEG", quality=q, optimize=True, progressive=True)
        got = dst.stat().st_size
        if got <= max_bytes:
            log.info("封面 %s %dKB → %dKB（质量 %d，尺寸仍是 %dx%d）",
                     src.name, size // 1024, got // 1024, q, wh[0], wh[1])
            return str(dst)

    raise ImageError(
        f"{src.name} 压到最低质量还有 {dst.stat().st_size // 1024}KB，"
        f"仍超过 {max_bytes // 1024}KB。这张图尺寸是 {wh[0]}x{wh[1]}，"
        f"太大了 —— 尺寸不能动，只能换一张。")
