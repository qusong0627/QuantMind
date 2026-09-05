@echo off
rem ============================================================
rem QuantMind Portable One-Click Start (Windows x64)  v3-flat
rem Flat goto style: no nested parenthesized blocks, no caret
rem continuations. Safer across cmd versions/codepages.
rem ============================================================
setlocal
cd /d "%~dp0"
set "ROOT=%~dp0"
echo MARK-A

rem ---- ports ----
set "QM_PG_PORT=5432"
set "QM_REDIS_PORT=6379"
set "QM_API_PORT=8000"
set "QM_ENGINE_PORT=8001"
set "QM_TRADE_PORT=8002"
set "QM_STREAM_PORT=8003"

set "STORAGE_ROOT=%ROOT%data"
set "PYTHON=%ROOT%runtime\python\python.exe"
set "PGBIN=%ROOT%pgsql\bin"
set "PATH=%ROOT%redis;%PGBIN%;%PATH%"

if exist "%PYTHON%" goto :py_ok
echo [!] python.exe not found: %PYTHON%
echo     Make sure the package is fully extracted to a LOCAL disk.
pause
exit /b 1
:py_ok
echo MARK-B

mkdir "%ROOT%logs" 2>nul
mkdir "%ROOT%run" 2>nul
echo [%date% %time%] start.bat v3 begin (root %ROOT%) >> "%ROOT%logs\startup.log" 2>nul

if not "%ROOT:~0,2%"=="\\" goto :not_share
echo [!] Detected a network share path.
echo     PostgreSQL/Redis cannot run on SMB shares.
echo     Please copy the whole folder to a local disk, e.g. C:\QuantMind
pause
exit /b 2
:not_share

for %%D in (models uploads strategies reports backtest_results hf qlib_data quantdb quantus quanthk quantbc quantfutures) do if not exist "%STORAGE_ROOT%\%%D" mkdir "%STORAGE_ROOT%\%%D"

if exist "%ROOT%run\secrets.cmd" goto :secrets_ok
powershell -NoProfile -Command "$a=[guid]::NewGuid().ToString('N')+[guid]::NewGuid().ToString('N'); ('set SECRET_KEY='+$a.Substring(0,64)) | Out-File -Encoding ascii '%ROOT%run\secrets.cmd'; ('set JWT_SECRET_KEY='+$a.Substring(0,64)) | Out-File -Encoding ascii -Append '%ROOT%run\secrets.cmd'; ('set INTERNAL_CALL_SECRET='+$a.Substring(0,64)) | Out-File -Encoding ascii -Append '%ROOT%run\secrets.cmd'"
:secrets_ok
if exist "%ROOT%run\secrets.cmd" call "%ROOT%run\secrets.cmd"
echo MARK-C

