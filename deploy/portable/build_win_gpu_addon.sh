#!/usr/bin/env bash
# ============================================================
# QuantMind 便携版 GPU 增补包构建（Windows x64 交叉组装）
# 产出: deploy/portable/dist/QuantMind-Portable-gpu-addon-win-x64.zip
#
# 用法（用户侧 Win）: 解压 zip 到便携包根目录 → 双击 install_gpu.bat
#   torch 从 CPU 版(2.9.1+cpu) 切换为 CUDA 版(2.9.1+cu128，与主包同小版本)
#
# 数据来源: download.pytorch.org cu128 win_amd64 wheel（约 2.6GB，含全套 nvidia 运行库）
# 依赖: 需已构建 Win 主包（build_windows_pack.sh 完成后运行）
#
# 显卡要求同 Linux 版: RTX 20 系+ (sm_75~120)，驱动 ≥ 525
# ============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$REPO_ROOT/deploy/portable"
BUILD="$HERE/build"
DIST="$HERE/dist"
STAGE_MAIN="$BUILD/QuantMind-Portable-win-x64"
STAGE_GPU="$BUILD/gpu-addon-win"
PAYLOAD="$STAGE_GPU/QuantMind-Portable-gpu-addon-win-x64"
CUDA_INDEX="https://download.pytorch.org/whl/cu128"
TORCH_GPU="${TORCH_GPU:-torch==2.9.1}"

log()  { echo -e "\033[36m[build-win-gpu]\033[0m $(date '+%H:%M:%S') $*"; }
ok()   { echo -e "\033[32m[build-win-gpu]\033[0m $(date '+%H:%M:%S') $*"; }
fail() { echo -e "\033[31m[build-win-gpu]\033[0m $*" >&2; exit 1; }

[ -f "$STAGE_MAIN/runtime/python/python.exe" ] || fail "Win 主包尚未构建（缺 runtime），先跑 build_windows_pack.sh"
mkdir -p "$BUILD/cache" "$DIST"

TARGET_SP="$PAYLOAD/runtime/python/Lib/site-packages"

# ── 1. 下载并解包 CUDA torch（win_amd64）───────────────────
if [ ! -f "$PAYLOAD/.payload_done" ]; then
    rm -rf "$PAYLOAD"
    mkdir -p "$TARGET_SP"

    WHEELS_GPU="$BUILD/cache/wheels-win-gpu"
    rm -rf "$WHEELS_GPU"; mkdir -p "$WHEELS_GPU"
    log "从 $CUDA_INDEX 下载 $TORCH_GPU (win_amd64, 约 2.6GB) ..."
    # nvidia-* CUDA 运行库在 PyPI 官方源；torch cu128 wheel 声明了这些依赖
    PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip download \
        --platform win_amd64 --python-version 3.10 --implementation cp \
        --only-binary=:all: \
        --extra-index-url "$CUDA_INDEX" \
        -d "$WHEELS_GPU" "$TORCH_GPU"

    log "解包到 payload site-packages ..."
    python3 - <<PYEOF
import zipfile, glob, os
target = os.path.abspath(r"$TARGET_SP")
for whl in glob.glob(r"$WHEELS_GPU/*.whl"):
    with zipfile.ZipFile(whl) as z:
        z.extractall(target)
    print("extracted:", os.path.basename(whl))
PYEOF

    [ -d "$TARGET_SP/torch" ] || fail "payload 缺少 torch 目录"
    # Windows 的 cu128 torch wheel 自包含 CUDA 运行库(torch/lib 内 dll)，无独立 nvidia 目录
    if ! ls "$TARGET_SP/torch/lib/" 2>/dev/null | grep -qiE "cudnn|cublas|cufft|curand|cusparse|nvToolsExt|c10_cuda"; then
        fail "payload torch/lib 未发现 CUDA 运行库，组装结果不是 CUDA 版"
    fi
    touch "$PAYLOAD/.payload_done"
fi

# ── 2. 压缩 payload + 生成安装脚本（zip 布局与 linux tar 版同构）─
cp "$HERE/pack_assets/install_gpu.bat" "$PAYLOAD/install_gpu.bat"
# .bat 必须 ASCII+CRLF：中文系统 cmd 以 GBK 解析 UTF-8+LF 的 bat 会错乱闪退
python3 - "$PAYLOAD/install_gpu.bat" <<'PYEOF'
import sys
f = sys.argv[1]
text = open(f, encoding="utf-8", errors="replace").read()
text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
open(f, "w", encoding="ascii", errors="replace", newline="").write(text)
print("  [bat-crlf]", f.split("/")[-1])
PYEOF
if [ ! -f "$PAYLOAD/gpu_payload.zip" ]; then
    log "压缩 payload (runtime) ..."
    (cd "$PAYLOAD" && python3 -m zipfile -c gpu_payload.zip runtime)
    rm -rf "$PAYLOAD/runtime"
fi

ZIP_OUT="$DIST/QuantMind-Portable-gpu-addon-win-x64.zip"
rm -f "$ZIP_OUT"
(cd "$PAYLOAD" && python3 -m zipfile -c "$ZIP_OUT" .)
ok "Win GPU 增补包完成: $ZIP_OUT ($(du -sh "$ZIP_OUT" | cut -f1))"
echo "  用户使用: 解压到便携包根目录 → 双击 install_gpu.bat"
