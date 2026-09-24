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
#   AUTO_DL             yes | no | skip（默认：无数据 yes；已有数据 skip）
#   AUTODL_RESYNC       1=已有数据时仍做增量同步（ModelScope 续传，已下载文件跳过）
#   AUTODL_DATASETS     逗号分隔，默认 l1_factors,l2_factors,l1_l2_factors
#   AUTODL_SINCE        YYYY-MM-DD 才裁剪下载窗口；默认空=全量历史（3-year 可解析为近三年）
#   MODELSCOPE_DATASET_REPO    魔搭数据集，默认 qusong0627/LightGBM_Alpha300
#   MODELSCOPE_ENDPOINT        默认 https://www.modelscope.cn
#   MODELSCOPE_DATASET_REVISION 默认 master
#   MODELSCOPE_TOKEN           可选（私有仓库 / 提高限流阈值）
#   MODELSCOPE_SYNC_WORKERS    并发下载数，默认 6
#   QUANTDB_API_KEY     可选；仅主节点编排器的日常增量同步需要，初始数据不再依赖
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
DEFAULT_DATASETS="${AUTODL_DATASETS:-l1_factors,l2_factors,l1_l2_factors}"
# 初始训练数据来源：魔搭（ModelScope）公开数据集（即 QuantDB 本体，纯 HTTP，无需 SDK/Key）
MODELSCOPE_ENDPOINT="${MODELSCOPE_ENDPOINT:-https://www.modelscope.cn}"
MODELSCOPE_DATASET_REPO="${MODELSCOPE_DATASET_REPO:-qusong0627/LightGBM_Alpha300}"
MODELSCOPE_DATASET_REVISION="${MODELSCOPE_DATASET_REVISION:-master}"
MODELSCOPE_SYNC_WORKERS="${MODELSCOPE_SYNC_WORKERS:-6}"

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
        warn "未提供 QUANTDB_API_KEY（初始数据改由 ModelScope 拉取，不需要 Key；仅编排器日常增量同步需要）"
    else
        ask_line "请输入 QUANTDB_API_KEY（可选；初始数据从 ModelScope 拉取不需要，仅编排器日常增量同步需要）:" || true
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
    # 默认不裁剪（拉全量历史）；AUTODL_SINCE 显式给出时才按 dt= 窗口过滤。
    local raw="${AUTODL_SINCE:-}"
    case "$(echo "$raw" | tr 'A-Z' 'a-z')" in
        ""|0|none|full|off)
            echo ""
            ;;
        3-year|3-years)
            date -d '3 years ago' +%Y-%m-%d 2>/dev/null || echo ""
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
    warn "离线 / 手动获取数据："
    echo "  1) 从魔搭数据集下载（公开，无需 Key，可断点续传）："
    echo "     ${MODELSCOPE_ENDPOINT}/datasets/${MODELSCOPE_DATASET_REPO}"
    echo "  2) 将 6_ml_datasets/ 放到："
    ok "     $FACTOR_DIR/"
    echo "  3) 或在本机稍后重跑本脚本（已下载文件自动跳过）："
    echo "     AUTODL_RESYNC=1 bash $0"
}

