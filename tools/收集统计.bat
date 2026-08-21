@echo off
chcp 65001 >nul
cd /d "%~dp0.."
REM 用法：在「机器人统计」群里 Ctrl+A、Ctrl+C，然后双击这个文件。
REM 它会把群里的上报消息整理成 config\team.json，下次打包自动带给同事。
python tools\collect_usage.py --sheet
echo.
pause
