@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo SENSE+ 최초 설치 - Python 3.11 또는 3.12가 필요합니다.
echo 기존 프로젝트와 전역 Python 설정은 바꾸지 않습니다.
echo.

if exist ".venv\Scripts\python.exe" goto install
py -3.12 -c "import sys; assert sys.version_info[:2] == (3, 12)" >nul 2>&1
if not errorlevel 1 (
    set "SENSE_PYTHON=py -3.12"
    goto create
)
py -3.11 -c "import sys; assert sys.version_info[:2] == (3, 11)" >nul 2>&1
if not errorlevel 1 (
    set "SENSE_PYTHON=py -3.11"
    goto create
)
python -c "import sys; assert sys.version_info[:2] in ((3, 11), (3, 12))" >nul 2>&1
if not errorlevel 1 (
    set "SENSE_PYTHON=python"
    goto create
)
echo Python 3.11 또는 3.12를 찾지 못했습니다.
echo Python 설치 후 이 파일을 다시 실행하세요. Python은 자동 설치하지 않습니다.
goto failed

:create
%SENSE_PYTHON% -m venv .venv
if errorlevel 1 goto failed

:install
".venv\Scripts\python.exe" -c "import sys; assert sys.version_info[:2] in ((3, 11), (3, 12)), 'Python 3.11/3.12 required'"
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo.
echo 설치 완료.
echo iPhone 연결 및 QR 표시 - start_phone_windows.bat
echo PC에서만 시험 - start_windows.bat
echo 유료 AI API는 호출하지 않았습니다.
pause
exit /b 0

:failed
echo.
echo 설치가 완료되지 않았습니다. 위 오류 내용을 확인해 주세요.
echo 다른 PC에서 복사한 .venv가 있다면 지운 뒤 다시 설치하세요.
echo README_KO.md의 문제 해결 부분을 참고하세요.
pause
exit /b 1
