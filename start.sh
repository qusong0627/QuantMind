#!/bin/bash
# QuantMind 一键启动脚本
# 同时启动后端 (Docker) 和前端 (Vite dev server)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- 颜色输出 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# ---- 清理子进程 ----
BACKEND_PID=""
FRONTEND_PID=""
DOCKER_MANAGED=false
FRONTEND_PORT="${FRONTEND_PORT:-3000}"

compose() {
    if command -v docker-compose &>/dev/null; then
        docker-compose "$@"
    elif docker compose version &>/dev/null; then
        docker compose "$@"
    else
        err "未找到 docker-compose 或 docker compose，请安装 Docker Compose"
        exit 1
    fi
}

cleanup() {
    echo ""
    info "正在停止服务..."
    if [ -n "$FRONTEND_PID" ]; then
        kill "$FRONTEND_PID" 2>/dev/null
        # 清理 Electron 子进程
        pkill -P "$FRONTEND_PID" 2>/dev/null || true
        info "前端已停止"
    fi
    if [ -n "$BACKEND_PID" ]; then
        kill "$BACKEND_PID" 2>/dev/null
        info "后端已停止"
    fi
    if [ "$DOCKER_MANAGED" = true ]; then
        info "正在停止本次启动的 Docker 容器..."
        compose stop
        info "Docker 容器已停止"
    fi
    exit 0
}
trap cleanup INT TERM

# ---- 解析参数 ----
MODE="dev"        # dev | prod | local
SKIP_BACKEND=false
SKIP_FRONTEND=false
WEB_ONLY=false    # 只启动 web 版前端（不启动 Electron）

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --dev          开发模式 (默认): Docker 后端 + Vite 开发服务器"
    echo "  --prod         生产模式: Docker 后端 + Vite 预览服务器"
    echo "  --local        本地模式: 直接运行 Python 后端 + Vite 开发服务器"
    echo "  --web          仅启动 Web 版前端（不启动 Electron）"
    echo "  --backend-only 仅启动后端"
    echo "  --frontend-only 仅启动前端"
    echo "  -h, --help     显示帮助"
    echo ""
    echo "Examples:"
    echo "  $0                    # 开发模式一键启动"
    echo "  $0 --web              # Web 开发模式（浏览器访问）"
    echo "  $0 --local            # 本地 Python 后端 + 前端"
    echo "  $0 --backend-only     # 仅启动后端"
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --dev)          MODE="dev" ;;
        --prod)         MODE="prod" ;;
        --local)        MODE="local" ;;
        --web)          WEB_ONLY=true ;;
        --backend-only)  SKIP_FRONTEND=true ;;
        --frontend-only) SKIP_BACKEND=true ;;
        -h|--help)      usage; exit 0 ;;
        *)              err "未知参数: $1"; usage; exit 1 ;;
    esac
    shift
done

echo ""
echo "========================================="
echo "       QuantMind 一键启动"
echo "  模式: $MODE"
echo "========================================="
echo ""

