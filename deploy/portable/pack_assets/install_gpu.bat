@echo off
rem ============================================================
rem QuantMind Portable GPU Addon Installer (Windows)
rem Usage: extract QuantMind-Portable-gpu-addon-win-x64.zip into the
rem        portable package root (install_gpu.bat next to start.bat),
rem        then double-click this script.
rem
rem Effect: switches embedded torch from CPU to CUDA (2.9.1+cu128,
rem         bundled CUDA runtime included in torch/lib).
rem
rem Requirements:
rem   * NVIDIA GPU Turing (RTX 20 series) or newer (CUDA 12.8: sm_75~sm_120)
rem   * GTX 10 series (Pascal) or older NOT supported
rem   * NVIDIA driver >= 525
rem   * No CUDA Toolkit install needed (runtimes bundled)
rem   * Free disk space >= 6GB
rem Rollback to CPU: runtime\python\python.exe -m pip install torch==2.9.1+cpu
rem   --index-url https://download.pytorch.org/whl/cpu
rem NOTE: ASCII only on purpose (CRLF) - safe on any codepage.
rem ============================================================
setlocal
cd /d "%~dp0"
set "PY=%CD%\runtime\python\python.exe"

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

echo [gpu] Current torch:
"%PY%" -c "import torch; print(torch.__version__)" 2>nul || echo     (not installed)

echo [gpu] Uninstalling CPU torch ...
"%PY%" -m pip uninstall -y torch >nul 2>&1

echo [gpu] Extracting CUDA torch + runtime (about 3GB, may take 1-3 min) ...
"%PY%" -c "import zipfile; zipfile.ZipFile(r'%CD%\gpu_payload.zip').extractall(r'%CD%')"
if errorlevel 1 (
    echo [gpu] Extract failed. Check free disk space (need >= 6GB).
    pause
    exit /b 1
)

echo [gpu] Self check ...
"%PY%" -c "import torch; ok=torch.cuda.is_available(); print('[gpu] torch', torch.__version__); print('[gpu] cuda available:', ok); print('[gpu] device:', torch.cuda.get_device_name(0) if ok else 'none')" || echo [gpu] self-check error

echo.
echo [gpu] Done. CUDA torch active. Restart QuantMind services to apply.
echo [gpu] Rollback: runtime\python\python.exe -m pip install torch==2.9.1+cpu --index-url https://download.pytorch.org/whl/cpu
pause
endlocal
