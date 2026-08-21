"""生成大会员 logo 图标：粉色圆形 + 白色「大」字。

产出 assets/icon.ico（多尺寸，给窗口和 exe 用）和 assets/logo.png（给界面内嵌用）。
用法：python tools/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"

PINK = (251, 92, 138, 255)      # 大会员粉
WHITE = (255, 255, 255, 255)

# 中文字体候选：黑体系优先，笔画粗，缩到 16px 还认得出
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",   # 微软雅黑 Bold
    r"C:\Windows\Fonts\msyh.ttc",     # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",   # 黑体
    r"C:\Windows\Fonts\simsun.ttc",   # 宋体（兜底）
]


def load_font(size: int):
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def render(size: int) -> Image.Image:
    """按 size 渲染一张图。4 倍超采样再缩回来，边缘才平滑。"""
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆形底
    d.ellipse([0, 0, s - 1, s - 1], fill=PINK)

    # 「大」字占直径 ~52%，四周留出和原 logo 相当的边距
    font = load_font(int(s * 0.52))
    box = d.textbbox((0, 0), "大", font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((s - w) / 2 - box[0], (s - h) / 2 - box[1]), "大", font=font, fill=WHITE)

    return img.resize((size, size), Image.LANCZOS)


def main():
    OUT.mkdir(parents=True, exist_ok=True)

    sizes = [256, 128, 64, 48, 32, 24, 16]
    imgs = [render(n) for n in sizes]

    ico = OUT / "icon.ico"
    imgs[0].save(ico, format="ICO", sizes=[(n, n) for n in sizes])
    print(f"已生成：{ico}")

    for n in (64, 128):
        p = OUT / f"logo{n}.png"
        render(n).save(p, format="PNG")
        print(f"已生成：{p}")


if __name__ == "__main__":
    main()
