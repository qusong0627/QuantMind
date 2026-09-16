#!/bin/bash
# QuantMind AutoDL 免 Docker 训练节点初始化
# =====================================================
# 【在哪里运行】 AutoDL 节点本机（SSH 登录后执行），不是主节点。
#
# 交互：
#   bash setup-autodl-native.sh
# 非交互（无 TTY 或 AUTODL_NONINTERACTIVE=1）：
#   QUANTDB_API_KEY=qdb_xxx AUTO_DL=yes bash setup-autodl-native.sh
#
# 环境变量：
#   QUANTDB_API_KEY     自动同步必填（也可事先 export）
#   AUTO_DL             yes | no | skip（默认：无数据 yes；已有数据 skip）
#   AUTODL_RESYNC       1=已有数据时仍做增量同步
#   AUTODL_SINCE        YYYY-MM-DD，默认三年前；full=不裁剪
#   AUTODL_DATASETS     逗号分隔，默认 l1_factors
#   PIP_INDEX           默认清华 PyPI
#   PYTHON_BIN / WORK_DIR / QUANTDB_DIR / ENV_FILE
set -euo pipefail

info()  { printf "\033[0;36m[INFO]\033[0m  %s\n" "$1"; }
ok()    { printf "\033[0;32m[OK]\033[0m    %s\n" "$1"; }
warn()  { printf "\033[0;33m[WARN]\033[0m  %s\n" "$1"; }
error() { printf "\033[0;31m[ERROR]\033[0m %s\n" "$1"; exit 1; }

PYTHON_BIN="${PYTHON_BIN:-/root/miniconda3/bin/python}"
WORK_DIR="${WORK_DIR:-/root/workspace}"
QUANTDB_DIR="${QUANTDB_DIR:-/root/autodl-fs/quantdb}"
ENV_FILE="${ENV_FILE:-/etc/profile.d/quantmind_sh.sh}"
NODE_ENV_FILE="${NODE_ENV_FILE:-$WORK_DIR/.env}"
PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple/}"
PIP_HOST="${PIP_TRUSTED_HOST:-pypi.tuna.tsinghua.edu.cn}"
DEFAULT_DATASETS="${AUTODL_DATASETS:-l1_factors}"

is_noninteractive() {
    [ "${AUTODL_NONINTERACTIVE:-0}" = "1" ] || [ ! -t 0 ]
}

ask_line() {
    local prompt="$1"
    local reply=""
    if is_noninteractive; then
        printf "\033[0;33m[?]\033[0m    %s（非交互，跳过输入）\n" "$prompt"
        return 1
    fi
    printf "\033[0;33m[?]\033[0m    %s " "$prompt"
    read -r reply || return 1
    REPLY="$reply"
    return 0
}

# ── 0. 平台自检 ──────────────────────────────────────
echo "=============================================="
echo " QuantMind AutoDL 训练节点初始化"
echo "=============================================="

[ "$(id -u)" = "0" ] || warn "建议以 root 运行（AutoDL 默认 root）"

if [ ! -x "$PYTHON_BIN" ]; then
    CANDIDATES=(/root/miniconda3/bin/python /opt/conda/bin/python /usr/bin/python3 /usr/local/bin/python3)
    for c in "${CANDIDATES[@]}"; do [ -x "$c" ] && PYTHON_BIN="$c" && break; done
fi
[ -x "$PYTHON_BIN" ] || error "未找到 Python。请先确认 AutoDL 具备 Python 环境，或用 PYTHON_BIN 指定。"
info "使用 Python: $PYTHON_BIN  ($("$PYTHON_BIN" --version 2>&1))"
is_noninteractive && info "非交互模式（AUTO_DL=${AUTO_DL:-auto} AUTODL_DATASETS=$DEFAULT_DATASETS）"

echo ""
if command -v nvidia-smi >/dev/null 2>&1; then
    GPU_LINE="$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -1 || true)"
    if [ -n "$GPU_LINE" ]; then
        info "检测到 GPU: $GPU_LINE"
    else
        warn "nvidia-smi 存在但未返回 GPU 信息"
    fi
else
    warn "未检测到 nvidia-smi（无 GPU 或驱动未装），将以 CPU 训练"
fi

pip_install() {
    # shellcheck disable=SC2086
    "$PYTHON_BIN" -m pip install --disable-pip-version-check \
        -i "$PIP_INDEX" --trusted-host "$PIP_HOST" "$@"
}

