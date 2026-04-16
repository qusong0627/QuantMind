#!/bin/bash
#===============================================================================
# QuantMind 一键部署脚本
# 适用于 Ubuntu 20.04/22.04/24.04
# 
# 使用方式:
#   chmod +x deploy.sh
#   sudo ./deploy.sh
#===============================================================================

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 配置变量
DEPLOY_DIR="/opt/quantmind"
DATA_DIR="/opt/quantmind/data"
REPO_URL="https://gitee.com/qusong0627/quantmind.git"
SERVER_IP="139.199.75.121"

# 日志函数
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_step() {
    echo -e "\n${BLUE}========================================${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}========================================${NC}\n"
}

# 检查是否为 root 用户
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
    
    if [[ "$OS" != "ubuntu" && "$OS" != "debian" ]]; then
        log_warn "此脚本主要针对 Ubuntu/Debian 系统，其他系统可能需要调整"
    fi
}

#===============================================================================
# 第一阶段：系统准备
#===============================================================================

step1_update_system() {
    log_step "第一阶段：更新系统依赖"
    
    log_info "更新 apt 源..."
    apt-get update -y
    
    log_info "升级系统包..."
    apt-get upgrade -y
    
    log_info "安装基础工具..."
    apt-get install -y \
        curl \
        wget \
        git \
        vim \
        htop \
        net-tools \
        ca-certificates \
        gnupg \
        lsb-release \
        software-properties-common \
        build-essential \
        libssl-dev \
        libffi-dev \
        python3-dev \
        python3-pip \
        python3-venv
    
    log_info "系统依赖安装完成"
}

step2_install_docker() {
    log_step "安装 Docker & Docker Compose"
    
    # 检查是否已安装
    if command -v docker &> /dev/null; then
        log_warn "Docker 已安装，跳过..."
        docker --version
    else
        log_info "安装 Docker..."
        
        # 添加 Docker 官方 GPG 密钥
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg
        
        # 添加 Docker 仓库
        echo \
          "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
          $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
          tee /etc/apt/sources.list.d/docker.list > /dev/null
        
        # 安装 Docker
        apt-get update -y
        apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        
        # 启动 Docker
        systemctl start docker
        systemctl enable docker
        
        # 将当前用户加入 docker 组（非 root 用户）
        if [[ -n "$SUDO_USER" ]]; then
            usermod -aG docker $SUDO_USER
            log_info "已将用户 $SUDO_USER 加入 docker 组"
        fi
        
        log_info "Docker 安装完成"
        docker --version
    fi
    
    # Docker Compose
    if command -v docker-compose &> /dev/null || docker compose version &> /dev/null; then
        log_warn "Docker Compose 已安装，跳过..."
        docker compose version 2>/dev/null || docker-compose --version
    else
        log_info "Docker Compose 已随 Docker 安装"
    fi
}

step3_install_nodejs() {
    log_step "安装 Node.js"
    
    if command -v node &> /dev/null; then
        log_warn "Node.js 已安装，跳过..."
        node --version
        npm --version
    else
        log_info "安装 Node.js 20.x LTS..."
        
        # 使用 NodeSource 仓库
        curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
        apt-get install -y nodejs
        
        # 安装 PM2
        npm install -g pm2
        
        log_info "Node.js 安装完成"
        node --version
        npm --version
        pm2 --version
    fi
}

step4_install_nginx() {
    log_step "安装 Nginx"
    
    if command -v nginx &> /dev/null; then
        log_warn "Nginx 已安装，跳过..."
        nginx -v
    else
        log_info "安装 Nginx..."
        apt-get install -y nginx
        
        # 备份默认配置
        cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.bak
        
        log_info "Nginx 安装完成"
        nginx -v
    fi
}

#===============================================================================
# 第二阶段：代码部署
#===============================================================================

step5_clone_code() {
    log_step "克隆代码仓库"
    
    # 创建部署目录
    mkdir -p $DEPLOY_DIR
    mkdir -p $DATA_DIR
    
    # 检查是否已存在代码
    if [[ -d "$DEPLOY_DIR/quantmind" ]]; then
        log_warn "代码目录已存在，执行更新..."
        cd $DEPLOY_DIR/quantmind
        git pull origin master
    else
        log_info "从 Gitee 克隆代码..."
        cd $DEPLOY_DIR
        git clone $REPO_URL quantmind
        cd quantmind
    fi
    
    log_info "代码克隆完成，当前分支: $(git branch --show-current)"
}

