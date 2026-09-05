#!/usr/bin/env bash
# ============================================================
# QuantMind Portable One-Click Start (macOS Apple Silicon)
# Double-click in Finder to start; use stop.command to stop.
# If Gatekeeper blocks: xattr -dr com.apple.quarantine <folder>
# ============================================================
cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"

QM_PG_PORT=5432
QM_REDIS_PORT=6379
QM_API_PORT=8000
QM_ENGINE_PORT=8001
QM_TRADE_PORT=8002
QM_STREAM_PORT=8003

STORAGE_ROOT="$ROOT/data"
PYTHON="$ROOT/runtime/python/bin/python3"
PGBIN="$ROOT/pgsql/bin"
export PATH="$ROOT/redis:$PGBIN:$PATH"

echo "[QuantMind] root: $ROOT"

[ -x "$PYTHON" ] || { echo "[!] python3 missing: $PYTHON"; read -r -p "Press enter to exit"; exit 1; }

mkdir -p "$ROOT/logs" "$ROOT/run"
for D in models uploads strategies reports backtest_results hf qlib_data quantdb quantus quanthk quantbc quantfutures; do
    mkdir -p "$STORAGE_ROOT/$D"
done

# Apple Silicon + x86_64 PostgreSQL -> need Rosetta
if [ "$(uname -m)" = "arm64" ]; then
    PG_ARCH="$(file "$PGBIN/postgres" 2>/dev/null | grep -o 'x86_64' | head -1)"
    if [ "$PG_ARCH" = "x86_64" ] && ! [ -x /Library/Apple/usr/libexec/oah/RosettaHelper ]; then
        echo "[!] This package bundles x86_64 PostgreSQL; Apple Silicon needs Rosetta 2."
        echo "    Install with:  softwareupdate --install-rosetta"
        read -r -p "Press enter to exit"; exit 1
    fi
fi

# secrets
SECRETS="$ROOT/run/secrets.env"
if [ ! -f "$SECRETS" ]; then
    {
        echo "SECRET_KEY=$(uuidgen | tr -d '-' | head -c 64)"
        echo "JWT_SECRET_KEY=$(uuidgen | tr -d '-' | head -c 64)"
        echo "INTERNAL_CALL_SECRET=$(uuidgen | tr -d '-' | head -c 64)"
    } > "$SECRETS"
fi
set -a; . "$SECRETS"; set +a

export APP_EDITION=oss APP_ENV=production SERVICE_MODE=all TZ=Asia/Shanghai
export PYTHONPATH="$ROOT"
export DB_DRIVER=asyncpg DB_HOST=127.0.0.1 DB_PORT=$QM_PG_PORT
export DB_NAME=quantmind DB_USER=quantmind DB_PASSWORD=quantmind2026
export DATABASE_URL="postgresql+asyncpg://quantmind:quantmind2026@127.0.0.1:$QM_PG_PORT/quantmind"
export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=$QM_PG_PORT POSTGRES_USER=quantmind
export POSTGRES_PASSWORD=quantmind2026 POSTGRES_DB=quantmind
export REDIS_HOST=127.0.0.1 REDIS_PORT=$QM_REDIS_PORT
export STORAGE_MODE=local QUANTMIND_ENABLE_WEB_UPDATE=false
export API_PORT=$QM_API_PORT ENGINE_PORT=$QM_ENGINE_PORT TRADE_PORT=$QM_TRADE_PORT STREAM_PORT=$QM_STREAM_PORT
export API_WORKERS=1 ENGINE_WORKERS=1 TRADE_WORKERS=1 STREAM_WORKERS=1
export TRADE_SERVICE_URL=http://127.0.0.1:$QM_TRADE_PORT
export ENGINE_SERVICE_URL=http://127.0.0.1:$QM_ENGINE_PORT
export AI_IDE_SERVICE_URL=http://127.0.0.1:$QM_ENGINE_PORT
export STREAM_SERVICE_URL=http://127.0.0.1:$QM_STREAM_PORT
export MARKET_DATA_SERVICE_URL=http://127.0.0.1:$QM_STREAM_PORT
export STRATEGY_SERVICE_URL=http://127.0.0.1:$QM_ENGINE_PORT
export PORTFOLIO_SERVICE_URL=http://127.0.0.1:$QM_TRADE_PORT
export USER_SERVICE_URL=http://127.0.0.1:$QM_TRADE_PORT
export REAL_TRADING_SERVICE_URL=http://127.0.0.1:$QM_TRADE_PORT
export ENABLE_TDX_PUSH=false ENABLE_REAL_TRADING=false DEBUG=false LOG_LEVEL=INFO
export STRATEGY_TEMPLATES_DIR="$ROOT/strategy_templates"
export QLIB_BACKTEST_RESULT_DIR="$STORAGE_ROOT/backtest_results"
export QLIB_BACKTEST_KERNELS=1
export QM_REPORTS_DIR="$STORAGE_ROOT/reports"
export TRADING_AGENTS_RESULTS_DIR="$STORAGE_ROOT/reports/trading_agents"
export QM_WEB_DIST_DIR="$ROOT/web"
export HF_HOME="$STORAGE_ROOT/hf" MPLCONFIGDIR="$ROOT/run/mpl"
export ENABLE_CRYPTO=false
export QM_QUANTDB_DATA_DIR="$STORAGE_ROOT/quantdb"
export QM_QUANTUS_DATA_DIR="$STORAGE_ROOT/quantus"
export QM_QUANTHK_DATA_DIR="$STORAGE_ROOT/quanthk"
export QM_QUANTBC_DATA_DIR="$STORAGE_ROOT/quantbc"
export QM_QUANTFUTURES_DATA_DIR="$STORAGE_ROOT/quantfutures"
export LLM_API_KEY="${LLM_API_KEY:-not-configured}"
export HUNTLY_USERNAME=admin HUNTLY_PASSWORD=admin123
export ADMIN_DASHBOARD_DISABLED_SERVICES=data_gateway,web,qwenpaw,rsshub,huntly

