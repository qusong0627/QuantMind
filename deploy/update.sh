#!/usr/bin/env bash
# QuantMind 一键更新脚本
# 核心流程（P2-6 不可变发布）：拉代码 → 构建（注戳）→ 晋级 :latest → 强制重建容器
# → 发布断言（无代码挂载 / 运行态戳==检出，不过则自动回滚上一镜像）→ 跑
# data/upgrade_*.sql → 健康检查。
# 生成容器默认走 docker-compose.prod.yml 覆盖层（去全部代码 bind mount）；
# 本地开发不经过本脚本（直接裸用 docker-compose.yml 热更新）。
# db/redis/qwenpaw 等基础设施容器不强制重启（仅 compose 配置漂移时按需重建）。
# 用法：sudo bash deploy/update.sh [--ref <branch>] [--remote gitee|github|origin] [--force] [--no-build] [--mounts] [--skip-backup]
# 不传 --ref 时默认更新到**当前 checkout 的分支**（见 default_ref）。

set -Eeuo pipefail

PROJECT_DIR="${QUANTMIND_PROJECT_DIR:-/opt/quantmind}"
REF="${QUANTMIND_REF:-}"               # 空 = 未指定，main 里按当前 checkout 的分支解析
REMOTE="${QUANTMIND_REMOTE:-origin}"   # 项目实际远端是 gitee/github；默认 origin 兼容旧配置
FORCE=false
BUILD=true
MOUNTS_MODE=false                      # --mounts：应急退回 bind-mount 模式（默认不可变发布）
SKIP_BACKUP=false

log() { printf '[quantmind-update] %s\n' "$*"; }
die() { log "错误: $*" >&2; exit 1; }
# 诊断信息必须走 stderr：default_ref 的结果是经 $( ) 取的，混进 stdout 会被
# 当成 REF 的一部分（多行字符串），后面 fetch/checkout 会以一个不存在的 ref 失败。
warn() { printf '[quantmind-update] %s\n' "$*" >&2; }

# 未显式 --ref/QUANTMIND_REF 时的默认版本 = 宿主机当前 checkout 的分支。
# 裸跑 update.sh **不应该切换分支**：本仓 next 与 master 已分叉（双向都有独有提交），
# 原先硬编码的默认 master 会把 next 上的部署整体倒退到 master，而且没有任何确认提示，
# 表现就是「点了一下更新系统，功能少了一批」。要换分支必须显式 --ref。
default_ref() {
    local br
    br="$(git -C "$PROJECT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    # 非 git 仓库（未走部署脚本的裸目录）或 detached HEAD 都返回 "HEAD"/空，
    # 此时无法「跟随当前分支」，退回 master 保持旧行为。
    if [[ -z "$br" || "$br" == "HEAD" ]]; then
        warn "无法识别 $PROJECT_DIR 的当前分支（非 git 仓库或 detached HEAD），回退 master"
        br="master"
    fi
    printf '%s' "$br"
}

usage() {
    cat <<'EOF'
用法: sudo bash deploy/update.sh [选项]

  --ref <branch|tag>    更新到指定版本（默认：当前 checkout 的分支；识别不到时为 master）
  --remote <name>       远端名（默认 origin；项目实际远端是 gitee/github）
  --force               覆盖服务器上的未提交代码改动，不删除业务数据
  --no-build            跳过镜像构建（仅当镜像身份==检出 commit 时允许；代码变更必须重建）
  --mounts              应急：退回 bind-mount 模式（默认走 docker-compose.prod.yml 不可变发布）
  --skip-backup         跳过升级前数据库备份
  -h, --help            显示帮助
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ref) REF="${2:-}"; shift 2 ;;
        --remote) REMOTE="${2:-}"; shift 2 ;;
        --force|-force) FORCE=true; shift ;;
        --no-build) BUILD=false; shift ;;
        --mounts|-mounts) MOUNTS_MODE=true; shift ;;
        --skip-backup) SKIP_BACKUP=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数: $1（force 请用 --force）" ;;
    esac
done

if [[ -z "$REF" ]]; then
    REF="$(default_ref)"
    log "未指定 --ref，更新到当前 checkout 的分支：$REF"
fi

