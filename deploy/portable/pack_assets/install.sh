#!/usr/bin/env bash
# ============================================================
# QuantMind Linux 便携版一键安装/启动
#
#   bash install.sh              # 体检 + 生成 pack.env + 后台启动
#   bash install.sh --foreground # 体检 + 前台启动（Ctrl+C 停止）
#   bash install.sh --no-start   # 只体检并生成 pack.env
#   bash install.sh --help
#
# 退出码: 0 正常 / 1 有阻塞项 / 2 包不完整
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

NO_START=0
FOREGROUND=0
for arg in "$@"; do
    case "$arg" in
        --no-start) NO_START=1 ;;
        --foreground) FOREGROUND=1 ;;
        --help|-h)
            sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "未知参数: $arg（--help 查看用法）" >&2; exit 1 ;;
    esac
done

log()  { printf '\033[36m[QuantMind]\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m[QuantMind]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[QuantMind]\033[0m %s\n' "$*" >&2; exit "${2:-1}"; }

# ── 平台与权限 ──────────────────────────────────────────────
[ "$(uname -s)" = "Linux" ] || fail "本包只支持 Linux x86_64（当前 $(uname -s)）。"
[ "$(uname -m)" = "x86_64" ] || fail "本包只支持 x86_64（当前 $(uname -m)）。"
if [ "$(id -u)" = "0" ]; then
    fail "请勿用 root 运行（PostgreSQL 拒绝以 root 身份运行）。请用普通用户执行 bash install.sh。"
fi

# ── 包完整性 ────────────────────────────────────────────────
PY="$ROOT/runtime/python/bin/python3"
MISSING=()
for p in \
    "$PY" \
    "$ROOT/backend/main_oss.py" \
    "$ROOT/web/index.html" \
    "$ROOT/pgsql/bin/initdb" \
    "$ROOT/redis/redis-server" \
    "$ROOT/pg_setup.py" \
    "$ROOT/start.sh"; do
    [ -e "$p" ] || MISSING+=("${p#$ROOT/}")
done
if [ "${#MISSING[@]}" -gt 0 ]; then
    printf '[QuantMind] 包不完整，缺少:\n' >&2
    printf '  - %s\n' "${MISSING[@]}" >&2
    exit 2
fi
[ -x "$PY" ] || fail "runtime Python 不可执行: $PY" 2

# ── 磁盘 ────────────────────────────────────────────────────
AVAIL_KB="$(df -Pk "$ROOT" | awk 'NR==2 {print $4}')"
if [ "${AVAIL_KB:-0}" -lt 15728640 ]; then
    fail "磁盘剩余不足 15GB（当前 $(( ${AVAIL_KB:-0} / 1024 / 1024 ))GB）。解压后约 7.3GB，行情数据建议另备 60GB。"
fi

http_ok() {
    "$PY" -S - "$1" >/dev/null 2>&1 <<'PY'
import sys
import urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as resp:
        raise SystemExit(0 if resp.status < 400 else 1)
except Exception:
    raise SystemExit(1)
PY
}

ports_free() {
    "$PY" -S - "$@" >/dev/null 2>&1 <<'PY'
import socket
import sys
bad = []
for item in sys.argv[1:]:
    host, port = item.rsplit(":", 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, int(port)))
    except OSError:
        bad.append(port)
    finally:
        sock.close()
raise SystemExit(0 if not bad else 1)
PY
}

# ── pack.env：不存在则生成 ─────────────────────────────────
NEW_ENV=0
if [ ! -f "$ROOT/pack.env" ]; then
    NEW_ENV=1
    PG_PORT=5432
    REDIS_PORT=6379
    API_PORT=8000
    ENGINE_PORT=8001
    TRADE_PORT=8002
    STREAM_PORT=8003
    HUNTLY_PORT=18090
    # dsh（QuantBot AI 面）固定 18088：前端构建期按该端口内联，
    # 不能跟着“默认端口空闲与否”漂移到 8088。
    DSH_PORT=18088
    if ! ports_free "127.0.0.1:$PG_PORT" "127.0.0.1:$REDIS_PORT" \
        "0.0.0.0:$API_PORT" "0.0.0.0:$ENGINE_PORT" \
        "0.0.0.0:$TRADE_PORT" "0.0.0.0:$STREAM_PORT" \
        "127.0.0.1:$HUNTLY_PORT" "0.0.0.0:$DSH_PORT"; then
        # 默认端口被占（Docker 栈/其他服务）：整组切到 18000 段，避免和本机实例冲突。
        PG_PORT=15432
        REDIS_PORT=16379
        API_PORT=18000
        ENGINE_PORT=18001
        TRADE_PORT=18002
        STREAM_PORT=18003
        HUNTLY_PORT=18090
        DSH_PORT=18088
        ports_free "127.0.0.1:$PG_PORT" "127.0.0.1:$REDIS_PORT" \
            "0.0.0.0:$API_PORT" "0.0.0.0:$ENGINE_PORT" \
            "0.0.0.0:$TRADE_PORT" "0.0.0.0:$STREAM_PORT" \
            "127.0.0.1:$HUNTLY_PORT" "0.0.0.0:$DSH_PORT" \
            || fail "默认端口和 18000 段都被占用，请复制 pack.env.example 为 pack.env 后手动改端口。"
    fi
    OPEN_BROWSER=0
    if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then OPEN_BROWSER=1; fi
    DB_PASSWORD="$("$PY" -S -c 'import secrets; print(secrets.token_hex(16))')"
    cat > "$ROOT/pack.env" <<EOF
