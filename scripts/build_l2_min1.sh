#!/usr/bin/env bash
# L2 分钟表全量构建守护进程。
#
# - 逐日构建，已完成日期自动跳过（断点续跑）
# - 进程崩溃 / 被 OOM 杀掉后自动重启（最多 30 次）
# - 默认 2 进程 × DuckDB 5GB 上限：2026-11-07 的全局 OOM 是 4 个无上限
#   pandas 进程打穿 60G 主机，这里不允许重演
#
# 用法（宿主机仓库根）：
#   nohup bash scripts/build_l2_min1.sh 20240101 20251231 >/dev/null 2>&1 &
#   nohup bash scripts/build_l2_min1.sh 20230101 20231231 >/dev/null 2>&1 &
#
# 进度：
#   tail -f logs/minute_model/build_min1_<start>_<end>.log
#   bash scripts/build_l2_min1.sh --status
set -uo pipefail
cd "$(dirname "$0")/.."

START="${1:-}"
END="${2:-}"
WORKERS="${MIN1_WORKERS:-2}"
MEM="${MIN1_MEM:-5GB}"
THREADS="${MIN1_THREADS:-6}"
LOG="logs/minute_model/build_min1_${START:-all}_${END:-all}.log"
mkdir -p "$(dirname "$LOG")"

count_remaining() {
  .venv/bin/python - "$START" "$END" <<'PY'
import sys
sys.path.insert(0, '.')
from backend.scripts.minute_model.build_features import available_days
from backend.scripts.minute_model.build_min1_l2 import OUT
start, end = sys.argv[1], sys.argv[2]
days = available_days()
left = [d for d in days
        if (not start or d >= start) and (not end or d <= end)
        and not (OUT / f"dt={d}" / "data.parquet").exists()]
print(len(left))
PY
}

if [ "${1:-}" = "--status" ]; then
  echo "总可用交易日：$(.venv/bin/python - <<'PY'
import sys
sys.path.insert(0, '.')
from backend.scripts.minute_model.build_features import available_days
print(len(available_days()))
PY
)"
  echo "已落库天数：$(ls -d data/quantdb/1_kline_data/l2_min1/dt=* 2>/dev/null | wc -l)"
  echo "占用：$(du -sh data/quantdb/1_kline_data/l2_min1 2>/dev/null | cut -f1)"
  exit 0
fi

prev_left=""
stuck=0
for attempt in $(seq 1 30); do
  left=$(count_remaining)
  echo "=== $(date -Is) 尝试 $attempt，剩余 $left 天（$START ~ $END）===" | tee -a "$LOG"
  [ "$left" -eq 0 ] && break
  if [ "$left" = "$prev_left" ]; then
    stuck=$((stuck + 1))
  else
    stuck=0
  fi
  if [ "$stuck" -ge 3 ]; then
    echo "=== $(date -Is) 连续 3 次无进展（剩余 $left 天），停止重试，需人工介入 ===" | tee -a "$LOG"
    exit 2
  fi
  prev_left="$left"

  .venv/bin/python -m backend.scripts.minute_model.build_min1_l2 --all \
      --start "$START" --end "$END" --workers "$WORKERS" \
      --memory-limit "$MEM" --threads "$THREADS" >> "$LOG" 2>&1
  rc=$?
  echo "=== $(date -Is) 退出码 $rc ===" >> "$LOG"
  sleep 30
done

echo "=== $(date -Is) 结束，剩余 $(count_remaining) 天 ===" | tee -a "$LOG"
