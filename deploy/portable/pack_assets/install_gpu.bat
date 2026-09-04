@echo off
chcp 65001 >nul
rem ============================================================
rem QuantMind 便携版 GPU 增补安装脚本 (Windows)
rem 用法: 把 QuantMind-Portable-gpu-addon-win-x64.zip 解压到便携包根目录
rem       （install_gpu.bat 与 gpu_payload.zip 与 start.bat 同级），然后双击本脚本
rem
rem 效果: 包内 torch 从 CPU 版切换为 CUDA 版(2.9.1+cu128, 含全套 CUDA 运行库)
rem
rem 显卡/驱动要求:
rem   * NVIDIA 显卡 RTX 20 系(Turing)及更新: 20/30/40/50 系（CUDA 12.8: sm_75~sm_120）
rem   * GTX 10 系及更老架构不支持
rem   * 驱动版本 >= 525 (2023 年后驱动基本满足)
rem   * CUDA 运行库随包附带，无需安装 CUDA Toolkit
rem   * 磁盘剩余 >= 6GB
rem 回退 CPU 版: runtime\python\python.exe -m pip install torch==2.9.1+cpu
rem              --index-url https://download.pytorch.org/whl/cpu
rem ============================================================
setlocal
cd /d "%~dp0"
set "PY=%CD%\runtime\python\python.exe"

if not exist "%PY%" (
    echo [!] 未找到 runtime\python\python.exe，请在便携包根目录运行本脚本
    pause & exit /b 1
)
if not exist "%CD%\gpu_payload.zip" (
    echo [!] 找不到 gpu_payload.zip，请先把 GPU 增补包解压到便携包根目录
    pause & exit /b 1
)

echo [gpu] 当前 torch:
"%PY%" -c "import torch; print(torch.__version__)" 2>nul || echo     (未安装)

echo [gpu] 卸载 CPU 版 torch ...
"%PY%" -m pip uninstall -y torch >nul 2>&1

echo [gpu] 解压 CUDA 版 torch + CUDA 运行库（约 3GB，需 1-3 分钟）...
"%PY%" -c "import zipfile; zipfile.ZipFile(r'%CD%\gpu_payload.zip').extractall(r'%CD%')" || (
    echo [gpu] 解压失败，请检查磁盘空间(需>=6GB)后重试
    pause & exit /b 1
)

echo [gpu] 自检 ...
"%PY%" -c "import torch; ok=torch.cuda.is_available(); print('[gpu] torch', torch.__version__); print('[gpu] cuda available:', ok); print('[gpu] 设备:', torch.cuda.get_device_name(0) if ok else '无')" || echo [gpu] 自检异常

echo.
echo [gpu] 完成。GPU 版 torch 已生效（重启 QuantMind 后端服务后对训练/推理生效）。
echo [gpu] 回退 CPU 版: runtime\python\python.exe -m pip install torch==2.9.1+cpu --index-url https://download.pytorch.org/whl/cpu
pause
endlocal
