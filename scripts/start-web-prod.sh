#!/bin/bash
# QuantMind Web 前端生产环境启动脚本
# 用于服务器部署，监听 3000 端口

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT/electron"

# 默认配置
HOST="${VITE_HOST:-0.0.0.0}"
PORT="${VITE_PORT:-3000}"

echo "========================================"
echo "QuantMind Web Frontend (Production)"
echo "========================================"
echo "Host: $HOST"
echo "Port: $PORT"
echo "========================================"

# 检查构建产物
if [ ! -d "dist-react" ]; then
    echo "Building production bundle..."
    npm run build:react
fi

# 启动预览服务器
echo "Starting preview server..."
npx vite preview --host "$HOST" --port "$PORT"
