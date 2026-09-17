#!/usr/bin/env bash
# 一键安装本仓库全部技能到 Claude Code 全局技能目录
#
# 用法:
#   bash scripts/install_skills.sh              # 安装到 ~/.claude/skills
#   bash scripts/install_skills.sh <目标目录>    # 安装到指定目录
#
# 说明:
#   - 以仓库 skills/ 为唯一事实源，逐技能整目录同步（rsync 可用时精确镜像；
#     否则 cp 覆盖，上游删除的文件会残留，可手动清理）
#   - QuantBot（dsh 容器）无需本脚本：docker-compose 已把 ./skills 只读挂载
#     为 /root/.dsh/skills，改仓库即生效
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${ROOT}/skills"
DEST="${1:-${HOME}/.claude/skills}"

[ -d "${SRC}" ] || { echo "[fail] 未找到 ${SRC}"; exit 1; }
mkdir -p "${DEST}"

count=0
skipped=0
for d in "${SRC}"/*/; do
    name="$(basename "${d}")"
    [ -f "${d}/SKILL.md" ] || { skipped=$((skipped + 1)); continue; }
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete "${d}" "${DEST}/${name}/"
    else
        rm -rf "${DEST:?}/${name}"
        cp -r "${d}" "${DEST}/${name}"
    fi
    count=$((count + 1))
done

echo "[ok] 已安装 ${count} 个技能 → ${DEST}（跳过 ${skipped} 个非技能目录）"
echo "     重启 Claude Code 会话后生效；QuantBot（dsh 容器）经挂载自动生效，无需本脚本"
