"""B站风格主题。

tkinter 原生控件长得很 Windows 98，这里用 Canvas 自绘按钮拿到圆角，
配合 ttk.Style 把表格、进度条、下拉框的颜色统一成 B 站那套。
"""
import logging
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

log = logging.getLogger(__name__)

# ---------- 配色（取自 bilibili.com 设计规范）----------
PINK = "#FB7299"          # 主色 · 粉
PINK_HOVER = "#FF85AD"
PINK_ACTIVE = "#E5638A"
PINK_LIGHT = "#FFF0F5"
BLUE = "#23ADE5"          # 辅色 · 蓝
BLUE_HOVER = "#3BB9EA"
GREEN = "#00A1D6"
SUCCESS = "#2BA471"
DANGER = "#F56C6C"
WARNING = "#FF9A2E"

BG = "#F6F7F8"            # 页面底
CARD = "#FFFFFF"          # 卡片
BORDER = "#E3E5E7"
TEXT = "#18191C"          # 主文字
TEXT_SUB = "#61666D"      # 次要文字
TEXT_MUTED = "#9499A0"    # 弱化文字

CONSOLE_BG = "#1C1F23"
CONSOLE_FG = "#C9CCD0"


# ---------- 高分屏缩放 ----------
# 不声明 DPI 感知的话，Windows 会让程序按 96 DPI 画完，再把整张图拉伸到实际缩放
# 比例（150% 就是拉 1.5 倍），结果就是满屏糊字。声明之后拿到的是物理像素，
# 字变清楚了，但所有写死的像素尺寸也跟着"缩水"成原来的 2/3，所以必须配一个
# 缩放系数把它们乘回去 —— 两件事得一起做，只做一半界面会小到没法用。
_SCALE = 1.0


def enable_dpi_awareness():
    """声明 DPI 感知。必须在 Tk() 之前调用，建窗口之后再声明就没用了。

    用 system-aware(1) 而不是 per-monitor(2)：Tk 没法在窗口被拖到另一块不同
    缩放的屏幕时重算尺寸，声明 per-monitor 只会让副屏上的布局直接错乱；
    system-aware 下 Windows 仍会替我们拉伸副屏，糊一点但不会坏。
    """
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # Win 8.1+
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()        # Win Vista+ 兜底
    except Exception:
        log.info("声明 DPI 感知失败，高分屏下界面会发虚", exc_info=True)


def init_scaling(root) -> float:
    """读实际 DPI，定标缩放系数，并让 Tk 按同样的比例换算字号。

    字号在 tkinter 里是「点」，Tk 用 scaling 值换算成像素，所以设好 scaling
    之后字体自己就对了，要手动乘的只有写死的像素值（见 px）。
    """
    global _SCALE
    dpi = root.winfo_fpixels("1i")
    if not dpi or dpi <= 0:
        return _SCALE
    _SCALE = dpi / 96.0
    root.tk.call("tk", "scaling", dpi / 72.0)
    log.info("显示 DPI %.0f，缩放系数 %.2f", dpi, _SCALE)
    return _SCALE


def px(n: float) -> int:
    """把按 96 DPI 写的像素值换算成当前屏幕的物理像素。"""
    return max(1, round(n * _SCALE))


def scale() -> float:
    return _SCALE


def fonts():
    """微软雅黑打底，没有就退回系统默认。

    字号照常写「点」，不要在这里乘 _SCALE —— init_scaling 设过 tk scaling 之后
    Tk 会自己按 DPI 换算，再乘一遍就是双重放大。
    """
    fam = "Microsoft YaHei UI"
    if fam not in tkfont.families():
        fam = "Microsoft YaHei" if "Microsoft YaHei" in tkfont.families() else "Segoe UI"
    return {
        "title": (fam, 14, "bold"),
        "h2": (fam, 10, "bold"),
        "body": (fam, 9),
        "small": (fam, 8),
        "mono": ("Consolas", 9),
    }


