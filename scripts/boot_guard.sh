#!/usr/bin/env bash
# boot_guard.sh — 主机重启后自动补拉 QuantMind 后端核心容器（宿主 cron @reboot 调用；幂等，可手动重跑）
#
# 背景（2026-10-11 事故）：主机重启存在两个启动竞态，且 restart=unless-stopped 对
# 「daemon 启动期 start 失败」不重试，容器会一直躺到人工介入：
#   1) quantmind-db 绑 QM_DB_BIND（LAN IP）时网卡尚未就绪 → start 失败；
#      且该失败会留下 daemon 半状态：容器 Running/healthy 但无网络端点
#      （Networks={}、容器内只有 lo、容器名不可解析）——docker restart 修不回，
#      必须 docker network connect 在线补挂（2026-10-11 实测）。
#   2) quantmind 主容器 bind 挂载 /mnt/tdx-shared（CIFS automount）时网络未就绪
#      → 挂载失败 → 容器起不来（automount 稍后自愈，但没人补拉容器）。
# 另有一个二级坑：db 不可达时主容器启动会「降级跳过」自动建表 / admin seed /
# trade 库初始化（进程期内不重试），因此本脚本先起 db 并等 healthy 才起主容器；
# 若发现主容器处于「运行但无网络」则重启一次拿干净初始化。
#
# 只动四个核心容器（db / 主容器 / celery 两兄弟），不碰其它容器；其余容器靠
# 各自的 restart 策略自愈。日志：logs/boot_guard.log。

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

PROJECT_DIR=/home/zbox/projects/quantmind
ENV_FILE="$PROJECT_DIR/.env"
LOG_FILE="$PROJECT_DIR/logs/boot_guard.log"

DB_CONTAINER=quantmind-db
MAIN_CONTAINER=quantmind
SIDE_CONTAINERS="quantmind-celery quantmind-celery-beat"

DOCKER_WAIT=240       # 等 dockerd
IP_WAIT=300           # 等 QM_DB_BIND 对应的网卡 IP
DB_HEALTH_WAIT=150    # 等 db healthy
MOUNT_WAIT=600        # 等 autofs（CIFS）挂载
MAIN_HEALTH_WAIT=180  # 等主容器 healthy
MAIN_START_RETRIES=5  # 主容器启动失败重试次数（间隔 60s）

ISSUES=0

mkdir -p "$(dirname "$LOG_FILE")"
exec > >(tee -a "$LOG_FILE") 2>&1

log()  { printf '%s %s\n' "$(date '+%F %T')" "$*"; }
ok()   { log "✓ $*"; }
bad()  { log "✗ $*"; ISSUES=$((ISSUES + 1)); }
info() { log "· $*"; }

# 单实例锁：@reboot 与手动重跑并发时，后到者直接退出
exec 200>/tmp/quantmind_boot_guard.lock
if ! flock -n 200; then
  log "另一个 boot_guard 实例在运行，退出"
  exit 0
fi

log "===== boot_guard 启动 ====="

