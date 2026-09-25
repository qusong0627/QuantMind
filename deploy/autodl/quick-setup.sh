#!/usr/bin/env bash
# QuantMind AutoDL 训练节点「一键初始化」下载器。
#
# 作用：无需先 scp，直接在 AutoDL 实例上一条命令拉取并执行
#      deploy/autodl/setup-autodl-native.sh（免 Docker 的 native_python 节点）。
#
# 用法（AutoDL 实例上，保留 stdin 以便交互）：
#   bash -c "$(curl -fsSL https://quantmindai.cn/gitea/qusong0627/QuantMind/raw/branch/master/deploy/autodl/quick-setup.sh)"
#
# 非交互（CI / 管道执行，需带齐环境变量）：
#   QUANTDB_API_KEY=qdb_xxx AUTO_DL=yes bash quick-setup.sh
#   curl -fsSL .../deploy/autodl/quick-setup.sh | QUANTDB_API_KEY=qdb_xxx AUTO_DL=yes bash
#
# 若在 git 工作区内执行（脚本同目录已有 setup-autodl-native.sh），则直接复用本地脚本，不联网。
#
# 本下载器环境变量：
#   QUANTMIND_REF           要拉取的 Git 分支/tag（默认 master）
#   QUANTMIND_RAW_BASE      raw 文件根地址（默认自建 Gitea）
#   QUANTMIND_SETUP_URL     直接指定脚本 URL，覆盖上面两项拼装
#   QUANTMIND_SETUP_FILE    直接指定本地脚本路径，跳过下载
#   QUANTMIND_SETUP_SHA256  期望脚本的 SHA-256（可选，供应链校验）
# 其余变量（AUTO_DL / AUTODL_SINCE / AUTODL_DATASETS / MODELSCOPE_* / PIP_INDEX …）
# 原样透传给 setup-autodl-native.sh。

set -Eeuo pipefail

REF="${QUANTMIND_REF:-master}"
RAW_BASE="${QUANTMIND_RAW_BASE:-https://quantmindai.cn/gitea/qusong0627/QuantMind/raw/branch}"
SCRIPT_REL="deploy/autodl/setup-autodl-native.sh"
URL="${QUANTMIND_SETUP_URL:-${RAW_BASE%/}/${REF}/${SCRIPT_REL}}"

log() { printf '[quantmind-autodl] %s\n' "$*"; }
die() { log "错误: $*" >&2; exit 1; }

usage() {
    cat <<'EOF'
用法: bash quick-setup.sh [setup-autodl-native.sh 的参数]

本脚本负责定位/下载 deploy/autodl/setup-autodl-native.sh 并执行。
初始化行为由环境变量控制（AUTO_DL / AUTODL_* / MODELSCOPE_* / PIP_INDEX …）。

下载器选项（环境变量）:
  QUANTMIND_REF=<branch|tag>   拉取版本（默认 master）
  QUANTMIND_RAW_BASE=<url>     raw 根地址（默认自建 Gitea）
  QUANTMIND_SETUP_URL=<url>    直接指定脚本 URL
  QUANTMIND_SETUP_FILE=<path>  使用本地脚本，跳过下载
  QUANTMIND_SETUP_SHA256=<hex> 校验脚本 SHA-256（可选）
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
esac

[ "$(id -u)" = "0" ] || log "提示：建议以 root 运行（AutoDL 默认 root），否则依赖可能装到非预期位置"

# ── 1. 定位脚本：优先本地同目录（从 git checkout 运行时）──────────────
SELF_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

SETUP_FILE="${QUANTMIND_SETUP_FILE:-}"
if [[ -n "$SETUP_FILE" ]]; then
    [ -f "$SETUP_FILE" ] || die "QUANTMIND_SETUP_FILE 不存在: $SETUP_FILE"
elif [[ -n "$SELF_DIR" && -f "$SELF_DIR/setup-autodl-native.sh" ]]; then
    SETUP_FILE="$SELF_DIR/setup-autodl-native.sh"
    log "使用本地脚本: $SETUP_FILE"
fi

TMP_DIR=""
cleanup() { [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]] && rm -rf "$TMP_DIR"; }
trap cleanup EXIT

# ── 2. 需要时下载 ────────────────────────────────────────────────
if [[ -z "$SETUP_FILE" ]]; then
    command -v curl >/dev/null 2>&1 || die "未找到 curl，无法下载脚本"
    TMP_DIR="$(mktemp -d /tmp/quantmind-autodl.XXXXXX)"
    SETUP_FILE="$TMP_DIR/setup-autodl-native.sh"
    log "下载节点初始化脚本: $URL"
    curl --fail --location --retry 3 --retry-delay 3 "$URL" -o "$SETUP_FILE" \
        || die "下载失败：请检查网络、QUANTMIND_REF 或 QUANTMIND_RAW_BASE"
fi

# ── 3. 校验（可选）───────────────────────────────────────────────
if [[ -n "${QUANTMIND_SETUP_SHA256:-}" ]]; then
    echo "${QUANTMIND_SETUP_SHA256}  ${SETUP_FILE}" | sha256sum --check --status \
        || die "脚本校验失败（SHA-256 不匹配）"
    log "SHA-256 校验通过"
fi

# ── 4. 执行（保留 stdin，交互提示可用）────────────────────────────
log "开始执行节点初始化（REF=${REF}）..."
bash "$SETUP_FILE" "$@"
