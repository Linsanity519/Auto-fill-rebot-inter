@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === 1/6 检查打包依赖 ===
REM 走国内镜像，直连 PyPI 在内网会卡到超时
python -c "import PyInstaller" 2>nul || pip install -i https://mirrors.aliyun.com/pypi/simple/ pyinstaller

REM 先检查安装包编译器，别等主程序打完了才发现少依赖。
set "ISCC="
for /f "delims=" %%i in ('where ISCC.exe 2^>nul') do if not defined ISCC set "ISCC=%%i"
if not defined ISCC if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC (
  echo 找不到 Inno Setup 6 的 ISCC.exe。
  echo 请先安装免费的 Inno Setup 6，然后重新运行 build.bat。
  goto :fail
)

echo === 2/6 版本号与发布配置 ===
REM 本机直接运行时版本号末位 +1；GitHub Actions 发版时由 RELEASE_VERSION 指定。
REM 安装目录里的 EXE 名固定，只有安装包名带版本：更新器才能在同一个位置覆盖程序。
if defined RELEASE_VERSION (
  set VER=%RELEASE_VERSION%
) else (
  for /f %%v in ('python tools\bump_version.py') do set VER=%%v
)
if "%VER%"=="" (echo 版本号自增失败 & goto :fail)

REM 运行时代号：只有 requirements.txt 变了才需要手动 +1（见 src/update.py）。
REM 代码包声明 min_runtime，本机 runtime.txt 达不到就自动改走完整安装包。
set "RT="
for /f "usebackq delims=" %%r in ("RUNTIME_ID") do if not defined RT set "RT=%%r"
if "%RT%"=="" (echo 读不到 RUNTIME_ID & goto :fail)
echo     本次版本：%VER%　运行时代号：%RT%

REM 注入不进仓库、但必须进分发包的东西（统计回传地址），
REM 并把 config\settings.yaml 同步成 assets\settings.default.yaml 供老配置兜底。
python tools\inject_release_config.py
if errorlevel 1 goto :fail

echo === 3/6 清理上次的产物 ===
REM ⚠ 只删自己的产物，别把整个 dist 端了：
REM   同事/自己常把分发包解在 dist 下面直接用，那里面有他们改过的
REM   config\strategies（策略中心的配置）。rd /s /q dist 会连人家的配置一起删掉，
REM   实测干掉过一次别人配了一下午的策略。
if exist build rd /s /q build
if exist "dist\配置助手" rd /s /q "dist\配置助手"
if exist "dist\配置助手更新器.exe" del /q "dist\配置助手更新器.exe"
if exist "dist\ConfigAssistant-Setup-%VER%.exe" del /q "dist\ConfigAssistant-Setup-%VER%.exe"
if exist "dist\ConfigAssistant-%VER%.zip" del /q "dist\ConfigAssistant-%VER%.zip"

echo === 4/6 打包主程序（onedir，src/ 与 assets/ 外置）===
REM 打包配方在 build_app.spec 里 —— 入口 launcher.py 故意不 import src，
REM 所以 hiddenimports 由 spec 扫描 src/ 自动生成，理由见那个文件开头。
pyinstaller --noconfirm --distpath dist --workpath build build_app.spec
if errorlevel 1 goto :fail

echo === 5/6 打包更新器 ===
REM 更新器必须是独立进程：主 EXE 退出后由它把新版本换上去，避开 Windows 文件锁。
pyinstaller --noconfirm --onefile --windowed ^
  --icon "assets\icon.ico" ^
  --name "配置助手更新器" ^
  --distpath dist --workpath build ^
  tools\updater.py
if errorlevel 1 goto :fail

REM 运行时代号写进安装目录。必须在代码包之外，代码包会被更新覆盖。
> "dist\runtime.txt" echo %RT%

REM 自检：把打好的 exe 真跑一遍，确认动态 import 的依赖一个没漏。
REM ⚠ 这一步不是多余的：漏收 pywebview 要的 pythonnet/clr 时，程序会
REM   「正常启动、埋点都记上、然后静默退出，退出码 0、日志一个字都没有」。
REM   实测踩过一次 —— 那种包看起来打成功了，发出去才发现谁都打不开。
if exist "dist\配置助手\selftest-ok.txt" del /q "dist\配置助手\selftest-ok.txt"
if exist "dist\配置助手\selftest-failure.log" del /q "dist\配置助手\selftest-failure.log"
"%~dp0dist\配置助手\配置助手.exe" --selftest --root "%~dp0."
if not exist "dist\配置助手\selftest-ok.txt" (
  echo.
  echo 自检失败：打出来的程序起不来，详情：
  if exist "dist\配置助手\selftest-failure.log" type "dist\配置助手\selftest-failure.log"
  goto :fail
)
del /q "dist\配置助手\selftest-ok.txt"
echo     自检通过：依赖齐全，程序能起来

echo === 6/6 生成安装包 + 代码包 ===
"%ISCC%" /DMyAppVersion=%VER% /DMyRuntimeId=%RT% installer.iss
if errorlevel 1 goto :fail

REM 代码包：日常发版真正要发的东西，300KB 上下。
python tools\make_payload.py --version %VER% --runtime %RT%
if errorlevel 1 goto :fail

echo.
echo 完成：
echo   完整安装包  dist\ConfigAssistant-Setup-%VER%.exe   ^<- 首次安装 / 运行时变了才需要
echo   代码包      dist\ConfigAssistant-%VER%.zip         ^<- 日常发版发这个
echo.
echo 发布：用 tools\make_update_manifest.py 生成 latest.json，
echo       把三个文件一起传上去（国内镜像地址放前面，GitHub 兜底）。
if not defined CI pause
exit /b 0

:fail
echo.
echo 打包失败，请把上面的报错发给我。
if not defined CI pause
exit /b 1
