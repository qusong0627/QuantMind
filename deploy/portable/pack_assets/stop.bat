@echo off
rem ============================================================
rem QuantMind Portable Stop Script (Windows x64)
rem Stops Celery / backend / Redis / PostgreSQL.
rem NOTE: ASCII only on purpose (CRLF) - safe on any codepage.
rem ============================================================
setlocal
cd /d "%~dp0"
set "ROOT=%~dp0"

echo [stop] Stopping Celery / backend / Redis / PostgreSQL ...
taskkill /FI "WINDOWTITLE eq QuantMind-CeleryBeat*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-CeleryWorker*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-Backend*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-Redis*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-Huntly*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-QwenPaw*" /T /F >nul 2>&1

"%ROOT%redis\redis-cli.exe" -p 6379 shutdown nosave >nul 2>&1 && echo [stop] Redis stopped
if exist "%ROOT%pgdata\PG_VERSION" (
    "%ROOT%pgsql\bin\pg_ctl.exe" -D "%ROOT%pgdata" stop -m fast >nul 2>&1 && echo [stop] PostgreSQL stopped
)
echo [stop] Done
pause
endlocal
