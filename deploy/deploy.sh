#!/bin/bash
#===============================================================================
# QuantMind 一键部署脚本 v4.0
# 适用于 Ubuntu 20.04/22.04/24.04
#
# 特性:
#   - 国内镜像加速 (Docker/Node.js/npm)
#   - 支持断点续传
#   - 支持独立部署后端/前端
#   - 自动健康检查
#
# 使用方式:
#   chmod +x deploy.sh
#   sudo ./deploy.sh                    # 完整部署
#   sudo ./deploy.sh --yes              # 自动确认，无需交互
#   sudo ./deploy.sh --backend-only     # 仅部署后端
#   sudo ./deploy.sh --frontend-only    # 仅部署前端
#   sudo ./deploy.sh --resume           # 从断点继续
#   sudo ./deploy.sh --reset            # 重置进度重新部署
#   sudo ./deploy.sh --force-sync       # 强制同步代码（覆盖本地修改）
#===============================================================================

set -euo pipefail

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# 配置变量
DEPLOY_DIR="/opt/quantmind"
PROJECT_DIR="${DEPLOY_DIR}/quantmind"
DATA_DIR="${PROJECT_DIR}/data"
REPO_URL="https://gitee.com/qusong0627/quantmind.git"
NODE_VERSION="20.19.0"
PROGRESS_FILE="/tmp/quantmind_deploy_progress"
DOCKER_DAEMON_FILE="/etc/docker/daemon.json"
DOCKER_DAEMON_BACKUP="/tmp/quantmind_docker_daemon_backup.json"
DOCKER_DAEMON_EXISTED_FLAG="/tmp/quantmind_docker_daemon_existed"

# Docker 镜像加速器列表（自动选择可用）
DOCKER_MIRRORS=(
    "https://docker.1ms.run"
    "https://docker.xuanyuan.live"
    "https://hub.rat.dev"
    "https://naw1faud2gpqbs.xuanyuan.run"
)

# npm 镜像加速器列表（自动选择最快）
NPM_MIRRORS=(
    "https://registry.npmmirror.com"
    "https://mirrors.cloud.tencent.com/npm"
)

# 解析参数
BACKEND_ONLY=false
FRONTEND_ONLY=false
AUTO_YES=false
RESUME=false
RESET=false
FORCE_SYNC=false

for arg in "$@"; do
    case $arg in
        --backend-only) BACKEND_ONLY=true ;;
        --frontend-only) FRONTEND_ONLY=true ;;
        --yes) AUTO_YES=true ;;
        --resume) RESUME=true ;;
        --reset) RESET=true ;;
        --force-sync) FORCE_SYNC=true ;;
        *)
            echo "错误: 未知参数: $arg" >&2
            echo "支持参数: --yes --backend-only --frontend-only --resume --reset --force-sync" >&2
            exit 1
            ;;
    esac
done

if $BACKEND_ONLY && $FRONTEND_ONLY; then
    echo "错误: --backend-only 和 --frontend-only 不能同时使用" >&2
    exit 1
fi

# 自动检测服务器IP
detect_server_ip() {
    local public_ip=$(curl -s --connect-timeout 3 ifconfig.me 2>/dev/null || curl -s --connect-timeout 3 icanhazip.com 2>/dev/null)
    if [[ -n "$public_ip" && "$public_ip" != "127.0.0.1" ]]; then
        echo "$public_ip"
        return
    fi
    local local_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    if [[ -n "$local_ip" && "$local_ip" != "127.0.0.1" ]]; then
        echo "$local_ip"
        return
    fi
    echo "localhost"
}

SERVER_IP=$(detect_server_ip)

# 日志函数
log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step() { echo -e "\n${BLUE}========================================${NC}\n${BLUE}  $1${NC}\n${BLUE}========================================${NC}\n"; }
log_done() { echo -e "${GREEN}✅ $1 完成${NC}\n"; }

# 进度管理
save_progress() { echo "$1" > $PROGRESS_FILE; }
get_progress() { [[ -f $PROGRESS_FILE ]] && cat $PROGRESS_FILE || echo "0"; }
reset_progress() { rm -f $PROGRESS_FILE; log_info "进度已重置"; }

# 检查 root 权限
check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "此脚本需要 root 权限运行"
        log_info "请使用: sudo ./deploy.sh"
        exit 1
    fi
}