step6_config_environment() {
    log_step "配置环境变量"
    
    cd $DEPLOY_DIR/quantmind
    
    # 创建 .env 文件
    if [[ -f ".env" ]]; then
        log_warn ".env 文件已存在，跳过..."
    else
        log_info "创建 .env 配置文件..."
        
        # 生成随机密钥
        SECRET_KEY=$(openssl rand -hex 32)
        JWT_SECRET_KEY=$(openssl rand -hex 32)
        
        cat > .env << EOF
# QuantMind OSS Edition 配置

# 应用配置
APP_EDITION=oss
APP_ENV=production
TZ=Asia/Shanghai

# 安全配置
SECRET_KEY=${SECRET_KEY}
JWT_SECRET_KEY=${JWT_SECRET_KEY}

# 数据库配置
DB_HOST=db
DB_PORT=5432
DB_NAME=quantmind
DB_USER=quantmind
DB_PASSWORD=quantmind2026

# Redis 配置
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_PASSWORD=

# 存储配置
STORAGE_MODE=local
STORAGE_ROOT=${DATA_DIR}

# 前端配置
VITE_API_URL=http://${SERVER_IP}:8000
VITE_WS_URL=ws://${SERVER_IP}:8000

# 调试配置
DEBUG=false
LOG_LEVEL=INFO
EOF
        
        log_info ".env 文件创建完成"
    fi
    
    # 创建数据目录
    mkdir -p $DATA_DIR/{postgres,redis,logs,models,qlib_data}
    
    log_info "环境配置完成"
}

step7_init_database() {
    log_step "初始化数据库"
    
    cd $DEPLOY_DIR/quantmind
    
    # 数据库将在 Docker 启动后自动初始化
    # 这里只准备初始化 SQL 文件
    if [[ -f "data/quantmind_init.sql" ]]; then
        log_info "数据库初始化 SQL 已准备: data/quantmind_init.sql"
    else
        log_warn "未找到数据库初始化 SQL 文件"
    fi
    
    log_info "数据库将在 Docker 启动后自动初始化"
}

#===============================================================================
# 第三阶段：后端部署
#===============================================================================

step8_build_docker() {
    log_step "构建 Docker 镜像"
    
    cd $DEPLOY_DIR/quantmind
    
    log_info "构建 QuantMind OSS 镜像..."
    docker build -t quantmind-oss:latest -f docker/Dockerfile.oss .
    
    log_info "Docker 镜像构建完成"
    docker images | grep quantmind-oss
}

step9_start_backend() {
    log_step "启动后端服务"
    
    cd $DEPLOY_DIR/quantmind
    
    log_info "启动 Docker Compose 服务..."
    docker compose up -d
    
    log_info "等待服务启动..."
    sleep 10
    
    # 健康检查
    log_info "检查服务状态..."
    docker compose ps
    
    # 等待数据库就绪
    log_info "等待数据库就绪..."
    sleep 5
    
    # 初始化数据库
    if [[ -f "data/quantmind_init.sql" ]]; then
        log_info "初始化数据库..."
        docker exec -i quantmind-db psql -U quantmind -d quantmind < data/quantmind_init.sql 2>/dev/null || \
            log_warn "数据库可能已初始化"
    fi
    
    log_info "后端服务启动完成"
}

#===============================================================================
# 第四阶段：前端部署
#===============================================================================

step10_install_frontend() {
    log_step "安装前端依赖"
    
    cd $DEPLOY_DIR/quantmind
    
    log_info "安装 npm 依赖..."
    npm install
    
    log_info "前端依赖安装完成"
}

step11_build_frontend() {
    log_step "构建前端"
    
    cd $DEPLOY_DIR/quantmind
    
    log_info "构建生产版本..."
    npm run dashboard:build
    
    log_info "前端构建完成"
}

step12_start_frontend() {
    log_step "启动前端服务"
    
    cd $DEPLOY_DIR/quantmind
    
    log_info "使用 PM2 启动前端服务..."
    
    # 停止旧服务
    pm2 delete quantmind-web 2>/dev/null || true
    
    # 启动新服务
    pm2 start npm --name "quantmind-web" -- run dashboard:preview
    
    # 保存 PM2 配置
    pm2 save
    
    # 设置开机自启
    pm2 startup systemd -u ${SUDO_USER:-root} --hp /home/${SUDO_USER:-root} 2>/dev/null || true
    
    log_info "前端服务启动完成"
    pm2 status
}

#===============================================================================
# 第五阶段：Nginx 配置
#===============================================================================

