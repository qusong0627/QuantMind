#!/bin/bash
# QuantMind 初始化脚本：安装 RD-Agent + 数据初始化
# 用法: docker exec quantmind bash /app/scripts/setup/init.sh [--no-rd-agent] [--no-data-sync]

set -euo pipefail

INSTALL_RD_AGENT=true
DATA_SYNC=true

for arg in "$@"; do
    case "$arg" in
        --no-rd-agent) INSTALL_RD_AGENT=false ;;
        --no-data-sync) DATA_SYNC=false ;;
        *) echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

echo "========================================="
echo "  QuantMind 初始化脚本"
echo "========================================="

# -------------------------------------------
# 1. RD-Agent 安装
# -------------------------------------------
if [ "$INSTALL_RD_AGENT" = true ]; then
    echo ""
    echo "[1/2] 安装 RD-Agent..."

    if [ ! -d /app/rd-agent ]; then
        echo "  [错误] /app/rd-agent 目录不存在"
        echo "  请确保 docker-compose.yml 中已挂载 rd-agent 目录"
        exit 1
    fi

    # 检查是否已安装
    if python -c "import rdagent" 2>/dev/null; then
        echo "  [跳过] RD-Agent 已安装"
    else
        echo "  [安装中] 正在 pip install -e /app/rd-agent ..."
        # 修复 git safe directory 问题
        git config --global --add safe.directory /app/rd-agent
        pip install -e /app/rd-agent --quiet
        echo "  [完成] RD-Agent 安装成功"
    fi
fi

# -------------------------------------------
# 2. 数据初始化检查
# -------------------------------------------
if [ "$DATA_SYNC" = true ]; then
    echo ""
    echo "[2/2] 检查数据状态..."

    # 检查 stock_daily_latest 是否有数据
    if [ -n "$DB_HOST" ] && [ -n "$DB_NAME" ]; then
        ROW_COUNT=$(python -c "
import asyncio, os
from backend.shared.db_manager_v2 import DatabaseManager

async def check():
    db = DatabaseManager()
    await db.initialize()
    async with db.get_session(read_only=True) as session:
        from sqlalchemy import text
        result = await session.execute(text('SELECT COUNT(*) FROM stock_daily_latest'))
        return result.scalar()

count = asyncio.run(check())
print(count)
" 2>/dev/null || echo "ERROR")

        if [ "$ROW_COUNT" = "ERROR" ]; then
            echo "  [跳过] 数据库检查失败，请手动检查数据库连接"
        elif [ "$ROW_COUNT" = "0" ]; then
            echo "  [警告] stock_daily_latest 表为空，需要运行数据同步"
            echo "  运行: python /app/scripts/data/maintenance/sync_stock_daily_full.py"
        else
            echo "  [OK] stock_daily_latest 已有 ${ROW_COUNT} 条数据"
        fi
    else
        echo "  [跳过] 数据库环境变量未设置"
    fi
fi

echo ""
echo "========================================="
echo "  初始化完成！"
echo "========================================="
