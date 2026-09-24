#!/usr/bin/env bash
# QuantMind GPU 扩展脚本：把默认（CPU 版 torch）部署切换为 CUDA 版 torch。
#
# 初始部署（deploy.sh / full-deploy.sh）默认强制 TORCH_DEVICE=cpu：镜像小、构建快、
# 无 GPU 依赖。需要 GPU 推理/训练时用本脚本显式升级，并顺带开启 compose 的 GPU 直通。
#
# 用法:
#   sudo bash deploy/enable-gpu.sh               # 切到 CUDA 版 torch（并开启 GPU 直通）
#   sudo bash deploy/enable-gpu.sh --no-restart  # 只重建镜像，不重启服务
#   sudo bash deploy/enable-gpu.sh --cpu         # 回退到 CPU 版（并移除 GPU 直通）
#   sudo bash deploy/enable-gpu.sh --help
#
# 前置条件（GPU 模式）：宿主机已装 NVIDIA 驱动（nvidia-smi 可用）+
# nvidia-container-toolkit（docker 可 --gpus all）。
#
# 实现说明：
#   - torch 版本与本地训练镜像（ml-runtime / trainer）的重建复用
#     deploy/install-model-deps.sh，不重复实现；
#   - GPU 直通写入 docker-compose.override.yml（compose 自动合并；update.sh 的
#     `docker compose up` 不带 -f，同样会读取），不改动受版本管理的 docker-compose.yml。

set -Eeuo pipefail

PROJECT_DIR="${QUANTMIND_PROJECT_DIR:-/opt/quantmind}"
OVERRIDE_FILE="$PROJECT_DIR/docker-compose.override.yml"
OVERRIDE_MARKER="# generated-by: deploy/enable-gpu.sh"
MODE=gpu
NO_RESTART=false

log() { printf '[enable-gpu] %s\n' "$*"; }
die() { log "错误: $*" >&2; exit 1; }

usage() {
    cat <<'EOF'
用法: sudo bash deploy/enable-gpu.sh [选项]

  (默认)        切换到 CUDA 版 torch（重建镜像 + 开启 GPU 直通 + 重启校验）
  --cpu         回退到 CPU 版 torch（重建镜像 + 移除 GPU 直通 + 重启）
  --no-restart  仅重建镜像，不重启服务（重启后生效）
  -h, --help    显示帮助
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cpu) MODE=cpu ; shift ;;
        --no-restart) NO_RESTART=true ; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数: $1" ;;
    esac
done

require_root() { [[ $EUID -eq 0 ]] || die '请使用 sudo 执行'; }

require_project() {
    [[ -f "$PROJECT_DIR/docker-compose.yml" ]] || die "缺少 docker-compose.yml: $PROJECT_DIR"
    command -v docker >/dev/null || die 'Docker 未安装'
    docker compose version >/dev/null || die 'Docker Compose 不可用'
    [[ -f "$PROJECT_DIR/deploy/install-model-deps.sh" ]] \
        || die "缺少 deploy/install-model-deps.sh（本脚本复用它重建 torch）"
}

check_gpu_prereq() {
    log '检查 GPU 前置条件'
    command -v nvidia-smi >/dev/null 2>&1 \
        || die '未检测到 nvidia-smi：请先在宿主机安装 NVIDIA 驱动'
    nvidia-smi -L >/dev/null 2>&1 \
        || die 'nvidia-smi 无法列出 GPU：驱动异常'
    log "  GPU: $(nvidia-smi -L | head -1)"
    if docker info 2>/dev/null | grep -qi nvidia; then
        log '  docker: 已注册 nvidia runtime'
    else
        log '  警告：docker info 未显示 nvidia runtime；若最后校验 CUDA 不可用，'
        log '        请安装 nvidia-container-toolkit 后重试'
    fi
}

enable_passthrough() {
    if [[ -f "$OVERRIDE_FILE" ]] && ! grep -q "$OVERRIDE_MARKER" "$OVERRIDE_FILE"; then
        die "$OVERRIDE_FILE 已存在且非本脚本生成，拒绝覆盖；请先手工处理"
    fi
    cat > "$OVERRIDE_FILE" <<EOF
$OVERRIDE_MARKER
# 为 quantmind 服务开启 NVIDIA GPU 直通（compose 自动合并本文件）。
# 回退 CPU: sudo bash deploy/enable-gpu.sh --cpu
services:
  quantmind:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
EOF
    log "已写入 GPU 直通: $OVERRIDE_FILE"
}

disable_passthrough() {
    [[ -f "$OVERRIDE_FILE" ]] || return 0
    if grep -q "$OVERRIDE_MARKER" "$OVERRIDE_FILE"; then
        rm -f "$OVERRIDE_FILE"
        log "已移除 GPU 直通: $OVERRIDE_FILE"
    else
        log "警告：$OVERRIDE_FILE 非本脚本生成，保持不动"
    fi
}

delegate_rebuild() {
    local args=()
    if [[ "$MODE" == gpu ]]; then
        args+=(--gpu)
    fi
    if $NO_RESTART; then
        args+=(--refine)
    fi
    log "调用 deploy/install-model-deps.sh ${args[*]:-}（重建 torch 镜像）"
    QUANTMIND_TORCH_DEVICE="$MODE" bash "$PROJECT_DIR/deploy/install-model-deps.sh" "${args[@]}"
}

verify() {
    if $NO_RESTART; then
        log '未重启（--no-restart）。重启后生效: sudo docker compose up -d --force-recreate quantmind'
        return 0
    fi
    log '校验容器内 torch/CUDA'
    local out
    if out="$(docker exec quantmind python3 -c \
        'import torch; print(torch.__version__, "cuda=" + str(torch.cuda.is_available()))' 2>/dev/null)"; then
        log "  $out"
        if [[ "$MODE" == gpu && "$out" != *"cuda=True"* ]]; then
            log '  ⚠️ CUDA 不可用：请检查宿主 nvidia-container-toolkit 与 GPU 直通'
        fi
    else
        log '  ⚠️ 无法在容器内校验（容器未就绪？可稍后手动执行 docker exec quantmind python3 -c "import torch;print(torch.cuda.is_available())"）'
    fi
}

main() {
    require_root
    require_project
    if [[ "$MODE" == gpu ]]; then
        check_gpu_prereq
        enable_passthrough
    else
        disable_passthrough
    fi
    delegate_rebuild
    verify
    log "完成：TORCH_DEVICE=$MODE"
    if [[ "$MODE" == gpu ]]; then
        log '回退 CPU: sudo bash deploy/enable-gpu.sh --cpu'
    else
        log '升级 GPU: sudo bash deploy/enable-gpu.sh'
    fi
}

main "$@"
