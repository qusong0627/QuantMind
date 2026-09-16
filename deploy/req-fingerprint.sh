#!/usr/bin/env bash
# 计算 quantmind-oss 镜像的「依赖指纹」（qm.req.sha）。
#
# 指纹 = 影响镜像依赖环境的文件内容哈希（SHA-256 截取前 16 位），仅包含：
#   - requirements.txt / requirements/production.txt / requirements/ai.txt
#   - docker/Dockerfile.oss（构建逻辑变化同样要求重建）
#   - docker-compose.yml 中 quantmind 服务的构建参数（TORCH_DEVICE 等）
# 业务代码走 bind mount，不改变指纹、不触发重建 —— 这正是「速度」的来源。
#
# 用法：
#   构建/打包侧（把指纹烙进镜像 Label，供部署侧比对）：
#     QM_REQ_SHA=$(bash deploy/req-fingerprint.sh) docker compose build quantmind
#   部署侧（deploy/full-deploy.sh 内部使用）：
#     比对「当前代码指纹」与「镜像 LABEL qm.req.sha」，一致→复用，不一致→重建。
#   从未写明 TORCH_DEVICE 时，按镜像 Label 反推形态（避免默认 skip 误重建）：
#     bash deploy/req-fingerprint.sh --infer-torch [ROOT] [IMAGE]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

compute_req_sha() {
    local ROOT="$1"
    local torch_device="$2"
    local parts='' f h build_args
    for f in \
        requirements.txt \
        requirements/production.txt \
        requirements/ai.txt \
        docker/Dockerfile.oss; do
        if [[ -f "$ROOT/$f" ]]; then
            h="$(sha256sum "$ROOT/$f" | awk '{print $1}')"
        else
            h=missing
        fi
        parts="${parts}${f}=${h}"$'\n'
    done
    # compose 中真正影响镜像构建的参数取值（如 TORCH_DEVICE 切换即视为依赖变更）。
    build_args="$(grep -E '^\s*(TORCH_DEVICE|TORCH_CPU_INDEX_URL|QM_REQ_SHA):' \
        "$ROOT/docker-compose.yml" 2>/dev/null || true)"
    parts="${parts}build-args=${build_args}"
    parts="${parts}torch=${torch_device:-skip}"
    printf '%s' "$parts" | sha256sum | awk '{print substr($1,1,16)}'
}

read_torch_from_env() {
    local ROOT="$1"
    local torch_device="${TORCH_DEVICE:-}"
    if [[ -z "$torch_device" && -f "$ROOT/.env" ]]; then
        torch_device="$(grep -E '^\s*TORCH_DEVICE=' "$ROOT/.env" 2>/dev/null | tail -1 \
            | cut -d= -f2- | tr -d "\"' " || true)"
    fi
    printf '%s' "${torch_device:-skip}"
}

image_label() {
    local image="$1" key="$2"
    docker image inspect "$image" \
        --format "{{ index .Config.Labels \"$key\" }}" 2>/dev/null || true
}

# 优先读 qm.torch.device；旧镜像没有该 Label 时，用当前代码分别按 cpu/gpu/skip
# 重算指纹，与 qm.req.sha 对得上的即为打包时的 torch 形态。
infer_torch_from_image() {
    local ROOT="$1"
    local IMAGE="${2:-quantmind-oss:latest}"
    local labeled have want d
    docker image inspect "$IMAGE" >/dev/null 2>&1 || return 0
    labeled="$(image_label "$IMAGE" "qm.torch.device")"
    case "$labeled" in
        cpu|gpu|skip)
            printf '%s' "$labeled"
            return 0
            ;;
    esac
    have="$(image_label "$IMAGE" "qm.req.sha")"
    case "$have" in
        ""|none|"<no value>")
            # 早于指纹机制的镜像：能 import torch 则按 cpu 保留，避免 skip 重建拆掉推理环境。
            if docker run --rm --network none --entrypoint python "$IMAGE" \
                -c 'import torch' >/dev/null 2>&1; then
                printf 'cpu'
            fi
            return 0
            ;;
    esac
    for d in cpu gpu skip; do
        want="$(compute_req_sha "$ROOT" "$d")"
        if [[ -n "$want" && "$want" == "$have" ]]; then
            printf '%s' "$d"
            return 0
        fi
    done
}

MODE="hash"
ROOT_ARG=""
IMAGE_ARG="quantmind-oss:latest"
if [[ "${1:-}" == "--infer-torch" ]]; then
    MODE="infer"
    ROOT_ARG="${2:-}"
    IMAGE_ARG="${3:-quantmind-oss:latest}"
else
    ROOT_ARG="${1:-}"
fi

ROOT="$(cd "${ROOT_ARG:-$SCRIPT_DIR/..}" && pwd)"

if [[ "$MODE" == "infer" ]]; then
    infer_torch_from_image "$ROOT" "$IMAGE_ARG"
    exit 0
fi

compute_req_sha "$ROOT" "$(read_torch_from_env "$ROOT")"