# ── 1. 安装训练依赖（已满足则跳过） ─────────────────
echo ""
info "[1/4] 安装训练依赖..."
"$PYTHON_BIN" -m pip install -q --upgrade pip setuptools wheel \
    -i "$PIP_INDEX" --trusted-host "$PIP_HOST" >/dev/null 2>&1 || true

NEED_CORE=0
"$PYTHON_BIN" - <<'PY' || NEED_CORE=1
import importlib
import pandas
mods = [
    "numpy", "pandas", "scipy", "pyarrow", "sklearn", "lightgbm", "xgboost",
    "catboost", "optuna", "yaml", "requests", "psutil", "shap", "duckdb", "quantdb_sdk",
]
missing = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception:
        missing.append(m)
major = int(str(pandas.__version__).split(".", 1)[0])
if major >= 3:
    raise SystemExit("pandas>=3")
if missing:
    raise SystemExit(",".join(missing))
PY
if [ "$NEED_CORE" != "0" ]; then
    info "核心依赖缺失或 pandas 主版本过高，开始安装（源: $PIP_INDEX）"
    pip_install numpy "pandas>=2.1,<3" scipy pyarrow scikit-learn \
        lightgbm xgboost catboost optuna pyyaml requests psutil shap duckdb quantdb-sdk \
        || error "核心依赖安装失败"
else
    ok "核心依赖已就绪（pandas < 3）"
fi

if "$PYTHON_BIN" -c "import qlib" 2>/dev/null; then
    ok "qlib 已安装"
else
    info "安装 pyqlib（QuantDB 直读可不装，失败不阻断）..."
    pip_install pyqlib && ok "pyqlib 已安装" || warn "pyqlib 安装失败（不影响 QuantDB 直读训练）"
fi

"$PYTHON_BIN" -c "import lightgbm, duckdb, quantdb_sdk; print('deps OK')" >/dev/null
ok "lightgbm / duckdb / quantdb-sdk 可导入"

# ── 2. 创建目录 ──────────────────────────────────────
echo ""
info "[2/4] 创建训练目录..."
mkdir -p "$WORK_DIR/modules" "$WORK_DIR/templates" "$QUANTDB_DIR"
ok "工作目录: $WORK_DIR"
ok "数据目录: $QUANTDB_DIR  ($(df -h "$QUANTDB_DIR" 2>/dev/null | awk 'NR==2{print $2" 可用 "$4}'))"

# ── 3. 配置 QuantDB API Key ──────────────────────────
read_saved_key() {
    local file="$1"
    [ -f "$file" ] || return 0
    # 兼容 QUANTDB_API_KEY= / export QUANTDB_API_KEY=，去掉引号
    grep -E '^(export[[:space:]]+)?QUANTDB_API_KEY=' "$file" 2>/dev/null \
        | tail -1 \
        | sed -E 's/^(export[[:space:]]+)?QUANTDB_API_KEY=//' \
        | sed -E 's/^["'\'']//; s/["'\'']$//' \
        || true
}

write_key() {
    local file="$1"
    local key="$2"
    mkdir -p "$(dirname "$file")"
    if [ -f "$file" ]; then
        grep -vE '^(export[[:space:]]+)?QUANTDB_API_KEY=' "$file" > "$file.tmp" || true
        mv "$file.tmp" "$file"
    fi
    printf 'export QUANTDB_API_KEY=%s\n' "$key" >> "$file"
}

echo ""
info "[3/4] 配置 QuantDB 数据源..."
EXISTING_KEY="$(read_saved_key "$ENV_FILE")"
[ -z "$EXISTING_KEY" ] && EXISTING_KEY="$(read_saved_key "$HOME/.bashrc")"
[ -z "$EXISTING_KEY" ] && EXISTING_KEY="$(read_saved_key "$NODE_ENV_FILE")"
# 进程环境优先（非交互传入）
if [ -n "${QUANTDB_API_KEY:-}" ]; then
    EXISTING_KEY="$QUANTDB_API_KEY"
fi

API_KEY="$EXISTING_KEY"
if [ -n "$API_KEY" ]; then
    MASKED="${API_KEY:0:6}…"
    KEEP=yes
    if ! is_noninteractive; then
        if ask_line "已检测到 QUANTDB_API_KEY（$MASKED），是否保持不变？[Y/n]"; then
            case "${REPLY:0:1}" in
                n|N) KEEP=no ;;
                *) KEEP=yes ;;
            esac
        fi
    fi
    if [ "$KEEP" = "no" ]; then
        API_KEY=""
        if ask_line "请输入 QUANTDB_API_KEY:"; then
            API_KEY="$REPLY"
        fi
        [ -n "$API_KEY" ] || error "未输入 API Key"
    else
        ok "沿用已有 QUANTDB_API_KEY（$MASKED）"
    fi
