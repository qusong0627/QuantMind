#!/usr/bin/env bash
# ============================================================
# QuantMind 训练节点一键包(免 Docker,服务器侧)构建器
#
# 产出: deploy/portable/dist/qm-train-node-<日期>.tar.gz
#   服务器解压即用(内嵌 Python runtime + backend + 训练代码 + 包装脚本),
#   本地主节点在「训练中心」把该服务器配成 executor=process 节点即可远程训练。
#
# 数据: CN 直读因子 —— 服务器首次 start_node.sh 或训练前自动
#       sync_factors.sh CN(QUANTDB_API_KEY 来自 .env / train_env.sh)。
#
# 用法:
#   bash scripts/setup/build_node_pack.sh \
#       [--runtime <已构建便携包根>]   # 缺省自动探测本机便携包 runtime
#       [--key <QUANTDB_API_KEY>]     # 缺省读 .env
#       [--out <输出目录>]
# ============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT_DIR=""
RUNTIME_SRC=""
API_KEY=""

while [ $# -gt 0 ]; do
    case "$1" in
        --runtime) RUNTIME_SRC="$2"; shift 2 ;;
        --key) API_KEY="$2"; shift 2 ;;
        --out) OUT_DIR="$2"; shift 2 ;;
        *) echo "[!] 未知参数: $1"; exit 1 ;;
    esac
done

# ── 1. 定位 runtime(内嵌 python,复用已构建便携包) ──────────────
if [ -z "$RUNTIME_SRC" ]; then
    for cand in \
        "$REPO_ROOT/deploy/portable/build/QuantMind-Portable-linux-x64" \
        "$REPO_ROOT/deploy/portable/build/QuantMind-Portable-win-x64"; do
        if [ -x "$cand/runtime/bin/python3" ] || [ -x "$cand/runtime/python/bin/python3" ]; then
            RUNTIME_SRC="$cand"; break
        fi
    done
fi
if [ -z "$RUNTIME_SRC" ] || [ ! -d "$RUNTIME_SRC/runtime" ]; then
    echo "[!] 未找到便携包 runtime,请 --runtime 指定(含 runtime/ 的包根)"
    exit 1
fi
RTPY="$RUNTIME_SRC/runtime/bin/python3"
[ -x "$RTPY" ] || RTPY="$RUNTIME_SRC/runtime/python/bin/python3"
[ -x "$RTPY" ] || { echo "[!] runtime 解释器未找到"; exit 1; }
echo "[INFO] runtime: $RTPY"

# ── 2. API key ────────────────────────────────────────────────
[ -z "$API_KEY" ] && API_KEY="$(grep -E '^QUANTDB_API_KEY=' "$REPO_ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' || true)"

# ── 3. 组装 ───────────────────────────────────────────────────
STAGE="$(mktemp -d /tmp/qm-node-pack.XXXXXX)"
NODE="$STAGE/qm-train-node"
mkdir -p "$NODE"/{backend,data,workspace,logs}
DATE="$(date +%Y%m%d)"

echo "[INFO] 复制 backend/ ..."
rsync -a --exclude='__pycache__' --exclude='tests' "$REPO_ROOT/backend/" "$NODE/backend/"
echo "[INFO] 复制训练代码 ..."
mkdir -p "$NODE/docker/training"
cp "$REPO_ROOT/docker/training/train.py" "$NODE/docker/training/"
cp "$REPO_ROOT/docker/training/preprocessing.py" "$NODE/docker/training/"
cp "$REPO_ROOT/docker/training/parallel_utils.py" "$NODE/docker/training/"
# 包根平铺一份(与便携包直跑布局一致,便于手工调试/自检)
cp "$NODE/docker/training/train.py" "$NODE/train.py"
cp "$NODE/docker/training/preprocessing.py" "$NODE/preprocessing.py"
cp "$NODE/docker/training/parallel_utils.py" "$NODE/parallel_utils.py"
cp "$REPO_ROOT/docker/autodl/Dockerfile" "$NODE/docker/autodl/Dockerfile" 2>/dev/null || true
echo "[INFO] 复制 runtime(约数分钟,大)..."
mkdir -p "$NODE/runtime"
rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='pip/cache' "$RUNTIME_SRC/runtime/" "$NODE/runtime/"

# ── 4. train_env.sh(密钥留在服务器侧) ─────────────────────────
cat > "$NODE/train_env.sh" <<EOF
# QuantMind 训练节点包环境(服务器侧维护,勿提交 git)
# QUANTDB_API_KEY 为量化数据平台访问凭据(因子同步用)
export QUANTDB_API_KEY="${API_KEY:-<在此填入 QUANTDB_API_KEY>}"
# CN 数据根(6_ml_datasets 所在);HK/US 节点包对应换 QM_QUANTHK/QUANTUS_DATA_DIR
export QM_QUANTDB_DATA_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)/data/quantdb"
EOF

