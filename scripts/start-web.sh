#!/bin/bash
# QuantMind Web 前端启动脚本
# 用于服务器部署，监听 3000 端口

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

# 默认配置
HOST="${VITE_HOST:-0.0.0.0}"
PORT="${VITE_PORT:-3000}"
API_URL="${VITE_API_URL:-http://localhost:8000}"
WS_URL="${VITE_WS_URL:-ws://localhost:8000}"

echo "========================================"
echo "QuantMind Web Frontend"
echo "========================================"
echo "Host: $HOST"
echo "Port: $PORT"
echo "API:  $API_URL"
echo "WS:   $WS_URL"
echo "========================================"

# 检查 node_modules
if [ ! -d "node_modules" ]; then
    echo "Installing dependencies..."
    npm install
fi

# 启动开发服务器
export VITE_API_URL="$API_URL"
export VITE_WS_URL="$WS_URL"
export VITE_PORT="$PORT"

echo "Starting development server..."
npm run dev
