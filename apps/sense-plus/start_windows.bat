@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo 먼저 setup_windows.bat를 실행해 주세요.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" run.py --open-browser %*
set "SENSE_EXIT=%errorlevel%"
if not "%SENSE_EXIT%"=="0" (
    echo.
    echo 실행에 실패했습니다. 위 오류 내용과 README_KO.md를 확인해 주세요.
    pause
)
exit /b %SENSE_EXIT%