# ── 5. sync_factors.sh(市场因子同步包装器,编排器按市场调用) ───
cat > "$NODE/sync_factors.sh" <<'EOF'
#!/usr/bin/env bash
# QuantMind 训练节点:按市场同步训练因子(6_ml_datasets 直读源)
# 用法: bash sync_factors.sh <CN|HK|US>
set -euo pipefail
NODE_ROOT="$(cd "$(dirname "$0")" && pwd)"
[ -f "$NODE_ROOT/train_env.sh" ] && . "$NODE_ROOT/train_env.sh"
MARKET="${1:-CN}"
RUNTIME="$NODE_ROOT/runtime/bin/python3"
[ -x "$RUNTIME" ] || RUNTIME="$NODE_ROOT/runtime/python/bin/python3"
PY="PYTHONPATH=$NODE_ROOT/backend"

case "$MARKET" in
  CN)
    mkdir -p "$QM_QUANTDB_DATA_DIR"
    echo "[sync] CN 因子同步 → $QM_QUANTDB_DATA_DIR"
    export PYTHONPATH="$NODE_ROOT/backend"
    "$RUNTIME" backend/scripts/quantdb_daily_sync.py --parquet-only \
        --datasets l1_factors,l2_factors,l1_l2_factors
    ;;
  HK|US)
    # TODO(扩展):HK→quanthk_daily_sync 因子段(QM_QUANTHK_DATA_DIR 6_ml_datasets)
    #            US→quantus 摄取对应因子源;与主节点同步链路保持一致
    echo "[sync] 市场 $MARKET 因子同步尚未在节点包实现,请联系维护者扩展 sync_factors.sh"
    exit 2
    ;;
  *) echo "[sync] 未知市场: $MARKET (CN/HK/US)"; exit 2 ;;
esac
echo "[sync] 因子就绪"
EOF
chmod +x "$NODE/sync_factors.sh"

# ── 6. start_node.sh(自检 + 引导) ─────────────────────────────
cat > "$NODE/start_node.sh" <<'EOF'
#!/usr/bin/env bash
# QuantMind 训练节点包:环境自检 + 首次因子同步引导
set -uo pipefail
NODE_ROOT="$(cd "$(dirname "$0")" && pwd)"
RUNTIME="$NODE_ROOT/runtime/bin/python3"
[ -x "$RUNTIME" ] || RUNTIME="$NODE_ROOT/runtime/python/bin/python3"
echo "== QuantMind 训练节点包自检 =="
echo "runtime : $("$RUNTIME" --version 2>&1)"
"$RUNTIME" -c "import lightgbm, xgboost, pandas, torch; print('deps    : lightgbm/xgboost/pandas/torch OK; torch.cuda =', torch.cuda.is_available())" 2>&1 | tail -1
if [ -f "$NODE_ROOT/train.py" ] || [ -f "$NODE_ROOT/docker/training/train.py" ]; then
    echo "train.py: OK"
else
    echo "train.py: 缺失(docker/training 未复制)"
fi
echo
echo "== 首次使用 =="
echo "1) 编辑 train_env.sh 填入 QUANTDB_API_KEY"
echo "2) 同步因子(可跳过,训练时会自动触发): bash sync_factors.sh CN"
echo "3) 在本机 QuantMind「训练中心 → AutoDL 节点」新增节点:"
echo "   host/port/user/密码=服务器 SSH;executor=process"
echo "   pack_root=$NODE_ROOT ; work_dir=$NODE_ROOT/workspace"
echo "   (训练产物与日志落在 workspace/,自动回传主节点)"
EOF
chmod +x "$NODE/start_node.sh"

# ── 7. README ─────────────────────────────────────────────────
cat > "$NODE/README-node.md" <<'EOF'
# QuantMind 训练节点一键包(免 Docker)

服务器/云主机解压即用的「远程训练节点」:内嵌 Python runtime 与训练链路,
本机 QuantMind 训练中心把它当 AutoDL 节点(executor=process)SSH 直连训练,
**不需要服务器装 Docker**,因子数据由本包按市场同步(直读 6_ml_datasets)。

步骤:1) 解压  2) 编辑 train_env.sh 填 QUANTDB_API_KEY
      3) bash start_node.sh 自检  4) 本机训练中心添加节点(示例见下)
      5) 训练时自动同步因子并开训,产物自动回传本机注册

节点配置(本机 config/training_nodes.yaml 或训练中心表单):
  id: node-cn-1 / executor: process / pack_root: 包根绝对路径
  work_dir: 包根/workspace / quantdb_dir: 包根/data/quantdb
  其余字段同普通 SSH 节点。市场=CN 直读 l1/l2/l1_l2 因子;
  HK/US 待 sync_factors.sh 扩展。
EOF

# ── 8. 打包 ───────────────────────────────────────────────────
OUT_DIR="${OUT_DIR:-$REPO_ROOT/deploy/portable/dist}"
mkdir -p "$OUT_DIR"
PKG="$OUT_DIR/qm-train-node-$DATE.tar.gz"
tar czf "$PKG" -C "$STAGE" qm-train-node
echo "[OK] 节点包: $PKG"
echo "     大小: $(du -sh "$PKG" | cut -f1)  解压即用"
rm -rf "$STAGE"