rem ---- env ----
rem UTF-8 mode required on zh-CN Windows (GBK breaks SQL/seed/logging)
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONLEGACYWINDOWSSTDIO="
set "APP_EDITION=oss"
set "APP_ENV=production"
set "SERVICE_MODE=all"
set "TZ=Asia/Shanghai"
set "PYTHONPATH=%ROOT%"
set "DB_DRIVER=asyncpg"
set "DB_HOST=127.0.0.1"
set "DB_PORT=%QM_PG_PORT%"
set "DB_NAME=quantmind"
set "DB_USER=quantmind"
set "DB_PASSWORD=quantmind2026"
set "DATABASE_URL=postgresql+asyncpg://quantmind:quantmind2026@127.0.0.1:%QM_PG_PORT%/quantmind"
set "POSTGRES_HOST=127.0.0.1"
set "POSTGRES_PORT=%QM_PG_PORT%"
set "POSTGRES_USER=quantmind"
set "POSTGRES_PASSWORD=quantmind2026"
set "POSTGRES_DB=quantmind"
set "REDIS_HOST=127.0.0.1"
set "REDIS_PORT=%QM_REDIS_PORT%"
set "REDIS_URL=redis://127.0.0.1:%QM_REDIS_PORT%"
set "REDIS_PASSWORD="
set "STORAGE_MODE=local"
set "QUANTMIND_ENABLE_WEB_UPDATE=false"
set "API_PORT=%QM_API_PORT%"
set "ENGINE_PORT=%QM_ENGINE_PORT%"
set "TRADE_PORT=%QM_TRADE_PORT%"
set "STREAM_PORT=%QM_STREAM_PORT%"
set "API_WORKERS=1"
set "ENGINE_WORKERS=1"
set "TRADE_WORKERS=1"
set "STREAM_WORKERS=1"
set "TRADE_SERVICE_URL=http://127.0.0.1:%QM_TRADE_PORT%"
set "ENGINE_SERVICE_URL=http://127.0.0.1:%QM_ENGINE_PORT%"
set "AI_IDE_SERVICE_URL=http://127.0.0.1:%QM_ENGINE_PORT%"
set "STREAM_SERVICE_URL=http://127.0.0.1:%QM_STREAM_PORT%"
set "MARKET_DATA_SERVICE_URL=http://127.0.0.1:%QM_STREAM_PORT%"
set "STRATEGY_SERVICE_URL=http://127.0.0.1:%QM_ENGINE_PORT%"
set "PORTFOLIO_SERVICE_URL=http://127.0.0.1:%QM_TRADE_PORT%"
set "USER_SERVICE_URL=http://127.0.0.1:%QM_TRADE_PORT%"
set "REAL_TRADING_SERVICE_URL=http://127.0.0.1:%QM_TRADE_PORT%"
set "ENABLE_TDX_PUSH=false"
set "ENABLE_REAL_TRADING=false"
set "DEBUG=false"
set "LOG_LEVEL=INFO"
set "STRATEGY_TEMPLATES_DIR=%ROOT%strategy_templates"
set "QLIB_BACKTEST_RESULT_DIR=%STORAGE_ROOT%\backtest_results"
set "QLIB_BACKTEST_KERNELS=1"
set "QM_REPORTS_DIR=%STORAGE_ROOT%\reports"
set "TRADING_AGENTS_RESULTS_DIR=%STORAGE_ROOT%\reports\trading_agents"
set "QM_WEB_DIST_DIR=%ROOT%web"
set "HF_HOME=%STORAGE_ROOT%\hf"
set "MPLCONFIGDIR=%ROOT%run\mpl"
set "ENABLE_CRYPTO=false"
set "QM_QUANTDB_DATA_DIR=%STORAGE_ROOT%\quantdb"
set "QM_QUANTUS_DATA_DIR=%STORAGE_ROOT%\quantus"
set "QM_QUANTHK_DATA_DIR=%STORAGE_ROOT%\quanthk"
set "QM_QUANTBC_DATA_DIR=%STORAGE_ROOT%\quantbc"
set "QM_QUANTFUTURES_DATA_DIR=%STORAGE_ROOT%\quantfutures"
if not defined LLM_API_KEY set "LLM_API_KEY=not-configured"
set "ADMIN_DASHBOARD_API_HEALTH_URL=http://127.0.0.1:%QM_API_PORT%/health"
set "ADMIN_DASHBOARD_ENGINE_HEALTH_URL=http://127.0.0.1:%QM_ENGINE_PORT%/health"
set "ADMIN_DASHBOARD_TRADE_HEALTH_URL=http://127.0.0.1:%QM_TRADE_PORT%/health"
set "ADMIN_DASHBOARD_STREAM_HEALTH_URL=http://127.0.0.1:%QM_STREAM_PORT%/health"
set "ADMIN_DASHBOARD_DB_HOST=127.0.0.1"
set "ADMIN_DASHBOARD_DB_PORT=%QM_PG_PORT%"
set "ADMIN_DASHBOARD_REDIS_HOST=127.0.0.1"
set "ADMIN_DASHBOARD_REDIS_PORT=%QM_REDIS_PORT%"
set "ADMIN_DASHBOARD_DISABLED_SERVICES=data_gateway,web,rsshub"
set "HUNTLY_USERNAME=admin"
set "HUNTLY_PASSWORD=admin123"
set "QM_HUNTLY_PORT=8090"
set "QM_QWENPAW_PORT=8088"
rem local service endpoints (otherwise backend/health falls back to docker names)
set "HUNTLY_BASE_URL=http://127.0.0.1:%QM_HUNTLY_PORT%"
set "QWENPAW_BASE_URL=http://127.0.0.1:%QM_QWENPAW_PORT%"
set "ADMIN_DASHBOARD_HUNTLY_HOST=127.0.0.1"
set "ADMIN_DASHBOARD_HUNTLY_PORT=%QM_HUNTLY_PORT%"
set "ADMIN_DASHBOARD_QWENPAW_HOST=127.0.0.1"
set "ADMIN_DASHBOARD_QWENPAW_PORT=%QM_QWENPAW_PORT%"
echo MARK-D