step13_config_nginx() {
    log_step "配置 Nginx"
    
    # 创建 Nginx 配置
    cat > /etc/nginx/sites-available/quantmind << 'EOF'
server {
    listen 80;
    server_name _;
    
    client_max_body_size 100M;
    
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
        proxy_pass http://127.0.0.1:8003/ws/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 86400;
    }
    
    # Engine API
    location /engine/ {
        proxy_pass http://127.0.0.1:8001/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 300s;
    }
    
    # Trade API
    location /trade/ {
        proxy_pass http://127.0.0.1:8002/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
EOF
    
    # 启用配置
    ln -sf /etc/nginx/sites-available/quantmind /etc/nginx/sites-enabled/
    rm -f /etc/nginx/sites-enabled/default
    
    # 测试配置
    nginx -t
    
    # 重启 Nginx
    systemctl restart nginx
    systemctl enable nginx
    
    log_info "Nginx 配置完成"
}

#===============================================================================
# 第六阶段：验证与监控
#===============================================================================

step14_health_check() {
    log_step "健康检查"
    
    log_info "检查 Docker 容器状态..."
    docker compose -f $DEPLOY_DIR/quantmind/docker-compose.yml ps
    
    log_info "检查 PM2 服务状态..."
    pm2 status
    
    log_info "检查端口监听..."
    netstat -tlnp | grep -E ':(80|3000|8000|8001|8002|8003|5432|6379)'
    
    log_info "测试后端 API..."
    curl -s http://localhost:8000/api/health 2>/dev/null || log_warn "后端 API 可能未就绪"
    
    log_info "测试前端..."
    curl -s -o /dev/null -w "%{http_code}" http://localhost:3000 2>/dev/null || log_warn "前端可能未就绪"
    
    log_info "健康检查完成"
}

step15_firewall() {
    log_step "配置防火墙"
    
    if command -v ufw &> /dev/null; then
        log_info "配置 UFW 防火墙..."
        
        # 允许必要端口
        ufw allow 22/tcp    # SSH
        ufw allow 80/tcp    # HTTP
        ufw allow 443/tcp   # HTTPS
        ufw allow 3000/tcp  # Frontend (可选，通过 Nginx 访问)
        
        # 启用防火墙
        ufw --force enable
        
        ufw status
    else
        log_warn "UFW 未安装，跳过防火墙配置"
    fi
}

step16_show_info() {
    log_step "部署完成"
    
    echo -e "\n${GREEN}========================================${NC}"
    echo -e "${GREEN}  QuantMind 部署成功！${NC}"
    echo -e "${GREEN}========================================${NC}\n"
    
    echo -e "访问地址:"
    echo -e "  前端: ${BLUE}http://${SERVER_IP}${NC}"
    echo -e "  后端: ${BLUE}http://${SERVER_IP}:8000${NC}"
    echo -e ""
    echo -e "默认管理员账号:"
    echo -e "  用户名: ${YELLOW}admin${NC}"
    echo -e "  密码:   ${YELLOW}需要通过 API 重置${NC}"
    echo -e ""
    echo -e "常用命令:"
    echo -e "  查看后端日志: docker compose -f $DEPLOY_DIR/quantmind/docker-compose.yml logs -f"
    echo -e "  查看前端日志: pm2 logs quantmind-web"
    echo -e "  重启后端:     docker compose -f $DEPLOY_DIR/quantmind/docker-compose.yml restart"
    echo -e "  重启前端:     pm2 restart quantmind-web"
    echo -e "  重启 Nginx:   systemctl restart nginx"
    echo -e ""
    echo -e "部署目录: $DEPLOY_DIR/quantmind"
    echo -e "数据目录: $DATA_DIR"
    echo -e ""
}

#===============================================================================
# 主函数
#===============================================================================

main() {
    clear
    
    echo -e "${GREEN}"
    echo "========================================"
    echo "  QuantMind 一键部署脚本"
    echo "  版本: 1.0.0"
    echo "========================================"
    echo -e "${NC}"
    
    check_root
    check_system
    
    # 第一阶段：系统准备
    step1_update_system
    step2_install_docker
    step3_install_nodejs
    step4_install_nginx
    
    # 第二阶段：代码部署
    step5_clone_code
    step6_config_environment
    step7_init_database
    
    # 第三阶段：后端部署
    step8_build_docker
    step9_start_backend
    
    # 第四阶段：前端部署
    step10_install_frontend
    step11_build_frontend
    step12_start_frontend
    
    # 第五阶段：Nginx 配置
    step13_config_nginx
    
    # 第六阶段：验证与监控
    step14_health_check
    step15_firewall
    step16_show_info
}

# 执行主函数
main "$@"
