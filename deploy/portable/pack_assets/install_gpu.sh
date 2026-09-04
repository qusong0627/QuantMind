#!/usr/bin/env bash
# QuantMind 便携版 GPU 增补安装脚本
# 用法: 把 GPU 增补包解压到便携包根目录，然后 bash install_gpu.sh
#
# 效果: 包内 torch 从 CPU 版切换为 CUDA 版（与 quantmind-oss-gpu GPU 镜像同版本，含全套 CUDA 运行库）
#
# ── 显卡/驱动要求 ──────────────────────────────────────────
#   * NVIDIA 显卡，架构 RTX 20 系(Turing)及更新：20/30/40/50 系、
#     专业卡(Tesla/A100/H100/RTX A 系等)均支持（CUDA 12.8 覆盖 sm_75~sm_120）
#   * GTX 10 系(Pascal)及更老架构不受支持（需另打 cu118/cu121 变体包）
#   * 驱动版本 ≥ 525（2023 年后的驱动基本满足），nvidia-smi 能正常输出即可
#   * CUDA 运行库已随包附带，无需单独安装 CUDA Toolkit
#   * 磁盘剩余 ≥ 6GB
# ──────────────────────────────────────────────────────────
# 回退: bash runtime/python/bin/python3 -m pip install torch==2.9.1+cpu \
#         --index-url https://download.pytorch.org/whl/cpu （需联网）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/runtime/python/bin/python3"

[ -x "$PY" ] || { echo "[!] 请在便携包根目录运行本脚本（未找到 runtime/python）"; exit 1; }
[ -f "$ROOT/gpu_payload.tar.gz" ] || { echo "[!] 找不到 gpu_payload.tar.gz，请先把 GPU 增补包整体解压到便携包根目录"; exit 1; }
command -v nvidia-smi >/dev/null 2>&1 || { echo "[!] 未检测到 NVIDIA 驱动（nvidia-smi 不可用），请先安装驱动"; exit 1; }

echo "[gpu] 当前 torch: $("$PY" -c 'import torch; print(torch.__version__)' 2>/dev/null || echo '未安装')"
echo "[gpu] 卸载 CPU 版 torch ..."
"$PY" -m pip uninstall -y torch >/dev/null 2>&1 || true

echo "[gpu] 解压 CUDA 版 torch + CUDA 运行库（约 3GB，需 1-3 分钟）..."
tar -xzf "$ROOT/gpu_payload.tar.gz" -C "$ROOT"

echo "[gpu] 自检 ..."
"$PY" - <<'PYEOF'
import torch

print("[gpu] torch 版本:", torch.__version__)
ok = torch.cuda.is_available()
print("[gpu] cuda available:", ok)
if ok:
    x = torch.randn(256, 256, device="cuda")
    y = (x @ x).sum().item()
    print("[gpu] GPU 矩阵运算自检通过:", y == y and y != 0)
    print("[gpu] 设备:", torch.cuda.get_device_name(0))
else:
    print("[gpu] 警告: 未见可用 GPU，请确认驱动版本 (nvidia-smi)")
PYEOF

echo "[gpu] 完成。GPU 版 torch 已生效（重启 QuantMind 后端服务后对引擎/训练生效）。"
echo "[gpu] 回退 CPU 版: bash runtime/python/bin/python3 -m pip install 'torch==2.9.1+cpu' --index-url https://download.pytorch.org/whl/cpu"
