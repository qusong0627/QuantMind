#!/bin/bash
# ====================================================================
# QuantMind 日常数据同步脚本
# 1) 远端 PG -> 本地 parquet
# 2) fundamental_aligned.parquet -> stock_daily_latest
# 3) qlib features -> index_ohlcv_daily
# 4) 回填 stock_daily_latest.consecutive_limit_up_days
# 5) 回填 stock_daily_latest.return_1d / return_3d
# 6) fundamental_aligned.parquet -> qlib_data
# 7) 从 csi500.txt 回填 stock_daily_latest.idx_zz500
# ====================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${PROJECT_DIR}/.venv/bin/python}"
PYTHON_BIN_FALLBACK="${PYTHON_BIN_FALLBACK:-python3}"
LOG_DIR="${LOG_DIR:-/tmp/quantmind-data-sync}"
LOG_FILE="${LOG_DIR}/daily_pg_sync_$(date +%Y%m%d).log"

if [ ! -x "${PYTHON_BIN}" ]; then
    PYTHON_BIN="${PYTHON_BIN_FALLBACK}"
elif ! "${PYTHON_BIN}" -c "import pandas, pyarrow, sqlalchemy, dotenv, psycopg2" >/dev/null 2>&1; then
    PYTHON_BIN="${PYTHON_BIN_FALLBACK}"
fi



mkdir -p "${LOG_DIR}"

run_step() {
    local title="$1"
    shift
    echo "[$(date '+%F %T')] ${title}" | tee -a "${LOG_FILE}"
    "$@" 2>&1 | tee -a "${LOG_FILE}"
}

cd "${PROJECT_DIR}"

echo "========================================================" | tee -a "${LOG_FILE}"
echo "[$(date '+%F %T')] 开始执行日常数据同步" | tee -a "${LOG_FILE}"
echo "PROJECT_DIR=${PROJECT_DIR}" | tee -a "${LOG_FILE}"
echo "PYTHON_BIN=${PYTHON_BIN}" | tee -a "${LOG_FILE}"


run_step \
    "Step 1/2: 远端 PG -> parquet" \
    "${PYTHON_BIN}" scripts/data/maintenance/sync_parquets_from_remote_pg.py

run_step \
    "Step 2/2: fundamental_aligned.parquet -> qlib_data" \
    "${PYTHON_BIN}" scripts/data/maintenance/sync_qlib_from_fundamental_parquet.py

echo "[$(date '+%F %T')] 所有同步步骤执行完成" | tee -a "${LOG_FILE}"