# 检查系统版本
check_system() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        OS=$ID
        VER=$VERSION_ID
        log_info "检测到系统: $PRETTY_NAME"
    else
        log_error "无法检测系统版本"
        exit 1
    fi

    # 仅支持 Ubuntu 22.04 及以上版本
    if [[ "$OS" != "ubuntu" ]]; then
        log_error "不支持的系统: $OS"
        log_info "QuantMind 仅支持 Ubuntu 22.04 及以上版本"
        exit 1
    fi

    # 检查 Ubuntu 版本号
    MAJOR_VER=$(echo "$VER" | cut -d. -f1)
    if [[ -z "$MAJOR_VER" ]] || [[ "$MAJOR_VER" -lt 22 ]]; then
        log_error "Ubuntu 版本过低: $VER"
        log_info "QuantMind 仅支持 Ubuntu 22.04 及以上版本"
        log_info "推荐使用 Ubuntu 22.04 LTS 或 Ubuntu 24.04 LTS"
        exit 1
    fi

    log_info "系统版本检查通过: Ubuntu $VER"
}

# 测试 Docker 镜像加速器
test_docker_mirror() {
    local mirror=$1
    if curl -s --connect-timeout 5 "${mirror}/v2/" > /dev/null 2>&1; then
        return 0
    fi
    return 1
}

# 选择最佳 Docker 镜像
select_docker_mirror() {
    log_info "测试 Docker 镜像加速器..." >&2
    for mirror in "${DOCKER_MIRRORS[@]}"; do
        if test_docker_mirror "$mirror"; then
            log_info "选择镜像: $mirror" >&2
            echo "$mirror"
            return
        fi
    done
    log_warn "未找到可用镜像加速器，使用默认源" >&2
    echo ""
}

# 测试 npm 镜像加速器
test_npm_mirror() {
    local mirror=$1
    if curl -s --connect-timeout 5 "${mirror}/" > /dev/null 2>&1; then
        return 0
    fi
    return 1
}

# 选择最佳 npm 镜像
select_npm_mirror() {
    log_info "测试 npm 镜像加速器..." >&2
    for mirror in "${NPM_MIRRORS[@]}"; do
        log_info "测试: $mirror" >&2
        if test_npm_mirror "$mirror"; then
            log_info "选择镜像: $mirror" >&2
            echo "$mirror"
            return
        fi
    done
    log_warn "未找到可用镜像加速器，使用默认源" >&2
    echo "https://registry.npmjs.org"
}

backup_docker_daemon_config() {
    if [[ -f "$DOCKER_DAEMON_EXISTED_FLAG" ]]; then
        return
    fi

    if [[ -f "$DOCKER_DAEMON_FILE" ]]; then
        cp "$DOCKER_DAEMON_FILE" "$DOCKER_DAEMON_BACKUP"
        echo "1" > "$DOCKER_DAEMON_EXISTED_FLAG"
    else
        echo "0" > "$DOCKER_DAEMON_EXISTED_FLAG"
    fi
}

restore_docker_daemon_config() {
    [[ -f "$DOCKER_DAEMON_EXISTED_FLAG" ]] || return

    local existed
    existed="$(cat "$DOCKER_DAEMON_EXISTED_FLAG")"
    if [[ "$existed" == "1" && -f "$DOCKER_DAEMON_BACKUP" ]]; then
        cp "$DOCKER_DAEMON_BACKUP" "$DOCKER_DAEMON_FILE"
    else
        rm -f "$DOCKER_DAEMON_FILE"
    fi

    systemctl daemon-reload
    systemctl restart docker
}

