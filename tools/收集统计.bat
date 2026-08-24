@echo off
chcp 65001 >nul
cd /d "%~dp0.."
REM 用法：在「机器人统计」群里 Ctrl+A、Ctrl+C，然后双击这个文件。
REM 它会把群里的上报消息整理成 config\team.json，并自动推上 GitHub ——
REM 同事下次打开程序就能看到新数字，不用等下一次发版。
where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] 找不到 python。这个脚本只在收集端跑，需要本机装了 Python 3.10+。
  echo.
  pause
  exit /b 1
)
python tools\collect_usage.py --sheet
echo.
pause