# ============================================================
# 后端启动
# ============================================================
start_backend() {
    if [ "$SKIP_BACKEND" = true ]; then
        info "跳过后端启动 (--frontend-only)"
        return
    fi

    info "正在启动后端服务..."

    if [ "$MODE" = "local" ]; then
        # 本地 Python 模式
        if [ ! -d "$SCRIPT_DIR/.venv" ]; then
            err "未找到 .venv 虚拟环境，请先执行: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
            exit 1
        fi

        # 检查 PostgreSQL
        DB_HOST="${DB_HOST:-localhost}"
        DB_PORT="${DB_PORT:-5432}"
        if ! command -v psql &>/dev/null; then
            warn "未检测到 psql 客户端，跳过数据库连通性检查"
        elif ! psql -h "$DB_HOST" -p "$DB_PORT" -U "${DB_USER:-quantmind}" -d "${DB_NAME:-quantmind}" -c "SELECT 1" &>/dev/null; then
            err "无法连接到 PostgreSQL ($DB_HOST:$DB_PORT)"
            err "请确保 PostgreSQL 正在运行，或设置环境变量: DB_HOST, DB_PORT, DB_USER, DB_NAME, DB_PASSWORD"
            exit 1
        else
            ok "PostgreSQL 已就绪 ($DB_HOST:$DB_PORT)"
        fi

        # 检查 Redis
        REDIS_HOST="${REDIS_HOST:-localhost}"
        REDIS_PORT="${REDIS_PORT:-6379}"
        if command -v redis-cli &>/dev/null; then
            if ! redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping &>/dev/null; then
                err "无法连接到 Redis ($REDIS_HOST:$REDIS_PORT)"
                err "请确保 Redis 正在运行，或设置环境变量: REDIS_HOST, REDIS_PORT"
                exit 1
            else
                ok "Redis 已就绪 ($REDIS_HOST:$REDIS_PORT)"
            fi
        else
            warn "未检测到 redis-cli，跳过 Redis 连通性检查"
        fi

        source "$SCRIPT_DIR/.venv/bin/activate"

        export SERVICE_MODE=all
        export APP_EDITION=oss
        export PYTHONPATH="$SCRIPT_DIR"

        info "以本地模式启动后端 (SERVICE_MODE=all)..."
        python3 backend/main_oss.py &
        BACKEND_PID=$!
    else
        # Docker 模式
        if ! command -v docker &>/dev/null; then
            err "未找到 docker，请先安装 Docker"
            exit 1
        fi

        # 检查 Docker 是否运行
        if ! docker info &>/dev/null; then
            err "Docker 未运行，请先启动 Docker"
            exit 1
        fi

        # 检查是否已有运行中的容器
        if docker ps --format '{{.Names}}' | grep -qE '^(quantmind|quantmind-db|quantmind-redis|quantmind-celery)$'; then
            warn "检测到已有 QuantMind 容器运行中"
            read -p "是否重启? [y/N] " -n 1 -r
            echo
            if [[ ${REPLY:-} =~ ^[Yy]$ ]]; then
                info "正在重启 Docker 容器..."
                compose restart
            fi
        else
            info "正在启动 Docker 容器 (后台运行)..."
            compose up -d
            DOCKER_MANAGED=true
        fi

        # 等待后端就绪
        info "等待后端服务就绪..."
        MAX_WAIT=60
        WAITED=0
        while [ $WAITED -lt $MAX_WAIT ]; do
            if curl -s http://localhost:8000/health &>/dev/null || curl -s http://localhost:8000/ &>/dev/null; then
                break
            fi
            sleep 2
            WAITED=$((WAITED + 2))
            printf "\r  等待中... %ds/%ds" "$WAITED" "$MAX_WAIT"
        done
        echo ""

        if [ $WAITED -ge $MAX_WAIT ]; then
            warn "后端未在 ${MAX_WAIT}s 内就绪，可能仍在启动中"
            warn "可通过 compose logs -f 查看日志"
        else
            ok "后端服务已就绪"
        fi
    fi

    ok "后端已启动"
}