configure_docker_mirror() {
    local mirror="$1"
    backup_docker_daemon_config
    mkdir -p "$(dirname "$DOCKER_DAEMON_FILE")"

    python3 - "$DOCKER_DAEMON_FILE" "$mirror" << 'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
mirror = sys.argv[2]

data = {}
if path.exists():
    raw = path.read_text(encoding="utf-8").strip()
    if raw:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            data = parsed

mirrors = data.get("registry-mirrors", [])
if not isinstance(mirrors, list):
    mirrors = []
mirrors = [m for m in mirrors if isinstance(m, str) and m]
if mirror not in mirrors:
    mirrors.insert(0, mirror)

data["registry-mirrors"] = mirrors
path.write_text(json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
PY

    systemctl daemon-reload
    systemctl restart docker
}

ensure_frontend_prereqs() {
    if [[ ! -d "$PROJECT_DIR/.git" ]]; then
        log_warn "未检测到代码目录，先执行代码拉取"
        step6_clone_code
    fi

    if ! command -v node &> /dev/null || [[ "$(node --version)" != "v${NODE_VERSION}" ]]; then
        log_warn "Node.js 未安装或版本不匹配，先安装 Node.js"
        step3_install_nodejs
    fi

    if ! command -v pm2 &> /dev/null; then
        log_warn "PM2 未安装，先安装 PM2"
        step4_install_pm2
    fi
}

#===============================================================================
# Step 1: 更新系统
#===============================================================================
step1_update_system() {
    log_step "Step 1: 更新系统依赖"

    log_info "更新 apt 源..."
    apt-get update -y

    log_info "安装基础工具..."
    apt-get install -y \
        curl wget git vim htop net-tools \
        ca-certificates gnupg lsb-release \
        software-properties-common build-essential \
        libssl-dev libffi-dev python3-dev python3-pip python3-venv \
        openssl

    log_done "Step 1"
    save_progress "1"
}

#===============================================================================
# Step 2: 安装 Docker
#===============================================================================
step2_install_docker() {
    log_step "Step 2: 安装 Docker"

    if command -v docker &> /dev/null; then
        log_warn "Docker 已安装: $(docker --version)"
    else
        log_info "安装 Docker..."

        # 优先使用 apt 安装（更稳定）
        apt-get update -y
        DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2

        systemctl start docker
        systemctl enable docker

        if [[ -n "$SUDO_USER" ]]; then
            usermod -aG docker $SUDO_USER
            log_info "已将用户 $SUDO_USER 加入 docker 组"
        fi

        log_info "Docker 安装完成: $(docker --version)"
    fi

    # 配置 Docker 镜像加速器
    local mirror
    mirror=$(select_docker_mirror)
    if [[ -n "$mirror" ]]; then
        log_info "配置 Docker 镜像加速器: $mirror"
        if configure_docker_mirror "$mirror"; then
            log_info "Docker 镜像加速器配置成功"
        else
            log_warn "Docker 镜像加速器配置失败，恢复原配置"
            restore_docker_daemon_config
        fi
        sleep 3
    fi

    docker compose version &> /dev/null && log_info "Docker Compose: $(docker compose version)"

    log_done "Step 2"
    save_progress "2"
}

#===============================================================================
# Step 3: 安装 Node.js
#===============================================================================
step3_install_nodejs() {
    log_step "Step 3: 安装 Node.js"

    if command -v node &> /dev/null && [[ "$(node --version)" == "v${NODE_VERSION}" ]]; then
        log_warn "Node.js 已安装: $(node --version)"
    else
        log_info "安装 Node.js ${NODE_VERSION}..."

        apt-get remove -y nodejs 2>/dev/null || true

        NODE_URL="https://npmmirror.com/mirrors/node/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz"
        log_info "下载: $NODE_URL"
        curl -fsSL $NODE_URL -o /tmp/node.tar.xz

        mkdir -p /usr/local/nodejs
        tar -xJf /tmp/node.tar.xz -C /usr/local/nodejs --strip-components=1

        ln -sf /usr/local/nodejs/bin/node /usr/local/bin/node
        ln -sf /usr/local/nodejs/bin/npm /usr/local/bin/npm
        ln -sf /usr/local/nodejs/bin/npx /usr/local/bin/npx

        # 选择最佳 npm 镜像
        local npm_mirror=$(select_npm_mirror)
        npm config set registry "$npm_mirror"

        log_info "Node.js: $(node --version)"
        log_info "npm: $(npm --version)"
        log_info "npm 镜像: $(npm config get registry)"
    fi

    log_done "Step 3"
    save_progress "3"
}

#===============================================================================
# Step 4: 安装 PM2
#===============================================================================
step4_install_pm2() {
    log_step "Step 4: 安装 PM2"

    # 确保 npm registry 已配置（强制设置）
    local npm_mirror=$(select_npm_mirror)
    npm config set registry "$npm_mirror"
    log_info "npm 镜像: $(npm config get registry)"

    if command -v pm2 &> /dev/null; then
        log_warn "PM2 已安装: $(pm2 --version)"
    else
        log_info "安装 PM2..."
        npm install -g pm2

        ln -sf /usr/local/nodejs/bin/pm2 /usr/local/bin/pm2
        ln -sf /usr/local/nodejs/bin/pm2-runtime /usr/local/bin/pm2-runtime

        log_info "PM2: $(pm2 --version)"
    fi

    log_done "Step 4"
    save_progress "4"
}

#===============================================================================
# Step 5: 安装 Nginx
#===============================================================================
step5_install_nginx() {
    log_step "Step 5: 安装 Nginx"

    if command -v nginx &> /dev/null; then
        log_warn "Nginx 已安装"
    else
        log_info "安装 Nginx..."
        apt-get install -y nginx
        cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.bak
        log_info "Nginx 安装完成"
    fi

    log_done "Step 5"
    save_progress "5"
}

#===============================================================================
# Step 6: 克隆代码
#===============================================================================
step6_clone_code() {
    log_step "Step 6: 克隆代码"

    mkdir -p "$DEPLOY_DIR"

    if [[ -d "$PROJECT_DIR/.git" ]]; then
        log_warn "代码目录已存在，执行更新..."
        cd "$PROJECT_DIR"
        if [[ -n "$(git status --porcelain)" && "$FORCE_SYNC" != "true" ]]; then
            log_error "检测到本地未提交变更，已停止自动更新以避免覆盖"
            log_info "如需强制同步远端，请使用: sudo ./deploy.sh --force-sync"
            exit 1
        fi
        git fetch origin
        if [[ "$FORCE_SYNC" == "true" ]]; then
            git reset --hard origin/master
        else
            git pull --ff-only origin master
        fi
    else
        log_info "从 Gitee 克隆代码..."
        cd "$DEPLOY_DIR"
        git clone $REPO_URL quantmind
        cd quantmind
    fi

    chown -R "${SUDO_USER:-root}:${SUDO_USER:-root}" "$PROJECT_DIR"

    log_info "目录: $(pwd)"
    log_info "分支: $(git branch --show-current)"
    log_info "提交: $(git log -1 --oneline)"

    log_done "Step 6"
    save_progress "6"
}

#===============================================================================
# Step 7: 配置环境变量
#===============================================================================
step7_config_environment() {
    log_step "Step 7: 配置环境变量"

    cd "$PROJECT_DIR"

    if [[ -f ".env" ]]; then
        log_warn ".env 文件已存在，跳过创建"
    else
        log_info "创建 .env 配置文件..."

        # 单机部署使用固定 secret，简化配置
        cat > .env << EOF
# QuantMind OSS Edition 配置

APP_EDITION=oss
APP_ENV=production
TZ=Asia/Shanghai

# 固定密钥（单机部署简化配置）
SECRET_KEY=quantmind-oss-secret-key-2026-production
JWT_SECRET_KEY=quantmind-oss-jwt-secret-key-2026-production

DB_HOST=db
DB_PORT=5432
DB_NAME=quantmind
DB_USER=quantmind
DB_PASSWORD=quantmind2026

REDIS_HOST=redis
REDIS_PORT=6379

STORAGE_MODE=local
STORAGE_ROOT=${DATA_DIR}

DEBUG=false
LOG_LEVEL=INFO
EOF

        log_info ".env 文件创建完成"
    fi

    mkdir -p "$DATA_DIR"/{postgres,redis,logs,models,backtest_results,feature_snapshots}
    mkdir -p "$PROJECT_DIR/db/feature_snapshots"
    log_info "数据目录: $DATA_DIR"

    log_done "Step 7"
    save_progress "7"
}

#===============================================================================
# Step 8: 构建 Docker 镜像
#===============================================================================
step8_build_docker() {
    log_step "Step 8: 构建 Docker 镜像"

    cd "$PROJECT_DIR"

    log_info "构建 QuantMind OSS 镜像 (5-10分钟)..."

    # 尝试使用不同镜像源构建
    local build_success=false
    for mirror in "${DOCKER_MIRRORS[@]}"; do
        if [[ -n "$mirror" ]]; then
            log_info "尝试使用镜像源: $mirror"
            if ! configure_docker_mirror "$mirror"; then
                log_warn "镜像源 $mirror 配置失败，尝试下一个..."
                continue
            fi
            sleep 3

            if docker build -t quantmind-oss:latest -f docker/Dockerfile.oss . 2>&1; then
                build_success=true
                log_info "构建成功，使用镜像源: $mirror"
                break
            else
                log_warn "镜像源 $mirror 构建失败，尝试下一个..."
            fi
        fi
    done

    # 如果所有镜像都失败，尝试不使用镜像加速
    if ! $build_success; then
        log_warn "所有镜像加速器均失败，尝试直接拉取..."
        restore_docker_daemon_config
        sleep 3

        if docker build -t quantmind-oss:latest -f docker/Dockerfile.oss . 2>&1; then
            build_success=true
        fi
    fi

    if ! $build_success; then
        log_error "Docker 镜像构建失败"
        exit 1
    fi

    docker images | grep quantmind-oss

    log_done "Step 8"
    save_progress "8"
}

#===============================================================================
# Step 9: 启动后端服务
#===============================================================================
step9_start_database() {
    log_step "Step 9: 启动数据库服务"

    cd "$PROJECT_DIR"

    # 确保数据目录存在
    mkdir -p "$DATA_DIR/postgres" "$DATA_DIR/redis"

    # 修复数据目录权限
    # postgres:15-alpine 使用 UID=70, redis:7-alpine 使用 UID=999
    # 必须在启动容器前设置，否则服务无法访问数据文件
    chown -R 70:70 "$DATA_DIR/postgres"
    chmod 700 "$DATA_DIR/postgres"
    chown -R 999:999 "$DATA_DIR/redis"

    log_info "启动数据库和 Redis..."
    docker compose up -d db redis

    log_info "等待数据库就绪 (15秒)..."
    sleep 15

    # 验证数据库是否正常连接
    if ! docker exec quantmind-db psql -U quantmind -d quantmind -c "SELECT 1" > /dev/null 2>&1; then
        log_warn "数据库连接失败，检查权限..."
        docker compose down
        chown -R 70:70 "$DATA_DIR/postgres"
        chmod 700 "$DATA_DIR/postgres"
        chown -R 999:999 "$DATA_DIR/redis"
        docker compose up -d db redis
        sleep 10
    fi

    docker compose ps db redis

    log_done "Step 9"
    save_progress "9"
}

#===============================================================================
# Step 10: 初始化数据库
#===============================================================================
step10_init_database() {
    log_step "Step 10: 初始化数据库"

    cd "$PROJECT_DIR"

    log_info "等待数据库就绪..."
    sleep 5

    if [[ -f "data/quantmind_init.sql" ]]; then
        log_info "初始化数据库..."
        if docker exec -i quantmind-db psql -U quantmind -d quantmind < data/quantmind_init.sql 2>&1; then
            log_info "数据库初始化完成"
        else
            log_warn "数据库初始化可能失败，检查日志"
        fi
    else
        log_warn "未找到初始化 SQL: data/quantmind_init.sql"
    fi

    # 创建默认管理员用户（如果不存在）
    log_info "创建默认管理员用户..."
    docker exec quantmind-db psql -U quantmind -d quantmind -c "
    INSERT INTO users (id, user_id, tenant_id, username, email, password_hash, is_active, is_admin, is_verified, is_locked, login_count, created_at, updated_at, is_deleted)
    SELECT 1, 'admin', 'default', 'admin', 'admin@quantmind.local',
           '\$2b\$12\$B/yjK9cT.wx4BlB9j.r/t.dADjCbmutIXoDM7PdKZmV6ypuYiiUvW',
           true, true, true, false, 37, now(), now(), false
    WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'admin' AND tenant_id = 'default');
    " 2>/dev/null || log_warn "管理员用户可能已存在"

    log_done "Step 10"
    save_progress "10"
}

