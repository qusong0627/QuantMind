#!/bin/bash
# 看门狗：保证 /media/zbox/sata 挂载健康（crontab 每 5 分钟调用）。
#
# 背景（2026-09-10）：dockerd 成批 stop 容器把 l2-sata-fuse 一起停了，容器 cgroup 被回收 →
# 容器内持有的 ntfs-3g daemon 被杀 → 挂载变 ENOTCONN 残留，S2 静默跳过 202501 的 18 天。
# 本脚本只做两件事：容器没起就拉起；挂载不健康就 umount -l + 按 fstab 重挂。
# 健康时不产生任何动作、不写日志。绝不触碰流水线进程。
set -u

MOUNTPOINT=/media/zbox/sata
PROBE=$MOUNTPOINT/l2-warehouse/parquet          # 真实盘上必存在；空目录/lazy-umount 残留都不满足
CONTAINER=l2-sata-fuse
NAS_REPO=/media/zbox/nas-seagate/l2-warehouse
LOG=$HOME/.l2-warehouse/logs/sata_watchdog.log

mkdir -p "$(dirname "$LOG")"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

# 1) 挂载健康 → 静默退出
if ls "$PROBE" >/dev/null 2>&1; then
  exit 0
fi

# 2) 容器没起 → 拉起（docker 不可用则记一次错等下次）
if ! /usr/bin/docker start "$CONTAINER" >/dev/null 2>&1; then
  echo "$(ts) ERROR 无法启动 $CONTAINER 容器（docker 不可用？）" >> "$LOG"
  exit 1
fi
sleep 2

# 3) 清掉 stale 挂载并按 fstab 重挂（在宿主机 mount ns 里执行）
if /usr/bin/docker exec "$CONTAINER" busybox nsenter -t 1 -m -p -- sh -c \
     "umount -l $MOUNTPOINT 2>/dev/null; mount $MOUNTPOINT" >>"$LOG" 2>&1 \
   && ls "$PROBE" >/dev/null 2>&1; then
  echo "$(ts) 自愈完成：$MOUNTPOINT 已重新挂载（$(df -h "$MOUNTPOINT" | awk 'NR==2{print $4}' ) 可用）" >> "$LOG"
  exit 0
fi

echo "$(ts) ERROR 自愈失败：$MOUNTPOINT 仍不可用，需人工处理" >> "$LOG"
exit 1
