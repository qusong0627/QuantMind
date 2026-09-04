#!/usr/bin/env bash
# ============================================================
# QuantMind 便携版 一键启动（Linux / WSL）
# 用法:
#   bash start.sh          前台启动，Ctrl+C 停止全部服务
#   bash start.sh --bg     后台启动，用 bash stop.sh 停止
# 可选配置: 编辑同目录 pack.env（端口、数据目录等）
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

UNAME_S="$(uname -s)"
UNAME_M="$(uname -m)"

# ── 平台检查 ────────────────────────────────────────────────
if [ "$UNAME_S" = "Darwin" ]; then
    echo "[!] 本包是 Linux x86_64 便携版，macOS 无法直接运行。"
    echo "    macOS 请使用 Mac 版便携包，或改用 Docker 部署。"
    exit 1
fi
if [ "$UNAME_S" != "Linux" ]; then
    echo "[!] 未识别的平台: $UNAME_S。Windows 请直接双击 start.bat（或在 WSL 里运行本脚本）。"
    exit 1
fi
if [ "$UNAME_M" != "x86_64" ]; then
    echo "[!] 本包仅支持 x86_64 架构，当前为 $UNAME_M。"
    exit 1
fi
if [ "$(id -u)" = "0" ]; then
    echo "[!] 请勿用 root 运行（PostgreSQL 拒绝以 root 身份运行）。"
    echo "    请用普通用户执行: bash start.sh"
    exit 1
fi

# ── 可选用户配置 ────────────────────────────────────────────
if [ -f "$ROOT/pack.env" ]; then
    # shellcheck disable=SC1091
    set -a; source "$ROOT/pack.env"; set +a
fi

QM_PG_PORT="${QM_PG_PORT:-5432}"
QM_REDIS_PORT="${QM_REDIS_PORT:-6379}"
QM_API_PORT="${QM_API_PORT:-8000}"
QM_ENGINE_PORT="${QM_ENGINE_PORT:-8001}"
QM_TRADE_PORT="${QM_TRADE_PORT:-8002}"
QM_STREAM_PORT="${QM_STREAM_PORT:-8003}"
STORAGE_ROOT="$ROOT/data"

# ── 运行目录 ────────────────────────────────────────────────
mkdir -p "$ROOT/logs" "$ROOT/run" "$STORAGE_ROOT"
mkdir -p "$STORAGE_ROOT"/{models,uploads,strategies,reports,backtest_results,hf,qlib_data}
mkdir -p "$STORAGE_ROOT"/{quantdb,quantus,quanthk,quantbc,quantfutures}

