@echo off
rem QuantMind 便携版 停止脚本（Windows）
chcp 65001 >nul
cd /d "%~dp0"
set "ROOT=%~dp0"

echo [stop] 停止 Celery / 后端 / Redis / PostgreSQL ...
taskkill /FI "WINDOWTITLE eq QuantMind-CeleryBeat*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-CeleryWorker*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-Backend*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-Redis*" /T /F >nul 2>&1

"%ROOT%redis\redis-cli.exe" -p 6379 shutdown nosave >nul 2>&1 && echo [stop] Redis 已停止
if exist "%ROOT%pgdata\PG_VERSION" (
    "%ROOT%pgsql\bin\pg_ctl.exe" -D "%ROOT%pgdata" stop -m fast >nul 2>&1 && echo [stop] PostgreSQL 已停止
)
echo [stop] 完成
pause