require_root() {
    if [[ $EUID -eq 0 ]]; then return; fi
    # 无 sudo 时：若用户在 docker 组且对项目目录可写，仅告警后继续；否则再阻断
    if groups 2>/dev/null | grep -qw docker && [[ -w "$PROJECT_DIR" ]] && docker ps >/dev/null 2>&1; then
        log "提示: 未使用 sudo，但检测到 docker 权限正常，继续执行"
        return
    fi
    log "提示: 未使用 sudo 且 docker 权限不足，尝试继续（失败请改用 sudo 或将用户加入 docker 组）"
}
record_system_event() {
    # 写入 system_events，供管理后台“最近事件”展示；失败不阻断主流程
    local _level="$1" _title="$2" _msg="${3:-}"
    local _pg_user
    _pg_user="$(grep -E '^DB_USER=' "$PROJECT_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d \"\' || echo quantmind)"
    _pg_user="${_pg_user:-quantmind}"
    # 转义单引号
    local _t_esc _m_esc
    _t_esc="$(printf '%s' "$_title" | sed "s/'/''/g")"
    _m_esc="$(printf '%s' "$_msg" | sed "s/'/''/g" | head -c 4000)"
    docker exec -e PGUSER="$_pg_user" quantmind-db psql -U "$_pg_user" -v ON_ERROR_STOP=0 \
        -c "INSERT INTO system_events (event_type, level, source, title, message) VALUES ('system_update', '$_level', 'updater', '$_t_esc', '$_m_esc')" >/dev/null 2>&1 || true
}

require_project() {
    [[ -d "$PROJECT_DIR/.git" ]] || die "不是 Git 部署目录: $PROJECT_DIR"
    [[ -f "$PROJECT_DIR/docker-compose.yml" ]] || die "缺少 docker-compose.yml: $PROJECT_DIR"
    command -v docker >/dev/null || die 'Docker 未安装'
    docker compose version >/dev/null || die 'Docker Compose 不可用'
}

# 升级前数据库快照（防御性：失败不阻断升级；可 --skip-backup 跳过）
backup_database() {
    if $SKIP_BACKUP; then
        log '跳过升级前数据库备份（--skip-backup）'
        return
    fi
    if ! docker ps --format '{{.Names}}' | grep -qx 'quantmind-db'; then
        log 'quantmind-db 未运行，跳过备份'
        return
    fi
    local backup_dir="$PROJECT_DIR/data/backups"
    mkdir -p "$backup_dir"
    local stamp backup_file pg_user pg_db pg_pass
    stamp="$(TZ=Asia/Shanghai date +%Y%m%dT%H%M%S+08:00)"
    backup_file="$backup_dir/quantmind_pre_update_${stamp}.sql.gz"
    # 从 .env 读库凭据（脚本自身环境变量里 DB_PASSWORD 几乎必为空，须显式加载 .env）
    pg_user="$(grep -E '^DB_USER=' "$PROJECT_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d \"\' || echo quantmind)"
    pg_db="$(grep -E '^DB_NAME=' "$PROJECT_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d \"\' || echo quantmind)"
    pg_pass="$(grep -E '^(DB_PASSWORD|POSTGRES_PASSWORD)=' "$PROJECT_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d \"\' | head -c 200 || echo '')"
    if [[ -z "$pg_pass" ]]; then
        pg_pass="${POSTGRES_PASSWORD:-${DB_PASSWORD:-quantmind2026}}"
    fi
    if docker exec -e PGPASSWORD="$pg_pass" quantmind-db \
            pg_dump -U "$pg_user" -d "$pg_db" --no-owner --no-acl --clean --if-exists 2>/dev/null \
            | gzip > "$backup_file"; then
        log "数据库备份完成: $backup_file ($(du -h "$backup_file" | cut -f1))"
    else
        log "数据库备份失败（不影响升级）"
        rm -f "$backup_file"
    fi
}