echo [QuantMind] Checking PostgreSQL ...
if exist "%ROOT%pgdata\PG_VERSION" goto :pg_started
echo [QuantMind] First run: initializing PostgreSQL data dir ...
echo quantmind2026> "%ROOT%run\pg_pw.txt"
"%PGBIN%\initdb.exe" -D "%ROOT%pgdata" -U quantmind -A scram-sha-256 --pwfile="%ROOT%run\pg_pw.txt" -E UTF8 --no-locale
del "%ROOT%run\pg_pw.txt" 2>nul
:pg_started
"%PYTHON%" "%ROOT%pg_setup.py" wait --timeout 1 >nul 2>&1
if not errorlevel 1 goto :pg_ready
echo [QuantMind] Starting PostgreSQL (port %QM_PG_PORT%) ...
"%PGBIN%\pg_ctl.exe" -D "%ROOT%pgdata" -l "%ROOT%logs\postgres.log" -o "-p %QM_PG_PORT% -c listen_addresses=127.0.0.1" start
:pg_ready
"%PYTHON%" "%ROOT%pg_setup.py" wait --timeout 60 >nul 2>&1
if not errorlevel 1 goto :pg_ok
echo [!] PostgreSQL failed to start, see logs\postgres.log
pause
exit /b 1
:pg_ok
"%PYTHON%" "%ROOT%pg_setup.py" ensure-db >nul 2>&1
if not errorlevel 1 goto :pg_ok2
echo [!] ensure-db failed, see logs\postgres.log
pause
exit /b 1
:pg_ok2
echo [QuantMind] PostgreSQL ready
echo [%date% %time%] PostgreSQL ready >> "%ROOT%logs\startup.log" 2>nul
echo MARK-E

echo [QuantMind] Checking Redis ...
"%ROOT%redis\redis-cli.exe" -p %QM_REDIS_PORT% ping 2>nul | findstr PONG >nul
if not errorlevel 1 goto :redis_ready
echo [QuantMind] Starting Redis (port %QM_REDIS_PORT%) ...
start "QuantMind-Redis" /min cmd /c ""%ROOT%redis\redis-server.exe" --port %QM_REDIS_PORT% --bind 127.0.0.1 --dir "%ROOT%run""
set TRY=0
:redis_wait
"%ROOT%redis\redis-cli.exe" -p %QM_REDIS_PORT% ping 2>nul | findstr PONG >nul
if not errorlevel 1 goto :redis_ready
set /a TRY+=1
if %TRY% GEQ 15 goto :redis_fail
timeout /t 1 /nobreak >nul
goto :redis_wait
:redis_fail
echo [!] Redis failed to start, see logs\redis.log
pause
exit /b 1
:redis_ready
echo [QuantMind] Redis ready
echo [%date% %time%] Redis ready >> "%ROOT%logs\startup.log" 2>nul
echo MARK-F