else
    if is_noninteractive; then
        warn "未提供 QUANTDB_API_KEY，跳过数据源配置（后续可用 AUTODL_RESYNC=1 QUANTDB_API_KEY=… 补）"
    else
        ask_line "请输入 QUANTDB_API_KEY（可选，跳过则不自动下载因子）:" || true
        API_KEY="${REPLY:-}"
    fi
fi

export QUANTDB_API_KEY="$API_KEY"
if [ -n "$API_KEY" ]; then
    write_key "$ENV_FILE" "$API_KEY"
    chmod 600 "$ENV_FILE" 2>/dev/null || true
    write_key "$HOME/.bashrc" "$API_KEY"
    write_key "$NODE_ENV_FILE" "$API_KEY"
    chmod 600 "$NODE_ENV_FILE" 2>/dev/null || true
    if [ "$ENV_FILE" = "/etc/profile.d/quantmind_sh.sh" ]; then
        grep -q "quantmind_sh.sh" /etc/profile 2>/dev/null \
            || echo ". /etc/profile.d/quantmind_sh.sh" >> /etc/profile 2>/dev/null \
            || true
    fi
    ok "QUANTDB_API_KEY 已写入 $ENV_FILE 、~/.bashrc、$NODE_ENV_FILE"
else
    warn "未写入 QUANTDB_API_KEY（跳过数据源配置）"
fi
info "非登录 SSH（编排器）不一定会 source profile.d；开训同步由主节点注入 Key。"

# ── 4. 训练数据集：自动 / 增量 / 手动 ───────────────
echo ""
info "[4/4] 训练数据集..."

FACTOR_DIR="$QUANTDB_DIR/6_ml_datasets"
DATA_PRESENT=no
if [ -d "$FACTOR_DIR" ] && [ -n "$(ls -A "$FACTOR_DIR" 2>/dev/null)" ]; then
    DATA_PRESENT=yes
    SIZE="$(du -sh "$FACTOR_DIR" 2>/dev/null | cut -f1)"
    ok "检测到已有数据集 $FACTOR_DIR（$SIZE）: $(ls -1 "$FACTOR_DIR" | tr '\n' ' ')"
fi

resolve_since() {
    local raw="${AUTODL_SINCE:-3-year}"
    case "$(echo "$raw" | tr 'A-Z' 'a-z')" in
        ""|3-year|3-years)
            date -d '3 years ago' +%Y-%m-%d 2>/dev/null || echo "2023-01-01"
            ;;
        0|none|full|off)
            echo ""
            ;;
        *)
            echo "$raw"
            ;;
    esac
}

SINCE="$(resolve_since)"
DATASETS="${DEFAULT_DATASETS}"

choose_sync_mode() {
    # 输出: yes | no | skip
    if [ -n "${AUTO_DL:-}" ]; then
        case "$(echo "$AUTO_DL" | tr 'A-Z' 'a-z')" in
            y|yes|1|true) echo yes; return ;;
            n|no) echo no; return ;;
            s|skip) echo skip; return ;;
        esac
    fi
    if [ "${AUTODL_RESYNC:-0}" = "1" ]; then
        echo yes
        return
    fi
    if [ "$DATA_PRESENT" = "yes" ]; then
        if is_noninteractive; then
            echo skip
            return
        fi
        ask_line "已有数据。选择: [S]跳过 / [Y]增量同步 / [N]仅显示手动上传说明" || { echo skip; return; }
        case "${REPLY:0:1}" in
            y|Y) echo yes ;;
            n|N) echo no ;;
            *) echo skip ;;
        esac
        return
    fi
    if is_noninteractive; then
        echo yes
        return
    fi
    ask_line "是否自动同步因子 parquet？[Y]自动(默认, since=${SINCE:-full}) / [N]手动上传 / [S]跳过" || { echo yes; return; }
    case "${REPLY:0:1}" in
        n|N) echo no ;;
        s|S) echo skip ;;
        *) echo yes ;;
    esac
}

SYNC_MODE="$(choose_sync_mode)"

print_manual_hint() {
    echo ""
    warn "离线 / 手动同步："
    echo "  1) 在已有 QuantDB 的机器同步 6_ml_datasets/"
    echo "  2) 上传到："
    ok "     $FACTOR_DIR/"
    echo "  3) 或在本机稍后执行增量同步："
    echo "     AUTODL_RESYNC=1 QUANTDB_API_KEY=… bash $0"
}