# 由 install.sh 首次生成；可手工修改后重启生效。
QM_PG_PORT=$PG_PORT
QM_REDIS_PORT=$REDIS_PORT
QM_API_PORT=$API_PORT
QM_ENGINE_PORT=$ENGINE_PORT
QM_TRADE_PORT=$TRADE_PORT
QM_STREAM_PORT=$STREAM_PORT
QM_HUNTLY_PORT=$HUNTLY_PORT
QM_DSH_PORT=$DSH_PORT
QM_OPEN_BROWSER=$OPEN_BROWSER
DB_PASSWORD=$DB_PASSWORD
EOF
    chmod 600 "$ROOT/pack.env"
    ok "已生成 pack.env（端口 $API_PORT，数据库口令已随机生成）"
fi

# shellcheck disable=SC1091
set -a; . "$ROOT/pack.env"; set +a
API_PORT="${QM_API_PORT:-8000}"
PG_PORT="${QM_PG_PORT:-5432}"
REDIS_PORT="${QM_REDIS_PORT:-6379}"
HUNTLY_PORT="${QM_HUNTLY_PORT:-18090}"
DSH_PORT="${QM_DSH_PORT:-18088}"

# 已在运行：先判活，再判端口。否则用户第二次运行 install.sh 会被
# 自己实例占用的端口误报成「端口冲突」。
if http_ok "http://127.0.0.1:${API_PORT}/health"; then
    ok "QuantMind 已在运行: http://127.0.0.1:${API_PORT}/"
    exit 0
fi

if [ "$NEW_ENV" = "0" ]; then
    ports_free "127.0.0.1:$PG_PORT" "127.0.0.1:$REDIS_PORT" \
        "0.0.0.0:${QM_API_PORT:-8000}" "0.0.0.0:${QM_ENGINE_PORT:-8001}" \
        "0.0.0.0:${QM_TRADE_PORT:-8002}" "0.0.0.0:${QM_STREAM_PORT:-8003}" \
        "127.0.0.1:$HUNTLY_PORT" "0.0.0.0:$DSH_PORT" \
        || fail "pack.env 里的端口已被占用，请修改 port 后重试（ss -ltnp 查看占用）。"
fi

if [ "$NO_START" = "1" ]; then
    ok "体检通过；未启动。执行 bash start.sh --bg 或 bash install.sh 启动。"
    exit 0
fi

# ── 启动 ────────────────────────────────────────────────────
log "首次启动需要初始化 PostgreSQL/Redis/数据库，约 1-3 分钟 ..."
if [ "$FOREGROUND" = "1" ]; then
    exec bash "$ROOT/start.sh"
fi
if ! bash "$ROOT/start.sh" --bg; then
    echo "启动失败，最近日志：" >&2
    for f in backend.log postgres.log redis.log celery-worker.log; do
        [ -f "$ROOT/logs/$f" ] && { echo "--- logs/$f ---" >&2; tail -40 "$ROOT/logs/$f" >&2; }
    done
    exit 1
fi

if http_ok "http://127.0.0.1:${API_PORT}/health"; then
    ok "=============================================="
    ok "QuantMind 已启动: http://127.0.0.1:${API_PORT}/"
    ok "默认账号: admin / admin123（首次登录后请修改）"
    ok "停止: bash stop.sh    日志: logs/"
    ok "=============================================="
    exit 0
fi
fail "启动脚本返回成功但健康检查未通过，请查看 logs/backend.log"
