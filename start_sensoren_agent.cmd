@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe -m price_monitor.sensoren_supervisor --enable --fresh-connections %*
) else (
    if exist data\cloud_deps\psycopg set "PYTHONPATH=%~dp0data\cloud_deps"
    if not exist data\cloud_deps\psycopg if exist ..\price_monitor_app\data\cloud_deps\psycopg set "PYTHONPATH=%~dp0..\price_monitor_app\data\cloud_deps"
    python -m price_monitor.sensoren_supervisor --enable --fresh-connections %*
)
pause
