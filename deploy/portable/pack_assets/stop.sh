#!/usr/bin/env bash
# QuantMind 便携版 停止脚本（配合 bash start.sh --bg 使用）
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QM_PG_PORT="${QM_PG_PORT:-5432}"
QM_REDIS_PORT="${QM_REDIS_PORT:-6379}"

stop_pidfile() {
    local f="$1" name="$2"
    if [ -f "$f" ]; then
        local pid; pid="$(cat "$f")"
        if kill -0 "$pid" 2>/dev/null; then
            echo "[stop] 停止 $name (pid $pid，含子进程) ..."
            # setsid 启动的进程组：组杀连 multiprocessing 子进程一起停止
            kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null
        fi
        rm -f "$f"
    fi
}

stop_pidfile "$ROOT/run/celery-beat.pid"   "Celery beat"
stop_pidfile "$ROOT/run/celery-worker.pid" "Celery worker"
stop_pidfile "$ROOT/run/backend.pid"       "后端服务"

sleep 2
"$ROOT/redis/redis-cli" -h 127.0.0.1 -p "$QM_REDIS_PORT" shutdown nosave 2>/dev/null \
    && echo "[stop] Redis 已停止" || echo "[stop] Redis 未在运行"
# 兜底：shutdown 未生效时按 pidfile 击杀
if [ -f "$ROOT/run/redis.pid" ]; then
    rpid="$(cat "$ROOT/run/redis.pid")"
    if kill -0 "$rpid" 2>/dev/null; then kill "$rpid" 2>/dev/null; fi
    rm -f "$ROOT/run/redis.pid"
fi
if [ -f "$ROOT/pgdata/PG_VERSION" ]; then
    "$ROOT/pgsql/bin/pg_ctl" -D "$ROOT/pgdata" stop -m fast 2>/dev/null \
        && echo "[stop] PostgreSQL 已停止" || echo "[stop] PostgreSQL 未在运行"
fi
echo "[stop] 完成"
