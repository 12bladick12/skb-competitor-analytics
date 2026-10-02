@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
if exist ".venv-local\Scripts\python.exe" (
    ".venv-local\Scripts\python.exe" scripts\run_local.py %*
) else if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" scripts\run_local.py %*
) else (
    python scripts\run_local.py %*
)
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" (
    echo.
    echo Не удалось запустить приложение. Проверьте сообщение выше.
    pause
)
exit /b %APP_EXIT%
