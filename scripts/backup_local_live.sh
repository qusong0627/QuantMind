#!/usr/bin/env bash
# 「实盘交易」栏目（electron/src/features/local-live/）的本机备份。
#
# 为什么单独有一支备份脚本：该目录被 .gitignore 排除，**不在任何 clone 里**。
# 换机、磁盘故障、误 `git clean -fdx` 都会直接丢，且没有任何"从远端拉回来"的途径。
# 这是本方案唯一的不可逆风险点。
#
# 用法:
#   LOCAL_LIVE_BACKUP_TARGET=/mnt/nas/quantmind_local_live bash scripts/backup_local_live.sh
#   LOCAL_LIVE_BACKUP_TARGET=... bash scripts/backup_local_live.sh --dry-run
#
# 目标路径**必须**由 env 给出，不设默认值：默认值意味着"没配也能跑"，
# 而跑在一个错的默认路径上比直接失败更糟——它会让人以为备份过了。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="${PROJECT_ROOT}/electron/src/features/local-live"
TARGET="${LOCAL_LIVE_BACKUP_TARGET:-}"
DRY_RUN="${1:-}"

log()  { echo -e "\033[36m[backup]\033[0m $*"; }
ok()   { echo -e "\033[32m[ok]\033[0m $*"; }
fail() { echo -e "\033[31m[fail]\033[0m $*" >&2; exit 1; }

# ── 1. 前置检查（全部 fail-fast，一个都不许静默跳过）────────────
[[ -n "${TARGET}" ]] || fail "未设置 LOCAL_LIVE_BACKUP_TARGET —— 备份目标必须显式给出"

# 目标必须落在**根文件系统之外**的挂载卷上 —— 这是本脚本唯一无法靠 md5 发现的失败。
# 飞牛 NAS 的 fstab 带 _netdev,nofail：掉线时 /media/zbox/nas-NSF 不会消失，
# 而是退化成一个位于**根文件系统**上的同名空目录。此时下面的 mkdir -p 就地建目录、
# rsync 照写、md5 逐字节一致（同一个本地盘，当然一致）—— 全套校验全绿，
# 而 NAS 上一个字节都没有，直到真要恢复的那天才发现。
#
# 判据用「目标所在的挂载点是不是 /」，而不是「目标本身是不是挂载点」：
# 备份过一次之后目标目录就存在了，它本身永远不是挂载点。
# （TARGET 可能尚不存在，findmnt 要求路径存在，故先向上找到最近的已存在目录；
#   mkdir -p 只会在这同一个文件系统上造目录，所以祖先的挂载点就是目标的挂载点。）
probe="${TARGET}"
while [[ ! -d "${probe}" ]]; do probe="$(dirname "${probe}")"; done
containing_mount="$(findmnt -n -o TARGET --target "${probe}" 2>/dev/null | tail -1 || true)"
[[ -n "${containing_mount}" ]] || fail "无法确定 ${probe} 所在的挂载点（findmnt 无输出）"
if [[ "${containing_mount}" == "/" ]]; then
    fail "备份目标落在根文件系统上：${TARGET}
       最可能的原因：NAS 未挂载（掉线/未开机），挂载点退化成了同名的空目录。
       现在写下去会「备份成功」且 md5 全过，但 NAS 上什么都没有。
       请先确认已挂载（findmnt --target ${probe}）再重跑。"
fi

# 源目录不存在 = 这台机器本来就没有本机栏目（公开仓形态）。
# 明确报错而不是"成功备份了 0 个文件"：后者会在真正丢目录时伪装成一切正常。
[[ -d "${SRC_DIR}" ]] || fail "源目录不存在: ${SRC_DIR}（本机没有该栏目，无需备份）"

# 空目录同理：备份成功但什么都没保护，是最坏的假绿。
if [[ -z "$(find "${SRC_DIR}" -type f -print -quit)" ]]; then
    fail "源目录是空的: ${SRC_DIR}（拒绝把「备份了 0 个文件」报告成成功）"
fi

command -v rsync >/dev/null 2>&1 || fail "未找到 rsync"

SRC_COUNT=$(find "${SRC_DIR}" -type f | wc -l | tr -d ' ')
log "源: ${SRC_DIR}（${SRC_COUNT} 个文件）"
log "目标: ${TARGET}"

# ── 2. 同步 ───────────────────────────────────────────────────
# --inplace 是必需的，不是优化：CIFS/SMB 目标上 rsync 默认的"写临时名再改名"
# 会丢文件（下划线开头的尤其容易，已实际踩过）。--inplace 就地覆写，绕开该行为。
RSYNC_ARGS=(-a --inplace --no-perms --no-owner --no-group)
if [[ "${DRY_RUN}" == "--dry-run" ]]; then
    RSYNC_ARGS+=(--dry-run -v)
    log "dry-run：只列出会传什么，不写目标"
fi

mkdir -p "${TARGET}"
rsync "${RSYNC_ARGS[@]}" "${SRC_DIR}/" "${TARGET}/"

if [[ "${DRY_RUN}" == "--dry-run" ]]; then
    ok "dry-run 结束（未写入）"
    exit 0
fi

# ── 3. 逐文件 md5 比对 ────────────────────────────────────────
# NAS/SMB 已知会**静默截断**（传输"成功"但内容不完整），所以不能只看 rsync 退出码。
# 逐文件比对是唯一能发现它的手段。
log "校验 md5 ..."
MISMATCH=0
MISSING=0
while IFS= read -r rel; do
    src_file="${SRC_DIR}/${rel}"
    dst_file="${TARGET}/${rel}"
    if [[ ! -f "${dst_file}" ]]; then
        echo "  缺失: ${rel}" >&2
        MISSING=$((MISSING + 1))
        continue
    fi
    src_md5=$(md5sum "${src_file}" | cut -d' ' -f1)
    dst_md5=$(md5sum "${dst_file}" | cut -d' ' -f1)
    if [[ "${src_md5}" != "${dst_md5}" ]]; then
        echo "  不一致: ${rel}（源 ${src_md5} / 目标 ${dst_md5}）" >&2
        MISMATCH=$((MISMATCH + 1))
    fi
done < <(cd "${SRC_DIR}" && find . -type f | sed 's|^\./||')

(( MISSING == 0 )) || fail "备份不完整：${MISSING} 个文件缺失"
(( MISMATCH == 0 )) || fail "备份损坏：${MISMATCH} 个文件 md5 不一致（NAS 静默截断？）"

ok "备份完成并校验通过：${SRC_COUNT} 个文件全部一致"