echo [QuantMind] Starting QuantMind backend (api:%QM_API_PORT% engine:%QM_ENGINE_PORT% trade:%QM_TRADE_PORT% stream:%QM_STREAM_PORT%) ...
start "QuantMind-Backend" /min cmd /c "cd /d "%ROOT%" && "%PYTHON%" backend\main_oss.py > "%ROOT%logs\backend.log" 2>&1"
start "QuantMind-CeleryWorker" /min cmd /c "cd /d "%ROOT%" && "%PYTHON%" -m celery -A backend.services.engine.qlib_app.celery_config:celery_app worker -Q qlib_backtest_srv --loglevel=info --concurrency=2 --pool=solo > "%ROOT%logs\celery-worker.log" 2>&1"
start "QuantMind-CeleryBeat" /min cmd /c "cd /d "%ROOT%" && "%PYTHON%" -m celery -A backend.services.engine.qlib_app.celery_config:celery_app beat --loglevel=info --schedule="%ROOT%run\celerybeat-schedule" > "%ROOT%logs\celery-beat.log" 2>&1"

rem ---- optional components (huntly RSS / qwenpaw AI) ----
mkdir "%ROOT%data\huntly" 2>nul
if exist "%ROOT%huntly\server.jar" if exist "%ROOT%huntly\jre\bin\java.exe" (
    start "QuantMind-Huntly" /min cmd /c ""%ROOT%huntly\jre\bin\java.exe" -Xmx512m -jar "%ROOT%huntly\server.jar" --huntly.dataDir="%ROOT%data\huntly\" --server.port=%QM_HUNTLY_PORT% > "%ROOT%logs\huntly.log" 2>&1"
)
if exist "%ROOT%qwenpaw_runtime\python\python.exe" (
    if not exist "%ROOT%data\qwenpaw-home" mkdir "%ROOT%data\qwenpaw-home"
    start "QuantMind-QwenPaw" /min cmd /c "cd /d "%ROOT%" && "%ROOT%qwenpaw_runtime\python\python.exe" -m qwenpaw app --host 127.0.0.1 --port %QM_QWENPAW_PORT% > "%ROOT%logs\qwenpaw.log" 2>&1"
)

echo [QuantMind] Waiting for services (first run may take 1-2 min) ...
echo [QuantMind] Progress: dots each second, percent every 15s
set TRY=0
:http_wait
curl -fsS -m 2 http://127.0.0.1:%QM_API_PORT%/health >nul 2>&1
if not errorlevel 1 goto :http_ok
set /a TRY+=1
set /a MOD=TRY %% 15
if not %MOD%==0 goto :dot_only
set /a PCT=TRY*100/180
<nul set /p "=[%PCT%%%] "
:dot_only
<nul set /p "=."
if %TRY% GEQ 180 goto :http_fail
timeout /t 1 /nobreak >nul
goto :http_wait
:http_fail
echo.
echo [!] Service start timeout, see logs\backend.log and logs\startup.log
pause
exit /b 1
:http_ok

echo ==============================================
echo [QuantMind] Ready: http://127.0.0.1:%QM_API_PORT%/
echo ==============================================
echo [%date% %time%] ready, opening browser >> "%ROOT%logs\startup.log" 2>nul
start "" http://127.0.0.1:%QM_API_PORT%/
echo.
echo [QuantMind] ============================================
echo [QuantMind] CLOSING THIS WINDOW IS SAFE - all services
echo [QuantMind] keep running in the background.
echo [QuantMind] To STOP everything later: double-click
echo [QuantMind] stop.bat (stops backend/celery/redis/pg/
echo [QuantMind] huntly/qwenpaw). Re-start: start.bat again.
echo [QuantMind] Logs: logs\backend.log / logs\startup.log
echo [QuantMind] ============================================
pause
endlocal
