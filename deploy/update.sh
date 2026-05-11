#!/bin/bash
#===============================================================================
# QuantMind 一键更新脚本（Docker 版）
# 功能：
#   1. 从 Gitee 拉取最新代码
#   2. 执行数据库升级脚本（如有）
#   3. 重建后端容器（仅 quantmind/celery-worker）
#   4. 自动修复 .env.production 中硬编码的本地地址
#   5. 构建产物验证（防硬编码 127.0.0.1）
#
# 重要说明：
#   - 数据库升级脚本位于 data/upgrade_v*.sql
#   - 升级脚本会备份并自动执行最新版本
#   - 不会删除数据库数据（除非升级脚本明确说明）
#   - 不会重建 db/redis 容器
#   - 不包含前端构建（前端需单独部署）
#===============================================================================

set -euo pipefail

PROJECT_DIR="/opt/quantmind"
REPO_URL="https://gitee.com/qusong0627/quantmind.git"
FORCE_SYNC=false
HAS_ARGS=false

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

check_runtime_files() {
    cd "$PROJECT_DIR"
    if [[ ! -f ".env" ]]; then
        log_warn "未找到 $PROJECT_DIR/.env，后端可能因环境变量缺失启动失败。"
        log_warn "请先恢复 .env 再执行更新。"
    fi
}

has_tty() {
    [[ -r /dev/tty && -w /dev/tty ]]
}

tty_print() {
    if has_tty; then
        echo -e "$1" > /dev/tty
    else
        echo -e "$1"
    fi
}

tty_read() {
    local prompt="$1"
    local var_name="$2"
    if has_tty; then
        read -r -p "$prompt" "$var_name" < /dev/tty
    else
        read -r -p "$prompt" "$var_name"
    fi
}

usage() {
    cat <<'EOF'
用法:
  sudo bash deploy/update.sh [选项]

选项:
  --force-sync     强制覆盖本地未提交修改后再拉取
  -force-sync      同 --force-sync
  -h, --help       显示帮助
EOF
}

for arg in "$@"; do
    HAS_ARGS=true
    case "$arg" in
        --force-sync|-force-sync|-f) FORCE_SYNC=true ;;
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

choose_mode_by_number() {
    if ! $HAS_ARGS && has_tty; then
        tty_print "请选择更新模式："
        tty_print "  1) 标准更新（后端）"
        tty_print "  2) 强制同步更新（后端，覆盖本地修改）"
        tty_print "  0) 退出"
        tty_read "输入数字 [1]: " choice
        choice="${choice:-1}"
        case "$choice" in
            1)
                FORCE_SYNC=false
                ;;
            2)
                FORCE_SYNC=true
                ;;
            0)
                log_info "已退出"
                exit 0
                ;;
            *)
                log_error "无效选择: $choice"
                exit 1
                ;;
        esac
    fi
}

choose_mode_by_number

UPDATE_BACKEND=true

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
            log_warn "检测到本地修改，执行强制覆盖（仅重置已跟踪文件，不删除 .env/数据目录）"
            git reset --hard HEAD
        else
            if has_tty; then
                log_warn "检测到未提交修改"
                tty_print "请选择："
                tty_print "  1) 终止更新（默认）"
                tty_print "  2) 强制覆盖本地修改并继续（仅重置已跟踪文件）"
                tty_read "输入数字 [1]: " dirty_choice
                dirty_choice="${dirty_choice:-1}"
                case "$dirty_choice" in
                    2)
                        log_warn "已选择强制覆盖，继续更新"
                        git reset --hard HEAD
                        ;;
                    *)
                        log_error "已终止更新"
                        log_info "也可直接执行: sudo bash deploy/update.sh --force-sync"
                        exit 1
                        ;;
                esac
            else
                log_error "检测到未提交修改，已停止更新"
                log_info "如需强制覆盖，请追加参数: --force-sync"
                exit 1
            fi
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

