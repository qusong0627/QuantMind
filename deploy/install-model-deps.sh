#!/usr/bin/env bash
# QuantMind 模型依赖补齐脚本
#
# 离线部署镜像默认 TORCH_DEVICE=skip（不含 PyTorch，省构建时间/体积）。
# 本脚本为已部署环境按需补装 PyTorch(torch)，供 FinBERT 新闻情感、模型推理与训练使用。
# 默认采用「重建镜像」持久方案：重装 torch 后生成的镜像重启不丢。
#
# 用法: sudo bash deploy/install-model-deps.sh [选项]
#   默认安装 torch CPU 版（无需 GPU，构建快、镜像小）
#   可选模式：
#     --gpu     安装完整 CUDA 版 torch（需 NVIDIA GPU），并顺带构建本地训练镜像
#               quantmind-trainer（CUDA torch）写入 TRAINING_IMAGE，让模型训练真用上 GPU
#     --refine  仅重建镜像不重启服务
#
# 环境变量覆盖（可选）:
#   QUANTMIND_TORCH_DEVICE     cpu | gpu | skip      （默认 cpu）
#   QUANTMIND_TORCH_CPU_INDEX_URL   CPU 版 torch 下载源（默认官方 https://download.pytorch.org/whl/cpu）
#   PIP_INDEX_URL / PIP_TRUSTED_HOST   PyPI 镜像源（CUDA 完整 torch 走此源）

set -Eeuo pipefail

PROJECT_DIR="${QUANTMIND_PROJECT_DIR:-/opt/quantmind}"
TORCH_DEVICE="${QUANTMIND_TORCH_DEVICE:-cpu}"
TORCH_CPU_INDEX_URL="${QUANTMIND_TORCH_CPU_INDEX_URL:-https://download.pytorch.org/whl/cpu}"
RESTART=true

log() { printf '[model-deps] %s\n' "$*"; }
die() { log "错误: $*" >&2; exit 1; }

usage() {
    cat <<'EOF'
用法: sudo bash deploy/install-model-deps.sh [选项]

  --gpu       安装 CUDA 版 torch，并构建本地训练镜像(quantmind-trainer)使其可用 GPU
              （需 NVIDIA GPU + nvidia-container-toolkit，镜像体积大、构建慢）
  --refine    已重建镜像但不重启服务（默认重建后会自动重启）
  -h, --help  显示帮助

说明：
  离线部署镜像默认不含 PyTorch。本脚本通过重建镜像补装 torch，重启不丢。
  CPU 版足够 FinBERT 情感与多数推理；GPU 训练才需要 --gpu（同时补齐本地训练镜像）。
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --gpu) TORCH_DEVICE=gpu ; shift ;;
        --refine) RESTART=false ; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数: $1" ;;
    esac
done

require_root() { [[ $EUID -eq 0 ]] || die '请使用 sudo 执行'; }
require_project() {
    [[ -d "$PROJECT_DIR/.git" ]] || die "不是 Git 部署目录: $PROJECT_DIR"
    [[ -f "$PROJECT_DIR/docker-compose.yml" ]] || die "缺少 docker-compose.yml: $PROJECT_DIR"
    command -v docker >/dev/null || die 'Docker 未安装'
    docker compose version >/dev/null || die 'Docker Compose 不可用'
}

prepare_env() {
    TORCH_DEVICE="$1"; export TORCH_DEVICE
    export TORCH_CPU_INDEX_URL
    log "TORCH_DEVICE=$TORCH_DEVICE"
    if [[ "$TORCH_DEVICE" == gpu ]]; then
        log "提示：GPU 版 torch 仅下载依赖即可能数 GB、构建耗时较长；请确认宿主机已装 NVIDIA 驱动 + nvidia-container-toolkit。"
        log "      若镜像已含 CUDA 仍无法用 GPU，常因 docker-compose.yml 中 quantmind 服务的 GPU 直通(devices/reservations)被注释，需按仓库注释放开后再 up。"
    elif [[ "$TORCH_DEVICE" == skip ]]; then
        die "TORCH_DEVICE=skip 表示不安装 torch，本脚本无意义；请用 cpu 或 gpu"
    fi
    # 持久化到 .env：避免后续 update/full-deploy 按默认(skip)判定依赖指纹漂移，
    # 把已含 torch 的镜像回退重建（指纹把 TORCH_DEVICE 纳入）。
    local env_file="$PROJECT_DIR/.env"
    if [[ -f "$env_file" ]]; then
        if grep -qE '^[[:space:]]*TORCH_DEVICE=' "$env_file"; then
            sed -i "s|^[[:space:]]*TORCH_DEVICE=.*|TORCH_DEVICE=${TORCH_DEVICE}|" "$env_file"
        else
            printf '\n# torch 形态（install-model-deps 写入，用于依赖指纹对齐）\nTORCH_DEVICE=%s\n' \
                "$TORCH_DEVICE" >> "$env_file"
        fi
        log "已同步 .env → TORCH_DEVICE=${TORCH_DEVICE}"
    fi
}

