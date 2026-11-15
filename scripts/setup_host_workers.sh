#!/usr/bin/env bash
# ============================================================================
# 宿主 worker 一键安装（幂等）：Pine 转写 / Pine 对话 / 批量回测
#
# 用法::
#
#     bash scripts/setup_host_workers.sh
#
# 做什么：
#   在**当前用户**的 crontab 里追加三条（已存在则跳过），并在修改前备份：
#     * * * * * pine_transpile_worker.py   # Pine → Pyne 转写 + 沙箱回测
#     * * * * * pine_chat_worker.py        # 策略对话 / 改策略候选
#     * * * * * lab_batch_worker.py        # 界面发起的批量回测
#
# 前置：
#   - Pine / 批量回测依赖 pynecore + bwrap：先跑 `bash scripts/setup_pine_lab.sh`
#   - 用**仓库属主用户**运行（不要 sudo/root），否则 data/ 下文件归属会错乱
#   - Windows 便携包没有 cron/bwrap：请用 WSL2 或 Docker 版
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${ROOT}/logs"
BACKUP_DIR="${ROOT}/logs/keepers"
PY="/usr/bin/python3"

[ -x "$PY" ] || { echo "[fail] 找不到 $PY" >&2; exit 1; }
command -v crontab >/dev/null 2>&1 || {
    echo "[fail] 找不到 crontab（Ubuntu: sudo apt-get install cron && sudo service cron start）" >&2
    exit 1
}
if [ "$(id -u)" = "0" ]; then
    echo "[warn] 当前是 root；建议改用仓库属主用户运行，避免 data/ 文件归属错乱" >&2
fi

mkdir -p "$LOG_DIR" "$BACKUP_DIR"
BACKUP="${BACKUP_DIR}/crontab.bak.$(date '+%Y%m%d-%H%M%S')"
crontab -l >"$BACKUP" 2>/dev/null || true
echo "[ok] crontab 已备份：$BACKUP"

add_line() {
    local marker="$1" line="$2"
    if crontab -l 2>/dev/null | grep -Fq "$marker"; then
        echo "[skip] 已存在：$marker"
        return
    fi
    { crontab -l 2>/dev/null || true; echo "$line"; } | crontab -
    echo "[ok] 已安装：$marker"
}

add_line "scripts/pine_transpile_worker.py" \
    "* * * * * cd ${ROOT} && ${PY} scripts/pine_transpile_worker.py >> logs/pine_transpile_worker.log 2>&1"
add_line "scripts/pine_chat_worker.py" \
    "* * * * * cd ${ROOT} && ${PY} scripts/pine_chat_worker.py >> logs/pine_chat_worker.log 2>&1"
add_line "scripts/lab_batch_worker.py" \
    "* * * * * cd ${ROOT} && ${PY} scripts/lab_batch_worker.py >> logs/lab_batch_worker.log 2>&1"

echo ""
echo "[done] 三套宿主 worker 已就绪；crontab -l | grep -E 'pine_|lab_batch' 可核对。"
