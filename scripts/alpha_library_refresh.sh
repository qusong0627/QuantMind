#!/usr/bin/env bash
# alpha_library 夜间刷新（Alpha101 + GTJA191 + Alpha158 → 6_ml_datasets/alpha_library）
#
# 为什么有这个脚本：alpha_library_factors.py 一直是手动批处理，从未进 cron →
# 分区滞后日K 5 天（2026-09-09 实测 alpha dt=20260903 vs kline dt=20260908），
# alert.sh 2c 每 30 分钟报一次。
#
# 注意：每次都是**全量重算**（2026-09-09 实测：全量计算 + 合并 ~80 分钟、峰值 RSS
# ~24GB，启动时可用内存 40GB、swap 已满 15/15）。脚本里的「已有分区跳过」只看
# _partial_* 临时目录，而它在每轮 merge 后被 rmtree，所以对跨轮次没有增量效果；
# 要真增量得给上游加 --start-dt/ 带预热窗口的增量计算，不在本脚本职责内。
#
# ⚠️ 分区属主：合并阶段按日期顺序重写最终分区，遇到非本用户所有的目录会
# PermissionError 中止（2026-09-09 实录：dt=20260831..20260903 四个分区归 root，
# 合并跑到 20260831 即失败，之后的 20260904/07/08 再没写入——白跑 80 分钟）。
# 故下面加了写权限闸门；修法：sudo chown -R zbox:zbox <alpha_library>。
#
# 闸门：
#   1) flock 防重入（上一次没跑完就跳过）；
#   2) 已最新则空转（cron 早于 quantdb 夜间同步时不会白跑 80 分钟）；
#   3) 最终分区存在非本用户可写的目录 → 跳过（合并必失败，别白跑）；
#   4) 可用内存 < MIN_MEM_GB 跳过——全量计算峰值 RSS ~24GB，本机 swap 已用 15/15，
#      不能和盘中/夜间任务抢内存（宁可滞后一天，也不要 OOM 拖垮实盘）。
#
# cron（JST = 北京+1h）：30 2 * * 2-6  ← 北京 01:30，接在 quantdb 夜间同步
# （实测 dt 分区 01:55 JST 落盘）之后。
set -uo pipefail

QM_ROOT=/home/zbox/projects/quantmind
PY=/usr/bin/python3
SCRIPT="$QM_ROOT/backend/scripts/alpha_library_factors.py"
ALPHA_ROOT="$QM_ROOT/data/quantdb/6_ml_datasets/alpha_library"
KLINE_ROOT="$QM_ROOT/data/quantdb/1_kline_data/daily_forward"
LOG="$ALPHA_ROOT/cron_run.log"
LOCK=/tmp/.qm_alpha_refresh.lock
MIN_MEM_GB=30

ts() { date '+%F %T'; }
log_line() { echo "$(ts) $*" >> "$LOG"; }

mkdir -p "$ALPHA_ROOT"
touch "$LOG" 2>/dev/null

# 1) 防重入
exec 9>"$LOCK"
if ! flock -n 9; then
    log_line "跳过：上一次刷新仍在运行"
    exit 0
fi

# 2) 已最新则空转（比较 8 位日期串，同为整数比较）
KL_DT=$(ls -d "$KLINE_ROOT"/dt=* 2>/dev/null | grep -oE '[0-9]{8}' | sort | tail -1)
AL_DT=$(ls -d "$ALPHA_ROOT"/dt=* 2>/dev/null | grep -oE '[0-9]{8}' | sort | tail -1)
if [ -z "$KL_DT" ]; then
    log_line "跳过：日K 分区目录为空（$KLINE_ROOT）"
    exit 0
fi
if [ -n "$AL_DT" ] && [ "$AL_DT" -ge "$KL_DT" ]; then
    log_line "跳过：已最新（alpha=$AL_DT kline=$KL_DT）"
    exit 0
fi

# 3) 写权限闸门：合并阶段要重写最终分区（**dt=* 目录**），非本用户可写的 dt=* 会
#    PermissionError 中止（白跑一整轮）。只检查 dt=* —— report/ 等旁路目录由其他
#    工具维护，合并不写它们（2026-09-18 实录：report/ 属 root 触发本闸门连续
#    4 夜误跳（09-15~09-18），而合并根本不碰 report/；正确修法是把检查面收窄到
#    合并真正重写的 dt=*，同时 chown 掉旁路目录保持树整洁）。
NOWRITABLE=$(find "$ALPHA_ROOT" -maxdepth 1 -type d -name 'dt=*' ! -writable 2>/dev/null | head -3)
if [ -n "$NOWRITABLE" ]; then
    log_line "跳过：最终分区有 $(find "$ALPHA_ROOT" -maxdepth 1 -type d -name 'dt=*' ! -writable 2>/dev/null | wc -l) 个 dt=* 目录不可写（合并会 PermissionError）→ sudo chown -R $(id -un):$(id -gn) $ALPHA_ROOT；示例：$(echo "$NOWRITABLE" | tr '\n' ' ')"
    exit 0
fi

# 4) 内存闸门
AVAIL_GB=$(awk '/^MemAvailable:/ {print int($2/1024/1024)}' /proc/meminfo)
if [ -z "$AVAIL_GB" ] || [ "$AVAIL_GB" -lt "$MIN_MEM_GB" ]; then
    log_line "跳过：可用内存 ${AVAIL_GB:-?}GB < ${MIN_MEM_GB}GB（alpha=$AL_DT kline=$KL_DT）"
    exit 0
fi

log_line "开始刷新（alpha=${AL_DT:-无} kline=$KL_DT 可用内存=${AVAIL_GB}GB）"
nice -n 10 "$PY" "$SCRIPT" >> "$LOG" 2>&1
rc=$?
AL_NEW=$(ls -d "$ALPHA_ROOT"/dt=* 2>/dev/null | grep -oE '[0-9]{8}' | sort | tail -1)
log_line "结束 rc=$rc（alpha=${AL_NEW:-无}）"
exit "$rc"