run_modelscope_sync() {
    local datasets="$1"
    local since="$2"
    info "从魔搭拉取训练数据 datasets=$datasets since=${since:-full} → $QUANTDB_DIR"
    MODELSCOPE_ENDPOINT="$MODELSCOPE_ENDPOINT" \
    MODELSCOPE_DATASET_REPO="$MODELSCOPE_DATASET_REPO" \
    MODELSCOPE_DATASET_REVISION="$MODELSCOPE_DATASET_REVISION" \
    MODELSCOPE_SYNC_WORKERS="$MODELSCOPE_SYNC_WORKERS" \
    MODELSCOPE_TOKEN="${MODELSCOPE_TOKEN:-${MODELSCOPE_API_TOKEN:-}}" \
    QM_QUANTDB_DATA_DIR="$QUANTDB_DIR" \
    AUTODL_DATASETS="$datasets" \
    AUTODL_SINCE="$since" \
    "$PYTHON_BIN" - <<'PY'
"""从魔搭（ModelScope）公开数据集拉取训练 parquet（纯 HTTP，无需 modelscope SDK/Key）。

与 backend/services/engine/data_platform/modelscope_dataset_sync.py 同源：
分页枚举仓库 tree（含 sha256/size）→ 并发流式下载到 QM_QUANTDB_DATA_DIR → 逐文件
sha256 校验 + .part 原子覆盖；本地 size 一致则跳过（可断点续传）。
只拉 6_ml_datasets/<dataset>/ 下的 parquet，并按 AUTODL_SINCE 过滤 dt= 分区。
"""
import concurrent.futures
import hashlib
import os
import sys
import time
from urllib.parse import quote

import requests

EP = (os.environ.get("MODELSCOPE_ENDPOINT") or "https://www.modelscope.cn").rstrip("/")
REPO = os.environ.get("MODELSCOPE_DATASET_REPO") or "qusong0627/LightGBM_Alpha300"
REV = os.environ.get("MODELSCOPE_DATASET_REVISION") or "master"
TOKEN = (os.environ.get("MODELSCOPE_TOKEN") or "").strip()
ROOT = os.path.abspath(os.environ.get("QM_QUANTDB_DATA_DIR") or ".")
SINCE = (os.environ.get("AUTODL_SINCE") or "").strip().replace("-", "")
DATASETS = [
    x.strip()
    for x in (os.environ.get("AUTODL_DATASETS") or "l1_factors").split(",")
    if x.strip()
]
WORKERS = max(1, int(os.environ.get("MODELSCOPE_SYNC_WORKERS") or 6))
PAGE = 1000
RETRIES = 3

HEADERS = {"User-Agent": "QuantMind-AutoDL-ModelScopeSync/1.0"}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"


def dt_ok(path):
    """按 dt=YYYYMMDD 分区过滤（SINCE 之后的窗口；无 dt= 的静态文件保留）。"""
    if not SINCE:
        return True
    for part in path.split("/"):
        if part.startswith("dt="):
            return part[3:] >= SINCE
    return True


def enumerate_files():
    url = f"{EP}/api/v1/datasets/{REPO}/repo/tree"
    out, page, seen, total = [], 1, 0, None
    with requests.Session() as sess:
        sess.headers.update(HEADERS)
        while True:
            resp = sess.get(
                url,
                params={
                    "Revision": REV,
                    "Recursive": "true",
                    "PageNumber": page,
                    "PageSize": PAGE,
                },
                timeout=(15, 120),
            )
            resp.raise_for_status()
            body = resp.json()
            if body.get("Code") != 200:
                print(
                    f"[ERROR] ModelScope tree API 失败: {body.get('Message') or body.get('Code')}",
                    file=sys.stderr,
                )
                sys.exit(2)
            data = body.get("Data") or {}
            files = data.get("Files") or []
            if total is None:
                total = int(data.get("TotalCount") or 0)
            seen += len(files)
            for ent in files:
                if ent.get("Type") != "blob":
                    continue
                path = ent.get("Path") or ""
                if not path.endswith(".parquet"):
                    continue
                if not any(path.startswith(f"6_ml_datasets/{ds}/") for ds in DATASETS):
                    continue
                if not dt_ok(path):
                    continue
                out.append(
                    (path, int(ent.get("Size") or 0), (ent.get("Sha256") or "").lower())
                )
            if page % 20 == 0 or not files or (total is not None and seen >= total):
                print(
                    f"[ENUM] 已扫描 {seen}/{total or '?'} 项，命中 {len(out)} 个 parquet",
                    flush=True,
                )
            if not files or (total is not None and seen >= total):
                break
            page += 1
    return out


def download_one(rel, size, sha):
    url = (
        f"{EP}/api/v1/datasets/{REPO}/repo"
        f"?Revision={quote(REV, safe='')}&FilePath={quote(rel, safe='')}"
    )
    dst = os.path.join(ROOT, *rel.split("/"))
    if size and os.path.isfile(dst) and os.path.getsize(dst) == size:
        return "skipped", 0
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    part = dst + ".part"
    last = None
    for attempt in range(RETRIES):
        try:
            digest = hashlib.sha256()
            got = 0
            with requests.get(
                url, headers=HEADERS, stream=True, timeout=(15, 300), allow_redirects=True
            ) as resp:
                resp.raise_for_status()
                with open(part, "wb") as fh:
                    for chunk in resp.iter_content(1 << 20):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        digest.update(chunk)
                        got += len(chunk)
            if size and got != size:
                raise OSError(f"size 不符: 期望 {size} 实得 {got}")
            if sha and digest.hexdigest() != sha:
                raise OSError("sha256 校验失败")
            os.replace(part, dst)
            return "downloaded", got
        except Exception as exc:  # noqa: BLE001 - 重试后统一抛出
            last = exc
            try:
                if os.path.exists(part):
                    os.remove(part)
            except OSError:
                pass
            if attempt < RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last or RuntimeError("下载失败")


def main():
    os.makedirs(ROOT, exist_ok=True)
    print(
        f"[MODELSCOPE] repo={REPO} rev={REV} endpoint={EP} "
        f"datasets={DATASETS} since={SINCE or 'full'} → {ROOT}",
        flush=True,
    )
    files = enumerate_files()
    if not files:
        print(
            "[WARN] 未枚举到匹配文件（检查 AUTODL_DATASETS / AUTODL_SINCE / 网络）",
            flush=True,
        )
        return 5
    total_bytes = sum(f[1] for f in files)
    print(
        f"[MODELSCOPE] 待处理 {len(files)} 个文件，约 {total_bytes / 1024**3:.2f} GB",
        flush=True,
    )
    downloaded = skipped = errors = done_bytes = 0
    err_samples = []
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(download_one, rel, size, sha): rel
            for rel, size, sha in files
        }
        for idx, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            try:
                status, got = fut.result()
                if status == "downloaded":
                    downloaded += 1
                    done_bytes += got
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                errors += 1
                if len(err_samples) < 20:
                    err_samples.append(f"{futures[fut]}: {exc}")
            if idx % 25 == 0 or idx == len(files):
                print(
                    f"[DL] {idx}/{len(files)} 下载={downloaded} 跳过={skipped} "
                    f"失败={errors} 已下 {done_bytes / 1024**3:.2f} GB "
                    f"用时 {time.time() - t0:.0f}s",
                    flush=True,
                )
    print(
        f"[DONE] 魔搭同步结束: 下载={downloaded} 跳过={skipped} 失败={errors} "
        f"共 {done_bytes / 1024**3:.2f} GB，用时 {time.time() - t0:.0f}s",
        flush=True,
    )
    for sample in err_samples:
        print(f"[ERR] {sample}", flush=True)
    return 0 if errors == 0 else 4


