#!/usr/bin/env bash
# ============================================================
# QuantMind 便携版 GPU 增补包构建（Linux x86_64）
# 产出: deploy/portable/dist/QuantMind-Portable-gpu-addon-linux-x64.tar.gz
#
# 用法（用户侧）: tar -xzf 增补包 -C 便携包根目录/ → bash install_gpu.sh
#   torch 从 CPU 版切换为 CUDA 版（与 quantmind-oss-gpu 镜像同版本），
#   主包体积不变，不需要 GPU 的用户完全不用下载。
#
# 数据来源（二选一，自动选择）:
#   1. 本地 docker 镜像 quantmind-oss-gpu:latest（优先，免下载）
#   2. pip 下载 torch==2.9.1 CUDA wheel（约 2.5GB）
#
# 可覆盖: TORCH_SPEC 默认 torch==2.9.1
# 依赖: 需已构建主包（复用主包 runtime 的 python 做实测）
# ============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$REPO_ROOT/deploy/portable"
BUILD="$HERE/build"
DIST="$HERE/dist"
STAGE_MAIN="$BUILD/QuantMind-Portable-linux-x64"
STAGE_GPU="$BUILD/gpu-addon"
PAYLOAD="$STAGE_GPU/QuantMind-Portable-gpu-addon-linux-x64"
GPU_IMAGE="${GPU_IMAGE:-quantmind-oss-gpu:latest}"

TORCH_SPEC="${TORCH_SPEC:-torch==2.9.1}"
PIP_DEFAULT="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
PIP_FALLBACKS=("$PIP_DEFAULT" "https://pypi.tuna.tsinghua.edu.cn/simple/" "https://pypi.org/simple/")
# torch CUDA 版带入包内的顶层目录/元数据（其余依赖主包已有）
PAYLOAD_DIRS="torch torchgen functorch nvidia triton"

log()  { echo -e "\033[36m[build-gpu]\033[0m $(date '+%H:%M:%S') $*"; }
ok()   { echo -e "\033[32m[build-gpu]\033[0m $(date '+%H:%M:%S') $*"; }
fail() { echo -e "\033[31m[build-gpu]\033[0m $*" >&2; exit 1; }

[ -x "$STAGE_MAIN/runtime/python/bin/python3" ] || fail "主包尚未构建（缺 runtime），先跑 build_linux_pack.sh"
mkdir -p "$BUILD/cache" "$DIST"

PY="$STAGE_MAIN/runtime/python/bin/python3"
TARGET_SP="$PAYLOAD/runtime/python/lib/python3.10/site-packages"

# ── 1. 组装 CUDA payload（镜像提取优先，pip 下载兜底）─────────
if [ ! -f "$PAYLOAD/.payload_done" ]; then
    rm -rf "$PAYLOAD"
    mkdir -p "$TARGET_SP"

    if docker image inspect "$GPU_IMAGE" --format '{{.Id}}' >/dev/null 2>&1; then
        log "从本地镜像 $GPU_IMAGE 提取 CUDA torch（免下载）..."
        C="$(docker run -d --entrypoint sleep "$GPU_IMAGE" 600)"
        trap 'docker rm -f "$C" >/dev/null 2>&1 || true' EXIT
        SP_IN_IMAGE="$("$C" >/dev/null 2>&1 || true; docker exec "$C" python3 -c 'import site; print(site.getsitepackages()[0])' 2>/dev/null || echo /usr/local/lib/python3.10/site-packages)"
        for d in $PAYLOAD_DIRS; do
            docker cp "$C:$SP_IN_IMAGE/$d" "$TARGET_SP/" >/dev/null
        done
        # dist-info 元数据
        for di in $(docker exec "$C" ls "$SP_IN_IMAGE" | grep -E '^(torch-[0-9]|nvidia_|triton-)' | grep dist-info); do
            docker cp "$C:$SP_IN_IMAGE/$di" "$TARGET_SP/" >/dev/null
        done
        docker rm -f "$C" >/dev/null
        trap - EXIT
        log "镜像提取完成"
    else
        log "本地无 $GPU_IMAGE 镜像，pip 下载 CUDA torch（约 2.5GB）..."
        export PIP_DISABLE_PIP_VERSION_CHECK=1
        GPU_OK=0
        for _idx in "${PIP_FALLBACKS[@]}"; do
            log "使用镜像 $_idx 安装 $TORCH_SPEC ..."
            if PIP_INDEX_URL="$_idx" "$PY" -m pip install --no-cache-dir \
                    --target "$TARGET_SP" "$TORCH_SPEC"; then
                GPU_OK=1; break
            fi
            log "镜像 $_idx 失败，切换下一个 ..."
            rm -rf "$TARGET_SP"; mkdir -p "$TARGET_SP"
        done
        [ "$GPU_OK" = "1" ] || fail "所有镜像安装 CUDA torch 均失败"
    fi

    [ -d "$TARGET_SP/nvidia/cudnn" ] || fail "payload 缺少 nvidia/cudnn，组装结果不是 CUDA 版"
    touch "$PAYLOAD/.payload_done"
fi

# ── 2. 用主包 runtime 实测 payload（构建机有 NVIDIA 驱动时）────
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
    tar -cf - -C "$PAYLOAD" runtime | pigz > "$PAYLOAD/gpu_payload.tar.gz"
    rm -rf "$PAYLOAD/runtime"   # 压缩后移除明文目录，增补包只留脚本+payload
fi
chmod +x "$PAYLOAD/install_gpu.sh"

ZIP_OUT="$DIST/QuantMind-Portable-gpu-addon-linux-x64.tar.gz"
rm -f "$ZIP_OUT"
# 平铺布局：tar -xzf 增补包 -C 便携包根目录 即可直接合并（install_gpu.sh 落在包根）
tar -C "$PAYLOAD" -cf - . | pigz > "$ZIP_OUT"
ok "GPU 增补包完成: $ZIP_OUT ($(du -sh "$ZIP_OUT" | cut -f1))"
echo "  用户使用: tar -xzf 增补包 -C 便携包根目录/ → bash install_gpu.sh"
