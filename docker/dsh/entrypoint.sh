#!/usr/bin/env bash
# QuantBot（dsh）容器启动编排：
#   ① 起 dsh web（硬绑 127.0.0.1:3081 —— dsh 官方拒绝 0.0.0.0，对外暴露由 nginx 承担）
#   ② 等 dsh 生成持久化签名密钥（$DSH_HOME/.credentials.yaml）
#   ③ 签发浏览器鉴权 cookie → nginx map include（mint_cookies.py，重启自动重签）
#   ④ 起 nginx 前门（0.0.0.0:8088 → 127.0.0.1:3081）
set -euo pipefail

DSH_HOME="${DSH_HOME:-/root/.dsh}"
DSH_WORKSPACE="${DSH_WORKSPACE:-/root/workspace}"
EXTERNAL_PORT="${DSH_EXTERNAL_PORT:-8088}"
NGINX_CONF_DIR=/etc/nginx/conf.d
COOKIE_MAP="${NGINX_CONF_DIR}/dsh-cookies.conf"

log() { echo "[dsh-entrypoint] $*"; }

mkdir -p "$DSH_HOME" "$DSH_WORKSPACE" "$NGINX_CONF_DIR" /var/log/nginx

# ① trusted-host 参数（DSH_TRUSTED_HOSTS：逗号/空格分隔的 host[:port]，需小写规范格式）
TRUST_ARGS=()
IFS=$' \t\n,' read -r -a _hosts <<< "${DSH_TRUSTED_HOSTS:-}" || true
for h in "${_hosts[@]}"; do
    [[ -n "$h" ]] && TRUST_ARGS+=(--trusted-host "$h")
done
log "trusted hosts: ${DSH_TRUSTED_HOSTS:-<none>}"

# nginx 站点配置每次启动从挂载目录刷新（改 docker/dsh/nginx.conf 后 restart 容器生效）
cp /app/dsh/nginx.conf "$NGINX_CONF_DIR/dsh.conf"

# 工作区指令文件（技能路由表/平台地址/术语映射）同步进持久化卷——
# persona 指引 dsh 读 $DSH_HOME/AGENTS.md；改 docker/dsh/AGENTS.md 后 restart 容器生效
cp /app/dsh/AGENTS.md "$DSH_HOME/AGENTS.md"

# dsh 插件集（仓库 docker/dsh/profile/ 为源）：清单或依赖缺失时自动安装。
# 首次/清单变更时经 pnpm 拉取（锁文件固定版本）；已装齐则跳过（约 0s）。
PROFILE_DIR="$DSH_HOME/profiles/web"
PROFILE_SRC=/app/dsh/profile
if [[ -f "$PROFILE_SRC/package.json" ]]; then
    mkdir -p "$PROFILE_DIR"
    _want=$(sha256sum "$PROFILE_SRC/package.json" | cut -d' ' -f1)
    _have=$(cat "$PROFILE_DIR/.manifest.sha" 2>/dev/null || true)
    if [[ "$_want" != "$_have" ]]; then
        log "同步 dsh 插件清单（$PROFILE_SRC/package.json）并安装…"
        cp "$PROFILE_SRC/package.json" "$PROFILE_DIR/package.json"
        [[ -f "$PROFILE_SRC/pnpm-lock.yaml" ]] && cp "$PROFILE_SRC/pnpm-lock.yaml" "$PROFILE_DIR/pnpm-lock.yaml"
        if (cd "$DSH_HOME" && dsh plugin --profile web install --frozen-lockfile); then
            echo "$_want" > "$PROFILE_DIR/.manifest.sha"
            log "插件安装完成"
        else
            log "警告：插件安装失败（网络？）——本次以现有依赖启动，下次重启自动重试"
        fi
    fi
fi

# ② 起 dsh web。cwd 必须无 .env（dsh 会读 cwd 的 .env 并拒绝其中的启动类键）；
#    工作区根 = 启动目录 → cd 到 $DSH_WORKSPACE，会话文件落在持久化卷里。
cd "$DSH_WORKSPACE"
dsh --profile web --patch /app/dsh/dsh.cordis.yml \
    --host 127.0.0.1 --port 3081 --no-open "${TRUST_ARGS[@]}" &
DSH_PID=$!

# ③ 等持久化签名密钥（首启由 dsh 初始化生成；卷持久化后立即就有）
for _ in $(seq 1 60); do
    [[ -f "$DSH_HOME/.credentials.yaml" ]] && break
    if ! kill -0 "$DSH_PID" 2>/dev/null; then
        log "错误：dsh 进程提前退出（看上方日志）"
        exit 1
    fi
    sleep 1
done

python3 /app/dsh/mint_cookies.py \
    --days 3600 --port "$EXTERNAL_PORT" \
    --authorities "${DSH_TRUSTED_HOSTS:-}" \
    --out "$COOKIE_MAP" \
  || log "警告：mint_cookies 执行异常（见上方输出）"

# 兜底：map 文件必须存在，否则 nginx 起不来（透传模式：浏览器需手动 token 交换）
if [[ ! -f "$COOKIE_MAP" ]]; then
    printf 'map $http_cookie $dsh_auth_cookie {\n    default $http_cookie;\n}\n' > "$COOKIE_MAP"
    log "警告：cookie map 缺失，已写透传兜底"
fi

# ④ 起 nginx 前门
nginx -g 'daemon off;' &
NGINX_PID=$!

log "启动完成：dsh pid=$DSH_PID, nginx pid=$NGINX_PID（对外 :8088）"

# 任一子进程退出 → 收掉另一个，容器按 restart 策略重启
trap 'log "收到终止信号"; kill -TERM "$DSH_PID" "$NGINX_PID" 2>/dev/null || true' TERM INT
wait -n "$DSH_PID" "$NGINX_PID" || true
log "子进程退出，容器退出"
kill -TERM "$DSH_PID" "$NGINX_PID" 2>/dev/null || true
wait 2>/dev/null || true
