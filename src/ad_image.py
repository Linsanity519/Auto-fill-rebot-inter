"""封面图：网址下载 + 压到体积上限以内（尺寸一个像素都不动）。原生商广老 / 新两套共用。

⚠ 商广后台对封面有硬限制：「推荐尺寸 4:3 或 16:9，不大于 700kb」。
  运营从剧集里截的图动辄 1~8MB，直接传会被后台打回。

⚠ 只压体积，绝不缩尺寸 —— 广告位对宽高比和分辨率有要求，
  改了尺寸等于换了一张素材。所以唯一能动的就是编码质量：
  先看体积，本来就 <= 上限的原样上传、一个字节都不动；
  超了才重编码成 JPEG，按质量二分查找「达标的最高质量」。

  为什么不保 PNG：这些封面都是剧集截图（照片类），PNG 无损压缩对它们
  几乎没用，8MB 的图 optimize 完还是 7MB+；转 JPEG 才是数量级的差别。
  带透明通道的先合到白底上再转（封面不需要透明）。

  为什么二分而不是从高往低逐档试：逐档试停在「第一个达标的档」，档与档之间
  差 7 个质量点，白白多压掉一截画质；二分 6~7 次编码就能落到达标的最高那一点。

压出来的文件放 output/_covers/，按「源文件 + 修改时间 + 目标大小」命名，
同一张图重复跑不会反复压。

封面单元格的填法（见 resolve_cover）：
  · 留空                     → 不动页面，用稿件自己的默认封面（不报错、不暂停）
  · http(s) 网址              → 开跑前全部下好（prefetch_covers，失败自动重试），再压、再传
  · 本地路径 / 贴进单元格的图 → 直接压、再传
"""
from __future__ import annotations

import io
import logging
import time
import urllib.error
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from . import images
from .paths import user_path

log = logging.getLogger(__name__)

CACHE_DIR = "_covers"
# 二分查找的质量区间。低于 20 的 JPEG 糊得没法当封面，还超标就该换图了。
Q_MIN, Q_MAX = 20, 95
# 下载：一张最多试几次、两次之间等几秒（逐次加长）。
# ⚠ 内网 CDN 偶发超时 / 断流是常事，一次失败就拦整批太脆；但 404 这种重试也没用。
FETCH_TRIES = 3
FETCH_BACKOFF = (2, 5)
WORKERS = 8


class ImageError(Exception):
    pass


# ================================================================ 下载
def _retryable(e: Exception) -> bool:
    """网络抖动 / 5xx 值得重试；4xx（网址写错、链接过期）重试多少遍都一样。"""
    cause = e.__cause__
    if isinstance(cause, urllib.error.HTTPError):
        return cause.code >= 500 or cause.code in (408, 429)
    return True


def fetch_cover(url: str, tries: int = FETCH_TRIES) -> Path:
    """下载一张封面，失败按 FETCH_BACKOFF 重试。有磁盘缓存，下过的直接复用。"""
    last: Exception | None = None
    for n in range(1, tries + 1):
        try:
            return images.fetch_image(url)
        except images.ImageError as e:
            last = e
            if n == tries or not _retryable(e):
                break
            wait = FETCH_BACKOFF[min(n - 1, len(FETCH_BACKOFF) - 1)]
            log.info("封面下载失败（第 %d/%d 次），%ds 后重试：%s", n, tries, wait, e)
            time.sleep(wait)
    raise ImageError(f"{last}（试了 {n} 次）")