echo "[QuantMind] Checking PostgreSQL ..."
if [ ! -f "$ROOT/pgdata/PG_VERSION" ]; then
    echo "[QuantMind] First run: initializing PostgreSQL data dir ..."
    echo quantmind2026 > "$ROOT/run/pg_pw.txt"
    "$PGBIN/initdb" -D "$ROOT/pgdata" -U quantmind -A scram-sha-256 \
        --pwfile="$ROOT/run/pg_pw.txt" -E UTF8 --no-locale >/dev/null
    rm -f "$ROOT/run/pg_pw.txt"
fi
"$PYTHON" "$ROOT/pg_setup.py" wait --timeout 1 >/dev/null 2>&1 \
    || "$PGBIN/pg_ctl" -D "$ROOT/pgdata" -l "$ROOT/logs/postgres.log" \
        -o "-p $QM_PG_PORT -c listen_addresses=127.0.0.1" start >/dev/null
"$PYTHON" "$ROOT/pg_setup.py" wait --timeout 60 >/dev/null 2>&1 || {
    echo "[!] PostgreSQL failed to start, see logs/postgres.log"; read -r -p "Press enter to exit"; exit 1; }
"$PYTHON" "$ROOT/pg_setup.py" ensure-db >/dev/null 2>&1 || {
    echo "[!] ensure-db failed, see logs/postgres.log"; read -r -p "Press enter to exit"; exit 1; }
echo "[QuantMind] PostgreSQL ready"

echo "[QuantMind] Checking Redis ..."
if ! "$ROOT/redis/redis-cli" -p $QM_REDIS_PORT ping 2>/dev/null | grep -q PONG; then
    echo "[QuantMind] Starting Redis (port $QM_REDIS_PORT) ..."
    "$ROOT/redis/redis-server" --port $QM_REDIS_PORT --bind 127.0.0.1 \
        --dir "$ROOT/run" --daemonize yes --logfile "$ROOT/logs/redis.log"
fi
sleep 1
echo "[QuantMind] Redis ready"

echo "[QuantMind] Starting QuantMind backend ..."
nohup "$PYTHON" backend/main_oss.py > "$ROOT/logs/backend.log" 2>&1 &
nohup "$PYTHON" -m celery -A backend.services.engine.qlib_app.celery_config:celery_app \
    worker -Q qlib_backtest_srv --loglevel=info --concurrency=2 --pool=solo \
    > "$ROOT/logs/celery-worker.log" 2>&1 &
nohup "$PYTHON" -m celery -A backend.services.engine.qlib_app.celery_config:celery_app \
    beat --loglevel=info --schedule="$ROOT/run/celerybeat-schedule" \
    > "$ROOT/logs/celery-beat.log" 2>&1 &

echo "[QuantMind] Waiting for services (first run may take 1-2 min) ..."
TRY=0
until curl -fsS -m 2 http://127.0.0.1:$QM_API_PORT/health >/dev/null 2>&1; do
    TRY=$((TRY+1))
    [ $TRY -ge 180 ] && { echo "[!] Service start timeout, see logs/backend.log"; read -r -p "Press enter to exit"; exit 1; }
    sleep 1
done

echo "=============================================="
echo "[QuantMind] Ready: http://127.0.0.1:$QM_API_PORT/"
echo "=============================================="
open "http://127.0.0.1:$QM_API_PORT/"
echo "[QuantMind] Services run in background; stop with stop.command"
echo "[QuantMind] Logs: logs/startup.log (boot), logs/backend.log (service)"
echo "[QuantMind] You may close this window now."
read -r -p "Press enter to close"
