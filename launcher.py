"""打包入口（只在冻结成 EXE 后用；开发时照旧直接跑 main.py）。

为什么要多这么一层：src/ 和 assets/ **不打进 EXE**，而是以普通文件放在 exe 旁边。
这样日常发版只要下一个 300KB 的代码包换掉它们就行，不用重下 45MB 的运行时
（理由见 src/update.py 开头）。PyInstaller 的 frozen importer 只认打进存档里的
模块，存档里没有的会自然回落到 sys.path —— 所以这里把 exe 所在目录插到最前面，
main.py 里的 `from src...` 就会从磁盘上那份加载。

⚠ 本文件**不能** import src 里的任何东西：PyInstaller 是静态分析入口脚本来决定
  打包哪些模块的，这里一 import，src/ 就又被冻进 EXE 了，外置也就白做了。
  第三方库（playwright / openpyxl / webview…）改由 build.bat 的 --hidden-import
  显式声明。
"""
import sys
import traceback
from pathlib import Path


def _fatal(message: str) -> None:
    """--windowed 下没有控制台，起不来就只能弹窗说清楚，外加告诉人怎么自救。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "配置助手启动失败", 0x10)
    except Exception:
        pass


def _marker(name: str) -> Path:
    """标记文件一律写在 exe 旁边 —— 不能依赖当前目录，build.bat 里调用时
    工作目录是仓库根，不是 exe 所在的 dist\\配置助手\\。"""
    return Path(sys.executable).resolve().parent / name


def selftest(root: str) -> int:
    """打包后的自检：把真正会用到的东西全 import 一遍，起不来就让打包失败。

    ⚠ 为什么需要它：漏收一个动态 import 的依赖（比如 pywebview 的 WebView2
      后端要的 pythonnet/clr），表现是**进程正常启动、埋点都记上了，然后静默
      退出，退出码 0、日志一个字都没有**。这种包看起来打成功了，发出去才发现
      谁都打不开。所以在 build.bat 里跑一遍，把它变成打包期的硬失败。
    """
    try:
        if root:
            sys.path.insert(0, root)
        import importlib
        import webview                     # noqa: F401
        # ⚠ 不能写 `from webview import guilib`：webview/__init__.py 里有个同名
        #   变量 guilib = None，要等 start() 才被赋值，直接取到的是 None。
        #   必须按子模块导入，才能真正去挑并 import GUI 后端（那一步才会 import clr）。
        importlib.import_module("webview.guilib").initialize()
        import src.webapp                  # noqa: F401
        import src.datasource              # noqa: F401
        import src.update                  # noqa: F401
    except Exception:
        detail = traceback.format_exc()
        # --windowed 没有 stdout，结果只能落文件让 build.bat 去看
        try:
            _marker("selftest-failure.log").write_text(detail, encoding="utf-8")
        except OSError:
            pass
        return 1
    try:
        _marker("selftest-ok.txt").write_text("ok", encoding="utf-8")
    except OSError:
        pass
    return 0


def main() -> int:
    base = Path(sys.executable).resolve().parent
    sys.path.insert(0, str(base))

    if "--selftest" in sys.argv:
        i = sys.argv.index("--root") if "--root" in sys.argv else -1
        return selftest(sys.argv[i + 1] if i >= 0 else "")

    entry = base / "main.py"
    if not entry.is_file():
        _fatal(f"找不到程序主文件：{entry}\n\n"
               f"如果刚更新过，可以双击「配置助手更新器.exe」并加上 --rollback 参数回到上一版。")
        return 1

    try:
        import runpy
        runpy.run_path(str(entry), run_name="__main__")
        return 0
    except SystemExit as e:
        return int(e.code or 0)
    except Exception:
        detail = traceback.format_exc()
        try:
            log = base / "output" / "run.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as f:
                f.write("\n启动失败：\n" + detail + "\n")
        except OSError:
            pass
        _fatal("程序启动失败。\n\n"
               f"{detail.strip().splitlines()[-1]}\n\n"
               "如果是刚更新完出现的，在这个目录下打开命令行执行：\n"
               "  配置助手更新器.exe --rollback --log output\\update-run.log\n"
               "即可回到更新前的版本。完整信息见 output\\run.log。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