run_sdk_sync() {
    local since="$1"
    local datasets="$2"
    info "开始同步 datasets=$datasets since=${since:-full} → $QUANTDB_DIR"
    QUANTDB_API_KEY="$API_KEY" \
    QM_QUANTDB_DATA_DIR="$QUANTDB_DIR" \
    AUTODL_SINCE="$since" \
    AUTODL_DATASETS="$datasets" \
    "$PYTHON_BIN" - <<'PY'
import os, sys

try:
    from quantdb_sdk import QuantDBClient
except Exception as e:
    print(f"[ERROR] 导入 quantdb_sdk 失败: {e}", file=sys.stderr)
    sys.exit(2)

key = (os.environ.get("QUANTDB_API_KEY") or "").strip()
if not key:
    print("[ERROR] 未配置 QUANTDB_API_KEY", file=sys.stderr)
    sys.exit(3)

save_dir = os.environ.get("QM_QUANTDB_DATA_DIR") or "."
since = (os.environ.get("AUTODL_SINCE") or "").strip()
datasets = [x.strip() for x in (os.environ.get("AUTODL_DATASETS") or "l1_factors").split(",") if x.strip()]
os.makedirs(save_dir, exist_ok=True)
client = QuantDBClient(api_key=key, timeout=(15, 600), max_retries=3)
failed = 0

def sync_one(ds: str):
    attempts = []
    if since:
        attempts.append({"since": since})
        attempts.append({"start_date": since})
        attempts.append({"start": since})
    attempts.append({})
    last_err = None
    for extra in attempts:
        try:
            return client.sync_dataset(ds, save_dir=save_dir, **extra)
        except TypeError as exc:
            last_err = exc
            continue
    raise last_err or RuntimeError(f"sync_dataset({ds}) 调用失败")

for ds in datasets:
    print(f"[SYNC] {ds} since={since or 'full'} ...", flush=True)
    try:
        r = sync_one(ds)
        if isinstance(r, dict):
            errs = r.get("errors") or []
            print(
                f"[SYNC] {ds} 完成: synced={r.get('synced')} matched={r.get('matched')} errors={len(errs)}",
                flush=True,
            )
        else:
            print(f"[SYNC] {ds} 完成: {r}", flush=True)
    except Exception as e:
        failed += 1
        print(f"[WARN] {ds} 同步失败: {e}", flush=True)
if failed:
    sys.exit(4)
print("[DONE] 数据集同步结束", flush=True)
PY
}

case "$SYNC_MODE" in
    yes)
        if [ -z "$API_KEY" ]; then
            warn "未配置 QUANTDB_API_KEY，无法自动同步；改为手动上传"
            print_manual_hint
        elif run_sdk_sync "$SINCE" "$DATASETS"; then
            if [ -d "$FACTOR_DIR" ] && [ -n "$(ls -A "$FACTOR_DIR" 2>/dev/null)" ]; then
                ok "数据集已就绪 $(du -sh "$FACTOR_DIR" | cut -f1): $(ls -1 "$FACTOR_DIR" | tr '\n' ' ')"
            else
                warn "同步命令结束但未看到 $FACTOR_DIR，请检查 Key / 网络"
            fi
        else
            warn "自动同步未完全成功（可设 AUTODL_RESYNC=1 重跑续传）"
            print_manual_hint
        fi
        ;;
    no)
        print_manual_hint
        ;;
    skip)
        ok "跳过数据同步"
        ;;
esac

# ── 收尾 ─────────────────────────────────────────────
echo ""
echo "=============================================="
ok "节点初始化完成（依赖 + 目录 + API Key）。"
echo ""
info "train.py / training 包 / backend 直读子树由主节点编排器每次 rsync，本脚本不部署训练代码。"
info "日常增量：提交远程训练时编排器会再跑 quantdb_daily_sync.py --parquet-only。"
echo ""
info "手工验证："
echo "  set -a; . $NODE_ENV_FILE; set +a"
echo "  $PYTHON_BIN -c \"import lightgbm, duckdb, quantdb_sdk; print('deps OK')\""
echo "  nvidia-smi"
echo "  du -sh $FACTOR_DIR 2>/dev/null || true"
echo ""
info "非交互示例："
echo "  QUANTDB_API_KEY=qdb_xxx AUTO_DL=yes AUTODL_SINCE=2024-01-01 AUTODL_DATASETS=l1_factors bash $0"
echo "  AUTODL_RESYNC=1 QUANTDB_API_KEY=qdb_xxx bash $0   # 已有数据仍增量"
echo "=============================================="
ok "脚本完成"
