#!/usr/bin/env bash
# 大 QMT 桥 + 备源席一体体检（宿主执行；内部用 docker exec）
# 用法: bash qmt_bridge_status.sh [资金账号] [容器名]
set -uo pipefail
ACCT="${1:-40327478}"
C="${2:-quantmind}"

echo "== 大 QMT 桥体检 $(date '+%F %T %Z')（账号 $ACCT）=="
echo "-- 备源席状态（本机 db0）"
docker exec quantmind-redis redis-cli -n 0 hgetall qm:qmt:quote:backup:status 2>/dev/null | paste - - \
  | grep -E "bridge_ok|subscribed|written|skipped_fresh|last_error|last_push_age_s" || echo "  (状态键不存在)"
echo "-- 桥在线性（position_events 末条 / 队列长度 / 积压成分）"
docker exec quantmind-redis redis-cli -n 5 xrevrange "bigqmt:position_events:$ACCT" + - COUNT 1 2>/dev/null | head -1 \
  | awk -F'[-]' '{printf "  position_events 末条 epoch(ms)=%s（≈%d 秒前）\n", $1, systime()-$1/1000}'
docker exec -w /app/backend -e PYTHONPATH=/app "$C" \
  python scripts/qmt_rpc_queue_hygiene.py --account "$ACCT" 2>/dev/null | head -12
echo "-- 判读：bridge_ok=False 且 position_events 陈旧 → 桥离线；"
echo "   恢复=① 清队列(--trim 显式白名单) ② Windows QMT 策略编辑器重载 BIGQMT_REDIS_DRYRUN.py ③ 复验 bridge_ok=True"
