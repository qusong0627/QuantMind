#!/bin/bash
# ====================================================================
# QuantMind Qlib 数据转换脚本 - PostgreSQL -> Qlib Binary
# 时间：每日凌晨 04:00 执行 (确保数据库同步已完成)
# 功能：从数据库读取行情数据并转换为 Qlib 二进制格式
# ====================================================================

# 1. 基础环境配置
PROJECT_DIR="/home/quantmind"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
LOG_DIR="${PROJECT_DIR}/logs/data_sync"
LOG_FILE="${LOG_DIR}/sync_qlib_$(date +\%Y\%m\%d).log"

# 加载 .env 环境变量
if [ -f "${PROJECT_DIR}/.env" ]; then
    set -a
    . "${PROJECT_DIR}/.env"
    set +a
fi

# 创建日志目录
mkdir -p "${LOG_DIR}"

echo "📅 [$(date)] 开始 Qlib 数据转换任务 (PostgreSQL -> Binary)..." >> "${LOG_FILE}"

cd "${PROJECT_DIR}" || exit

# --------------------------------------------------------------------
# 从数据库同步到 Qlib 主数据目录
# --------------------------------------------------------------------
echo "🚀 正在同步 Qlib 主数据 (qlib_data)..." >> "${LOG_FILE}"
"${PYTHON_BIN}" scripts/data/ingestion/update_qlib_from_db.py --qlib-dir db/qlib_data --apply 2>&1 | tee -a "${LOG_FILE}"

# --------------------------------------------------------------------
# 从数据库同步到 Qlib 数据目录
# --------------------------------------------------------------------
echo "✅ [$(date)] Qlib 数据转换任务完成。" >> "${LOG_FILE}"
echo "--------------------------------------------------------" >> "${LOG_FILE}"