#===============================================================================
# Step 11: 启动后端服务
#===============================================================================
step11_start_backend() {
    log_step "Step 11: 启动后端服务"

    cd "$PROJECT_DIR"

    # 再次修复权限（Docker 重启后可能需要）
    chown -R 999:999 "$DATA_DIR/postgres" "$DATA_DIR/redis"

    log_info "启动后端容器..."
    docker compose up -d quantmind celery-worker

    log_info "等待后端启动 (20秒)..."
    sleep 20

    docker compose ps

    log_done "Step 11"
    save_progress "11"
}

#===============================================================================
# Step 12: 安装前端依赖
#===============================================================================
step12_install_frontend() {
    log_step "Step 12: 安装前端依赖"

    cd "$PROJECT_DIR"

    chown -R "${SUDO_USER:-root}:${SUDO_USER:-root}" .

    # 选择最佳 npm 镜像
    local npm_mirror=$(select_npm_mirror)

    # 配置 npm 镜像加速（包括 Electron、Puppeteer 等）
    log_info "配置 npm 镜像加速..."
    NPMRC_FILE="/home/${SUDO_USER:-root}/.npmrc"
    if [[ ! -f "$NPMRC_FILE" ]]; then
        NPMRC_FILE="/root/.npmrc"
    fi

    # 写入完整配置（覆盖旧配置避免重复）
    cat > "$NPMRC_FILE" << EOF
registry=${npm_mirror}
# Electron 二进制镜像
electron_mirror=https://npmmirror.com/mirrors/electron/
electron_builder_binaries_mirror=https://npmmirror.com/mirrors/electron-builder-binaries/
# Puppeteer Chromium 镜像
puppeteer_download_host=https://npmmirror.com/mirrors
# Sass 二进制镜像
sass_binary_site=https://npmmirror.com/mirrors/node-sass/
# Python 镜像（node-gyp 使用）
python_mirror=https://npmmirror.com/mirrors/python/
EOF

    log_info "npm 镜像配置完成: $npm_mirror"
    log_info "安装 npm 依赖 (3-5分钟)..."
    # 添加 --loglevel=verbose 显示进度
    sudo -u "${SUDO_USER:-root}" npm install --loglevel=verbose

    log_done "Step 12"
    save_progress "12"
}

