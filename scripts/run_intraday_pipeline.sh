#!/usr/bin/env bash
# 夜间流水线：特征重建完成 -> 重建面板 -> 4h 模型 -> 5h 模型 -> 卖出模型
# 用法：setsid nohup bash scripts/run_intraday_pipeline.sh >/dev/null 2>&1 &
set -uo pipefail
cd "$(dirname "$0")/.."
LOG="logs/minute_model/pipeline_night.log"
mkdir -p logs/minute_model
echo "=== PIPELINE START $(date -Is) ===" >> "$LOG"

# 1) 等特征重建完成（683 天）
for i in $(seq 1 240); do
  n=$(ls data/minute_model/daily/date=*/data.parquet 2>/dev/null | wc -l)
  [ "$n" -ge 683 ] && break
  sleep 15
done
echo "=== FEATURES_DONE n=$n $(date -Is) ===" >> "$LOG"

# 2) 重建面板（新特征 + regime + rank_5h）
rm -rf data/minute_model/panel
.venv/bin/python -m backend.scripts.minute_model.build_panel --all \
    --start 20240101 --end 20261231 >> "$LOG" 2>&1
echo "=== PANEL_DONE $(date -Is) ===" >> "$LOG"

# 3) 4h 模型
.venv/bin/python -m backend.scripts.minute_model.train_intraday_model \
    --sample-frac 0.5 --label ret_4h > logs/minute_model/train_4h.log 2>&1
echo "=== TRAIN_4H_DONE rc=$? $(date -Is) ===" >> "$LOG"

# 4) 5h 模型
.venv/bin/python -m backend.scripts.minute_model.train_intraday_model \
    --sample-frac 0.5 --label ret_5h > logs/minute_model/train_5h.log 2>&1
echo "=== TRAIN_5H_DONE rc=$? $(date -Is) ===" >> "$LOG"

# 5) 卖出模型 + 端到端
.venv/bin/python -m backend.scripts.minute_model.train_sell_model \
    > logs/minute_model/train_sell.log 2>&1
echo "=== TRAIN_SELL_DONE rc=$? $(date -Is) ===" >> "$LOG"

echo "=== PIPELINE DONE $(date -Is) ===" >> "$LOG"