sys.exit(main())
PY
}

case "$SYNC_MODE" in
    yes)
        if run_modelscope_sync "$DATASETS" "$SINCE"; then
            if [ -d "$FACTOR_DIR" ] && [ -n "$(ls -A "$FACTOR_DIR" 2>/dev/null)" ]; then
                ok "数据集已就绪 $(du -sh "$FACTOR_DIR" | cut -f1): $(ls -1 "$FACTOR_DIR" | tr '\n' ' ')"
            else
                warn "同步命令结束但未看到 $FACTOR_DIR，请检查 AUTODL_DATASETS / 网络"
            fi
        else
            warn "魔搭同步未完全成功（可设 AUTODL_RESYNC=1 重跑，已下载文件会跳过）"
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
ok "节点初始化完成（依赖 + 目录 + 训练数据）。"
echo ""
info "训练数据来源：魔搭 ${MODELSCOPE_ENDPOINT}/datasets/${MODELSCOPE_DATASET_REPO}（即 QuantDB 本体）。"
info "train.py / training 包 / backend 直读子树由主节点编排器每次 rsync，本脚本不部署训练代码。"
info "日常增量：提交远程训练时编排器会再跑 quantdb_daily_sync.py --parquet-only（需 QUANTDB_API_KEY）。"
echo ""
info "手工验证："
echo "  set -a; . $NODE_ENV_FILE; set +a"
echo "  $PYTHON_BIN -c \"import lightgbm, duckdb, quantdb_sdk; print('deps OK')\""
echo "  nvidia-smi"
echo "  du -sh $FACTOR_DIR 2>/dev/null || true"
echo ""
info "非交互示例："
echo "  AUTO_DL=yes bash $0                     # 默认拉 l1_factors,l2_factors,l1_l2_factors 全量历史"
echo "  AUTODL_RESYNC=1 bash $0                 # 已有数据仍续传（已下载文件跳过）"
echo "  AUTODL_DATASETS=l1_factors bash $0      # 只拉单个数据集"
echo "  AUTODL_SINCE=2024-01-01 bash $0         # 只拉 2024 年以后的 dt= 分区"
echo "=============================================="
ok "脚本完成"
