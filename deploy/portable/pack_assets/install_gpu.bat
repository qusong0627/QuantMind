@echo off
rem ============================================================
rem QuantMind Portable GPU Addon Installer (Windows)
rem Usage: extract QuantMind-Portable-gpu-addon-win-x64.zip into the
rem        portable package root, then double-click this script.
rem Effect: switches torch from CPU to CUDA 2.9.1+cu128.
rem Errors are printed and the window always pauses.
rem Flat style, ASCII, CRLF - safe on any codepage/cmd.
rem ============================================================
setlocal
cd /d "%~dp0"
set "PY=%CD%\runtime\python\python.exe"
set "LOG=%CD%\logs\gpu_install.log"

if not exist "%PY%" goto :no_py
if not exist "%CD%\gpu_payload.zip" goto :no_payload
mkdir "%CD%\logs" 2>nul
goto :start

:no_py
echo [!] runtime\python\python.exe not found.
echo     Run this script from the portable package root.
pause
exit /b 1

:no_payload
echo [!] gpu_payload.zip not found.
echo     Extract the whole GPU addon zip into the package root first.
pause
exit /b 1

:start
echo [gpu] Install log: %LOG%
echo [%date% %time%] gpu install begin >> "%LOG%"

echo [gpu] Current torch:
"%PY%" -c "import torch; print('  torch', torch.__version__)" >> "%LOG%" 2>&1
"%PY%" -c "import torch; print('  torch', torch.__version__)" 2>&1
if not errorlevel 1 goto :torch_ok

echo [!] torch import failed - full error above and in %LOG%.
echo     If it mentions a missing DLL, install Microsoft VC Redist x64
echo     from aka.ms/vs/17/release/vc_redist.x64.exe and retry.
echo     If torch is simply absent, a previous interrupted install may
echo     have removed the CPU build - continuing is fine in that case.
echo     If the error is anything else, close this window and report it.
echo [gpu] continuing anyway...
goto :proceed

:torch_ok
echo [gpu] CPU torch present - will uninstall then extract CUDA build.

:proceed
echo [gpu] Uninstalling CPU torch ...
"%PY%" -m pip uninstall -y torch >> "%LOG%" 2>&1

echo [gpu] Extracting CUDA torch + runtime - about 3GB, 1-3 minutes...
echo [%date% %time%] extracting payload >> "%LOG%"
"%PY%" -c "import zipfile; zipfile.ZipFile(r'%CD%\gpu_payload.zip').extractall(r'%CD%')" >> "%LOG%" 2>&1
if errorlevel 1 goto :extract_fail

echo [gpu] Self check ...
"%PY%" -c "import torch; print('  torch', torch.__version__); ok=torch.cuda.is_available(); print('  cuda available:', ok); print('  device:', torch.cuda.get_device_name(0) if ok else 'none')" >> "%LOG%" 2>&1
"%PY%" -c "import torch; print('  torch', torch.__version__); ok=torch.cuda.is_available(); print('  cuda available:', ok); print('  device:', torch.cuda.get_device_name(0) if ok else 'none')" 2>&1
if errorlevel 1 goto :check_fail

echo.
echo [gpu] Done. CUDA torch active. Restart with start.bat.
echo [gpu] Rollback: runtime\python\python.exe -m pip install torch==2.9.1+cpu --index-url https://download.pytorch.org/whl/cpu
pause
exit /b 0

:extract_fail
echo [!] Extract failed - see %LOG%
echo     Check free disk space, that services are stopped, and that
echo     no antivirus is locking the package folder.
pause
exit /b 1

:check_fail
echo [!] Self check failed - see %LOG%
pause
exit /b 1