sync_code() {
    log "1/4 同步代码：$REMOTE/$REF"
    if ! git -C "$PROJECT_DIR" diff --quiet || ! git -C "$PROJECT_DIR" diff --cached --quiet; then
        $FORCE || die '检测到未提交代码改动；确认覆盖请加 --force'
        git -C "$PROJECT_DIR" reset --hard
        # 仅清理"纯源码"区域里仓库未跟踪的残渣；显式豁免所有可能存运维数据/配置的目录。
        # 注意：-x 不启用（不删 .gitignore 的文件）；白名单与 .gitignore 的 ! 放行保持一致，
        # 以免误删 data/upgrade_*.sql（已跟踪，本不受 clean 影响）或 servers 上零时脚本。
        git -C "$PROJECT_DIR" clean -fd \
            -e data -e models -e db -e logs -e user_pools_local -e .env \
            -e .update -e .env.bak -e .env.bak.* -e secrets -e certs
    fi

    # 拉远端：指定远端不存在时回退 fetch --all（项目实际是 gitee/github）
    if git -C "$PROJECT_DIR" remote get-url "$REMOTE" >/dev/null 2>&1; then
        git -C "$PROJECT_DIR" fetch "$REMOTE" "$REF" || die "git fetch $REMOTE $REF 失败"
        local fetched_ref="FETCH_HEAD"
    else
        log "  远端 $REMOTE 不存在，回退 fetch --all"
        git -C "$PROJECT_DIR" fetch --all "$REF" 2>/dev/null || die "git fetch 失败"
        local fetched_ref
        fetched_ref="$(git -C "$PROJECT_DIR" for-each-ref --format='%(refname)' \
                        "refs/remotes/*/$REF" | head -1 || true)"
        fetched_ref="${fetched_ref:-$REF}"
    fi

    git -C "$PROJECT_DIR" checkout -B "$REF" "$fetched_ref" 2>/dev/null \
        || git -C "$PROJECT_DIR" checkout --detach "$fetched_ref" \
        || die "checkout $REF 失败"

    # 写入版本号到 backend/shared/version.{txt,json}（.gitignore 内）
    git -C "$PROJECT_DIR" describe --tags --always \
        > "$PROJECT_DIR/backend/shared/version.txt" 2>/dev/null \
        || rm -f "$PROJECT_DIR/backend/shared/version.txt"

    local head_sha head_describe
    head_sha="$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null || true)"
    head_describe="$(git -C "$PROJECT_DIR" describe --tags --always 2>/dev/null || echo dev)"
    if [[ -n "$head_sha" ]]; then
        cat > "$PROJECT_DIR/backend/shared/version.json" <<EOF
{
  "version": "$head_describe",
  "commit": "$head_sha",
  "branch": "$REF",
  "generated_at": "$(TZ=Asia/Shanghai date +%Y-%m-%dT%H:%M:%S+08:00)"
}
EOF
    else
        rm -f "$PROJECT_DIR/backend/shared/version.json"
    fi

    # 部署提交已变：清掉版本检查缓存（version_check.json），避免管理页
    # 继续显示过期的「落后 N 个提交」。
    rm -f "${STORAGE_ROOT:-$PROJECT_DIR/data}/version_check.json" \
        /data/version_check.json 2>/dev/null || true
}

build_core() {
    # 不可变发布（P2-6）构建闸门：镜像身份（qm.git.commit Label）== 检出 HEAD 才允许
    # 复用；不等（含旧镜像无戳）→ 重建。requirements 指纹不再单独触发——依赖变化必然
    # 伴随 commit 变化，commit 相等即镜像层输入全等。
    # bind-mount 回退模式（--mounts / 旧 tag 检出无覆盖层 / compose 过旧）维持旧语义：
    # 镜像存在即跳过，代码由挂载活供。
    local head_sha=''
    if [[ "${QM_PROD_OVERLAY_OK:-false}" == true ]]; then
        head_sha="$(qm_release_head_sha "$PROJECT_DIR")"
        [[ -n "$head_sha" && "$head_sha" != "unknown" ]] \
            || die '无法确定检出 commit（git rev-parse 失败），不可变发布中止'
    fi

    if ! $BUILD; then
        if [[ "${QM_PROD_OVERLAY_OK:-false}" == true ]]; then
            local cur_sha
            cur_sha="$(qm_image_git_commit quantmind-oss:latest)"
            [[ "$cur_sha" == "$head_sha" ]] \
                || die "--no-build 与不可变发布不兼容（镜像身份 ${cur_sha:-无} ≠ 检出 ${head_sha:0:12}）：去掉 --no-build 重建，或 --mounts 显式回退"
        fi
        log '2/4 跳过镜像构建（--no-build 显式指定）'
        return
    fi

    # 是否重建：不可变模式 = 镜像身份 == 检出 HEAD；回退模式 = 仅镜像缺失时建。
    local need_build=false reason=''
    if ! docker images --format '{{.Repository}}:{{.Tag}}' | grep -qx 'quantmind-oss:latest'; then
        need_build=true; reason='镜像不存在'
    elif [[ "${QM_PROD_OVERLAY_OK:-false}" == true ]]; then
        local image_sha
        image_sha="$(qm_image_git_commit quantmind-oss:latest)"
        if [[ -z "$image_sha" ]]; then
            need_build=true; reason='镜像无身份戳（T7-3 之前的旧镜像或手工构建）'
        elif [[ "$image_sha" != "$head_sha" ]]; then
            need_build=true; reason="镜像身份 ${image_sha:0:12} ≠ 检出 ${head_sha:0:12}"
        fi
    fi
    if ! $need_build; then
        if [[ "${QM_PROD_OVERLAY_OK:-false}" == true ]]; then
            log "2/4 镜像已对应当前检出（${head_sha:0:12}），跳过重建"
        else
            log '2/4 跳过镜像构建（bind-mount 回退模式：镜像已存在）'
        fi
        return
    fi

    # torch 形态解析（构建参数口径；写入 .env 避免下次再推断）
    local torch_val
    torch_val="${TORCH_DEVICE:-$(grep -E '^[[:space:]]*TORCH_DEVICE=' "$PROJECT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' " || true)}"
    if [[ -z "$torch_val" ]]; then
        # 唯一推断实现：deploy/req-fingerprint.sh --infer-torch
        # （含「旧指纹对不上当前代码」时的 torch 探测回落，禁止在此 die）
        torch_val="$(bash "$PROJECT_DIR/deploy/req-fingerprint.sh" --infer-torch \
            "$PROJECT_DIR" quantmind-oss:latest 2>/dev/null || true)"
        if [[ -n "$torch_val" ]]; then
            log "2/4 未指定 TORCH_DEVICE，已从镜像推断/回落为 $torch_val（写入 .env，避免下次再拦）"
            export TORCH_DEVICE="$torch_val"
            if [[ -f "$PROJECT_DIR/.env" ]]; then
                if grep -qE '^[[:space:]]*TORCH_DEVICE=' "$PROJECT_DIR/.env"; then
                    sed -i "s|^[[:space:]]*TORCH_DEVICE=.*|TORCH_DEVICE=${torch_val}|" "$PROJECT_DIR/.env"
                else
                    printf '\n# torch 形态（update.sh 写入，用于依赖指纹对齐）\nTORCH_DEVICE=%s\n' \
                        "$torch_val" >> "$PROJECT_DIR/.env"
                fi
            fi
        else
            log '2/4 未指定 TORCH_DEVICE 且无可用镜像推断，按 skip 形态构建'
        fi
    fi

    log "2/4 重建核心后端镜像（$reason）"
    # 旧依赖指纹 marker（.update/deps.sha256）已废弃：镜像身份改由 commit 决定；
    # 残留文件无害，可手动删除。
    # 把依赖指纹同步写入镜像 Label（qm.req.sha），与 full-deploy 的指纹闸门共用一套口径。
    local req_sha
    req_sha="$(bash "$PROJECT_DIR/deploy/req-fingerprint.sh" "$PROJECT_DIR" 2>/dev/null || true)"
    # 部署真相戳（T7-3）：构建时刻的代码身份一并写进镜像 LABEL/戳文件，
    # docker inspect 与容器内启动打点都能核对「镜像由哪版代码构建」。
    local git_commit git_branch git_dirty
    git_commit="$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
    git_branch="$(git -C "$PROJECT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
    if [[ -n "$(git -C "$PROJECT_DIR" status --porcelain 2>/dev/null)" ]]; then
        git_dirty=true
    else
        git_dirty=false
    fi
    QM_REQ_SHA="${req_sha:-unknown}" QM_GIT_COMMIT="$git_commit" \
        QM_GIT_BRANCH="$git_branch" QM_GIT_DIRTY="$git_dirty" \
        docker compose -f "$PROJECT_DIR/docker-compose.yml" build quantmind || {
        die "镜像构建失败，请检查以上日志"
    }

    # 构建后立即做身份断言（尚未触碰运行容器，fail-fast 在重启之前）
    if [[ "${QM_PROD_OVERLAY_OK:-false}" == true ]]; then
        local built_sha
        built_sha="$(qm_image_git_commit quantmind-oss:latest)"
        [[ "$built_sha" == "$head_sha" ]] \
            || die "构建产物身份断言失败：镜像=${built_sha:-无} 检出=${head_sha:0:12}（检查构建侧 QM_GIT_COMMIT 注入）"
        log "2/4 镜像已注戳：quantmind-oss:latest @ ${head_sha:0:12}"
    fi
}

# 关键步骤：强制重建 application 层容器（不可变模式下让新镜像生效；回退模式下让
# 挂载代码随进程重启生效），其余服务（含 db/redis/dsh）不强制重启，仅在 compose
# 配置发生漂移时按需重建。
restart_services() {
    log '3/4 重启后端服务（强制重建 quantmind + celery）'
    local services=(quantmind)
    local service
    for service in celery-worker celery-beat; do
        if "${QM_COMPOSE[@]}" config --services | grep -qx "$service"; then
            services+=("$service")
        fi
    done
    "${QM_COMPOSE[@]}" up -d --no-deps --force-recreate "${services[@]}"

    # legacy 迁移（一次性）：QuantBot 后端已由 qwenpaw 切换为 dsh；旧 qwenpaw 若仍在
    # 运行会与 dsh 抢宿主 8088 → 自动停掉（数据卷保留；回滚见 compose 中 qwenpaw 注释）。
    if "${QM_COMPOSE[@]}" config --services 2>/dev/null | grep -qx dsh \
        && docker ps --format '{{.Names}}' | grep -qx qwenpaw; then
        log '    停止 legacy qwenpaw 容器（已由 dsh 接管 8088；回滚用 --profile legacy）'
        docker stop qwenpaw >/dev/null 2>&1 || true
    fi

    # 配置漂移 reconcile：对其余服务执行一次 up -d，Compose 按配置 hash 仅重建
    # 端口/环境/镜像/挂载发生变化的容器，未变更者原地不动（db/redis 不会被无谓重启）。
    # 修复场景：改了 dsh 的绑定/环境等 compose 配置后，update 流程此前从不重建它，
    # 导致改动长期不生效。
    local others=()
    while IFS= read -r service; do
        [[ -z "$service" ]] && continue
        [[ " ${services[*]} " == *" $service "* ]] && continue
        # qwenpaw 已改 legacy profile：不参与 drift reconcile（各版本 compose 对 profile
        # 服务的 config --services 过滤行为不一致，显式跳过最稳）
        [[ "$service" == "qwenpaw" ]] && continue
        others+=("$service")
    done < <("${QM_COMPOSE[@]}" config --services)
    if (( ${#others[@]} > 0 )); then
        "${QM_COMPOSE[@]}" up -d --no-deps "${others[@]}"
    fi
}

# 载入发布共享库并初始化 compose 文件组（生产覆盖层 or bind-mount 回退）。
load_release_lib() {
    local lib="$PROJECT_DIR/deploy/release_lib.sh"
    [[ -f "$lib" ]] || die "缺少 $lib（代码未同步完整？）"
    # shellcheck source=/dev/null
    source "$lib"
    qm_compose_init "$PROJECT_DIR" "$MOUNTS_MODE" \
        || die 'compose 文件组初始化失败（见上方指引：升级 docker compose 或 --mounts 临时回退）'
    if [[ "${QM_PROD_OVERLAY_OK:-false}" == true ]]; then
        log '发布模式：不可变（docker-compose.prod.yml，容器无代码挂载）'
    else
        log '发布模式：bind-mount 回退（本次不做不可变断言）'
    fi
}

# 发布断言（P2-6）：核心判定在 release_lib.sh 的 qm_release_verify_runtime（与
# full-deploy 共用）；这里只加「失败 → 回滚上一镜像」。prev_image 为空时只报错不回滚。
assert_release_runtime() {
    local prev_image="$1"
    [[ "${QM_PROD_OVERLAY_OK:-false}" == true ]] || return 0
    local head_sha fail_msg=''
    head_sha="$(qm_release_head_sha "$PROJECT_DIR")"

    if ! fail_msg="$(qm_release_verify_runtime "$PROJECT_DIR" "$head_sha")"; then
        record_system_event "error" "发布断言失败" "$fail_msg"
        log "发布断言失败：$fail_msg" >&2
        if [[ -n "$prev_image" ]] && docker image inspect "$prev_image" >/dev/null 2>&1; then
            log "回滚：quantmind-oss:latest ← $prev_image（上一镜像）" >&2
            if docker tag "$prev_image" quantmind-oss:latest; then
                restart_services || true
            fi
        else
            log '无可回滚的上一镜像（首次不可变发布），容器留在现场待排查' >&2
        fi
        die "发布断言失败（已回滚到上一镜像）：$fail_msg"
    fi
    log '发布断言通过：三容器零代码挂载，运行态戳==检出 HEAD'
}

# 跑 data/upgrade_*.sql —— 这是用户最关心的"执行 SQL"主流程。
# db 健康检查：短等待 15×2s=30s（覆盖 db 首次启动 / restart 窗口），
# 不健康立即报错而不是傻等。
update_database() {
    log '4/4 执行数据库升级 SQL (data/upgrade_*.sql)'
    local max_attempts=15
    local attempt
    local pg_user
    pg_user="$(grep -E '^DB_USER=' "$PROJECT_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d \"\' || echo quantmind)"

    # 确认 db 容器在跑
    if ! docker ps --format '{{.Names}}' | grep -qx 'quantmind-db'; then
        log '  启动 quantmind-db'
        "${QM_COMPOSE[@]}" up -d --no-deps db \
            || die "启动 quantmind-db 失败"
    fi

    # 短等待 db 接受连接（pg_isready 不阻塞，最多 15×2s=30s）
    for attempt in $(seq 1 "$max_attempts"); do
        if docker exec -e PGUSER="$pg_user" quantmind-db \
                pg_isready -U "$pg_user" >/dev/null 2>&1; then
            break
        fi
        if (( attempt == max_attempts )); then
            log '  quantmind-db 不可达，打印尾部日志：' >&2
            docker logs --tail 50 quantmind-db >&2 || true
            die 'quantmind-db 不可用，请先修复 db 容器（多数情况是数据卷权限/PG 版本/.env 不一致）'
        fi
        sleep 2
    done

    # 幂等版本追踪表：记录已应用的 migration 文件名，天然防重放，并支撑按版本排序。
    pg_user="${pg_user:-quantmind}"
    docker exec -i -e PGUSER="$pg_user" quantmind-db \
        sh -lc 'psql -U "$PGUSER" -v ON_ERROR_STOP=1' >/dev/null 2>&1 <<EOSQL
CREATE TABLE IF NOT EXISTS schema_migrations (
    file_name  TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
EOSQL
    # 兼容既有布防：首次启用版本追踪（表为空）时，把当前仓库里的 upgrade 文件全部标记为已应用，
    # 避免此前裸跑已生效的 v1.0.x 被重放。
    local seed_count names vals
    seed_count="$(docker exec -e PGUSER="$pg_user" quantmind-db \
        psql -U "$pg_user" -tAc 'SELECT count(*) FROM schema_migrations' 2>/dev/null | tr -d ' ' || echo 0)"
    if [[ "$seed_count" == "0" ]]; then
        names=()
        for p in "$PROJECT_DIR"/data/upgrade_*.sql; do
            [[ -e "$p" ]] && names+=("$(basename "$p")")
        done
        if [[ ${#names[@]} -gt 0 ]]; then
            vals=""
            for n in "${names[@]}"; do
                vals="${vals:+$vals,}('${n}')"
            done
            docker exec -e PGUSER="$pg_user" quantmind-db \
                psql -U "$pg_user" -v ON_ERROR_STOP=1 -c \
                "INSERT INTO schema_migrations (file_name) VALUES ${vals} ON CONFLICT (file_name) DO NOTHING" \
                >/dev/null 2>&1
            log "  首次启用版本追踪：将现有 ${#names[@]} 个 upgrade 标记为已应用"
        fi
    fi

    # 按版本号排序（sort -V 而非字典序），遍历并跳过已应用项
    local patch applied
    applied=0
    while IFS= read -r patch; do
        [[ -e "$patch" ]] || continue
        local b
        b="$(basename "$patch")"
        local already
        already="$(docker exec -e PGUSER="$pg_user" quantmind-db \
            psql -U "$pg_user" -tAc "SELECT count(*) FROM schema_migrations WHERE file_name='$b'" 2>/dev/null | tr -d ' ' || echo 0)"
        if [[ "$already" != "0" ]]; then
            continue
        fi
        log "  应用 SQL: $b"
        if ! docker exec -i -e PGUSER="$pg_user" quantmind-db \
                sh -lc 'psql -U "$PGUSER" -v ON_ERROR_STOP=1' < "$patch"; then
            die "SQL 升级失败: $b"
        fi
        # 成功后标记已应用
        docker exec -e PGUSER="$pg_user" quantmind-db \
            psql -U "$pg_user" -c \
            "INSERT INTO schema_migrations (file_name) VALUES ('$b') ON CONFLICT (file_name) DO NOTHING" >/dev/null 2>&1
        log "  ✓ $b 已应用"
        applied=$((applied + 1))
    done < <(for p in "$PROJECT_DIR"/data/upgrade_*.sql; do printf '%s\n' "$p"; done | sort -V)
    if (( applied == 0 )); then
        log '  无新增 upgrade_*.sql（全部已应用）'
    fi
}

main() {
    require_root
    require_project
    record_system_event "info" "系统更新开始" "分支 $REF 远端 $REMOTE"
    backup_database
    sync_code
    load_release_lib
    build_core
    # 重启前记住当前镜像 ID——发布断言失败时按它回滚（首次部署/容器不存在时为空）
    local prev_image=''
    prev_image="$(docker inspect --format '{{.Image}}' quantmind 2>/dev/null || true)"
    restart_services
    assert_release_runtime "$prev_image"
    update_database

    # 健康检查：API + celery worker/beat 均就绪才算升级成功。
    # 仅 curl API 不充分——API 可能 200 而 celery 起崩。
    # 时序注意：容器是 --force-recreate 重建，celery healthcheck 为
    # StartPeriod=60s + Interval=30s + Retries=3，最坏 ~150s 才判 healthy，
    # 因此等待窗口必须 ≥ 180s，否则升级成功后仍会误报"未就绪"。
    local attempt
    for attempt in {1..90}; do
        # 容器带 healthcheck 时校验为 healthy；无 healthcheck 的基础设施（db/redis）不校验
        local hk
        api_ok=false; celery_ok=false; beat_ok=false
        # updater 容器为 bridge 网络，127.0.0.1 指向自身；改走宿主容器 exec，避免 180s 误报失败
        if docker exec quantmind curl --fail --silent --max-time 3 http://127.0.0.1:8000/health >/dev/null 2>&1; then
            api_ok=true
        elif curl --fail --silent --max-time 3 http://127.0.0.1:8000/health >/dev/null 2>&1; then
            api_ok=true
        fi
        for svc in quantmind-celery quantmind-celery-beat; do
            hk="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$svc" 2>/dev/null)"
            if [[ "$hk" == "healthy" ]]; then
                [[ "$svc" == "quantmind-celery" ]] && celery_ok=true || beat_ok=true
            fi
        done
        if $api_ok && $celery_ok && $beat_ok; then
            # admin 身份遗留收口（幂等分块；后台执行不阻塞升级；遗留为空时秒级完成）
            docker exec -d quantmind bash -c 'cd /app && python backend/scripts/fix_admin_identity_full.py > /tmp/admin_identity_sweep.log 2>&1' 2>/dev/null || true
            log "升级完成 ✓ (HEAD: $(git -C "$PROJECT_DIR" rev-parse --short HEAD))"
            record_system_event "info" "系统更新成功" "HEAD $(git -C "$PROJECT_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
            return
        fi
        sleep 2
    done
    record_system_event "error" "系统更新失败" "API/celery 180s 内未就绪，请查看 data/update.log"
    log '健康检查失败，尾部日志：' >&2
    docker logs --tail 100 quantmind >&2 || true
    for svc in quantmind-celery quantmind-celery-beat; do
        if [[ "$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$svc" 2>/dev/null)" != "healthy" ]]; then
            docker logs --tail 50 "$svc" >&2 || true
        fi
    done
    if [[ "$(docker inspect --format '{{.State.Health.Status}}' quantmind-db 2>/dev/null)" != "healthy" ]]; then
        docker logs --tail 50 quantmind-db >&2 || true
    fi
    die 'API/celery 未在 180s 内就绪，请根据上述日志排查'
}

main
