@echo off
chcp 65001 >nul
setlocal EnableExtensions
set "ROOT=%~dp0"
set "QMTARGS="
echo %* | findstr /C:"-Restart" >nul && set "QMTARGS=-Force"
set "TDXARGS=%*"
echo %* | findstr /C:"-NoPause" >nul || set "TDXARGS=%* -NoPause"

echo.
echo [1/2] TDX trading desk ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\launcher.ps1" %TDXARGS%
set "TDXRC=%ERRORLEVEL%"

echo.
echo [2/2] QMT companion bridge ...
if not exist "%ROOT%qmt-tools\start_pipe_bridge.ps1" goto :noqmt
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%qmt-tools\start_pipe_bridge.ps1" %QMTARGS%
set "QMTRC=%ERRORLEVEL%"
goto :qmtstatus

:noqmt
echo   QMT launcher not found: %ROOT%qmt-tools\start_pipe_bridge.ps1
set "QMTRC=9"

:qmtstatus
set "CACHERC=0"
echo.
echo [3/3] QMT cache / parameter check ...
echo %* | findstr /C:"-NoCacheCheck" >nul
if not errorlevel 1 goto :cache_skip
set "QMT_PY=C:\Program Files\Python312\python.exe"
if not exist "%QMT_PY%" set "QMT_PY=C:\xmt\venv\Scripts\python.exe"
if not exist "%ROOT%qmt-tools\cache_quality_check.py" goto :cache_missing
"%QMT_PY%" "%ROOT%qmt-tools\cache_quality_check.py" --wait 15
set "CACHERC=%ERRORLEVEL%"
goto :cache_done

:cache_missing
echo   cache_quality_check.py not found
set "CACHERC=9"
goto :cache_done

:cache_skip
echo   cache check skipped (-NoCacheCheck)

:cache_done
echo.
echo TDX rc=%TDXRC%  QMT rc=%QMTRC%  CACHE rc=%CACHERC%
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 17720 -State Listen -ErrorAction SilentlyContinue | Select-Object LocalPort,OwningProcess"
echo %* | findstr /C:"-NoPause" >nul
if errorlevel 1 pause
endlocal
