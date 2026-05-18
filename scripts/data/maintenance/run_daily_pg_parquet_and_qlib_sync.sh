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

if [ -f "${PROJECT_DIR}/.env" ]; then
    set -a
    . "${PROJECT_DIR}/.env"
    set +a
fi

APP_DB_HOST="${DB_HOST:-127.0.0.1}"
APP_DB_PORT="${DB_PORT:-5432}"
APP_DB_NAME="${DB_NAME:-quantmind}"
APP_DB_USER="${DB_USER:-quantmind}"
APP_DB_PASSWORD="${DB_PASSWORD:-}"

if [ "${APP_DB_HOST}" = "quantmind-postgresql" ]; then
    APP_DB_HOST="127.0.0.1"
fi

APP_DB_URL="$({ \
    export APP_DB_HOST APP_DB_PORT APP_DB_NAME APP_DB_USER APP_DB_PASSWORD; \
    python3 - <<'PY'
import os
from urllib.parse import quote

print(
    f"postgresql://{os.getenv('APP_DB_USER', 'quantmind')}:{quote(os.getenv('APP_DB_PASSWORD', ''), safe='')}@"
    f"{os.getenv('APP_DB_HOST', '127.0.0.1')}:{os.getenv('APP_DB_PORT', '5432')}/{os.getenv('APP_DB_NAME', 'quantmind')}"
)
PY
})"

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
echo "APP_DB_HOST=${APP_DB_HOST}" | tee -a "${LOG_FILE}"
echo "========================================================" | tee -a "${LOG_FILE}"

run_step \
    "Preflight: 校验并修复关键数据资产" \
    "${PYTHON_BIN}" - <<'PY'
from pathlib import Path
import pandas as pd
import pyarrow.parquet as pq

root = Path.cwd()
fa_path = root / "db" / "custom" / "fundamental_aligned.parquet"
qlib_features = root / "db" / "qlib_data" / "features"

if not fa_path.exists():
    raise FileNotFoundError(f"missing parquet: {fa_path}")

schema_cols = pq.ParquetFile(fa_path).schema.names
if "cum_bward_factor" not in schema_cols:
    print(f"[WARN] {fa_path} missing cum_bward_factor, auto patching...")
    df = pd.read_parquet(fa_path)
    if "cum_bward_factor" not in df.columns:
        df["cum_bward_factor"] = 1.0
    else:
        df["cum_bward_factor"] = pd.to_numeric(df["cum_bward_factor"], errors="coerce").fillna(1.0)
    cols = [c for c in df.columns if c != "cum_bward_factor"] + ["cum_bward_factor"]
    tmp = fa_path.with_suffix(".parquet.tmp")
    df[cols].to_parquet(tmp, index=False, engine="pyarrow", compression="snappy")
    tmp.replace(fa_path)
    print("[OK] cum_bward_factor added")
else:
    print("[OK] cum_bward_factor exists")

required_dirs = {
    "SH000300": ("idx_sh000300", "sh000300"),
    "SH000905": ("idx_sh000905", "sh000905"),
    "SH000852": ("idx_sh000852", "sh000852"),
}
for symbol, candidates in required_dirs.items():
    ok = any((qlib_features / d).exists() for d in candidates)
    if ok:
        print(f"[OK] qlib index dir found for {symbol}")
    else:
        print(f"[WARN] qlib index dir missing for {symbol}, tried={candidates}")
PY

run_step \
    "Step 1/6: 远端 PG -> parquet" \
    "${PYTHON_BIN}" scripts/data/maintenance/sync_parquets_from_remote_pg.py

run_step \
    "Step 2/6: fundamental_aligned.parquet -> stock_daily_latest" \
    "${PYTHON_BIN}" scripts/data/maintenance/sync_stock_daily_latest_from_parquet.py --database-url "${APP_DB_URL}"

run_step \
    "Step 3/6: qlib features -> index_ohlcv_daily" \
    "${PYTHON_BIN}" scripts/data/maintenance/sync_index_ohlcv_from_qlib_features.py --database-url "${APP_DB_URL}"

run_step \
    "Step 4/6: 回填 stock_daily_latest 连板字段" \
    "${PYTHON_BIN}" scripts/data/maintenance/backfill_consecutive_limit_up_days.py --database-url "${APP_DB_URL}" --apply

run_step \
    "Step 5/6: 回填 stock_daily_latest 收益率字段" \
    "${PYTHON_BIN}" scripts/data/processing/backfill_return_fields.py --database-url "${APP_DB_URL}" --recent-days 10

run_step \
    "Step 6/6: fundamental_aligned.parquet -> qlib_data" \
    "${PYTHON_BIN}" scripts/data/maintenance/sync_qlib_from_fundamental_parquet.py

run_step \
    "Step 7/7: 回填 stock_daily_latest 中证500标记字段" \
    "${PYTHON_BIN}" scripts/data/maintenance/patch_idx_zz500.py --database-url "${APP_DB_URL}"

echo "[$(date '+%F %T')] 所有同步步骤执行完成" | tee -a "${LOG_FILE}"
