@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe -m price_monitor.sensoren_agent --enable %*
) else (
    if exist data\cloud_deps\psycopg set "PYTHONPATH=%~dp0data\cloud_deps"
    python -m price_monitor.sensoren_agent --enable %*
)
pause
