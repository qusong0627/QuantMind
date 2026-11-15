#!/usr/bin/env bash
# ============================================================================
# 批量回测宿主 worker 的 cron 安装器（幂等）
#
# 用法::
#
#     bash scripts/setup_lab_batch_worker.sh
#
# 做什么：
#   在宿主机 crontab 里追加一行（已存在则跳过）：
#     * * * * * cd <repo> && /usr/bin/python3 scripts/lab_batch_worker.py >> logs/lab_batch_worker.log 2>&1
#
# 为什么需要：
#   回测用 pynecore，容器 Python 3.10 装不了；API 只入队，宿主 worker 消费队列。
#   worker 自身只用标准库 + backend.services.agent_arena.lab_batch（导入不碰
#   pynecore），真正跑批时再以 pine_lab/.venv/bin/python 启动 CLI。
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LINE="* * * * * cd ${ROOT} && /usr/bin/python3 scripts/lab_batch_worker.py >> logs/lab_batch_worker.log 2>&1"

mkdir -p "${ROOT}/logs"

if crontab -l 2>/dev/null | grep -F "scripts/lab_batch_worker.py" >/dev/null; then
    echo "[ok] 已安装：${LINE}"
    exit 0
fi

( crontab -l 2>/dev/null || true; echo "${LINE}" ) | crontab -
echo "[ok] 已安装：${LINE}"
