@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === 1/4 检查打包依赖 ===
REM 走国内镜像，直连 PyPI 在内网会卡到超时
python -c "import PyInstaller" 2>nul || pip install -i https://mirrors.aliyun.com/pypi/simple/ pyinstaller

REM 先检查安装包编译器，别等 100MB 主程序打完了才发现少依赖。
set "ISCC="
for /f "delims=" %%i in ('where ISCC.exe 2^>nul') do if not defined ISCC set "ISCC=%%i"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC (
  echo 找不到 Inno Setup 6 的 ISCC.exe。
  echo 请先安装免费的 Inno Setup 6，然后重新运行 build.bat。
  goto :fail
)

REM 打一次包 = 版本号末位 +1。安装目录里的 EXE 名固定，只有安装包名带版本：
REM 更新器才能在同一个位置覆盖程序，而不会每升级一次就留下一套旧文件夹。
for /f %%v in ('python tools\bump_version.py') do set VER=%%v
if "%VER%"=="" (echo 版本号自增失败 & goto :fail)
echo     本次版本：%VER%

echo === 2/4 清理旧产物 ===
REM ⚠ 只删自己的产物，别把整个 dist 端了：
REM   同事/自己常把分发包解在 dist 下面直接用，那里面有他们改过的
REM   config\strategies（策略中心的配置）。rd /s /q dist 会连人家的配置一起删掉，
REM   实测干掉过一次别人配了一下午的策略。
if exist build rd /s /q build
if exist "dist\配置助手.exe" del /q "dist\配置助手.exe"
if exist "dist\配置助手更新器.exe" del /q "dist\配置助手更新器.exe"
if exist "dist\配置助手-Setup-%VER%.exe" del /q "dist\配置助手-Setup-%VER%.exe"

echo === 3/4 打包 exe ===
REM --collect-all playwright: 把 playwright 的 node 驱动一起塞进去
REM 不需要浏览器内核，因为运行时挂载用户自己的 Chrome
REM --windowed: 图形界面，不弹黑色命令行窗口
REM --windowed: GUI, no console window
REM --icon: 大会员 logo, embedded into the exe
REM src.dmp_* / src.ab_* 这几个模块是在函数体里 import 的（用到才加载），显式声明避免被漏掉
REM webview: --web（方向C 新界面）走 src/webapp.py，同样是函数体里才 import 的；
REM   pywebview 自带 PyInstaller 钩子（webview/__pyinstaller/hook-webview.py）会顺带
REM   收 WebView2 需要的资源文件，这里只需要保证 webview 模块本身被发现
if not exist "assets\icon.ico" python tools\make_icon.py

pyinstaller ^
  --name "配置助手" ^
  --onefile ^
  --windowed ^
  --icon "assets\icon.ico" ^
  --collect-all playwright ^
  --hidden-import openpyxl ^
  --hidden-import PIL ^
  --hidden-import yaml ^
  --hidden-import pandas ^
  --hidden-import webview ^
  --hidden-import src.dmp_data ^
  --hidden-import src.dmp_date ^
  --hidden-import src.dmp_template ^
  --hidden-import src.ab_data ^
  --hidden-import src.ab_runner ^
  --hidden-import src.ab_template ^
  --hidden-import src.wizard_data ^
  --hidden-import src.wizard_filler ^
  --hidden-import src.wizard_runner ^
  --hidden-import src.wizard_schema ^
  --hidden-import src.wizard_strategy ^
  --hidden-import src.wizard_template ^
  --hidden-import src.ad_data ^
  --hidden-import src.ad_filler ^
  --hidden-import src.ad_image ^
  --hidden-import src.ad_prep ^
  --hidden-import src.ad_runner ^
  --hidden-import src.ad_template ^
  --hidden-import src.meeting_api ^
  --hidden-import src.meeting_data ^
  --hidden-import src.meeting_runner ^
  --hidden-import src.usage ^
  --hidden-import src.sheet ^
  --add-data "assets;assets" ^
  main.py
if errorlevel 1 goto :fail

REM 更新器必须是独立进程：主 EXE 退出后由它启动安装包，避免 Windows 文件锁。
pyinstaller ^
  --name "配置助手更新器" ^
  --onefile ^
  --windowed ^
  --icon "assets\icon.ico" ^
  tools\updater.py
if errorlevel 1 goto :fail

echo === 4/4 生成可自动更新的安装包 ===
"%ISCC%" /DMyAppVersion=%VER% installer.iss
if errorlevel 1 goto :fail

echo.
echo 完成：dist\配置助手-Setup-%VER%.exe
echo 首次发给同事的是这个安装包；后续版本再同时发布安装包和 latest.json。
echo latest.json 可用 tools\make_update_manifest.py 生成（需传入发布地址）。
pause
exit /b 0

:fail
echo.
echo 打包失败，请把上面的报错发给我。
pause
exit /b 1