#===============================================================================
# Step 13: 构建前端
#===============================================================================
step13_build_frontend() {
    log_step "Step 13: 构建前端"

    cd "$PROJECT_DIR"

    # 确保 build 目录存在（Electron 构建需要 icon.ico）
    mkdir -p electron/build
    if [[ ! -f electron/build/icon.ico ]]; then
        cp electron/public/favicon.ico electron/build/icon.ico 2>/dev/null || true
    fi

    log_info "构建生产版本..."
    sudo -u "${SUDO_USER:-root}" env VITE_API_BASE_URL="" npm run dashboard:build

    ls -la electron/dist-react/ 2>/dev/null | head -5 || log_warn "前端构建目录不存在"

    log_done "Step 13"
    save_progress "13"
}

#===============================================================================
# Step 14: 启动前端服务
#===============================================================================
step14_start_frontend() {
    log_step "Step 14: 启动前端服务"

    cd "$PROJECT_DIR"

    log_info "停止旧服务..."
    pm2 delete quantmind-web 2>/dev/null || true

    log_info "启动新服务..."
    pm2 start npm --name "quantmind-web" -- run dashboard:preview

    pm2 save
    local target_user="${SUDO_USER:-root}"
    local target_home
    target_home="$(getent passwd "$target_user" | cut -d: -f6)"
    pm2 startup systemd -u "$target_user" --hp "${target_home:-/root}" 2>/dev/null || true

    pm2 status

    log_done "Step 14"
    save_progress "14"
}

