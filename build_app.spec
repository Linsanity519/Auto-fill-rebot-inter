# -*- mode: python ; coding: utf-8 -*-
"""主程序的打包配方（onedir，src/ 和 assets/ 外置）。

⚠ 为什么必须用 spec 而不是一行 pyinstaller 命令：
  入口是 launcher.py，它**故意不 import src**（一 import，src 就又被冻进 EXE，
  外置也就白做了，见 launcher.py 开头）。但 PyInstaller 是顺着入口的 import
  静态分析来决定打包什么的 —— 入口什么都不 import，它就什么都不打，连 csv、
  zipfile 这些 src 要用的标准库都不会进包，装完直接 ImportError。

  所以这里反过来：扫一遍 src/ 和 main.py 的 import，把结果喂给 hiddenimports。
  这样以后加新依赖不用记得来改打包脚本，扫描会自动带上。
"""
import ast
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve()

# 这些库有数据文件 / 动态子模块，光靠 hiddenimports 不够，要整包收：
#   playwright 要它的 node 驱动，webview 要 WebView2 的运行时资源，
#   openpyxl 有 PyInstaller 静态分析看不见的 cell._writer。
COLLECT_PACKAGES = ("playwright", "webview", "openpyxl", "PIL", "yaml",
                    "pythonnet", "clr_loader")

# ⚠ 运行时才动态 import、扫描和 collect_all 都看不见的模块。
#   clr 是 pythonnet 装的顶层 clr.py，pywebview 的 WebView2 后端在
#   webview/platforms/winforms.py 第 14 行 `import clr` 才用到它。
#   缺了它的后果极其阴险：进程正常启动、埋点都记上了，然后**静默退出，
#   退出码 0，run.log 一个字都没有** —— 实测踩过，排查了很久。
#   build.bat 末尾的自检就是为这类问题加的。
EXTRA_HIDDEN = ["clr"]

# 明确不要的：pandas/numpy 已经被 src/datasource.py 换掉了（省 90MB），
# 不排除的话会被某些库的可选 import 顺带拖进来。
EXCLUDES = ["pandas", "numpy", "matplotlib", "scipy", "pytest", "IPython"]


def scan_imports() -> list[str]:
    """扫 main.py + src/**.py 里的顶层 import 名。"""
    names = set()
    files = [ROOT / "main.py", *sorted((ROOT / "src").rglob("*.py"))]
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    # src 自己要留在磁盘上，__future__ 不是真模块
    names -= {"src", "__future__", *EXCLUDES}
    return sorted(names)


datas, binaries, hiddenimports = [], [], []
for pkg in COLLECT_PACKAGES:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += scan_imports()
hiddenimports += EXTRA_HIDDEN
hiddenimports = sorted(set(hiddenimports))
print(f"[build_app.spec] hiddenimports 共 {len(hiddenimports)} 个，"
      f"其中扫描自 src/ 的：{', '.join(scan_imports())}")

a = Analysis(
    ["launcher.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    # ⚠ 这里**不要** --add-data assets：assets/ 要留在 exe 旁边当普通文件，
    #   才能被 300KB 的代码包更新掉。paths.resource() 会在 exe 旁边找到它。
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # onedir：二进制留在 _internal，不塞进 exe
    name="配置助手",
    debug=False,
    strip=False,
    upx=False,
    console=False,                  # 等价于 --windowed
    icon="assets/icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="配置助手",
)
