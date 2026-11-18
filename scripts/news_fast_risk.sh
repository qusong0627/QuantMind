#!/usr/bin/env bash
# P1 快速通道 cron 包装。容器内 /app 为 repo 根；宿主机按实际路径改。
set -euo pipefail
cd /app
exec python3 scripts/news_fast_risk.py "$@"
