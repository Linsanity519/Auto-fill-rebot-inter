@echo off
cd /d "%~dp0"

rem Launch the GUI with pythonw (no console window).
rem NOTE: keep this file ASCII-only. cmd parses .bat with the OEM codepage,
rem so UTF-8 Chinese here gets mangled and can break command parsing.

where pythonw >nul 2>&1
if not errorlevel 1 (
    start "" pythonw "%~dp0main.py"
    exit /b 0
)

where python >nul 2>&1
if not errorlevel 1 (
    python "%~dp0main.py"
    goto :check
)

echo.
echo [ERROR] Python not found. Install Python 3.10+ first.
echo.
pause
exit /b 1

:check
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to start. Install dependencies:
    echo   pip install -i https://mirrors.aliyun.com/pypi/simple/ PyYAML playwright pandas openpyxl
    echo.
    pause
)
exit /b 0
