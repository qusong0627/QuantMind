#!/usr/bin/env bash
# ============================================================================
# 手工创建容器的内存上限守卫  —— 2026-10-14 整机"假死"事故加固
#
# 背景：主栈 docker-compose.yml 里的 10 个服务已声明 mem_limit；但下列 5 个是
#   手工 `docker run` 起的，**不受 compose 管理**，限额只能用 `docker update`
#   热加 —— 而容器一旦重建（改镜像/改端口/rm + run）限额就退回 0（无上限）。
#   零上限的容器可以吃光 60GB 物理内存 → 页缓存被抽空 → ext4 日志提交(jbd2)
#   饿死 → 所有写阻塞 → sshd 连 banner 都发不出去，整机"假死"。
#
# 本脚本幂等：只给「当前 Memory==0（未设限）」的容器补上限，已有上限的容器一律
#   不动，所以可以安全地反复跑。quantmind-mem-guard.timer 每 5 分钟兜一次，
#   覆盖"重建 → 补限"之间的窗口期。
#
# 手工执行：bash /home/zbox/projects/quantmind/deploy/container-mem-guard.sh
# ============================================================================
set -uo pipefail

# 容器名 → 内存上限。取值依据：
#   quantmind-web              2g    nginx 静态站
#   futu-opend                 3g    富途 OpenD（Java，换股池在内存里）
#   chainkg-neo4j              3g    Neo4j（堆 2G + pagecache 1G）
#   quantmind-redis-lan-bridge 512m  纯 TCP 转发（deploy/redis_lan_bridge.py）
#   superalpha-pg              2g    Postgres 15（独立小项目）
declare -A LIMITS=(
  [quantmind-web]=2g
  [futu-opend]=3g
  [chainkg-neo4j]=3g
  [quantmind-redis-lan-bridge]=512m
  [superalpha-pg]=2g
)

# 这些容器各自的"重建入口"（重建时务必带上内联的 --memory；下方路径已同步加上）：
#   quantmind-web      docker run -d --name quantmind-web -p 3080:80 --memory 2g --memory-swap 2g \
#                        --network quantmind_quantmind-net --restart unless-stopped quantmind-web:latest
#   futu-opend         docker compose -f /home/zbox/projects/quantmind/docker/futu-opend/docker-compose.yml up -d
#   chainkg-neo4j      bash /home/zbox/projects/ChainKnowledgeGraph/run_neo4j.sh
#   superalpha-pg      docker compose -f /home/zbox/projects/superalpha/docker-compose.yml up -d
#   redis-lan-bridge   docker run -d --name quantmind-redis-lan-bridge --restart unless-stopped \
#                        --memory 512m --memory-swap 512m --network host \
#                        -e REDIS_LAN_BIND_IPS=192.168.31.68,192.168.31.56 \
#                        -e REDIS_LAN_ALLOW=192.168.31.250,192.168.31.68,192.168.31.56 \
#                        -v /home/zbox/projects/quantmind/deploy/redis_lan_bridge.py:/bridge.py:ro \
#                        python:3.10-slim-bookworm python3 /bridge.py

command -v docker >/dev/null 2>&1 || { echo "[mem-guard] docker 不可用" >&2; exit 1; }

changed=0
for name in "${!LIMITS[@]}"; do
  limit="${LIMITS[$name]}"
  if ! docker inspect "$name" >/dev/null 2>&1; then
    continue   # 容器不存在（未部署/已删）——不是错误
  fi
  cur="$(docker inspect -f '{{.HostConfig.Memory}}' "$name" 2>/dev/null || echo 0)"
  if [ "$cur" = "0" ]; then
    if docker update --memory "$limit" --memory-swap "$limit" "$name" >/dev/null 2>&1; then
      echo "$(date '+%F %T') [mem-guard] 已为 $name 补设内存上限 $limit（原为无上限）"
      changed=1
    else
      echo "$(date '+%F %T') [mem-guard] 为 $name 设置 $limit 失败" >&2
    fi
  fi
done

if [ "$changed" -eq 0 ]; then
  echo "$(date '+%F %T') [mem-guard] 全部容器均已有内存上限，无需处理"
fi
exit 0
