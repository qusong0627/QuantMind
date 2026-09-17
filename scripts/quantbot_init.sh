#!/usr/bin/env bash
# ============================================================================
# quantbot-init.sh — QuantBot【legacy QwenPaw】初始化脚本
# ============================================================================
# ⚠️ QuantBot 默认后端已切换为 dsh（DeepSeek Harness）：技能经 ./skills 只读挂载
#    （容器内 /root/.dsh/skills）、人格在 docker/dsh/{dsh.cordis.yml,AGENTS.md}，
#    改仓库 + docker compose restart dsh 即生效，无需本脚本。
#    本脚本仅用于【回滚启用 legacy QwenPaw】时（docker compose --profile legacy up -d qwenpaw）
#    的技能/人格注入。
# 用途：QwenPaw 容器装好后，一键完成：
#   1. 将本地 skills/ 目录全部技能包安装到 QwenPaw 技能池
#   2. 广播到目标工作区并启用
#   3. 写入量化人格（SOUL.md / PROFILE.md / AGENTS.md）
#
# 用法：
#   bash scripts/quantbot_init.sh                          # 全量初始化（技能+人格）
#   bash scripts/quantbot_init.sh --skills-only            # 只装技能
#   bash scripts/quantbot_init.sh --persona-only            # 只写人格
#
# 环境变量（可选，默认自动探测）：
#   QWENPAW_BASE_URL    QwenPaw API 地址（默认 http://127.0.0.1:8088）
#   QWENPAW_AGENT_ID    目标工作区（默认 default）
# ============================================================================
set -euo pipefail

SKILLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/skills"
PERSONA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/config/qwenpaw"
# QwenPaw 容器内兜底：/app/scripts 是独立只读挂载，仓库根在 /quantmind
[[ -d "$SKILLS_DIR" ]] || SKILLS_DIR="/quantmind/skills"
[[ -d "$PERSONA_DIR" ]] || PERSONA_DIR="/quantmind/config/qwenpaw"
QWENPAW_BASE_URL="${QWENPAW_BASE_URL:-http://127.0.0.1:8088}"
QWENPAW_AGENT_ID="${QWENPAW_AGENT_ID:-default}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

log()  { echo -e "\033[1;32m[quantbot-init]\033[0m $*"; }
warn() { echo -e "\033[1;33m[quantbot-init]\033[0m $*"; }
die()  { echo -e "\033[1;31m[quantbot-init]\033[0m $*" >&2; exit 1; }

MODE="all"
case "${1:-}" in
  --skills-only) MODE="skills" ;;
  --persona-only) MODE="persona" ;;
  "") ;;
  *) die "未知参数: $1（支持 --skills-only / --persona-only）" ;;
esac

if ! command -v curl >/dev/null 2>&1; then die "需要 curl"; fi
if [[ "$MODE" != "persona" ]] && ! command -v python3 >/dev/null 2>&1; then die "需要 python3（--persona-only 模式不需要）"; fi