rebuild_image() {
    log '重建量化核心镜像并安装 PyTorch...'
    cd "$PROJECT_DIR"
    # --pull=false 复用离线包导入的基镜像，不额外拉基础镜像；
    # TORCH_DEVICE/TORCH_CPU_INDEX_URL 由 docker-compose.yml 的 build.args 读取。
    # CPU 路径的 torch 走官方 CPU 源，其传递依赖走默认 PyPI 源（见 Dockerfile.oss 说明）。
    docker compose -f "$PROJECT_DIR/docker-compose.yml" build --pull=false quantmind
}

restart_services() {
    log '重启服务以使用新镜像...'
    cd "$PROJECT_DIR"
    # 镜像变化后 up -d 自动重建受影响容器；--pull never 不再尝试在线拉镜像。
    docker compose up -d --pull never
}

# 写入/更新服务器 .env 的 TRAINING_IMAGE，让编排器使用本机构建的训练镜像。
set_training_image() {
    local env_file="$PROJECT_DIR/.env"
    local val="TRAINING_IMAGE=quantmind-trainer:latest"
    if [[ -f "$env_file" ]] && grep -q '^TRAINING_IMAGE=' "$env_file"; then
        sed -i "s|^TRAINING_IMAGE=.*|${val}|" "$env_file"
    else
        printf '\n# 本地训练镜像（install-model-deps.sh 写入，GPU 环境使用）\n%s\n' "$val" >> "$env_file"
    fi
    log "已设置 .env → $val"
}

build_trainer_image() {
    # GPU 环境的本地训练链：quantmind-ml-runtime（含 CUDA torch）→ quantmind-trainer。
    # 默认 ml-runtime 不含 torch（省体积），此处显式 TORCH_DEVICE=gpu 补齐。
    log 'GPU 环境：构建本地训练镜像（CUDA torch）...'
    cd "$PROJECT_DIR"
    docker build --build-arg TORCH_DEVICE=gpu \
        -f docker/Dockerfile.ml-runtime -t quantmind-ml-runtime:latest .
    docker build -f docker/Dockerfile.training -t quantmind-trainer:latest .
    set_training_image
}

verify_torch() {
    log '验证量化容器内 PyTorch'
    local out
    if out="$(docker exec quantmind python3 -c \
        'import torch; print(f"torch {torch.__version__}", "CUDA" if torch.cuda.is_available() else "CPU-only" )' 2>/dev/null)"; then
        log "✅ 量化容器 PyTorch 可用: $out"
    else
        log '⚠️ 量化容器 pytorch 校验失败（可能容器未就绪或镜像仍旧）。可用:'
        log '   docker exec quantmind python3 -c "import torch; print(torch.__version__)"'
    fi
}

verify_trainer_cuda() {
    local out
    if out="$(docker run --rm --gpus all --entrypoint python quantmind-trainer:latest \
        -c 'import torch; print(f"{torch.__version__} cuda={torch.cuda.is_available()}")' 2>/dev/null)"; then
        log "✅ 训练镜像 CUDA 可用: $out"
    else
        log '⚠️ 训练镜像未探测到 CUDA。可能宿主缺 nvidia-container-toolkit，或需放开 docker-compose.yml 的 GPU 直通。'
        log '   量化容器内可再查: docker exec quantmind python3 -c "import torch; print(torch.cuda.is_available())"'
    fi
}

main() {
    require_root
    require_project
    prepare_env "$TORCH_DEVICE"
    rebuild_image
    if [[ "$TORCH_DEVICE" == gpu ]]; then
        build_trainer_image
    fi
    if $RESTART; then
        restart_services
        verify_torch
        if [[ "$TORCH_DEVICE" == gpu ]]; then
            verify_trainer_cuda
        fi
    else
        log '已重建镜像未重启（--refine）。重启后新镜像生效: sudo docker compose up -d'
    fi
    log '完成：PyTorch 已装进持久镜像。FinBERT 如需完全生效，确保权重也已就位（backend/scripts/download_finbert.py）'
    echo ""
    echo "========================================================================="
    echo " 🧠 PyTorch 补齐完成"
    echo " -------------------------------------------------------------------------"
    echo " 验证 FinBERT 是否生效（model_version 带 +finbert）:"
    echo "   docker exec quantmind-db psql -U quantmind -d quantmind -c \\"
    echo "     \"SELECT model_version, count(*) FROM news_article_enrichment GROUP BY model_version;\""
    echo "========================================================================="
}

main "$@"