# ---------- 基础谓词 ----------
state_of()    { docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null; }
health_of()   { docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$1" 2>/dev/null; }
endpoint_of() { docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$1" 2>/dev/null; }
is_healthy()  { [ "$(health_of "$1")" = "healthy" ]; }

env_get() { # env_get KEY [default]
  local v
  v=$(grep -E "^${1}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- \
      | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
  printf '%s' "${v:-${2:-}}"
}

# wait_until <超时秒> <描述> <命令...>
wait_until() {
  local timeout=$1 desc=$2
  shift 2
  local deadline=$(( $(date +%s) + timeout ))
  until "$@" >/dev/null 2>&1; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
      bad "等待超时（${timeout}s）：$desc"
      return 1
    fi
    sleep 5
  done
  ok "$desc"
}

# 半状态修复：端点缺失则补挂；若补挂前容器在运行（曾无网络启动），重启一次拿干净初始化
ensure_endpoint() { # ensure_endpoint <容器> <DNS 别名>
  local name=$1 dns_alias=$2 net
  if [ -n "$(endpoint_of "$name")" ]; then
    info "$name 网络端点正常"
    return 0
  fi
  net=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$name" 2>/dev/null)
  if [ -z "$net" ]; then
    bad "$name 无法确定网络（NetworkMode 为空）"
    return 1
  fi
  info "$name 处于无网络端点半状态，补挂到 $net（--alias $dns_alias）"
  docker network connect --alias "$dns_alias" "$net" "$name" >/dev/null 2>&1
  if [ -z "$(endpoint_of "$name")" ]; then
    bad "$name 补挂后仍无端点，需人工处理"
    return 1
  fi
  ok "$name 端点已补挂（$(endpoint_of "$name")）"
  if [ "$(state_of "$name")" = "running" ]; then
    info "$name 曾以无网络状态运行（降级初始化残留）——重启一次"
    docker restart "$name" >/dev/null 2>&1
    sleep 2
    if [ -z "$(endpoint_of "$name")" ]; then
      ensure_endpoint "$name" "$dns_alias" || return 1
    fi
  fi
  return 0
}

# ---------- 0) dockerd ----------
wait_until "$DOCKER_WAIT" "dockerd 就绪" docker info \
  || { log "===== 中止：dockerd 不可用 ====="; exit 1; }

DB_BIND=$(env_get QM_DB_BIND 127.0.0.1)
DB_HOST=$(env_get DB_HOST db)

# ---------- 1) 等 QM_DB_BIND 对应网卡 IP（db 端口发布的前置） ----------
case "$DB_BIND" in
  127.0.0.1|0.0.0.0)
    info "QM_DB_BIND=$DB_BIND，无需等网卡" ;;
  *)
    wait_until "$IP_WAIT" "网卡就绪（inet $DB_BIND）" \
      bash -c "ip -4 addr show | grep -q 'inet $DB_BIND/'" ;;
esac

# ---------- 2) 起 db 并等 healthy ----------
if [ "$(state_of "$DB_CONTAINER")" = "running" ]; then
  info "$DB_CONTAINER 已在运行"
else
  info "拉起 $DB_CONTAINER（原状态：$(state_of "$DB_CONTAINER")）"
  docker start "$DB_CONTAINER" >/dev/null 2>&1 || bad "docker start $DB_CONTAINER 失败"
fi
wait_until "$DB_HEALTH_WAIT" "$DB_CONTAINER healthy" is_healthy "$DB_CONTAINER"

# ---------- 3) db 半状态修复（只补端点不重启：postgres 无降级初始化问题） ----------
if [ -z "$(endpoint_of "$DB_CONTAINER")" ]; then
  net=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$DB_CONTAINER" 2>/dev/null)
  info "$DB_CONTAINER 处于无网络端点半状态，补挂到 $net（--alias $DB_HOST）"
  docker network connect --alias "$DB_HOST" "$net" "$DB_CONTAINER" >/dev/null 2>&1
  if [ -n "$(endpoint_of "$DB_CONTAINER")" ]; then
    ok "$DB_CONTAINER 端点已补挂（$(endpoint_of "$DB_CONTAINER")）"
  else
    bad "$DB_CONTAINER 补挂后仍无端点，需人工处理"
  fi
else
  info "$DB_CONTAINER 网络端点正常"
fi

# ---------- 4) 等主容器的 autofs 挂载源就绪（CIFS automount） ----------
# 叠加挂载时 findmnt --target 输出多行（autofs + cifs）；恰为单行 autofs 才视为未挂载。
bind_pending() { [ "$(findmnt -no FSTYPE --target "$1" 2>/dev/null)" = "autofs" ]; }
wait_for_mounts() {
  local deadline=$(( $(date +%s) + MOUNT_WAIT )) src
  while IFS= read -r src; do
    [ -n "$src" ] || continue
    bind_pending "$src" || continue
    info "等待挂载就绪（autofs）：$src"
    while bind_pending "$src"; do
      ls "$src" >/dev/null 2>&1   # 访问即触发 automount
      if [ "$(date +%s)" -ge "$deadline" ]; then
        bad "挂载等待超时（${MOUNT_WAIT}s）：$src 仍为 autofs（对端未开机/网络不通？）"
        return 1
      fi
      sleep 5
    done
    ok "挂载就绪：$src（$(findmnt -no FSTYPE --target "$src" | tr '\n' ' ')）"
  done < <(docker inspect -f '{{range .Mounts}}{{if eq .Type "bind"}}{{.Source}}{{"\n"}}{{end}}{{end}}' "$MAIN_CONTAINER" 2>/dev/null)
  return 0
}
wait_for_mounts

# ---------- 5) 起主容器（失败重试：docker start 本身会重新触发 automount） ----------
start_main() {
  local i
  for i in $(seq 1 "$MAIN_START_RETRIES"); do
    if [ "$(state_of "$MAIN_CONTAINER")" = "running" ]; then
      info "$MAIN_CONTAINER 已在运行"
      return 0
    fi
    info "拉起 $MAIN_CONTAINER（尝试 $i/$MAIN_START_RETRIES，原状态：$(state_of "$MAIN_CONTAINER")）"
    if docker start "$MAIN_CONTAINER" >/dev/null 2>&1; then
      return 0
    fi
    bad "docker start $MAIN_CONTAINER 失败（第 $i 次）"
    [ "$i" -lt "$MAIN_START_RETRIES" ] && sleep 60
  done
  return 1
}
start_main

# ---------- 6) 主容器端点（半状态 → 补挂 + 重启） ----------
if [ "$(state_of "$MAIN_CONTAINER")" = "running" ]; then
  ensure_endpoint "$MAIN_CONTAINER" "$MAIN_CONTAINER"
fi

# ---------- 7) 兄弟容器（celery/beat） ----------
for c in $SIDE_CONTAINERS; do
  st=$(state_of "$c")
  if [ "$st" = "running" ]; then
    info "$c 已在运行"
  else
    info "拉起 $c（原状态：${st:-不存在}）"
    docker start "$c" >/dev/null 2>&1 || bad "docker start $c 失败"
  fi
  [ "$(state_of "$c")" = "running" ] && ensure_endpoint "$c" "$c"
done

# ---------- 8) 等主容器 healthy ----------
[ "$(state_of "$MAIN_CONTAINER")" = "running" ] \
  && wait_until "$MAIN_HEALTH_WAIT" "$MAIN_CONTAINER healthy" is_healthy "$MAIN_CONTAINER"

# ---------- 9) 终检：容器内解析 db + API 冒烟 ----------
if [ "$(state_of "$MAIN_CONTAINER")" = "running" ]; then
  if docker exec "$MAIN_CONTAINER" getent hosts "$DB_HOST" >/dev/null 2>&1; then
    ok "容器内解析 $DB_HOST 正常"
  else
    bad "容器内无法解析 $DB_HOST"
  fi
  wait_until 60 "API 127.0.0.1:8000/health 冒烟" \
    curl -sf -m 5 -o /dev/null http://127.0.0.1:8000/health || true
fi

# ---------- 10) 开机补池链（异步，不阻塞本脚本退出） ----------
# 2026-11-05 崩机事故：宿主 21:44（北京）硬崩、停机 11 小时，05:30/06:30/07:30 三档
# 补池重试与 08:50 盘前检查**全部**被跳过；开机后没有任何机制把链接上，CN 数据停在
# 崩机前一天、08:00/09:05 两班批推理全撞数据门禁（l1_factors 最新可用 2026-11-04），
# 当天三个 agent 全部 empty_pool。
#
# 接在这里而不是再加一条 @reboot：本脚本已经是开机入口，且它**等容器 healthy
# 之后**才走到这一步（补池链要靠 docker exec 进容器重建 CUSTOM）。
# 链路自带 flock / 已最新即退出 / 内存闸，空跑只花三次 ls；自愈成功才推 🟢。
# QM_BOOT_CHAIN=0 可临时关掉（演练/调试用）。
if [ "${QM_BOOT_CHAIN:-1}" = "1" ] && [ "$(state_of "$MAIN_CONTAINER")" = "running" ]; then
  info "后台启动开机补池链：scripts/alpha_refresh_retry.sh（日志 logs/keepers/alpha_refresh_retry.log）"
  nohup bash "$PROJECT_DIR/scripts/alpha_refresh_retry.sh" \
    >> "$PROJECT_DIR/logs/keepers/alpha_refresh_retry.log" 2>&1 &
fi

# ---------- 汇总 ----------
if [ "$ISSUES" -eq 0 ]; then
  log "===== boot_guard 完成：全部就绪 ====="
  exit 0
fi
log "===== boot_guard 完成：$ISSUES 项异常，见上方 ✗ 行 ====="
exit 1
