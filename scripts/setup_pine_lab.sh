#!/usr/bin/env bash
# ============================================================================
# Pine 行情回测宿主运行时（pynecore）安装器
#
# 用法::
#
#     bash scripts/setup_pine_lab.sh
#
# 做什么:
#   1. 在 pine_lab/.venv 建 Python venv（该目录不入仓；已存在则复用，不删旧包）
#   2. 按 requirements/pine_lab.txt 装 pynecore 及钉版依赖
#   3. 检查 bwrap（沙箱回测必需；缺了 worker 会拒跑而不是裸执行）
#
# 为什么单独 venv:
#   主运行环境是 Python 3.10，pynecore 要求 >= 3.11；且它只在**宿主**跑
#   （容器里没有 bwrap），所以不能进主镜像/主环境。
#
# 装完后:
#   · 转写/回测 worker：scripts/pine_transpile_worker.py、pine_chat_worker.py
#   · 批量回测：pine_lab/.venv/bin/python scripts/lab_batch_backtest.py --pool hs300
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/pine_lab/.venv"
REQ="$ROOT/requirements/pine_lab.txt"
PYTHON="${PYTHON:-python3}"

log() { echo -e "\033[36m[pine_lab]\033[0m $*"; }
ok()  { echo -e "\033[32m[ok]\033[0m $*"; }
warn() { echo -e "\033[33m[warn]\033[0m $*"; }
fail() { echo -e "\033[31m[fail]\033[0m $*" >&2; exit 1; }

[ -f "$REQ" ] || fail "缺 $REQ"
command -v "$PYTHON" >/dev/null 2>&1 || fail "找不到 $PYTHON（可用 PYTHON=/path/to/python3.11+ 指定）"

"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || fail "pynecore 需要 Python >= 3.11（当前 $("$PYTHON" -V 2>&1)）"

if [ ! -x "$VENV/bin/python" ]; then
    log "创建 venv: $VENV"
    # 注意：**不加 --clear**。运营机的 pine_lab 可能只有 site-packages 没有 bin，
    # 重建时保留已有包；依赖版本由 requirements 钉住。
    "$PYTHON" -m venv "$VENV"
fi

log "安装依赖（$REQ）..."
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV/bin/python" -m pip install -r "$REQ"

"$VENV/bin/python" -c 'import pynecore; print("pynecore", getattr(pynecore, "__version__", "?"))' \
    || fail "pynecore 导入失败"

if command -v bwrap >/dev/null 2>&1; then
    ok "bwrap: $(command -v bwrap)"
else
    warn "未找到 bwrap（bubblewrap）——沙箱回测会拒跑；Ubuntu: sudo apt-get install -y bubblewrap"
fi

ok "Pine 运行时就绪：$VENV"
echo "  批量回测示例：$VENV/bin/python scripts/lab_batch_backtest.py --pool hs300"