def prefetch_covers(items, say, tries: int = FETCH_TRIES, checkpoint=None,
                    workers: int = WORKERS) -> None:
    """开跑前把封面网址全部并发下好；有下不下来的，列全了一次性抛 ImageError。

    items:      [(单元格的值, 行号), ...]，非网址的（留空 / 本地路径）自动忽略。
    say:        ui.log
    checkpoint: ui.checkpoint —— 下载期间每半秒调一次，用户点「停止」时它抛 Stopped，
                这里取消还没开始的下载、原样往上抛（正在下的那几张下完自己结束）。

    ⚠ 为什么「先全下完再报」而不是填到那一条再报：
      填到第 30 个单元才发现第 30 张图下不来，前 29 个单元已经填了（老的还已经保存了），
      用户得处理「跑了一半」的烂摊子；而且坏链接一次只冒一个，改一个跑一遍。
      开跑前全下完、坏的一次列全，要么全部能传，要么一个单元都不动。
    ⚠ 内网 CDN 慢：实测 399 张 8 路并发要 11 分钟。所以必须边下边报进度 ——
      原来只在开头说一句、下完再说一句，中间 11 分钟日志一动不动，看着就是卡死了；
      也必须能停，否则点了「停止」要干等到下完。
    """
    rows: dict[str, list[int]] = {}
    for value, row in items:
        u = str(value or "").strip()
        if images.is_url(u):
            rows.setdefault(u, []).append(row)
    if not rows:
        return

    n = len(rows)
    say(f"先把 {n} 张封面下到本地（{min(workers, n)} 路并发，失败的自动重试 {tries - 1} 次；"
        f"网慢的话要等几分钟，下面会报进度）")
    t0 = time.monotonic()

    errs: dict[str, str] = {}
    done = 0
    step = max(10, n // 10)             # 大约每 10% 报一次
    next_mark, last_say = step, t0
    pool = ThreadPoolExecutor(max_workers=min(workers, n))
    futs = {pool.submit(fetch_cover, u, tries): u for u in rows}
    pending = set(futs)
    try:
        while pending:
            if checkpoint:
                checkpoint()
            finished, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for f in finished:
                done += 1
                try:
                    f.result()
                except Exception as e:
                    errs[futs[f]] = str(e)
            now = time.monotonic()
            # 按比例报；太久没动静（大图、慢网）也报一次，免得看着像卡死
            if pending and (done >= next_mark or now - last_say >= 30):
                left = (now - t0) / done * (n - done) if done else 0
                say(f"    封面已下载 {done}/{n}，用了 {now - t0:.0f}s"
                    + (f"，预计还要 {left:.0f}s" if done else "")
                    + (f"（{len(errs)} 张失败，最后一起报）" if errs else ""))
                last_say = now
                while next_mark <= done:
                    next_mark += step
    except BaseException:
        # 点了「停止」（Stopped）或别的意外：没开始的全取消，正在下的让它自己跑完（有 TIMEOUT 兜底）
        pool.shutdown(wait=False, cancel_futures=True)
        say(f"封面下载中止（已下好 {done}/{n} 张，下次开跑会直接用缓存）", "warn")
        raise
    pool.shutdown(wait=True)

    bad = [(u, errs[u]) for u in rows if u in errs]
    say(f"封面下载好 {n - len(bad)}/{n} 张，用了 {time.monotonic() - t0:.0f}s",
        "ok" if not bad else "error")
    if not bad:
        return

    lines = []
    for u, err in bad[:10]:
        where = "、".join(f"第{r}行" for r in rows[u][:5])
        lines.append(f"　{where}：{err}")
    if len(bad) > 10:
        lines.append(f"　…… 还有 {len(bad) - 10} 张")
    raise ImageError(
        f"有 {len(bad)} 张封面下载失败，为了不跑一半，一个单元都没开始填：\n"
        + "\n".join(lines)
        + "\n把这些网址改对（或清空这一格 = 用稿件默认封面）再跑。")


# ================================================================ 解析 + 体检
def resolve_cover(value: str, max_bytes: int) -> str:
    """单元格里填的封面 → 一个能直接传的本地文件（体积 <= max_bytes）。

    调用方负责先判空：留空 = 用默认封面，根本不该走到这里。
    """
    v = str(value or "").strip()
    if images.is_url(v):
        try:
            src = fetch_cover(v)          # 开跑前已经下好，这里命中缓存
        except ImageError as e:
            raise ImageError(f"封面{e}") from e
    else:
        src = Path(v)
        if not src.exists():
            raise ImageError(f"封面文件不存在：{v}")
    return shrink(src, max_bytes)


def cover_problem(value: str) -> str:
    """离线体检：这个封面值有没有问题。没问题返回 ""。

    ⚠ 网址不在这里下载（内网慢，「载入并检查」不该卡几分钟），只拦「既不是网址、本地也没有」的。
    """
    v = str(value or "").strip()
    if not v or images.is_url(v) or Path(v).exists():
        return ""
    return (f"找不到：{v}（可以填 http 网址、本地图片路径，或直接把图贴进单元格；"
            f"留空 = 用稿件默认封面）")


# ================================================================ 压缩
def _encode(im, q: int) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=q, optimize=True, progressive=True)
    return buf.getvalue()


def shrink(path: str | Path, max_bytes: int) -> str:
    """返回一个体积 <= max_bytes 的图片路径。

    先看体积：本来就 <= max_bytes 的原样返回，不重编码。
    超了才转 JPEG，二分查找 [Q_MIN, Q_MAX] 里达标的最高质量。
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
    if dst.exists() and 0 < dst.stat().st_size <= max_bytes:
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

    best: tuple[int, bytes] | None = None
    lo, hi, tries = Q_MIN, Q_MAX, 0
    while lo <= hi:
        q = (lo + hi) // 2
        data = _encode(im, q)
        tries += 1
        if len(data) <= max_bytes:
            best = (q, data)
            lo = q + 1          # 达标：往高质量那半边找
        else:
            hi = q - 1

    if best is None:
        smallest = len(_encode(im, Q_MIN))
        raise ImageError(
            f"{src.name} 压到最低质量还有 {smallest // 1024}KB，"
            f"仍超过 {max_bytes // 1024}KB。这张图尺寸是 {wh[0]}x{wh[1]}，"
            f"太大了 —— 尺寸不能动，只能换一张。")

    q, data = best
    dst.write_bytes(data)
    log.info("封面 %s %dKB → %dKB（质量 %d，编码 %d 次，尺寸仍是 %dx%d）",
             src.name, size // 1024, len(data) // 1024, q, tries, wh[0], wh[1])
    return str(dst)
