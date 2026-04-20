#!/bin/bash
# ====================================================================
# QuantMind Qlib 数据每日自动补全脚本
# 时间：建议每日凌晨 03:30 运行
# ====================================================================

# 1. 基础环境配置 (根据您的服务器路径调整)
PROJECT_DIR="/home/quantmind"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
LOG_DIR="${PROJECT_DIR}/logs/data_sync"
LOG_FILE="${LOG_DIR}/sync_$(date +\%Y\%m\%d).log"

# 加载 .env 环境变量
if [ -f "${PROJECT_DIR}/.env" ]; then
    set -a
    . "${PROJECT_DIR}/.env"
    set +a
fi

# 创建日志目录
mkdir -p "${LOG_DIR}"

echo "📅 [$(date)] 开始每日量化数据同步任务..." >> "${LOG_FILE}"

cd "${PROJECT_DIR}" || exit

# --------------------------------------------------------------------
# 2. 同步数据库行情表 (Baostock -> PostgreSQL)
# --------------------------------------------------------------------
echo "🚀 正在同步 Baostock 数据到数据库 market_data_daily..." >> "${LOG_FILE}"
"${PYTHON_BIN}" scripts/data/ingestion/sync_market_data_daily_from_baostock.py --apply 2>&1 | tee -a "${LOG_FILE}"

# --------------------------------------------------------------------
# 3. 从数据库同步到 Qlib 二进制目录 (PostgreSQL -> Binary Bins)
#    更新主 Qlib 数据和 Alpha158 专用数据
# --------------------------------------------------------------------
echo "🚀 正在从数据库同步 Qlib 主数据 (qlib_data)..." >> "${LOG_FILE}"
"${PYTHON_BIN}" scripts/data/ingestion/update_qlib_from_db.py --qlib-dir db/qlib_data --apply 2>&1 | tee -a "${LOG_FILE}"

echo "✅ [$(date)] 所有同步任务执行完毕。" >> "${LOG_FILE}"
echo "--------------------------------------------------------" >> "${LOG_FILE}"