# ── 随机密钥（首次生成，之后复用）────────────────────────────
SECRETS="$ROOT/run/secrets.env"
if [ ! -f "$SECRETS" ]; then
    {
        echo "SECRET_KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
        echo "JWT_SECRET_KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
        echo "INTERNAL_CALL_SECRET=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    } > "$SECRETS"
    chmod 600 "$SECRETS"
fi
# shellcheck disable=SC1090
source "$SECRETS"

# ── 后端环境变量（与 docker-compose 保持一致）────────────────
export APP_EDITION=oss
export APP_ENV=production
export SERVICE_MODE=all
export TZ="${TZ:-Asia/Shanghai}"
export PYTHONPATH="$ROOT"
export SECRET_KEY JWT_SECRET_KEY INTERNAL_CALL_SECRET
export DB_DRIVER=asyncpg
export DB_HOST=127.0.0.1
export DB_PORT="$QM_PG_PORT"
export DB_NAME="${DB_NAME:-quantmind}"
export DB_USER="${DB_USER:-quantmind}"
export DB_PASSWORD="${DB_PASSWORD:-quantmind2026}"
export DATABASE_URL="postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@127.0.0.1:${QM_PG_PORT}/${DB_NAME}"
export POSTGRES_HOST=127.0.0.1
export POSTGRES_PORT="$QM_PG_PORT"
export POSTGRES_USER="$DB_USER"
export POSTGRES_PASSWORD="$DB_PASSWORD"
export POSTGRES_DB="$DB_NAME"
export REDIS_HOST=127.0.0.1
export REDIS_PORT="$QM_REDIS_PORT"
export REDIS_PASSWORD="${REDIS_PASSWORD:-}"
export STORAGE_MODE=local
export STORAGE_ROOT
export MARGIN_STOCK_POOL_PATH="$STORAGE_ROOT/融资融券.json"
export QUANTMIND_ENABLE_WEB_UPDATE=false
export API_PORT="$QM_API_PORT"
export ENGINE_PORT="$QM_ENGINE_PORT"
export TRADE_PORT="$QM_TRADE_PORT"
export STREAM_PORT="$QM_STREAM_PORT"
export API_WORKERS=1 ENGINE_WORKERS=1 TRADE_WORKERS=1 STREAM_WORKERS=1
export TRADE_SERVICE_URL="http://127.0.0.1:${QM_TRADE_PORT}"
export ENGINE_SERVICE_URL="http://127.0.0.1:${QM_ENGINE_PORT}"
export AI_IDE_SERVICE_URL="http://127.0.0.1:${QM_ENGINE_PORT}"
export STREAM_SERVICE_URL="http://127.0.0.1:${QM_STREAM_PORT}"
export MARKET_DATA_SERVICE_URL="http://127.0.0.1:${QM_STREAM_PORT}"
export STRATEGY_SERVICE_URL="http://127.0.0.1:${QM_ENGINE_PORT}"
export PORTFOLIO_SERVICE_URL="http://127.0.0.1:${QM_TRADE_PORT}"
export USER_SERVICE_URL="http://127.0.0.1:${QM_TRADE_PORT}"
export REAL_TRADING_SERVICE_URL="http://127.0.0.1:${QM_TRADE_PORT}"
export ENABLE_TDX_PUSH="${ENABLE_TDX_PUSH:-false}"
export ENABLE_REAL_TRADING="${ENABLE_REAL_TRADING:-false}"
export DEBUG=false
export LOG_LEVEL=INFO
export STRATEGY_TEMPLATES_DIR="$ROOT/strategy_templates"
export QLIB_BACKTEST_RESULT_DIR="$STORAGE_ROOT/backtest_results"
export QLIB_BACKTEST_KERNELS=1
export QM_REPORTS_DIR="$STORAGE_ROOT/reports"
export TRADING_AGENTS_RESULTS_DIR="$STORAGE_ROOT/reports/trading_agents"
export QM_WEB_DIST_DIR="$ROOT/web"
export HF_HOME="$STORAGE_ROOT/hf"
export MPLCONFIGDIR="$ROOT/run/mpl"
export ENABLE_CRYPTO="${ENABLE_CRYPTO:-false}"
export QM_QUANTDB_DATA_DIR="$STORAGE_ROOT/quantdb"
export QM_QUANTUS_DATA_DIR="$STORAGE_ROOT/quantus"
export QM_QUANTHK_DATA_DIR="$STORAGE_ROOT/quanthk"
export QM_QUANTBC_DATA_DIR="$STORAGE_ROOT/quantbc"
export QM_QUANTFUTURES_DATA_DIR="$STORAGE_ROOT/quantfutures"
# AI 功能未配置密钥时用占位值放行启动（真实密钥写 pack.env，重启生效）
export LLM_API_KEY="${LLM_API_KEY:-not-configured}"
# 后台管理健康面板探测目标 → 便携包真实地址（Docker 版的默认值是容器名/固定端口）
export ADMIN_DASHBOARD_API_HEALTH_URL="http://127.0.0.1:${QM_API_PORT}/health"
export ADMIN_DASHBOARD_ENGINE_HEALTH_URL="http://127.0.0.1:${QM_ENGINE_PORT}/health"
export ADMIN_DASHBOARD_TRADE_HEALTH_URL="http://127.0.0.1:${QM_TRADE_PORT}/health"
export ADMIN_DASHBOARD_STREAM_HEALTH_URL="http://127.0.0.1:${QM_STREAM_PORT}/health"
export ADMIN_DASHBOARD_DB_HOST="127.0.0.1"
export ADMIN_DASHBOARD_DB_PORT="$QM_PG_PORT"
export ADMIN_DASHBOARD_REDIS_HOST="127.0.0.1"
export ADMIN_DASHBOARD_REDIS_PORT="$QM_REDIS_PORT"
# Docker 专属组件便携版没有，从面板剔除避免误报（qwenpaw/huntly 若在包内会被启动）
export ADMIN_DASHBOARD_DISABLED_SERVICES="${ADMIN_DASHBOARD_DISABLED_SERVICES:-data_gateway,web,rsshub}"
export ADMIN_DASHBOARD_QWENPAW_HOST="127.0.0.1"
export ADMIN_DASHBOARD_QWENPAW_PORT="${QM_QWENPAW_PORT:-8088}"
export ADMIN_DASHBOARD_HUNTLY_HOST="127.0.0.1"
export ADMIN_DASHBOARD_HUNTLY_PORT="${QM_HUNTLY_PORT:-8090}"
export REDIS_URL="redis://127.0.0.1:${QM_REDIS_PORT}"
mkdir -p "$MPLCONFIGDIR"

PYTHON="$ROOT/runtime/python/bin/python3"
PGBIN="$ROOT/pgsql/bin"
export LD_LIBRARY_PATH="$ROOT/pgsql/lib:${LD_LIBRARY_PATH:-}"

log()  { echo -e "\033[36m[QuantMind]\033[0m $*"; }
ok()   { echo -e "\033[32m[QuantMind]\033[0m $*"; }
fail() { echo -e "\033[31m[QuantMind]\033[0m $*" >&2; exit 1; }

# ── 1. PostgreSQL ───────────────────────────────────────────
# 便携二进制只含 initdb/pg_ctl/postgres，就绪探测与建库走 pg_setup.py（psycopg2）
if [ ! -f "$ROOT/pgdata/PG_VERSION" ]; then
    log "首次运行：初始化 PostgreSQL 数据目录 ..."
    echo "$DB_PASSWORD" > "$ROOT/run/pg_pw.txt"
    "$PGBIN/initdb" -D "$ROOT/pgdata" -U "$DB_USER" \
        -A scram-sha-256 --pwfile="$ROOT/run/pg_pw.txt" \
        -E UTF8 --locale=C >/dev/null
    rm -f "$ROOT/run/pg_pw.txt"
fi

if "$PYTHON" "$ROOT/pg_setup.py" wait --timeout 1 2>/dev/null; then
    log "PostgreSQL 已在运行（端口 $QM_PG_PORT），跳过启动"
else
    log "启动 PostgreSQL (端口 $QM_PG_PORT) ..."
    "$PGBIN/pg_ctl" -D "$ROOT/pgdata" -l "$ROOT/logs/postgres.log" \
        -o "-p $QM_PG_PORT -c listen_addresses=127.0.0.1 -k $ROOT/run" start >/dev/null
fi
if ! "$PYTHON" "$ROOT/pg_setup.py" wait --timeout 60 2>/dev/null; then
    fail "PostgreSQL 启动失败（端口 $QM_PG_PORT 可能被占用），见 logs/postgres.log"
fi
"$PYTHON" "$ROOT/pg_setup.py" ensure-db || fail "创建数据库 $DB_NAME 失败"
ok "PostgreSQL 就绪"

# ── 2. Redis ────────────────────────────────────────────────
if "$ROOT/redis/redis-cli" -h 127.0.0.1 -p "$QM_REDIS_PORT" ping 2>/dev/null | grep -q PONG; then
    log "Redis 已在运行（端口 $QM_REDIS_PORT），跳过启动"
else
    log "启动 Redis (端口 $QM_REDIS_PORT) ..."
    "$ROOT/redis/redis-server" \
        --port "$QM_REDIS_PORT" --bind 127.0.0.1 \
        --dir "$ROOT/run" --pidfile "$ROOT/run/redis.pid" \
        --logfile "$ROOT/logs/redis.log" --daemonize yes
    for _ in $(seq 1 15); do
        "$ROOT/redis/redis-cli" -h 127.0.0.1 -p "$QM_REDIS_PORT" ping 2>/dev/null | grep -q PONG && break
        sleep 1
    done
fi
"$ROOT/redis/redis-cli" -h 127.0.0.1 -p "$QM_REDIS_PORT" ping 2>/dev/null | grep -q PONG \
    || fail "Redis 启动失败，见 logs/redis.log"
ok "Redis 就绪"

# ── 2b. Huntly（新闻聚合，内嵌 JRE，可选组件）────────────────
HUNTLY_PID=""
HUNTLY_PORT="${QM_HUNTLY_PORT:-8090}"
export HUNTLY_BASE_URL="http://127.0.0.1:${HUNTLY_PORT}"
if [ -x "$ROOT/huntly/jre/bin/java" ] && [ -f "$ROOT/huntly/server.jar" ]; then
    if curl -fsS -m 2 "http://127.0.0.1:${HUNTLY_PORT}/" >/dev/null 2>&1; then
        log "Huntly 已在运行（端口 $HUNTLY_PORT），跳过启动"
    else
        log "启动 Huntly (端口 $HUNTLY_PORT，首次启动初始化约 30 秒) ..."
        mkdir -p "$STORAGE_ROOT/huntly"
        setsid "$ROOT/huntly/jre/bin/java" -Xmx512m -jar "$ROOT/huntly/server.jar" \
            --huntly.dataDir="$STORAGE_ROOT/huntly/" --server.port="$HUNTLY_PORT" \
            > "$ROOT/logs/huntly.log" 2>&1 &
        HUNTLY_PID=$!
        [ "$BG" = "1" ] && echo "$HUNTLY_PID" > "$ROOT/run/huntly.pid"
    fi
else
    log "Huntly 未包含在包内，新闻聚合功能降级"
fi

# ── 2c. QwenPaw（AI 助手，独立 Python 3.11 运行时，可选组件）──
QWENPAW_PID=""
QWENPAW_PORT="${QM_QWENPAW_PORT:-8088}"
export QWENPAW_BASE_URL="http://127.0.0.1:${QWENPAW_PORT}"
export QWENPAW_SHARED_FILES_DIR="${QWENPAW_SHARED_FILES_DIR:-$STORAGE_ROOT/qwenpaw-shared}"
export QWENPAW_SHARED_VISIBLE_DIR="${QWENPAW_SHARED_VISIBLE_DIR:-$STORAGE_ROOT/qwenpaw-working}"
mkdir -p "$QWENPAW_SHARED_FILES_DIR" "$QWENPAW_SHARED_VISIBLE_DIR" "$STORAGE_ROOT/qwenpaw-home"
if [ -x "$ROOT/qwenpaw_runtime/python/bin/python3" ]; then
    if curl -fsS -m 2 "http://127.0.0.1:${QWENPAW_PORT}/" >/dev/null 2>&1; then
        log "QwenPaw 已在运行（端口 $QWENPAW_PORT），跳过启动"
    else
        log "启动 QwenPaw (端口 $QWENPAW_PORT) ..."
        setsid env HOME="$STORAGE_ROOT/qwenpaw-home" \
            "$ROOT/qwenpaw_runtime/python/bin/python3" -m qwenpaw app \
            --host 127.0.0.1 --port "$QWENPAW_PORT" \
            > "$ROOT/logs/qwenpaw.log" 2>&1 &
        QWENPAW_PID=$!
        [ "$BG" = "1" ] && echo "$QWENPAW_PID" > "$ROOT/run/qwenpaw.pid"
    fi
else
    log "QwenPaw 未包含在包内，AI 助手功能降级"
fi

# ── 启动后端（前台模式等待就绪；后台模式直接放行）────────────
BG=0
[ "${1:-}" = "--bg" ] && BG=1

BACKEND_PID=""
CELERY_WORKER_PID=""
CELERY_BEAT_PID=""

start_backend() {
    log "启动 QuantMind 后端 (api:$QM_API_PORT engine:$QM_ENGINE_PORT trade:$QM_TRADE_PORT stream:$QM_STREAM_PORT) ..."
    # setsid 独立进程组：停止时连 main_oss 的 multiprocessing 子进程一并组杀，避免孤儿进程占端口
    if [ "$BG" = "1" ]; then
        setsid "$PYTHON" backend/main_oss.py > "$ROOT/logs/backend.log" 2>&1 &
        echo $! > "$ROOT/run/backend.pid"
    else
        setsid "$PYTHON" backend/main_oss.py > "$ROOT/logs/backend.log" 2>&1 &
        BACKEND_PID=$!
    fi
}

start_celery() {
    log "启动 Celery (回测队列 qlib_backtest_srv) ..."
    if [ "$BG" = "1" ]; then
        setsid "$PYTHON" -m celery -A backend.services.engine.qlib_app.celery_config:celery_app \
            worker -Q qlib_backtest_srv --loglevel=info --concurrency=2 \
            > "$ROOT/logs/celery-worker.log" 2>&1 &
        echo $! > "$ROOT/run/celery-worker.pid"
        setsid "$PYTHON" -m celery -A backend.services.engine.qlib_app.celery_config:celery_app \
            beat --loglevel=info --schedule="$ROOT/run/celerybeat-schedule" \
            > "$ROOT/logs/celery-beat.log" 2>&1 &
        echo $! > "$ROOT/run/celery-beat.pid"
    else
        setsid "$PYTHON" -m celery -A backend.services.engine.qlib_app.celery_config:celery_app \
            worker -Q qlib_backtest_srv --loglevel=info --concurrency=2 \
            > "$ROOT/logs/celery-worker.log" 2>&1 &
        CELERY_WORKER_PID=$!
        setsid "$PYTHON" -m celery -A backend.services.engine.qlib_app.celery_config:celery_app \
            beat --loglevel=info --schedule="$ROOT/run/celerybeat-schedule" \
            > "$ROOT/logs/celery-beat.log" 2>&1 &
        CELERY_BEAT_PID=$!
    fi
}

kill_group() {
    local pid="$1"
    [ -z "$pid" ] && return 0
    kill -0 "$pid" 2>/dev/null || return 0
    kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null
}

cleanup() {
    log "正在停止全部服务 ..."
    kill_group "$QWENPAW_PID"
    kill_group "$HUNTLY_PID"
    kill_group "$CELERY_BEAT_PID"
    kill_group "$CELERY_WORKER_PID"
    kill_group "$BACKEND_PID"
    sleep 2
    "$ROOT/redis/redis-cli" -h 127.0.0.1 -p "$QM_REDIS_PORT" shutdown nosave 2>/dev/null || true
    "$PGBIN/pg_ctl" -D "$ROOT/pgdata" stop -m fast 2>/dev/null || true
    ok "已全部停止"
}
trap cleanup EXIT INT TERM

start_backend
start_celery

# ── 就绪等待 + 打开浏览器 ────────────────────────────────────
log "等待服务就绪（首次启动需初始化数据库，可能需要 1-2 分钟）..."
READY=0
for _ in $(seq 1 180); do
    if curl -fsS "http://127.0.0.1:${QM_API_PORT}/health" >/dev/null 2>&1; then READY=1; break; fi
    sleep 1
done

if [ "$READY" = "1" ]; then
    ok "=============================================="
    ok "QuantMind 已启动:  http://127.0.0.1:${QM_API_PORT}/"
    ok "=============================================="
    if [ "${QM_OPEN_BROWSER:-1}" = "1" ]; then
        URL="http://127.0.0.1:${QM_API_PORT}/"
        if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" 2>/dev/null || true
        elif command -v open >/dev/null 2>&1; then open "$URL" 2>/dev/null || true
        fi
    fi
else
    fail "服务启动超时，请查看 logs/backend.log"
fi

if [ "$BG" = "1" ]; then
    ok "后台模式: 服务已在后台运行，用 bash stop.sh 停止；日志在 logs/ 目录"
    trap - EXIT INT TERM
    exit 0
fi

# 前台监控：Ctrl+C 停止全部
log "按 Ctrl+C 停止全部服务。日志: logs/backend.log"
tail -f "$ROOT/logs/backend.log" &
TAIL_PID=$!
wait "$BACKEND_PID" 2>/dev/null || true
kill "$TAIL_PID" 2>/dev/null || true
