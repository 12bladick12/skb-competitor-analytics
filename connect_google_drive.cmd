@echo off
setlocal
cd /d "%~dp0"
set "SKB_DRIVE_PYTHON=.venv-cloud\Scripts\python.exe"
if not exist "%SKB_DRIVE_PYTHON%" set "SKB_DRIVE_PYTHON=.venv\Scripts\python.exe"
if not exist "%SKB_DRIVE_PYTHON%" (
  echo Python environment not found. Install requirements-cloud.txt first.
  pause
  exit /b 1
)
"%SKB_DRIVE_PYTHON%" -X utf8 scripts\connect_google_drive.py
set "SKB_DRIVE_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %SKB_DRIVE_EXIT%