class RoundButton(tk.Canvas):
    """圆角按钮。ttk 做不出圆角，只能自绘。

    kind: primary(粉底白字) / secondary(白底描边) / ghost(纯文字) / blue
    """

    STYLES = {
        "primary": dict(bg=PINK, hover=PINK_HOVER, active=PINK_ACTIVE, fg="#FFFFFF", border=None),
        "blue": dict(bg=BLUE, hover=BLUE_HOVER, active="#1B98C9", fg="#FFFFFF", border=None),
        "secondary": dict(bg=CARD, hover=PINK_LIGHT, active="#FFE3EC", fg=TEXT, border=BORDER),
        "ghost": dict(bg=None, hover=PINK_LIGHT, active="#FFE3EC", fg=TEXT_SUB, border=None),
    }

    def __init__(self, parent, text, command=None, kind="secondary",
                 width=None, height=32, radius=8, font=None, **kw):
        self.style = dict(self.STYLES[kind])
        self.parent_bg = kw.pop("parent_bg", CARD)
        self.f = font or fonts()["body"]
        self._text = text
        self._command = command
        self._enabled = True

        # measure() 量的是缩放后字体的真实像素，不用再乘；左右留白和最小宽度
        # 是按 96 DPI 写死的，得换算。width 由调用方传进来时同理。
        tmp = tkfont.Font(font=self.f)
        w = px(width) if width else max(px(72), tmp.measure(text) + px(32))
        height = px(height)

        super().__init__(parent, width=w, height=height, highlightthickness=0,
                         bd=0, bg=self.parent_bg, cursor="hand2", **kw)
        self.radius = px(radius)
        # ⚠ 不能叫 _w / _h：tkinter 的 Misc 内部用 self._w 存控件路径名，
        #   覆盖掉会让所有 Canvas 方法报 invalid command name
        self.bw, self.bh = w, height
        self._draw(self.style["bg"])

        self.bind("<Enter>", lambda e: self._enabled and self._draw(self.style["hover"]))
        self.bind("<Leave>", lambda e: self._enabled and self._draw(self.style["bg"]))
        self.bind("<ButtonPress-1>", lambda e: self._enabled and self._draw(self.style["active"]))
        self.bind("<ButtonRelease-1>", self._release)

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.create_polygon(pts, smooth=True, **kw)

    def _draw(self, fill):
        self.delete("all")
        fg = self.style["fg"] if self._enabled else TEXT_MUTED
        if fill is None:
            fill = self.parent_bg
        if not self._enabled:
            fill = "#F1F2F3" if self.style["bg"] else self.parent_bg
        self._round_rect(1, 1, self.bw - 1, self.bh - 1, self.radius,
                         fill=fill, outline=self.style["border"] or fill)
        self.create_text(self.bw / 2, self.bh / 2, text=self._text, fill=fg, font=self.f)

    def _release(self, _e):
        if not self._enabled:
            return
        self._draw(self.style["hover"])
        if self._command:
            self._command()

    def config_state(self, enabled: bool):
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw(self.style["bg"])

    def set_text(self, text):
        self._text = text
        self._draw(self.style["bg"])


def card(parent, **kw):
    """白色卡片：细边框 + 内边距。"""
    return tk.Frame(parent, bg=CARD, highlightthickness=1,
                    highlightbackground=BORDER, highlightcolor=BORDER, **kw)


def section_title(parent, text, sub=None):
    """左侧一道粉色竖条 + 标题，B站后台常见的分节样式。"""
    F = fonts()
    bar = tk.Frame(parent, bg=CARD)
    strip = tk.Frame(bar, bg=PINK, width=px(3), height=px(14))
    strip.pack(side="left", padx=(0, px(8)))
    strip.pack_propagate(False)
    tk.Label(bar, text=text, bg=CARD, fg=TEXT, font=F["h2"]).pack(side="left")
    if sub:
        tk.Label(bar, text=sub, bg=CARD, fg=TEXT_MUTED, font=F["small"]).pack(
            side="left", padx=(px(8), 0)
        )
    return bar


def apply_ttk(root):
    """把 ttk 控件（表格/进度条/下拉/输入框）也刷成 B站配色。"""
    F = fonts()
    st = ttk.Style(root)
    try:
        st.theme_use("clam")   # clam 才允许充分改色
    except tk.TclError:
        pass

    st.configure("TFrame", background=CARD)
    st.configure("Bg.TFrame", background=BG)
    st.configure("TLabel", background=CARD, foreground=TEXT, font=F["body"])
    st.configure("Muted.TLabel", background=CARD, foreground=TEXT_MUTED, font=F["small"])

    # 表格
    st.configure("Treeview",
                 background=CARD, fieldbackground=CARD, foreground=TEXT,
                 rowheight=px(30), borderwidth=0, font=F["body"])
    st.configure("Treeview.Heading",
                 background="#F1F2F3", foreground=TEXT_SUB,
                 relief="flat", font=F["body"], padding=(px(6), px(6)))
    st.map("Treeview.Heading", background=[("active", "#E9EAEB")])
    st.map("Treeview",
           background=[("selected", PINK_LIGHT)],
           foreground=[("selected", PINK)])
    st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])   # 去掉外框

    # 进度条
    st.configure("Bili.Horizontal.TProgressbar",
                 troughcolor="#EDEEF0", background=PINK,
                 borderwidth=0, thickness=px(8), lightcolor=PINK, darkcolor=PINK)

    # 输入框 / 下拉
    for name in ("TEntry", "TCombobox"):
        st.configure(name, fieldbackground=CARD, background=CARD,
                     foreground=TEXT, bordercolor=BORDER, arrowcolor=TEXT_SUB,
                     lightcolor=BORDER, darkcolor=BORDER, insertcolor=TEXT,
                     padding=px(5))
        st.map(name, bordercolor=[("focus", PINK)], arrowcolor=[("active", PINK)])

    # ⚠ 单选/复选的那个小圆点小方块不会跟着 DPI 走：字号被 tk scaling 放大了，
    #   指示器还是 clam 写死的 10px，高分屏下会明显偏小。clam 正好把这两个尺寸
    #   开成了元素选项，手动乘一遍。
    ind = dict(indicatorsize=px(10), indicatormargin=(px(1), px(1), px(4), px(1)))
    for name in ("TRadiobutton", "TCheckbutton"):
        st.configure(name, background=CARD, foreground=TEXT, font=F["body"], **ind)
        st.map(name, background=[("active", CARD)], indicatorcolor=[("selected", PINK)])

    st.configure("Vertical.TScrollbar", background="#D9DBDE", troughcolor=CARD,
                 bordercolor=CARD, arrowcolor=TEXT_MUTED, borderwidth=0)
    return st
