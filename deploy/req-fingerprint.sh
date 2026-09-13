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
set -euo pipefail

ROOT="$(cd "${1:-$(dirname "${BASH_SOURCE[0]}")/..}" && pwd)"

parts=''
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

# TORCH_DEVICE 生效值：环境变量优先，其次项目 .env（compose 插值同款优先级）。
# 镜像按 cpu/gpu/skip 构建出的是完全不同的依赖环境，必须纳入指纹。
torch_device="${TORCH_DEVICE:-}"
if [[ -z "$torch_device" && -f "$ROOT/.env" ]]; then
    torch_device="$(grep -E '^\s*TORCH_DEVICE=' "$ROOT/.env" 2>/dev/null | tail -1 \
        | cut -d= -f2- | tr -d "\"' " || true)"
fi
parts="${parts}torch=${torch_device:-skip}"

printf '%s' "$parts" | sha256sum | awk '{print substr($1,1,16)}'
