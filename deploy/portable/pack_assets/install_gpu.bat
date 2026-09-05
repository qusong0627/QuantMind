@echo off
rem ============================================================
rem QuantMind Portable GPU Addon Installer (Windows)
rem Usage: extract QuantMind-Portable-gpu-addon-win-x64.zip into the
rem        portable package root (install_gpu.bat next to start.bat),
rem        then double-click this script.
rem Effect: switches embedded torch from CPU to CUDA (2.9.1+cu128).
rem Errors are printed (never hidden) and the window always pauses.
rem NOTE: ASCII only on purpose (CRLF) - safe on any codepage.
rem ============================================================
setlocal
cd /d "%~dp0"
set "PY=%CD%\runtime\python\python.exe"
set "LOG=%CD%\logs\gpu_install.log"

if not exist "%PY%" (
    echo [!] runtime\python\python.exe not found.
    echo     Run this script from the portable package root.
    pause
    exit /b 1
)
if not exist "%CD%\gpu_payload.zip" (
    echo [!] gpu_payload.zip not found.
    echo     Extract the whole GPU addon zip into the package root first.
    pause
    exit /b 1
)
mkdir "%CD%\logs" 2>nul

echo [gpu] Install log: %LOG%
echo [%date% %time%] gpu install begin >> "%LOG%"

echo [gpu] Current torch:
"%PY%" -c "import torch; print('  torch', torch.__version__)" >> "%LOG%" 2>&1
"%PY%" -c "import torch; print('  torch', torch.__version__)" 2>&1
if errorlevel 1 (
    echo [!] torch import failed - full error above and in %LOG%.
    echo     If error mentions missing DLL, install Microsoft VC Redist x64
    echo     from aka.ms/vs/17/release/vc_redist.x64.exe and retry.
    echo     If torch is simply absent, re-extract the main pack first.
    pause
    exit /b 1
)

echo [gpu] Uninstalling CPU torch ...
"%PY%" -m pip uninstall -y torch >> "%LOG%" 2>&1

echo [gpu] Extracting CUDA torch + runtime (about 3GB, may take 1-3 min) ...
echo [%date% %time%] extracting payload >> "%LOG%"
"%PY%" -c "import zipfile; zipfile.ZipFile(r'%CD%\gpu_payload.zip').extractall(r'%CD%')" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [!] Extract failed - see %LOG%
    echo     Check free disk space (need 6GB+) and that the package is
    echo     not read-only. Full error is printed above this box.
    pause
    exit /b 1
)
echo [%date% %time%] extract done >> "%LOG%"

echo [gpu] Self check ...
"%PY%" -c "import torch; print('  torch', torch.__version__); ok=torch.cuda.is_available(); print('  cuda available:', ok); print('  device:', torch.cuda.get_device_name(0) if ok else 'none')" >> "%LOG%" 2>&1
"%PY%" -c "import torch; print('  torch', torch.__version__); ok=torch.cuda.is_available(); print('  cuda available:', ok); print('  device:', torch.cuda.get_device_name(0) if ok else 'none')" 2>&1
if errorlevel 1 (
    echo [!] Self check failed - see %LOG%
    pause
    exit /b 1
)

echo.
echo [gpu] Done. CUDA torch active. Restart QuantMind (stop.bat then start.bat).
echo [gpu] Rollback: runtime\python\python.exe -m pip install torch==2.9.1+cpu --index-url https://download.pytorch.org/whl/cpu
pause
endlocal
