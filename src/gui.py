"""B站风格图形界面（tkinter + 自绘圆角按钮，不引入新依赖）。

线程模型：Runner 跑在后台线程，界面更新一律通过队列回主线程。
tkinter 不是线程安全的，后台线程直接改控件会随机崩溃。
"""
import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import yaml

from . import chrome, notify, registry, theme
from . import settings as settings_defaults
from .paths import app_dir, resource, user_path
from .theme import (BG, BORDER, CARD, CONSOLE_BG, CONSOLE_FG, DANGER, PINK,
                    PINK_LIGHT, SUCCESS, TEXT, TEXT_MUTED, TEXT_SUB, WARNING,
                    RoundButton, card, fonts, px, section_title)
from .ui import BaseUI, Stopped

log = logging.getLogger(__name__)

ROOT = app_dir()                       # exe 旁边的真实目录（config / data / output 都在这）
FORMS_DIR = user_path("config", "forms")


def _asset(name: str) -> Path | None:
    return resource("assets", name)


def _set_app_id(app_id: str = "bilibili.vip.formbot"):
    """声明独立的 AppUserModelID。

    不设这个，pythonw 启动的窗口在任务栏会归到 Python 自己的图标下，
    改了窗口图标也没用。必须在建窗口之前调用。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        log.info("设置 AppUserModelID 失败，任务栏可能仍显示 Python 图标", exc_info=True)


class GuiUI(BaseUI):
    """Runner 通过它和界面通信，全部走队列。"""

    def __init__(self, app):
        self.app = app
        self.answer = queue.Queue(maxsize=1)

    def log(self, msg, level="info"):
        self.app.q.put(("log", (msg, level)))

    def progress(self, done, total, stats):
        self.app.q.put(("progress", (done, total, dict(stats))))

    def confirm(self, label, summary):
        self.app.q.put(("confirm", (label, summary)))
        while True:
            try:
                return self.answer.get(timeout=0.3)
            except queue.Empty:
                if self.app.stop_flag.is_set():
                    return "stop"

    def ask_continue(self, error):
        self.app.q.put(("ask_continue", error))
        while True:
            try:
                return self.answer.get(timeout=0.3)
            except queue.Empty:
                if self.app.stop_flag.is_set():
                    return False

    def checkpoint(self):
        if self.app.stop_flag.is_set():
            raise Stopped()
        while self.app.pause_flag.is_set():
            if self.app.stop_flag.is_set():
                raise Stopped()
            threading.Event().wait(0.2)

    def finished(self, title, body, ok):
        self.app.q.put(("finished", (title, body, ok)))


class App:
    def __init__(self, root):
        self.root = root
        self.F = fonts()
        self.q = queue.Queue()
        self.stop_flag = threading.Event()
        self.pause_flag = threading.Event()
        self.worker = None
        self.preview_rows = []
        self.runner = None

        root.title("大会员业务后台 配置助手")
        root.geometry(f"{px(1020)}x{px(820)}")
        root.minsize(px(940), px(720))
        root.configure(bg=BG)
        theme.apply_ttk(root)
        self._set_window_icon(root)

        self.settings = self._load_settings()

        self._build()
        self._refresh_forms()
        self._poll_queue()
        self._poll_browser()

    def _load_settings(self) -> dict:
        """读配置，并把里面的相对路径统一锚定到 exe 所在目录。

        不锚定的话，打包后工作目录可能是任意位置（比如从桌面快捷方式启动），
        日志和结果会写到莫名其妙的地方，用户根本找不到。
        """
        path = user_path("config", "settings.yaml")
        if not path.exists():
            raise FileNotFoundError(
                f"找不到配置文件：{path}\n"
                f"请确认 config 文件夹和程序放在同一目录下。")

        s = settings_defaults.apply_defaults(yaml.safe_load(path.read_text(encoding="utf-8")))
        for key in ("state_file", "result_file", "log_file", "screenshot_dir", "data_file"):
            v = s.get(key)
            if v and not Path(v).is_absolute():
                s[key] = str(ROOT / v)
        return s

    def _set_window_icon(self, win):
        """标题栏 + 任务栏图标。打包成 exe 后资源目录会变，两处都找。"""
        ico = _asset("icon.ico")
        if ico:
            try:
                win.iconbitmap(default=str(ico))
            except tk.TclError:
                pass

        # iconbitmap 在部分 Windows/Tk 组合下不影响任务栏，iconphoto 更可靠，两个都设
        png = _asset("logo64.png")
        if png:
            try:
                self._icon_img = tk.PhotoImage(file=str(png))
                win.iconphoto(True, self._icon_img)
            except tk.TclError:
                pass
        if not ico and not png:
            log.info("没找到 assets 下的图标文件，用系统默认")

    def _load_logo(self, target: int):
        """把 logo 缩到目标边长。

        PhotoImage 只会整数倍缩放，高分屏要的边长（150% 下是 48）往往不是
        128 的整数分之一，所以用 zoom(a)/subsample(b) 凑一个最接近的比例。
        zoom 会先放大出中间图，a 必须压得很小，否则内存直接爆。
        """
        png = _asset("logo128.png") or _asset("logo64.png")
        if not png:
            log.info("没找到 logo 图片，改用 Canvas 画一个")
            return None
        try:
            img = tk.PhotoImage(file=str(png))
            src = img.width()
            best = min(((a, max(1, round(src * a / target))) for a in range(1, 5)),
                       key=lambda ab: abs(src * ab[0] / ab[1] - target))
            a, b = best
            if a > 1:
                img = img.zoom(a, a)
            if b > 1:
                img = img.subsample(b, b)
            return img
        except tk.TclError:
            log.info("读取 logo 失败，改用 Canvas 画一个", exc_info=True)
            return None

    # ================= 界面 =================
    def _build(self):
        self._build_header()

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=px(16), pady=(0, px(12)))

        self._build_setup(body)
        self._build_preview(body)
        self._build_control(body)
        self._build_console(body)
        self._build_footer(body)

    def _build_header(self):
        """顶部粉色标题条 + 浏览器状态。"""
        head = tk.Frame(self.root, bg=CARD, height=px(64))
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Frame(self.root, bg=PINK, height=px(3)).pack(fill="x")

        left = tk.Frame(head, bg=CARD)
        left.pack(side="left", padx=px(18))

        self._logo_img = self._load_logo(px(32))

        if self._logo_img:
            tk.Label(left, image=self._logo_img, bg=CARD).pack(side="left", pady=px(14))
        else:
            # 没有图片文件时用 Canvas 画一个等效的
            d = px(32)
            logo = tk.Canvas(left, width=d, height=d, bg=CARD, highlightthickness=0)
            logo.pack(side="left", pady=px(16))
            logo.create_oval(0, 0, d, d, fill=PINK, outline="")
            logo.create_text(d / 2, d / 2, text="大", fill="#FFFFFF",
                             font=(self.F["body"][0], 15, "bold"))

        txt = tk.Frame(left, bg=CARD)
        txt.pack(side="left", padx=px(10))
        tk.Label(txt, text="配置助手", bg=CARD, fg=TEXT,
                 font=self.F["title"]).pack(anchor="w")
        tk.Label(txt, text="大会员业务后台", bg=CARD, fg=TEXT_MUTED,
                 font=self.F["small"]).pack(anchor="w")

        right = tk.Frame(head, bg=CARD)
        right.pack(side="right", padx=px(18))
        d = px(10)
        self.browser_dot = tk.Canvas(right, width=d, height=d, bg=CARD, highlightthickness=0)
        self.browser_dot.pack(side="left", pady=px(26))
        self._dot = self.browser_dot.create_oval(px(1), px(1), d - px(1), d - px(1),
                                                 fill=DANGER, outline="")
        self.browser_lbl = tk.Label(right, text="浏览器未连接", bg=CARD, fg=TEXT_SUB,
                                    font=self.F["body"])
        self.browser_lbl.pack(side="left", padx=(px(6), px(12)))
        RoundButton(right, "启动浏览器并登录", self.on_launch_browser,
                    kind="primary", parent_bg=CARD).pack(side="left", pady=px(16))

    def _build_setup(self, parent):
        c = card(parent)
        c.pack(fill="x", pady=(px(12), 0))
        inner = tk.Frame(c, bg=CARD)
        inner.pack(fill="x", padx=px(18), pady=px(14))

        section_title(inner, "配置来源", "选择要填的表单类型和数据文件").pack(
            anchor="w", pady=(0, px(12)))

        g = tk.Frame(inner, bg=CARD)
        g.pack(fill="x")

        tk.Label(g, text="配置类型", bg=CARD, fg=TEXT_SUB, font=self.F["body"]).grid(
            row=0, column=0, sticky="w", padx=(0, px(10)))
        self.form_var = tk.StringVar()
        self.form_box = ttk.Combobox(g, textvariable=self.form_var, state="readonly",
                                     width=26, font=self.F["body"])
        self.form_box.grid(row=0, column=1, sticky="w")
        self.form_box.bind("<<ComboboxSelected>>", lambda e: self.on_form_change())
        RoundButton(g, "生成 Excel 模板", self.on_make_template, kind="secondary",
                    parent_bg=CARD).grid(row=0, column=2, padx=px(10))

        self.data_lbl = tk.Label(g, text="数据文件", bg=CARD, fg=TEXT_SUB, font=self.F["body"])
        self.data_lbl.grid(row=1, column=0, sticky="w", pady=(px(12), 0), padx=(0, px(10)))
        self.data_var = tk.StringVar(value=self.settings.get("data_file", ""))
        self.data_entry = ttk.Entry(g, textvariable=self.data_var, width=52, font=self.F["body"])
        self.data_entry.grid(row=1, column=1, sticky="w", pady=(px(12), 0))
        self.data_browse = RoundButton(g, "浏览…", self.on_pick_file, kind="secondary",
                                       width=80, parent_bg=CARD)
        self.data_browse.grid(row=1, column=2, padx=px(10), pady=(px(12), 0))
        RoundButton(g, "载入并检查", self.on_load, kind="blue",
                    parent_bg=CARD).grid(row=1, column=3, pady=(px(12), 0))

        # 「延期范围」只在延期类配置下出现，选项按配置类型现搭（见 registry.scopes_for）
        self.scope_lbl = tk.Label(g, text="延期范围", bg=CARD, fg=TEXT_SUB, font=self.F["body"])
        self.scope_lbl.grid(row=2, column=0, sticky="w", pady=(px(12), 0), padx=(0, px(10)))
        self.scope_box = tk.Frame(g, bg=CARD)
        self.scope_box.grid(row=2, column=1, columnspan=3, sticky="w", pady=(px(12), 0))
        self.scope_var = tk.StringVar(value="active")
        self._scope_mode = None

        # 当前范围要不要 Excel，用一行小字明说，省得对着空的「数据文件」发懵
        self.scope_hint = tk.Label(g, text="", bg=CARD, fg=TEXT_MUTED, font=self.F["small"])
        self.scope_hint.grid(row=3, column=1, columnspan=3, sticky="w", pady=(px(6), 0))

        self._show_scope_row(False)

    def _build_preview(self, parent):
        c = card(parent)
        c.pack(fill="both", expand=True, pady=px(12))
        inner = tk.Frame(c, bg=CARD)
        inner.pack(fill="both", expand=True, padx=px(18), pady=px(14))

        head = tk.Frame(inner, bg=CARD)
        head.pack(fill="x", pady=(0, px(10)))
        section_title(head, "数据预览", "开跑前先查错，双击看详情").pack(side="left")
        self.preview_stat = tk.Label(head, text="", bg=CARD, fg=TEXT_MUTED, font=self.F["small"])
        self.preview_stat.pack(side="right")

        wrap = tk.Frame(inner, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        wrap.pack(fill="both", expand=True)

        cols = ("序号", "名称", "类型", "明细", "校验结果")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", height=8)
        for c_, w, anchor in zip(cols, (56, 300, 110, 60, 400),
                                 ("center", "w", "center", "center", "w")):
            self.tree.heading(c_, text=c_)
            self.tree.column(c_, width=px(w), anchor=anchor)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        self.tree.tag_configure("bad", foreground=DANGER)
        self.tree.tag_configure("done", foreground=TEXT_MUTED)
        self.tree.tag_configure("ok", foreground=TEXT)
        self.tree.tag_configure("odd", background="#FAFBFC")
        self.tree.bind("<Double-1>", self.on_row_detail)

    def _build_control(self, parent):
        c = card(parent)
        c.pack(fill="x")
        inner = tk.Frame(c, bg=CARD)
        inner.pack(fill="x", padx=px(18), pady=px(14))

        section_title(inner, "运行", "建议先空跑一遍确认无误").pack(anchor="w", pady=(0, px(12)))

        r = tk.Frame(inner, bg=CARD)
        r.pack(fill="x")

        self.mode = tk.StringVar(value="confirm")
        for txt, val in (("空跑（只填不提交）", "dry"), ("逐条确认", "confirm"), ("全自动", "auto")):
            ttk.Radiobutton(r, text=txt, variable=self.mode, value=val).pack(
                side="left", padx=(0, px(18)))

        self.skip_done = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="跳过已成功的", variable=self.skip_done).pack(side="left")

        self.btn_start = RoundButton(r, "开始配置", self.on_start, kind="primary",
                                     width=104, parent_bg=CARD)
        self.btn_start.pack(side="right")
        self.btn_stop = RoundButton(r, "停止", self.on_stop, kind="secondary",
                                    width=72, parent_bg=CARD)
        self.btn_stop.pack(side="right", padx=px(8))
        self.btn_pause = RoundButton(r, "暂停", self.on_pause, kind="secondary",
                                     width=72, parent_bg=CARD)
        self.btn_pause.pack(side="right")
        self.btn_pause.config_state(False)
        self.btn_stop.config_state(False)

        p = tk.Frame(inner, bg=CARD)
        p.pack(fill="x", pady=(px(14), 0))
        self.pbar = ttk.Progressbar(p, style="Bili.Horizontal.TProgressbar")
        self.pbar.pack(side="left", fill="x", expand=True)
        self.stat_lbl = tk.Label(p, text="未开始", bg=CARD, fg=TEXT_SUB,
                                 font=self.F["body"], width=32, anchor="e")
        self.stat_lbl.pack(side="left", padx=(px(12), 0))

    def _build_console(self, parent):
        c = card(parent)
        c.pack(fill="both", expand=True, pady=px(12))
        inner = tk.Frame(c, bg=CARD)
        inner.pack(fill="both", expand=True, padx=px(18), pady=px(14))

        section_title(inner, "运行日志").pack(anchor="w", pady=(0, px(10)))

        wrap = tk.Frame(inner, bg=CONSOLE_BG)
        wrap.pack(fill="both", expand=True)
        self.logbox = tk.Text(wrap, height=9, wrap="word", state="disabled", bd=0,
                              bg=CONSOLE_BG, fg=CONSOLE_FG, font=self.F["mono"],
                              relief="flat", padx=px(12), pady=px(10),
                              insertbackground=CONSOLE_FG)
        ls = ttk.Scrollbar(wrap, orient="vertical", command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=ls.set)
        self.logbox.pack(side="left", fill="both", expand=True)
        ls.pack(side="right", fill="y")

        self.logbox.tag_configure("ok", foreground="#7CD9A5")
        self.logbox.tag_configure("error", foreground="#FF8B8B")
        self.logbox.tag_configure("warn", foreground="#FFC978")
        self.logbox.tag_configure("info", foreground=CONSOLE_FG)
        self.logbox.tag_configure("ts", foreground="#6B7075")

    def _build_footer(self, parent):
        f = tk.Frame(parent, bg=BG)
        f.pack(fill="x")
        RoundButton(f, "打开结果目录", self.on_open_output, kind="ghost",
                    parent_bg=BG).pack(side="left")
        RoundButton(f, "清除断点记录", self.on_clear_state, kind="ghost",
                    parent_bg=BG).pack(side="left", padx=px(6))
        tk.Label(f, text="提交前请切到浏览器核对填写内容", bg=BG, fg=TEXT_MUTED,
                 font=self.F["small"]).pack(side="right", pady=px(8))

    # ================= 事件 =================
    def _refresh_forms(self):
        names = sorted(p.stem for p in FORMS_DIR.glob("*.yaml"))
        self.form_box["values"] = names
        if names:
            self.form_var.set(names[0])
            self._sync_form_ui()
            self.log(f"已加载 {len(names)} 个配置类型，当前：{names[0]}")

    def on_form_change(self):
        self.log(f"已切换配置类型：{self.form_var.get()}")
        self.tree.delete(*self.tree.get_children())
        self.preview_rows = []
        self.preview_stat.config(text="")
        self._sync_form_ui()

    def _show_scope_row(self, show: bool):
        for w in (self.scope_lbl, self.scope_box, self.scope_hint):
            if show:
                w.grid()
            else:
                w.grid_remove()

    def _render_scope(self, mode, options):
        """按配置类型重搭「延期范围」单选。

        DMP 和 AB 的可选范围不一样（AB 没有「全部实验中」—— 全站几千条实验，
        全扫既慢又会动到别人的），所以选项表跟着 mode 走，不写死一套。
        """
        if mode == self._scope_mode:
            return                       # 没换类型就别重建，免得把用户的选择重置了
        self._scope_mode = mode
        for w in self.scope_box.winfo_children():
            w.destroy()
        if not options:
            return
        self.scope_var.set(options[0][1])       # 每种类型的第一项就是默认值
        for txt, val in options:
            ttk.Radiobutton(self.scope_box, text=txt, variable=self.scope_var, value=val,
                            command=self.on_scope_change).pack(side="left", padx=(0, px(16)))

    def _sync_form_ui(self):
        """按当前配置类型显示/隐藏专属选项。读不出配置就当普通表单处理。"""
        try:
            cfg = self._form_cfg()
        except Exception:
            cfg = {}
        options = registry.scopes_for(cfg)
        self._render_scope(cfg.get("mode"), options)
        self._show_scope_row(bool(options))
        self._sync_data_row(options)

    def _sync_data_row(self, options):
        """只有「按清单」这一种范围要 Excel，其余情况把数据文件那一行收起来。

        留着一个填不填都没用的输入框，只会让人以为必须传表。
        """
        needs_excel = (not options) or self.scope_var.get() == "id_list"
        if needs_excel:
            self.data_lbl.config(text="数据文件", fg=TEXT_SUB)
            self.data_entry.grid()
            self.data_browse.grid()
        else:
            self.data_lbl.config(text="数据文件", fg=TEXT_MUTED)
            self.data_entry.grid_remove()
            self.data_browse.grid_remove()
        if options:
            self.scope_hint.config(
                text="这个范围直接读网页，不用选数据文件，点「载入并检查」即可"
                if not needs_excel else
                "这个范围要 Excel 清单：先点「生成 Excel 模板」，填好后用「浏览…」选它")

    def on_scope_change(self):
        hint = {
            "active": "范围：所有「生效中」人群，都延到系统最晚可选日期",
            "mine": ("范围：所有「我的实验」，都延到系统最晚可选日期"
                     if self._scope_mode == "ab_extension" else
                     "范围：所有「我创建的」人群，都延到系统最晚可选日期"),
            "id_list": ("范围：按清单里的实验ID逐个续期；先点「生成 Excel 模板」拿清单表头"
                        if self._scope_mode == "ab_extension" else
                        "范围：按清单里的人群ID逐个延期；先点「生成 Excel 模板」拿清单表头"),
        }
        self.log(hint.get(self.scope_var.get(), ""))
        self._sync_data_row(registry.scopes_for(self._form_cfg()))

    def _form_cfg(self):
        return yaml.safe_load((FORMS_DIR / f"{self.form_var.get()}.yaml").read_text(encoding="utf-8"))

    def _make_runner(self, settings, cfg, ui):
        """按 profile 的 mode 选执行器。

        ⚠ 老配置没有 mode 字段，永远落到 registry.DEFAULT_SPEC（走 Runner）；
          wizard 逻辑完全在另一套文件里，两条路径不共用代码，改新的不会影响老的。
        """
        return registry.spec_for(cfg.get("mode")).make_runner(settings, cfg, ui)

    def _settings(self):
        s = dict(self.settings)
        s["data_file"] = self.data_var.get()
        s["resume"] = self.skip_done.get()
        # 两个延期执行器各读各的键；别的执行器拿到这两个键也不影响
        s["dmp_scope"] = self.scope_var.get()
        s["ab_scope"] = self.scope_var.get()
        return s

    def on_launch_browser(self):
        try:
            # 当前配置类型自己指定了页面就用它，否则用全局登录页
            url = None
            try:
                url = self._form_cfg().get("form_url")
            except Exception:
                pass
            url = url or self.settings.get("start_url")

            msg = chrome.launch(self.settings["cdp_url"], ROOT / ".chrome-profile", url)
            self.log(msg, "ok")
            if url:
                threading.Thread(target=self._watch_login, args=(url,), daemon=True).start()
        except Exception as e:
            self.log(str(e), "error")
            messagebox.showerror("启动失败", str(e))

    def _watch_login(self, target_url: str):
        """守望登录：扫码登录后 SSO 会把人丢在管理平台首页，这里自动送回目标页。"""
        cdp = self.settings["cdp_url"]
        told = False

        for _ in range(300):          # 最多等 10 分钟
            threading.Event().wait(2)
            state = chrome.on_login_page(cdp)

            if state is None:         # 浏览器还没起来或者被关了
                continue
            if state:                 # 还停在登录页
                if not told:
                    told = True
                    self.q.put(("log", ("检测到未登录，请在浏览器里扫码登录，登录后会自动跳回配置页", "warn")))
                continue

            # 已经登录
            urls = [p.get("url", "") for p in chrome.list_pages(cdp)]
            if any(u.startswith(target_url) for u in urls):
                self.q.put(("log", ("已登录，当前就在配置页", "ok")))
            else:
                chrome.launch(cdp, ROOT / ".chrome-profile", target_url)
                self.q.put(("log", (f"登录成功，已跳转到配置页：{target_url}", "ok")))
            return

        self.q.put(("log", ("等待登录超时（10 分钟），需要的话再点一次启动", "warn")))

    def on_pick_file(self):
        p = filedialog.askopenfilename(
            title="选择数据文件", initialdir=str(ROOT / "data"),
            filetypes=[("Excel / CSV", "*.xlsx *.xlsm *.csv"), ("所有文件", "*.*")],
        )
        if p:
            self.data_var.set(p)
            self.on_load()

    def on_make_template(self):
        # ⚠ 直接函数调用，不能用 subprocess + sys.executable：
        #   打包后 sys.executable 就是本 exe，那样只会再开一个界面
        try:
            cfg = self._form_cfg()
            if cfg.get("mode") == "wizard":
                path = self._make_wizard_template(cfg)
                if path is None:
                    return           # 用户取消了
            else:
                spec = registry.spec_for(cfg.get("mode"))
                if registry.scopes_for(cfg) and self.scope_var.get() != "id_list":
                    messagebox.showinfo("无需模板", spec.no_template_hint)
                    return
                # 有的 mode 压根不吃 Excel（比如抢会议室，任务清单在新版界面上填），
                # build_template 是 None —— 直接调是 TypeError
                if spec.build_template is None:
                    messagebox.showinfo("无需模板",
                                        spec.no_template_hint or "这个配置类型不需要 Excel 模板")
                    return
                path = spec.build_template(self.form_var.get())
            self.log(f"模板已生成：{Path(path).name}", "ok")
            if messagebox.askyesno("生成成功", f"模板已生成：\n{path}\n\n现在打开吗？"):
                os.startfile(path)
        except Exception as e:
            log.exception("生成模板失败")
            self.log(f"生成模板失败：{e}", "error")
            messagebox.showerror("生成失败", str(e))

    def _make_wizard_template(self, cfg):
        """wizard 模式：先让用户勾资源位，再按勾选的生成模板。

        ⚠ 「挂到已有活动」「人群逐单元填」这两个开关只在新界面（webapp）上有；
          tk 版是应急备用界面，这里固定走「新建活动 + 人群跟随策略中心」。
          需要另一种组合时用命令行：--activity-id / --custom-audience。
        """
        from . import wizard_schema as W
        from . import wizard_template as WT

        names = W.position_names(cfg)
        picked = PositionPicker(self.root, names, self.F).show()
        if not picked:
            return None
        self.log(f"已选 {len(picked)} 个资源位：{'、'.join(picked)}")
        return WT.build(cfg, picked)

    def on_load(self):
        try:
            self.runner = self._make_runner(self._settings(), self._form_cfg(), GuiUI(self))
            self.preview_rows = self.runner.preview()
        except Exception as e:
            self.log(f"载入失败：{e}", "error")
            messagebox.showerror("载入失败", str(e))
            return

        self.tree.delete(*self.tree.get_children())
        bad = 0
        for i, row in enumerate(self.preview_rows):
            issues = row.issues
            if issues:
                bad += 1
                tags, verdict = ["bad"], "✗ " + "；".join(issues[:2]) + ("…" if len(issues) > 2 else "")
            elif row.done:
                tags, verdict = ["done"], "— 已完成，本次跳过"
            else:
                tags, verdict = ["ok"], "✓ 校验通过"
            if i % 2:
                tags.append("odd")
            self.tree.insert("", "end", tags=tags,
                             values=(row.index, row.name, row.kind, row.detail_count, verdict))

        n = len(self.preview_rows)
        self.preview_stat.config(
            text=f"共 {n} 条 · 通过 {n - bad} 条" + (f" · 有问题 {bad} 条" if bad else ""))
        self.log(f"载入 {n} 条配置，{n - bad} 条通过校验" + (f"，{bad} 条有问题" if bad else ""),
                 "error" if bad else "ok")
        if bad:
            self.log("双击标红的行可以看完整原因")

    def on_row_detail(self, _e):
        sel = self.tree.selection()
        if not sel:
            return
        row = self.preview_rows[self.tree.index(sel[0])]
        issues = row.issues
        rec = row.payload
        body = "\n".join(f"· {x}" for x in issues) if issues else "校验通过，没有发现问题。"
        detail = "\n".join(f"  {k}：{v}" for k, v in rec["header"].items() if str(v).strip())
        # 单弹窗表单（价格配置/DMP/AB）叫 items，wizard 的单元叫 creatives
        detail_items = rec.get("items") if "items" in rec else rec.get("creatives", [])
        items = "\n".join(
            f"  第{i}项：" + "，".join(f"{k}={v}" for k, v in it.items() if str(v).strip())
            for i, it in enumerate(detail_items, 1))
        messagebox.showinfo(f"第 {row.index} 条 · {row.name}",
                            f"【校验】\n{body}\n\n【主表】\n{detail}\n\n【明细】\n{items}")

    def on_start(self):
        if not self.preview_rows:
            messagebox.showwarning("还没有数据", "请先选择数据文件并点「载入并检查」")
            return

        mode = self.mode.get()
        bad = [r for r in self.preview_rows if r.issues]
        good = [r for r in self.preview_rows if not r.issues]
        if bad and not messagebox.askyesno(
                "有数据没通过校验",
                f"{len(bad)} 条数据有问题，会被跳过。\n\n继续跑其余 {len(good)} 条吗？"):
            return

        if not chrome.is_connected(self.settings["cdp_url"]):
            messagebox.showerror("浏览器没连上", "请先点右上角「启动浏览器并登录」，并在里面登录内网。")
            return

        if mode == "auto" and not messagebox.askyesno(
                "确认全自动",
                "全自动模式会连续提交，中途不再询问。\n\n建议先用「逐条确认」跑通前几条。确定继续？"):
            return

        records = [r.payload for r in good
                   if not (self.skip_done.get() and r.done)]
        if not records:
            messagebox.showinfo("没有要跑的", "所有数据要么有问题、要么已完成。")
            return

        s = self._settings()
        s["dry_run"] = mode == "dry"
        self.runner = self._make_runner(s, self._form_cfg(), GuiUI(self))
        self.runner.auto = mode == "auto"

        self.stop_flag.clear()
        self.pause_flag.clear()
        self._set_running(True)
        self.pbar["maximum"] = len(records)
        self.pbar["value"] = 0

        self.worker = threading.Thread(target=self._run_worker, args=(records,), daemon=True)
        self.worker.start()

    def _run_worker(self, records):
        try:
            self.runner.run(records)
        except Exception as e:
            log.exception("运行出错")
            self.q.put(("log", (f"运行中断：{e}", "error")))
        finally:
            self.q.put(("done", None))

    def on_pause(self):
        if self.pause_flag.is_set():
            self.pause_flag.clear()
            self.btn_pause.set_text("暂停")
            self.log("已继续")
        else:
            self.pause_flag.set()
            self.btn_pause.set_text("继续")
            self.log("已暂停（当前这条会填完再停）", "warn")

    def on_stop(self):
        self.stop_flag.set()
        self.pause_flag.clear()
        self.log("正在停止…", "warn")

    def on_open_output(self):
        p = (ROOT / "output").resolve()
        p.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["explorer", str(p)])

    def on_clear_state(self):
        if not messagebox.askyesno("清除断点", "清除后会从第一条重新开始（已提交的不会撤销）。确定？"):
            return
        try:
            self._make_runner(self._settings(), self._form_cfg(), GuiUI(self)).clear_state()
            self.log("断点已清除", "ok")
            if self.preview_rows:
                self.on_load()
        except Exception as e:
            self.log(f"清除失败：{e}", "error")

    def _set_running(self, running):
        self.btn_start.config_state(not running)
        self.btn_pause.config_state(running)
        self.btn_stop.config_state(running)
        if not running:
            self.btn_pause.set_text("暂停")

    # ================= 队列泵 =================
    def log(self, msg, level="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.logbox.config(state="normal")
        self.logbox.insert("end", f"{ts}  ", "ts")
        self.logbox.insert("end", f"{msg}\n", level)
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(*payload)
                elif kind == "progress":
                    done, total, st = payload
                    self.pbar["value"] = done
                    self.stat_lbl.config(
                        text=f"{done}/{total}　成功 {st['ok']} · 失败 {st['failed']} · 跳过 {st['skipped']}")
                elif kind == "confirm":
                    self._do_confirm(*payload)
                elif kind == "ask_continue":
                    ok = messagebox.askyesno("这条失败了", f"{payload}\n\n继续跑下一条吗？")
                    self.runner.ui.answer.put(ok)
                elif kind == "finished":
                    title, body, ok = payload
                    notify.beep(ok=ok)
                    (messagebox.showinfo if ok else messagebox.showwarning)(title, body)
                elif kind == "done":
                    self._set_running(False)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _do_confirm(self, label, name):
        """填完了，等用户切到浏览器核对后决定。"""
        win = tk.Toplevel(self.root)
        win.title("核对后确认")
        win.configure(bg=CARD)
        win.transient(self.root)
        win.attributes("-topmost", True)
        win.resizable(False, False)

        tk.Frame(win, bg=PINK, height=px(3)).pack(fill="x")
        box = tk.Frame(win, bg=CARD)
        box.pack(padx=px(28), pady=px(20))

        tk.Label(box, text=f"{label}  {name}", bg=CARD, fg=TEXT,
                 font=self.F["title"]).pack(anchor="w")
        tk.Label(box, text="已在浏览器里填好，请切到 Chrome 核对内容后选择",
                 bg=CARD, fg=TEXT_MUTED, font=self.F["body"]).pack(
                     anchor="w", pady=(px(6), px(16)))

        btns = tk.Frame(box, bg=CARD)
        btns.pack(anchor="w")

        def answer(v):
            self.runner.ui.answer.put(v)
            win.destroy()

        RoundButton(btns, "提交这条", lambda: answer("submit"), kind="primary",
                    parent_bg=CARD).pack(side="left", padx=(0, px(8)))
        RoundButton(btns, "跳过", lambda: answer("skip"), kind="secondary",
                    width=72, parent_bg=CARD).pack(side="left", padx=px(4))
        RoundButton(btns, "以后全部自动", lambda: answer("auto"), kind="secondary",
                    parent_bg=CARD).pack(side="left", padx=px(4))
        RoundButton(btns, "停止", lambda: answer("stop"), kind="secondary",
                    width=72, parent_bg=CARD).pack(side="left", padx=px(4))

        win.protocol("WM_DELETE_WINDOW", lambda: answer("skip"))
        self._set_window_icon(win)
        win.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - win.winfo_width()) // 2
        win.geometry(f"+{max(0, x)}+{self.root.winfo_y() + px(140)}")

    def _poll_browser(self):
        def check():
            ok = chrome.is_connected(self.settings["cdp_url"], timeout=0.8)
            self.root.after(0, lambda: self._set_browser(ok))

        threading.Thread(target=check, daemon=True).start()
        self.root.after(3000, self._poll_browser)

    def _set_browser(self, ok):
        self.browser_dot.itemconfig(self._dot, fill=SUCCESS if ok else DANGER)
        self.browser_lbl.config(text="浏览器已连接" if ok else "浏览器未连接",
                                fg=SUCCESS if ok else TEXT_SUB)


class PositionPicker:
    """资源位勾选弹窗（只在 wizard 模式用）。

    勾了哪些资源位，生成的模板就只有哪些资源位的列 —— 不同资源位要填的
    字段差别很大，全摊开会让人不知道该填哪些。
    """

    def __init__(self, parent, names: list[str], fonts_):
        self.result = None
        self.vars = {}
        self.F = fonts_

        self.win = tk.Toplevel(parent)
        self.win.title("选择要配置的资源位")
        self.win.configure(bg=BG)
        self.win.transient(parent)
        self.win.grab_set()
        self.win.geometry(f"{px(520)}x{px(600)}")

        tk.Label(self.win, text="勾选本次要配置的资源位", bg=BG, fg=TEXT,
                 font=self.F["title"]).pack(anchor="w", padx=px(18), pady=(px(16), px(2)))
        tk.Label(self.win, text="模板只会生成勾选项的列，不同资源位要填的内容不一样",
                 bg=BG, fg=TEXT_MUTED, font=self.F["small"]).pack(anchor="w", padx=px(18))

        wrap = tk.Frame(self.win, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        wrap.pack(fill="both", expand=True, padx=px(16), pady=px(12))

        canvas = tk.Canvas(wrap, bg=CARD, highlightthickness=0)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        box = tk.Frame(canvas, bg=CARD)
        box.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=box, anchor="nw")
        canvas.configure(yscrollcommand=vs.set)
        canvas.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        for n in names:
            v = tk.BooleanVar(value=False)
            self.vars[n] = v
            tk.Checkbutton(box, text=n, variable=v, bg=CARD, fg=TEXT, anchor="w",
                           activebackground=CARD, selectcolor=CARD,
                           font=self.F["body"]).pack(fill="x", padx=px(14), pady=px(3))

        bar = tk.Frame(self.win, bg=BG)
        bar.pack(fill="x", padx=px(16), pady=(0, px(14)))
        RoundButton(bar, "全选", lambda: [v.set(True) for v in self.vars.values()],
                    kind="secondary", width=70, parent_bg=BG).pack(side="left")
        RoundButton(bar, "清空", lambda: [v.set(False) for v in self.vars.values()],
                    kind="secondary", width=70, parent_bg=BG).pack(side="left", padx=px(8))
        RoundButton(bar, "生成模板", self._ok, kind="primary", parent_bg=BG).pack(side="right")
        RoundButton(bar, "取消", self._cancel, kind="secondary",
                    parent_bg=BG).pack(side="right", padx=px(8))

    def _ok(self):
        picked = [n for n, v in self.vars.items() if v.get()]
        if not picked:
            messagebox.showwarning("还没选", "至少勾一个资源位")
            return
        self.result = picked
        self.win.destroy()

    def _cancel(self):
        self.result = None
        self.win.destroy()

    def show(self):
        self.win.wait_window()
        return self.result


def main():
    # 这两个都必须在 Tk() 之前：窗口建出来之后再声明就不生效了
    _set_app_id()
    theme.enable_dpi_awareness()
    root = tk.Tk()
    theme.init_scaling(root)      # 要在建控件之前，否则先建的那些拿不到新字号
    try:
        App(root)
    except Exception as e:
        # 打包后没有控制台，异常必须弹窗告诉用户，不能只留一个 traceback 对话框
        log.exception("启动失败")
        root.withdraw()
        messagebox.showerror(
            "启动失败",
            f"{e}\n\n"
            f"程序目录：{app_dir()}\n\n"
            f"请确认 config 文件夹和程序放在同一目录下。")
        return
    root.mainloop()