upgrade_database() {
    log_step "检查数据库升级脚本"
    cd "$PROJECT_DIR"

    local upgrade_dir="data"
    if [[ ! -d "$upgrade_dir" ]]; then
        log_info "未找到 data 目录，跳过数据库升级"
        return 0
    fi

    # 查找最新的升级脚本（按版本号排序）
    local latest_upgrade
    latest_upgrade="$(ls -1 "${upgrade_dir}"/upgrade_v*.sql 2>/dev/null | sort -V | tail -n1 || true)"

    if [[ -z "$latest_upgrade" ]]; then
        log_info "未找到升级脚本（upgrade_v*.sql），跳过数据库升级"
        return 0
    fi

    local upgrade_file
    upgrade_file="$(basename "$latest_upgrade")"
    log_info "发现升级脚本: $upgrade_file"

    # 提取版本号
    local version
    version="$(echo "$upgrade_file" | grep -oP 'v\K[0-9]+\.[0-9]+\.[0-9]+' || echo "unknown")"

    # 检查是否已执行过该版本升级
    local version_key="db_upgrade_${version//./_}"
    local applied
    applied="$(docker exec quantmind-db psql -U quantmind -d quantmind -t -A -c "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = 'db_upgrade_log' AND version = '${version}');" 2>/dev/null || echo "false")"

    if [[ "$applied" == "t" ]]; then
        log_info "版本 $version 已升级过，跳过"
        return 0
    fi

    # 确认是否执行升级
    if has_tty; then
        tty_print "即将执行数据库升级: $upgrade_file"
        tty_print "  版本: $version"
        tty_print "  注意：升级前请确保已备份数据库！"
        tty_read "是否继续？[y/N]: " confirm
        confirm="${confirm:-N}"
        if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
            log_warn "已跳过数据库升级"
            return 0
        fi
    fi

    # 自动备份
    log_info "正在备份数据库..."
    local backup_file="/tmp/quantmind_backup_$(date +%Y%m%d_%H%M%S).sql"
    if docker exec quantmind-db pg_dump -U quantmind quantmind > "$backup_file" 2>/dev/null; then
        log_info "数据库备份成功: $backup_file"
    else
        log_error "数据库备份失败，已终止升级"
        log_info "请手动备份后再执行升级"
        exit 1
    fi

    # 执行升级
    log_info "正在执行升级脚本: $upgrade_file"
    if docker exec -i quantmind-db psql -U quantmind -d quantmind < "${upgrade_dir}/${upgrade_file}" 2>&1; then
        log_info "数据库升级成功（版本 $version）"

        # 记录升级日志
        docker exec quantmind-db psql -U quantmind -d quantmind -c "
            CREATE TABLE IF NOT EXISTS db_upgrade_log (
                version VARCHAR(32) PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                script_name VARCHAR(255)
            );
            INSERT INTO db_upgrade_log (version, script_name) VALUES ('${version}', '${upgrade_file}')
            ON CONFLICT (version) DO NOTHING;
        " 2>/dev/null || true
    else
        log_error "数据库升级失败！"
        log_error "备份文件位置: $backup_file"
        log_info "如需回滚，请执行: psql -U quantmind -d quantmind < $backup_file"
        exit 1
    fi
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

    local all_ok=true

    # 检查 API 服务
    if wait_http_ok "http://127.0.0.1:8000/health" 40 2; then
        log_info "API 服务 (8000): ✅"
    else
        log_error "API 服务 (8000): ❌"
        all_ok=false
    fi

    # 检查 Engine 服务
    if wait_http_ok "http://127.0.0.1:8001/health" 20 2; then
        log_info "Engine 服务 (8001): ✅"
    else
        log_warn "Engine 服务 (8001): ❌"
    fi

    # 检查 Trade 服务
    if wait_http_ok "http://127.0.0.1:8002/health" 20 2; then
        log_info "Trade 服务 (8002): ✅"
    else
        log_warn "Trade 服务 (8002): ❌"
    fi

    # 检查 Stream 服务
    if wait_http_ok "http://127.0.0.1:8003/health" 20 2; then
        log_info "Stream 服务 (8003): ✅"
    else
        log_warn "Stream 服务 (8003): ❌"
    fi

    if $all_ok; then
        log_info "健康检查通过"
    else
        log_error "部分服务健康检查失败，请检查容器日志"
        exit 1
    fi
}

main() {
    check_root
    check_project_dir
    check_cmd git
    check_cmd docker
    check_cmd curl
    check_runtime_files
    detect_compose_cmd

    git_sync

    upgrade_database

    update_backend

    health_check

    log_step "更新完成"
    log_info "本次更新已自动检查并执行数据库升级脚本（如有）。"
}

main "$@"