# ============================================================
# 前端启动
# ============================================================
start_frontend() {
    if [ "$SKIP_FRONTEND" = true ]; then
        info "跳过前端启动 (--backend-only)"
        return
    fi

    cd "$SCRIPT_DIR/electron"

    # 检查 node_modules
    if [ ! -d "node_modules" ]; then
        info "安装前端依赖..."
        if npm install; then
            ok "依赖安装完成"
        else
            warn "npm install 失败，尝试使用淘宝镜像临时重试..."
            npm install --registry=https://registry.npmmirror.com
            ok "依赖安装完成 (淘宝镜像)"
        fi
    fi

    # 确保 build/icon.ico 存在（Electron build 需要）
    if [ ! -f "build/icon.ico" ]; then
        info "生成缺失的 build/icon.ico..."
        mkdir -p build
        if [ -f "public/favicon.ico" ]; then
            cp public/favicon.ico build/icon.ico
            ok "已从 public/favicon.ico 复制"
        else
            warn "未找到 public/favicon.ico，Electron 打包可能失败"
        fi
    fi

    # 检查并释放端口（仅释放 QuantMind 相关进程，避免误杀）
    PORT_PID=""
    if command -v lsof &>/dev/null; then
        PORT_PID=$(lsof -ti:"$FRONTEND_PORT" 2>/dev/null || true)
    elif command -v fuser &>/dev/null; then
        PORT_PID=$(fuser "$FRONTEND_PORT"/tcp 2>/dev/null || true)
    fi
    if [ -n "$PORT_PID" ]; then
        SAFE_TO_KILL=true
        for pid in $PORT_PID; do
            cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
            if [[ ! "$cmd" =~ (vite|electron|quantmind|npm[[:space:]]run[[:space:]]dev) ]]; then
                SAFE_TO_KILL=false
                warn "端口 $FRONTEND_PORT 被非 QuantMind 进程占用: PID=$pid CMD=$cmd"
                break
            fi
        done

        if [ "$SAFE_TO_KILL" = true ]; then
            warn "端口 $FRONTEND_PORT 被 QuantMind 相关进程占用，正在释放..."
            kill $PORT_PID 2>/dev/null || true
            sleep 1
            # 重新检查端口
            REMAINING_PID=""
            if command -v lsof &>/dev/null; then
                REMAINING_PID=$(lsof -ti:"$FRONTEND_PORT" 2>/dev/null || true)
            elif command -v fuser &>/dev/null; then
                REMAINING_PID=$(fuser "$FRONTEND_PORT"/tcp 2>/dev/null || true)
            fi
            if [ -n "$REMAINING_PID" ]; then
                kill -9 $REMAINING_PID 2>/dev/null || true
                sleep 1
            fi
            ok "端口 $FRONTEND_PORT 已释放"
        else
            err "请先手动释放端口 $FRONTEND_PORT，或用 FRONTEND_PORT 指定其他端口"
            exit 1
        fi
    fi

    info "正在启动前端..."

    export VITE_API_URL="${VITE_API_URL:-http://localhost:8000}"
    export VITE_WS_URL="${VITE_WS_URL:-ws://localhost:8000}"

    if [ "$MODE" = "prod" ]; then
        # 生产预览模式
        if [ ! -d "dist-react" ]; then
            info "构建前端生产包..."
            npm run build:react
        fi
        info "启动 Vite 预览服务器..."
        npx vite preview --host 0.0.0.0 --port "$FRONTEND_PORT" &
        FRONTEND_PID=$!
        ok "前端已启动 (生产预览) -> http://localhost:$FRONTEND_PORT"
    elif [ "$WEB_ONLY" = true ]; then
        # Web 开发模式（不启动 Electron）
        info "启动 Vite 开发服务器 (Web 模式)..."
        npx vite --port "$FRONTEND_PORT" &
        FRONTEND_PID=$!
        ok "前端已启动 (Web 开发) -> http://localhost:$FRONTEND_PORT"
    else
        # Electron 桌面开发模式
        info "启动 Electron 开发服务器..."
        npm run dev &
        FRONTEND_PID=$!
        ok "前端已启动 (Electron + Vite 开发)"
    fi

    cd "$SCRIPT_DIR"
}

# ============================================================
# 主流程
# ============================================================
start_backend
start_frontend

echo ""
echo "========================================="
echo "  QuantMind 已启动!"
if [ "$SKIP_BACKEND" != true ]; then
    echo "  后端 API:  http://localhost:8000"
    echo "  后端 Engine: http://localhost:8001"
fi
if [ "$SKIP_FRONTEND" != true ]; then
    if [ "$WEB_ONLY" = true ] || [ "$MODE" = "prod" ]; then
        echo "  前端:     http://localhost:$FRONTEND_PORT"
    else
        echo "  前端:     Electron 桌面应用"
    fi
fi
echo "========================================="
echo ""
info "按 Ctrl+C 停止所有服务"
echo ""

# 保持脚本运行，等待子进程退出
# 使用 wait 捕获 SIGCHLD 以避免僵尸进程
wait -n 2>/dev/null || wait
