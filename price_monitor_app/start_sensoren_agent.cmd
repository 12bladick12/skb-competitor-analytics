@echo off
chcp 65001 >nul
if exist "%~dp0..\unified_analytics\start_sensoren_agent.cmd" (
    call "%~dp0..\unified_analytics\start_sensoren_agent.cmd" %*
    exit /b
)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe -m price_monitor.sensoren_agent --enable --fresh-connections %*
) else (
    if exist data\cloud_deps\psycopg set "PYTHONPATH=%~dp0data\cloud_deps"
    python -m price_monitor.sensoren_agent --enable --fresh-connections %*
)
pause
