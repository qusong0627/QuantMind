#!/bin/bash
# ==============================================================================
# QuantMind 自动化部署脚本
# 用途: 快速同步本地代码到服务器并重启对应的 Docker 容器
# 使用方法: ./scripts/ops/deploy.sh [service_name]
#          例如: ./scripts/ops/deploy.sh api
#          不传参数则同步代码但不重启特定服务
# ==============================================================================

SERVER="210.16.175.87"
REMOTE_PATH="/home/quantmind"
SERVICE=$1

echo "======================================================================"
echo "🚀 开始同步代码到 $SERVER..."
echo "======================================================================"

# 1. 使用 rsync 同步代码
# 排除项: .git, .venv, node_modules, db, data, models 等（不包含运行时数据）
rsync -avz --delete \
    --exclude='.git/' \
    --exclude='.venv/' \
    --exclude='node_modules/' \
    --exclude='electron/node_modules/' \
    --exclude='electron/dist/' \
    --exclude='electron/dist-react/' \
    --exclude='electron/dist-electron/' \
    --exclude='/dist/' \
    --exclude='/website/' \
    --exclude='/models/' \
    --exclude='/examples/' \
    --exclude='/k8s/' \
    --exclude='/redis/' \
    --exclude='htmlcov/' \
    --exclude='.coverage' \
    --exclude='__pycache__/' \
    --exclude='.mypy_cache/' \
    --exclude='.pytest_cache/' \
    --exclude='db/' \
    --exclude='data/' \
    --exclude='.DS_Store' \
    --exclude='.ipynb_checkpoints/' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='*.pyd' \
    --exclude='*.zip' \
    --exclude='*.gz' \
    --exclude='*.tar.gz' \
    --exclude='*.7z' \
    --exclude='*.rar' \
    --exclude='/_*.py' \
    --exclude='*.log' \
    --exclude='tmp/' \
    ./ "$SERVER:$REMOTE_PATH/"

if [ $? -eq 0 ]; then
    echo "✅ 代码同步完成。"
else
    echo "❌ 代码同步失败，请检查网络或 SSH 配置。"
    exit 1
fi

# 2. 如果指定了服务，则重启对应容器
if [ -n "$SERVICE" ]; then
    # 映射服务名称到 Docker Compose 中的真实 Service Name
    # OSS 版使用单容器 (quantmind) + celery-worker
    case $SERVICE in
        api|trade|engine|stream) DOCKER_SERVICE="quantmind" ;;
        celery)                  DOCKER_SERVICE="celery-worker" ;;
        *)                       DOCKER_SERVICE="quantmind" ;;
    esac

    echo -e "\n🔄 正在远程重启服务: $DOCKER_SERVICE..."
    ssh "$SERVER" "cd $REMOTE_PATH && docker compose up -d --build $DOCKER_SERVICE"
    
    if [ $? -eq 0 ]; then
        echo "✅ 服务 $DOCKER_SERVICE 已重启。"
    else
        echo "❌ 服务重启失败，请检查远程 docker compose 状态。"
        exit 1
    fi
else
    echo -e "\n💡 提示: 未指定服务名称，仅执行了代码同步。"
    echo "可用服务示例: api, trade, engine, stream"
fi

echo "======================================================================"
echo "✨ 部署流程结束。"
echo "======================================================================"