#===============================================================================
# Step 15: 配置 Nginx
#===============================================================================
step15_config_nginx() {
    log_step "Step 15: 配置 Nginx"

    # 创建 uploads 目录
    mkdir -p "$PROJECT_DIR/data/uploads"
    chown -R "$(id -u "${SUDO_USER:-root}")":"$(id -g "${SUDO_USER:-root}")" "$PROJECT_DIR/data/uploads"

    log_info "创建 Nginx 配置..."
    cat > /etc/nginx/sites-available/quantmind << 'EOF'
server {
    listen 80;
    server_name _;

    client_max_body_size 100M;

    # 静态文件 (uploads)
    location /uploads/ {
        alias /opt/quantmind/quantmind/data/uploads/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    # 前端
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }

    # 后端 API
    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }

    # WebSocket
    location /ws/ {
        proxy_pass http://127.0.0.1:8003/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 86400;
    }
}
EOF

    ln -sf /etc/nginx/sites-available/quantmind /etc/nginx/sites-enabled/
    rm -f /etc/nginx/sites-enabled/default

    nginx -t && systemctl restart nginx && systemctl enable nginx

    log_done "Step 15"
    save_progress "15"
}

#===============================================================================
# Step 16: 健康检查
#===============================================================================
step16_health_check() {
    log_step "Step 16: 健康检查"

    log_info "Docker 容器状态:"
    docker compose -f "$PROJECT_DIR/docker-compose.yml" ps

    echo ""
    log_info "PM2 服务状态:"
    pm2 status

    echo ""
    log_info "端口监听:"
    ss -tlnp | grep -E ':(80|3000|8000|8001|8002|8003|5432|6379)' || true

    echo ""
    log_info "服务测试:"
    curl -s http://localhost:8000/health > /dev/null && log_info "后端 API: ✅" || log_warn "后端 API: ❌"
    curl -s http://localhost:3000 > /dev/null && log_info "前端服务: ✅" || log_warn "前端服务: ❌"
    curl -s http://localhost > /dev/null && log_info "Nginx: ✅" || log_warn "Nginx: ❌"

    log_done "Step 16"
    save_progress "16"
}

