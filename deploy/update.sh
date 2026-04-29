#!/bin/bash
#===============================================================================
# QuantMind 一键更新脚本（Docker 版）
# 功能：
#   1. 从 Gitee 拉取最新代码
#   2. 重建后端容器（仅 quantmind/celery-worker）
#   3. 重建前端并重启 PM2
#
# 重要说明：
#   - 不会执行数据库初始化
#   - 不会删除数据库数据
#   - 不会重建 db/redis 容器
#===============================================================================

set -euo pipefail

PROJECT_DIR="/opt/quantmind"
REPO_URL="https://gitee.com/qusong0627/quantmind.git"
FORCE_SYNC=false
BACKEND_ONLY=false
FRONTEND_ONLY=false

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step() {
    echo -e "\n${BLUE}========================================${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}========================================${NC}\n"
}

usage() {
    cat <<'EOF'
用法:
  sudo bash deploy/update.sh [选项]

选项:
  --backend-only   仅更新后端容器
  --frontend-only  仅更新前端
  --force-sync     强制覆盖本地未提交修改后再拉取
  -h, --help       显示帮助
EOF
}

for arg in "$@"; do
    case "$arg" in
        --backend-only) BACKEND_ONLY=true ;;
        --frontend-only) FRONTEND_ONLY=true ;;
        --force-sync) FORCE_SYNC=true ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            log_error "未知参数: $arg"
            usage
            exit 1
            ;;
    esac
done

if $BACKEND_ONLY && $FRONTEND_ONLY; then
    log_error "--backend-only 和 --frontend-only 不能同时使用"
    exit 1
fi

if ! $BACKEND_ONLY && ! $FRONTEND_ONLY; then
    UPDATE_BACKEND=true
    UPDATE_FRONTEND=true
else
    UPDATE_BACKEND=$BACKEND_ONLY
    UPDATE_FRONTEND=$FRONTEND_ONLY
fi

check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "请使用 root 权限运行（sudo）"
        exit 1
    fi
}

check_project_dir() {
    if [[ ! -d "$PROJECT_DIR" ]]; then
        log_error "项目目录不存在: $PROJECT_DIR"
        exit 1
    fi
    if [[ ! -d "$PROJECT_DIR/.git" ]]; then
        log_error "不是有效 Git 仓库: $PROJECT_DIR"
        exit 1
    fi
}

check_cmd() {
    local cmd="$1"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        log_error "缺少命令: $cmd"
        exit 1
    fi
}

detect_compose_cmd() {
    if docker compose version >/dev/null 2>&1; then
        COMPOSE_CMD=(docker compose)
    elif command -v docker-compose >/dev/null 2>&1; then
        COMPOSE_CMD=(docker-compose)
    else
        log_error "未检测到 docker compose 或 docker-compose"
        exit 1
    fi
}

git_sync() {
    log_step "同步代码（Gitee）"
    cd "$PROJECT_DIR"

    local origin_url
    origin_url="$(git remote get-url origin 2>/dev/null || true)"
    if [[ "$origin_url" != "$REPO_URL" ]]; then
        log_warn "origin 当前地址: ${origin_url:-<空>}"
        log_warn "期望地址: $REPO_URL"
        log_info "自动修正 origin 为 Gitee 仓库"
        if git remote get-url origin >/dev/null 2>&1; then
            git remote set-url origin "$REPO_URL"
        else
            git remote add origin "$REPO_URL"
        fi
    fi

    if [[ -n "$(git status --porcelain)" ]]; then
        if $FORCE_SYNC; then
            log_warn "检测到本地修改，执行强制覆盖"
            git reset --hard HEAD
            git clean -fd
        else
            log_error "检测到未提交修改，已停止更新"
            log_info "如需强制覆盖，请追加参数: --force-sync"
            exit 1
        fi
    fi

    local branch
    branch="$(git rev-parse --abbrev-ref HEAD)"
    if [[ "$branch" == "HEAD" ]]; then
        branch="$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||' || echo "master")"
        log_warn "当前为 detached HEAD，改为更新分支: $branch"
        git checkout "$branch"
    fi

    git fetch origin "$branch"
    git pull --ff-only origin "$branch"
    log_info "代码同步完成（$branch）"
}

run_as_deploy_user() {
    local cmd="$1"
    local deploy_user="${SUDO_USER:-root}"

    if [[ "$EUID" -eq 0 && "$deploy_user" != "root" ]]; then
        su - "$deploy_user" -c "bash -lc 'cd \"$PROJECT_DIR\" && $cmd'"
    else
        bash -lc "cd \"$PROJECT_DIR\" && $cmd"
    fi
}

update_backend() {
    log_step "更新后端容器（不操作数据库）"
    cd "$PROJECT_DIR"

    "${COMPOSE_CMD[@]}" -f docker-compose.yml build quantmind celery-worker
    "${COMPOSE_CMD[@]}" -f docker-compose.yml up -d --no-deps --force-recreate quantmind celery-worker

    log_info "后端容器更新完成（db/redis 未重建）"
}

update_frontend() {
    log_step "更新前端"
    cd "$PROJECT_DIR"

    npm install
    npm run dashboard:build

    run_as_deploy_user "if pm2 describe quantmind-web >/dev/null 2>&1; then pm2 restart quantmind-web; else pm2 start npm --name quantmind-web -- run dashboard:preview; fi"
    run_as_deploy_user "pm2 save >/dev/null 2>&1 || true"

    log_info "前端更新完成"
}

wait_http_ok() {
    local url="$1"
    local max_retry="${2:-30}"
    local wait_sec="${3:-2}"
    local i=1

    while (( i <= max_retry )); do
        if curl -fsS "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep "$wait_sec"
        ((i++))
    done
    return 1
}

health_check() {
    log_step "健康检查"

    if $UPDATE_BACKEND; then
        if wait_http_ok "http://127.0.0.1:8000/health" 40 2; then
            log_info "后端健康检查通过: http://127.0.0.1:8000/health"
        else
            log_error "后端健康检查失败，请检查容器日志"
            exit 1
        fi
    fi

    if $UPDATE_FRONTEND; then
        if wait_http_ok "http://127.0.0.1:3000" 40 2; then
            log_info "前端健康检查通过: http://127.0.0.1:3000"
        else
            log_error "前端健康检查失败，请检查 PM2 日志"
            exit 1
        fi
    fi
}

main() {
    check_root
    check_project_dir
    check_cmd git
    check_cmd curl

    if $UPDATE_BACKEND; then
        check_cmd docker
        detect_compose_cmd
    fi
    if $UPDATE_FRONTEND; then
        check_cmd npm
        check_cmd pm2
    fi

    git_sync

    if $UPDATE_BACKEND; then
        update_backend
    fi
    if $UPDATE_FRONTEND; then
        update_frontend
    fi

    health_check

    log_step "更新完成"
    log_info "本次更新未执行任何数据库初始化或清库操作。"
}

main "$@"