# ---------------------------------------------------------------------------
# 1. 技能安装
# ---------------------------------------------------------------------------
install_skills() {
  log "==> 技能安装 [$QWENPAW_BASE_URL]"

  # 健康检查
  if ! curl -sf -o /dev/null "$QWENPAW_BASE_URL/health"; then
    die "QwenPaw 不可达: $QWENPAW_BASE_URL/health"
  fi

  # 1.1 打包本地技能（每个技能目录必须含 SKILL.md，zip 根为技能目录）
  # 用 python zipfile 打包，避免依赖宿主机 zip 命令
  local zip_file="$TMP_DIR/quantmind_skills.zip"
  python3 - "$SKILLS_DIR" "$zip_file" <<'PYEOF'
import pathlib
import sys
import zipfile

src, dst = pathlib.Path(sys.argv[1]), sys.argv[2]
with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
    for p in sorted(src.rglob("*")):
        if p.is_file():
            zf.write(p, p.relative_to(src).as_posix())
PYEOF

  # 1.2 删除同名旧技能（池中同名技能会冲突；删除后上传最新版）
  # 技能清单从本地 skills/ 目录动态枚举（含 SKILL.md 的目录），避免硬编码过期
  local -a pool_names=()
  local d
  for d in "$SKILLS_DIR"/*/; do
    [[ -f "${d}SKILL.md" ]] && pool_names+=("$(basename "$d")")
  done
  [[ ${#pool_names[@]} -gt 0 ]] || die "技能目录为空: $SKILLS_DIR"
  log "    本地技能清单: ${#pool_names[@]} 个"
  for name in "${pool_names[@]}"; do
    local resp
    resp="$(curl -s -X DELETE "$QWENPAW_BASE_URL/api/skills/pool/$name")"
    # 不存在时服务端返回 409 "cannot be deleted"（非 404），同样视为正常继续
    [[ "$resp" == *'"deleted":true'* || "$resp" == *'404'* || "$resp" == *'Not Found'* || "$resp" == *'cannot be deleted'* ]] \
      || warn "删除池技能 $name 返回: $resp"
  done

  # 1.3 上传到技能池
  local upload_resp
  upload_resp="$(curl -s -X POST "$QWENPAW_BASE_URL/api/skills/pool/upload-zip" \
    -F "file=@$zip_file")"
  echo "$upload_resp" | grep -q '"conflicts":\[\]' \
    || die "技能池上传失败: $upload_resp"
  log "    技能池导入: $(echo "$upload_resp" | grep -o '"imported":\[[^]]*\]' | head -c 200) ..."

  # 1.4 广播到目标工作区（overwrite 幂等；清单与本地 skills/ 目录一致）
  local count=0
  local dl_resp
  for name in "${pool_names[@]}"; do
    dl_resp="$(curl -s -X POST "$QWENPAW_BASE_URL/api/skills/pool/download" \
      -H "Content-Type: application/json" \
      -d "{\"skill_name\":\"$name\",\"targets\":[{\"workspace_id\":\"$QWENPAW_AGENT_ID\"}],\"overwrite\":true}")"
    echo "$dl_resp" | grep -q '"downloaded":\[' \
      || die "技能广播失败 [$name]: $dl_resp"
    count=$((count + 1))
  done
  log "    已广播 $count 个量化技能到工作区 '$QWENPAW_AGENT_ID'"
}

# ---------------------------------------------------------------------------
# 2. 人格写入
# ---------------------------------------------------------------------------
install_persona() {
  log "==> 人格写入（SOUL/PROFILE/AGENTS）"

  # QwenPaw 工作区标准路径（WORKING_DIR/workspaces/{agent_id}，
  # 容器内 WORKING_DIR 一般为 /app/working）
  local ws="/app/working/workspaces/$QWENPAW_AGENT_ID"

  if [[ -d "$ws" ]]; then
    # 在 QwenPaw 容器内执行本脚本时直接写
    for f in SOUL.md PROFILE.md AGENTS.md; do
      [[ -f "$PERSONA_DIR/$f" ]] || die "人格文件缺失: $PERSONA_DIR/$f"
      install -m 644 "$PERSONA_DIR/$f" "$ws/$f"
      log "    已写入 $ws/$f"
    done
  elif command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' | grep -qx qwenpaw; then
    # 宿主机执行时经 docker cp 写入 qwenpaw 容器
    for f in SOUL.md PROFILE.md AGENTS.md; do
      [[ -f "$PERSONA_DIR/$f" ]] || die "人格文件缺失: $PERSONA_DIR/$f"
      docker cp "$PERSONA_DIR/$f" "qwenpaw:$ws/$f"
      log "    已 docker cp 写入 qwenpaw:$ws/$f"
    done
  else
    warn "工作区不可达（无 $ws 也无 qwenpaw 容器），跳过人格写入"
    warn "手动执行: docker cp config/qwenpaw/SOUL.md qwenpaw:/app/working/workspaces/default/"
  fi
}

# ---------------------------------------------------------------------------
case "$MODE" in
  skills)  install_skills ;;
  persona) install_persona ;;
  all)     install_skills; install_persona ;;
esac

log "完成。重新登录 QuantBot 控制台或重启 qwenpaw 容器使技能生效："
log "  docker restart qwenpaw"
