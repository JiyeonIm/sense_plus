@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo 먼저 setup_windows.bat를 실행해 주세요.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" phone.py
pause
