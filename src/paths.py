"""路径解析。开发时和打包成 exe 之后，两类路径的位置完全不同。

  · 用户数据（config/ data/ output/ .chrome-profile）
      要放在 exe 旁边的真实目录 —— 用户得能改配置、放 Excel、看结果。
      ⚠ 不能用 __file__ 推导：打包后 __file__ 指向 PyInstaller 解压的临时目录
        （C:\\Users\\...\\Temp\\_MEIxxxxx），那里没有 config，而且退出即删。

  · 只读资源（assets/ tools/）
      跟着 exe 打包，运行时被解到 sys._MEIPASS。
"""
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)


def app_dir() -> Path:
    """用户数据根目录：打包后 = exe 所在目录，开发时 = 项目根目录。"""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundle_dir() -> Path:
    """只读资源根目录：打包后 = 解压目录，开发时 = 项目根目录。"""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return Path(__file__).resolve().parent.parent


def user_path(*parts) -> Path:
    return app_dir().joinpath(*parts)


def resource(*parts) -> Path | None:
    """找只读资源，打包目录和 exe 旁边都找一遍（方便用户自己替换）。"""
    for base in (bundle_dir(), app_dir()):
        p = base.joinpath(*parts)
        if p.exists():
            return p
    return None
