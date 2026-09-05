#!/bin/bash
# AutoDL 远程训练镜像一键构建（从 git 源码，在 AutoDL 节点上构建）
#
# 思路：不在本地构建 24GB 大镜像推送（慢），而是利用 AutoDL 更好的网络，
#      在远端直接 git 拉源码 + docker build，产物即留在 AutoDL 节点。
#
# 流程：
#   1. 读取 .env 的 AutoDL 连接配置（HOST/PORT/USER/PASSWORD）
#   2. ssh 到 AutoDL：删除旧 quantmind-train 镜像
#   3. AutoDL 上 git clone/更新源码（gitee 仓库）
#   4. AutoDL 上 docker build 构建训练镜像
#
# 用法：
#   source scripts/setup/torch_select.sh   # 提供 select_torch_config
#   source scripts/setup/build-autodl-remote.sh
#   build_autodl_remote                    # 执行远程构建

build_autodl_remote() {
    if ! declare -f info >/dev/null 2>&1; then
        info()  { echo -e "\033[0;36m[INFO]\033[0m $1"; }
        ok()    { echo -e "\033[0;32m[OK]\033[0m $1"; }
        warn()  { echo -e "\033[0;33m[WARN]\033[0m $1"; }
        error() { echo -e "\033[0;31m[ERROR]\033[0m $1"; exit 1; }
    fi

    ENV_FILE="${ENV_FILE:-.env}"
    local HOST PORT USER PASS KEY WORKDIR GIT_URL IMAGE_NAME
    HOST=$(grep -E "^TRAINING_AUTODL_HOST=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
    PORT=$(grep -E "^TRAINING_AUTODL_SSH_PORT=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
    USER=$(grep -E "^TRAINING_AUTODL_USER=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
    PASS=$(grep -E "^TRAINING_AUTODL_SSH_PASSWORD=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
    KEY=$(grep -E "^TRAINING_AUTODL_SSH_KEY=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
    WORKDIR=$(grep -E "^TRAINING_AUTODL_WORK_DIR=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
    IMAGE_NAME=$(grep -E "^TRAINING_AUTODL_DOCKER_IMAGE=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
    # 远端 git 仓库（默认 gitee）
    GIT_URL="${AUTODL_GIT_URL:-https://gitee.com/qusong0627/QuantMind.git}"
    # 远端源码目录（git clone 到这里）
    local SRC_DIR="${WORKDIR:-/workspace}/quantmind-src"

    [ -z "$HOST" ] && { error "TRAINING_AUTODL_HOST 未配置，无法远程构建。"; return 1; }
    [ -z "$PORT" ] && PORT=22
    [ -z "$USER" ] && USER=root
    [ -z "$WORKDIR" ] && WORKDIR=/workspace
    [ -z "$IMAGE_NAME" ] && IMAGE_NAME=quantmind-train:latest
    if [ -n "$KEY" ]; then
        SSHBASE="ssh -i ${KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=10"
    elif command -v sshpass >/dev/null 2>&1 && [ -n "$PASS" ]; then
        SSHBASE="sshpass -p ${PASS} ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10"
    else
        error "缺少 SSH 认证（TRAINING_AUTODL_SSH_KEY 或 TRAINING_AUTODL_SSH_PASSWORD 需配置其一，且 sshpass 需安装）"
        return 1
    fi
    local SSH_CMD
    SSH_CMD() { ${SSHBASE} -p "${PORT}" "${USER}@${HOST}" "$@"; }

    info "AutoDL 远程构建（${USER}@${HOST}:${PORT}）..."
    info "  远端源码目录: ${SRC_DIR}"
    info "  远端镜像: ${IMAGE_NAME}"
    info "  仓库: ${GIT_URL}"

    # 1. 删除旧镜像
    warn "删除旧镜像 ${IMAGE_NAME}（若存在）..."
    SSH_CMD "docker rmi -f ${IMAGE_NAME} 2>/dev/null; echo done" || { error "SSH 连接失败，请检查 AutoDL 配置"; return 1; }

    # 2. git 拉取源码（分支可经 AUTODL_GIT_BRANCH 覆盖，默认 master 稳定版；
    #    开发/修复管线在 next 时构建前设 AUTODL_GIT_BRANCH=next）
    info "AutoDL 上 git 拉取源码（分支 ${AUTODL_GIT_BRANCH:-master}）..."
    local BRANCH="${AUTODL_GIT_BRANCH:-master}"
    SSH_CMD "mkdir -p ${SRC_DIR} && cd ${SRC_DIR} && \
        (git rev-parse --git-dir >/dev/null 2>&1 && git fetch origin && git reset --hard origin/${BRANCH}) || \
        (git clone ${GIT_URL} . && git checkout ${BRANCH} 2>/dev/null || true)" || { error "git 拉取失败"; return 1; }

    # 3. 选择 torch 版本（可经 AUTODL_TORCH_DEVICE 覆盖：gpu=完整 CUDA / cpu=CPU 版）
    TORCH_DEVICE="${AUTODL_TORCH_DEVICE:-gpu}"
    if declare -f select_torch_config >/dev/null 2>&1; then
        select_torch_config
    fi

    # 4. 远端 docker build
    info "AutoDL 上 docker build（TORCH_DEVICE=${TORCH_DEVICE}，首次约 10-20 分钟）..."
    SSH_CMD "cd ${SRC_DIR} && docker build \
        --build-arg TORCH_DEVICE=${TORCH_DEVICE} \
        -f docker/autodl/Dockerfile \
        -t ${IMAGE_NAME} ." || { error "远程 docker build 失败"; return 1; }

    ok "AutoDL 训练镜像 ${IMAGE_NAME} 构建完成"
    warn "验证：ssh ${USER}@${HOST} 'docker images | grep ${IMAGE_NAME%%:*}'"
}
