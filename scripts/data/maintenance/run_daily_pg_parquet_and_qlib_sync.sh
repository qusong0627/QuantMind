#!/bin/bash
# ====================================================================
# QuantMind 日常数据同步脚本
# 1) 远端 PG -> 本地 parquet
# 2) fundamental_aligned.parquet -> stock_daily_latest
# 3) 回填 stock_daily_latest.return_1d / return_3d
# 4) fundamental_aligned.parquet -> qlib_data
# ====================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${PROJECT_DIR}/.venv/bin/python}"
LOG_DIR="${LOG_DIR:-/tmp/quantmind-data-sync}"
LOG_FILE="${LOG_DIR}/daily_pg_sync_$(date +%Y%m%d).log"

if [ ! -x "${PYTHON_BIN}" ]; then
    PYTHON_BIN="${PYTHON_BIN_FALLBACK:-python3}"
fi

if [ -f "${PROJECT_DIR}/.env" ]; then
    set -a
    . "${PROJECT_DIR}/.env"
    set +a
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
echo "========================================================" | tee -a "${LOG_FILE}"

run_step \
    "Step 1/4: 远端 PG -> parquet" \
    "${PYTHON_BIN}" scripts/data/maintenance/sync_parquets_from_remote_pg.py

run_step \
    "Step 2/4: fundamental_aligned.parquet -> stock_daily_latest" \
    "${PYTHON_BIN}" scripts/data/maintenance/sync_stock_daily_latest_from_parquet.py

run_step \
    "Step 3/4: 回填 stock_daily_latest 收益率字段" \
    "${PYTHON_BIN}" scripts/data/processing/backfill_return_fields.py --recent-days 10

run_step \
    "Step 4/4: fundamental_aligned.parquet -> qlib_data" \
    "${PYTHON_BIN}" scripts/data/maintenance/sync_qlib_from_fundamental_parquet.py

echo "[$(date '+%F %T')] 所有同步步骤执行完成" | tee -a "${LOG_FILE}"