#===============================================================================
# Step 17: 配置防火墙
#===============================================================================
step17_firewall() {
    log_step "Step 17: 配置防火墙"

    if command -v ufw &> /dev/null; then
        ufw allow 22/tcp comment 'SSH'
        ufw allow 80/tcp comment 'HTTP'
        ufw allow 443/tcp comment 'HTTPS'
        ufw --force enable
        ufw status
    else
        log_warn "UFW 未安装，跳过防火墙配置"
    fi

    log_done "Step 17"
    save_progress "17"
}

#===============================================================================
# 完成信息
#===============================================================================
show_info() {
    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║                    🎉 QuantMInd 部署成功！                       ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${YELLOW}📌 访问入口${NC}"
    echo -e "   前端应用:  ${BLUE}http://${SERVER_IP}${NC}"
    echo -e "   API 文档:  ${BLUE}http://${SERVER_IP}:8000/docs${NC}"
    echo ""
    echo -e "${YELLOW}🔑 默认管理员账号${NC}"
    echo -e "   用户名: ${GREEN}admin${NC}"
    echo -e "   密码:   ${GREEN}admin123${NC}"
    echo ""
    echo -e "${YELLOW}📋 常用命令${NC}"
    echo -e "   查看后端日志: docker compose -f $PROJECT_DIR/docker-compose.yml logs -f"
    echo -e "   查看前端日志: pm2 logs quantmind-web"
    echo -e "   重启后端服务: docker compose -f $PROJECT_DIR/docker-compose.yml restart"
    echo -e "   重启前端服务: pm2 restart quantmind-web"
    echo ""
    echo -e "${YELLOW}📁 目录信息${NC}"
    echo -e "   部署目录: $PROJECT_DIR"
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo -e "${YELLOW}💡 温馨提示${NC}"
    echo -e "   如需使用完整回测、模型训练及推理服务，请前往 GitHub 主页下载完整数据包："
    echo -e "   ${BLUE}https://github.com/anthropics/quantmind/releases${NC}"
    echo ""
}

#===============================================================================
# 欢迎信息
#===============================================================================
show_welcome() {
    # 仅在交互式终端清屏
    if [ -t 1 ]; then
        clear
    fi
    echo -e "${BLUE}"
    echo "╔══════════════════════════════════════════════════════════════════╗"
    echo "║   QQQQ   U   U   AAAAA   N   N  TTTTT  M   M  III  N   N  DDDD   ║"
    echo "║   Q   Q  U   U  A     A  NN  N   T    MM MM   I   NN  N  D   D   ║"
    echo "║   Q   Q  U   U  AAAAAAA  N N N   T    M M M   I   N N N  D   D   ║"
    echo "║   Q  QQ  U   U  A     A  N  NN   T    M   M   I   N  NN  D   D   ║"
    echo "║   QQQ Q   UUU   A     A  N   N   T    M   M  III  N   N  DDDD    ║"
    echo "║                                                                  ║"
    echo "║                                                                  ║"
    echo "║                                                                  ║"
    echo "║                    QuantMInd 量化交易平台                        ║"
    echo "║                        OSS 开源版 v1.0                           ║"
    echo "╚══════════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
    echo ""
    echo -e "${GREEN}🎉 Welcome! 欢迎部署 QuantMInd 开源量化交易平台！${NC}"
    echo ""
    echo -e "${YELLOW}📌 项目简介${NC}"
    echo "   QuantMInd 是一款面向个人投资者的量化交易研究与回测平台。"
    echo "   支持策略编写、历史回测、参数优化、实盘交易等核心功能。"
    echo ""
    echo -e "${YELLOW}✨ 核心功能${NC}"
    echo "   • 策略回测 - 基于 Qlib 引擎的高性能回测"
    echo "   • 参数优化 - 网格搜索与遗传算法优化"
    echo "   • AI 策略 - 大模型辅助策略生成"
    echo "   • 实盘交易 - 支持模拟盘与实盘对接"
    echo ""
    echo -e "${YELLOW}⏱️ 温馨提示${NC}"
    echo "   完整部署预计需要 10-30 分钟，具体时间取决于服务器配置和网络状况。"
    echo "   部署过程中请保持网络连接稳定，请勿关闭终端窗口。"
    echo ""
    echo -e "${YELLOW}📋 部署内容${NC}"
    echo "   • Docker & Docker Compose"
    echo "   • PostgreSQL 数据库"
    echo "   • Redis 缓存"
    echo "   • 后端 API 服务 (4个微服务)"
    echo "   • 前端 Web 应用"
    echo "   • Nginx 反向代理"
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
}

