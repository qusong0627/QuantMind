#!/usr/bin/env bash
# QuantMind Portable Stop (macOS) - stops celery/backend/redis/postgres
cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"

echo "[stop] Stopping QuantMind services ..."
pkill -f "celery.*qlib_backtest_srv" 2>/dev/null
pkill -f "celery.*celery_app beat" 2>/dev/null
pkill -f "backend.main_oss" 2>/dev/null
pkill -f "celery_app worker" 2>/dev/null

[ -x "$ROOT/redis/redis-cli" ] && "$ROOT/redis/redis-cli" -p 6379 shutdown nosave 2>/dev/null

if [ -f "$ROOT/pgdata/PG_VERSION" ] && [ -x "$ROOT/pgsql/bin/pg_ctl" ]; then
    "$ROOT/pgsql/bin/pg_ctl" -D "$ROOT/pgdata" stop -m fast 2>/dev/null \
        && echo "[stop] PostgreSQL stopped"
fi
echo "[stop] Done"
read -r -p "Press enter to close"
