#!/usr/bin/env bash
# ============================================================
# QuantMind 便携版 GPU 增补包构建（Linux x86_64）
# 产出: deploy/portable/dist/QuantMind-Portable-gpu-addon-linux-x64.tar.gz
#
# 用法（用户侧）: 增补包解压到便携包根目录 → bash install_gpu.sh
#   torch 从 CPU 版切换为 CUDA 版（与 quantmind-oss-gpu 镜像同版本），
#   主包体积不变，不需要 GPU 的用户完全不用下载。
#
# 可覆盖: TORCH_SPEC 默认 torch==2.9.1（PyPI 默认 CUDA 构建，与 GPU 镜像一致）
# 依赖: 需已构建主包（复用主包 runtime 的 python 来解析平台标签）
# ============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$REPO_ROOT/deploy/portable"
BUILD="$HERE/build"
DIST="$HERE/dist"
STAGE_MAIN="$BUILD/QuantMind-Portable-linux-x64"
STAGE_GPU="$BUILD/gpu-addon"
PAYLOAD="$STAGE_GPU/QuantMind-Portable-gpu-addon-linux-x64"

TORCH_SPEC="${TORCH_SPEC:-torch==2.9.1}"
PIP_DEFAULT="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
PIP_FALLBACKS=("$PIP_DEFAULT" "https://pypi.tuna.tsinghua.edu.cn/simple/" "https://pypi.org/simple/")

log()  { echo -e "\033[36m[build-gpu]\033[0m $(date '+%H:%M:%S') $*"; }
ok()   { echo -e "\033[32m[build-gpu]\033[0m $(date '+%H:%M:%S') $*"; }
fail() { echo -e "\033[31m[build-gpu]\033[0m $*" >&2; exit 1; }

[ -x "$STAGE_MAIN/runtime/python/bin/python3" ] || fail "主包尚未构建（缺 runtime），先跑 build_linux_pack.sh"
mkdir -p "$BUILD/cache" "$DIST"

PY="$STAGE_MAIN/runtime/python/bin/python3"
TARGET_SP="$PAYLOAD/runtime/python/lib/python3.10/site-packages"

# ── 1. 安装 CUDA 版 torch 到独立 payload 目录 ────────────────
if [ ! -f "$PAYLOAD/.payload_done" ]; then
    rm -rf "$PAYLOAD"
    mkdir -p "$TARGET_SP"
    export PIP_DISABLE_PIP_VERSION_CHECK=1
    GPU_OK=0
    for _idx in "${PIP_FALLBACKS[@]}"; do
        log "使用镜像 $_idx 安装 $TORCH_SPEC（CUDA 版，下载约 2.5GB）..."
        if PIP_INDEX_URL="$_idx" "$PY" -m pip install --no-cache-dir \
                --target "$TARGET_SP" "$TORCH_SPEC"; then
            GPU_OK=1; break
        fi
        log "镜像 $_idx 失败，切换下一个 ..."
        rm -rf "$TARGET_SP"; mkdir -p "$TARGET_SP"
    done
    [ "$GPU_OK" = "1" ] || fail "所有镜像安装 CUDA torch 均失败"
    # 校验 CUDA 构建已就位（带 nvidia 运行库目录）
    [ -d "$TARGET_SP/nvidia/cudnn" ] || fail "payload 缺少 nvidia/cudnn，装到的可能是 CPU 版"
    touch "$PAYLOAD/.payload_done"
fi

# ── 2. 用主包 runtime 实测 payload 可用（构建机有 NVIDIA 驱动时）──
if command -v nvidia-smi >/dev/null 2>&1; then
    log "构建机实测 CUDA payload ..."
    "$PY" -I <<PYEOF
import sys
sys.path.insert(0, "$TARGET_SP")
import torch
assert torch.version.cuda, "payload 中的 torch 不是 CUDA 构建: " + torch.__version__
assert torch.cuda.is_available(), "CUDA 不可用，payload 校验失败"
x = torch.randn(128, 128, device="cuda")
assert (x @ x).sum().item() != 0
print("  torch", torch.__version__, "| GPU 实测通过:", torch.cuda.get_device_name(0))
PYEOF
else
    log "警告: 构建机无 NVIDIA 驱动，跳过实测（用户侧 install_gpu.sh 会再自检）"
fi

# ── 3. 打包 payload + 安装脚本 ───────────────────────────────
cp "$HERE/pack_assets/install_gpu.sh" "$PAYLOAD/install_gpu.sh"
# 幂等：payload 明文已被上次运行移除时，直接复用已压缩的 gpu_payload.tar.gz
if [ ! -f "$PAYLOAD/gpu_payload.tar.gz" ]; then
    log "压缩 payload ..."
    tar -czf "$PAYLOAD/gpu_payload.tar.gz" -C "$PAYLOAD" runtime
    rm -rf "$PAYLOAD/runtime"   # 压缩后移除明文目录，增补包只留脚本+payload
fi
chmod +x "$PAYLOAD/install_gpu.sh"

ZIP_OUT="$DIST/QuantMind-Portable-gpu-addon-linux-x64.tar.gz"
rm -f "$ZIP_OUT"
# 平铺布局：tar -xzf 增补包 -C 便携包根目录 即可直接合并（install_gpu.sh 落在包根）
if command -v pigz >/dev/null 2>&1; then
    tar -C "$PAYLOAD" -cf - . | pigz > "$ZIP_OUT"
else
    tar -C "$PAYLOAD" -czf "$ZIP_OUT" .
fi
ok "GPU 增补包完成: $ZIP_OUT ($(du -sh "$ZIP_OUT" | cut -f1))"
echo "  用户使用: tar -xzf 增补包 -C 便携包根目录/ → bash install_gpu.sh"