# 确认部署
confirm_deploy() {
    # 自动确认模式
    if $AUTO_YES; then
        return 0
    fi

    # 检查是否为交互式终端
    if [[ ! -t 0 ]]; then
        log_error "检测到非交互式终端，无法执行人工确认"
        log_info "如需继续，请显式使用: sudo ./deploy.sh --yes"
        exit 1
    fi

    echo -e -n "是否继续部署？ [y/N]: "
    read -r response
    case "$response" in
        [yY][eE][sS]|[yY])
            return 0
            ;;
        *)
            echo ""
            log_info "部署已取消。如需帮助请访问: https://github.com/anthropics/quantmind"
            exit 0
            ;;
    esac
}

#===============================================================================
# 主函数
#===============================================================================
main() {
    show_welcome
    confirm_deploy

    check_root
    check_system

    # 处理部署进度
    CURRENT_STEP=$(get_progress)
    if ! [[ "$CURRENT_STEP" =~ ^[0-9]+$ ]]; then
        log_warn "检测到非法进度值: $CURRENT_STEP，已重置为 0"
        CURRENT_STEP=0
    fi
    if $RESET; then
        reset_progress
        CURRENT_STEP=0
    elif [[ "$CURRENT_STEP" != "0" ]]; then
        if $RESUME; then
            log_info "从 Step $CURRENT_STEP 继续部署"
        else
            log_warn "检测到历史部署进度: Step $CURRENT_STEP"
            log_info "默认继续执行。使用 --reset 可从头部署，使用 --resume 可显式继续。"
        fi
    fi

    # 根据部署模式执行步骤
    if $FRONTEND_ONLY; then
        log_info "仅部署前端..."
        ensure_frontend_prereqs
        step12_install_frontend
        step13_build_frontend
        step14_start_frontend
        step15_config_nginx
    elif $BACKEND_ONLY; then
        log_info "仅部署后端..."
        [[ $CURRENT_STEP -lt 1 ]] && step1_update_system
        [[ $CURRENT_STEP -lt 2 ]] && step2_install_docker
        [[ $CURRENT_STEP -lt 6 ]] && step6_clone_code
        [[ $CURRENT_STEP -lt 7 ]] && step7_config_environment
        [[ $CURRENT_STEP -lt 8 ]] && step8_build_docker
        [[ $CURRENT_STEP -lt 9 ]] && step9_start_database
        [[ $CURRENT_STEP -lt 10 ]] && step10_init_database
        [[ $CURRENT_STEP -lt 11 ]] && step11_start_backend
    else
        # 完整部署
        [[ $CURRENT_STEP -lt 1 ]] && step1_update_system
        [[ $CURRENT_STEP -lt 2 ]] && step2_install_docker
        [[ $CURRENT_STEP -lt 3 ]] && step3_install_nodejs
        [[ $CURRENT_STEP -lt 4 ]] && step4_install_pm2
        [[ $CURRENT_STEP -lt 5 ]] && step5_install_nginx
        [[ $CURRENT_STEP -lt 6 ]] && step6_clone_code
        [[ $CURRENT_STEP -lt 7 ]] && step7_config_environment
        [[ $CURRENT_STEP -lt 8 ]] && step8_build_docker
        [[ $CURRENT_STEP -lt 9 ]] && step9_start_database
        [[ $CURRENT_STEP -lt 10 ]] && step10_init_database
        [[ $CURRENT_STEP -lt 11 ]] && step11_start_backend
        [[ $CURRENT_STEP -lt 12 ]] && step12_install_frontend
        [[ $CURRENT_STEP -lt 13 ]] && step13_build_frontend
        [[ $CURRENT_STEP -lt 14 ]] && step14_start_frontend
        [[ $CURRENT_STEP -lt 15 ]] && step15_config_nginx
        [[ $CURRENT_STEP -lt 16 ]] && step16_health_check
        [[ $CURRENT_STEP -lt 17 ]] && step17_firewall
    fi

    show_info
    rm -f "$PROGRESS_FILE" "$DOCKER_DAEMON_BACKUP" "$DOCKER_DAEMON_EXISTED_FLAG"
}

main "$@"
