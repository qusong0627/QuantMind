#!/usr/bin/env bash
# QuantMind 一键更新脚本
# 核心流程：拉代码 → 重建/重启后端容器 → 跑 data/upgrade_*.sql → 健康检查。
# db/redis/qwenpaw 等基础设施容器不动（restart: unless-stopped 兜底）。
# 用法：sudo bash deploy/update.sh [--ref master] [--remote gitee|github|origin] [--force] [--no-build] [--skip-backup]

set -Eeuo pipefail

PROJECT_DIR="${QUANTMIND_PROJECT_DIR:-/opt/quantmind}"
REF="${QUANTMIND_REF:-master}"
REMOTE="${QUANTMIND_REMOTE:-origin}"   # 项目实际远端是 gitee/github；默认 origin 兼容旧配置
FORCE=false
BUILD=true
SKIP_BACKUP=false

log() { printf '[quantmind-update] %s\n' "$*"; }
die() { log "错误: $*" >&2; exit 1; }

usage() {
    cat <<'EOF'
用法: sudo bash deploy/update.sh [选项]

  --ref <branch|tag>    更新到指定版本（默认 master）
  --remote <name>       远端名（默认 origin；项目实际远端是 gitee/github）
  --force               覆盖服务器上的未提交代码改动，不删除业务数据
  --no-build            跳过核心镜像构建（仅代码改动时用，bind mount 已生效）
  --skip-backup         跳过升级前数据库备份
  -h, --help            显示帮助
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ref) REF="${2:-}"; shift 2 ;;
        --remote) REMOTE="${2:-}"; shift 2 ;;
        --force) FORCE=true; shift ;;
        --no-build) BUILD=false; shift ;;
        --skip-backup) SKIP_BACKUP=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数: $1" ;;
    esac
done

require_root() { [[ $EUID -eq 0 ]] || die '请使用 sudo 执行'; }
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
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    backup_file="$backup_dir/quantmind_pre_update_${stamp}.sql.gz"
    # 从 .env 读库凭据（脚本自身环境变量里 DB_PASSWORD 几乎必为空，须显式加载 .env）
    pg_user="$(grep -E '^DB_USER=' "$PROJECT_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d \"\' || echo quantmind)"
    pg_db="$(grep -E '^DB_NAME=' "$PROJECT_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d \"\' || echo quantmind)"
    pg_pass="$(grep -E '^(DB_PASSWORD|POSTGRES_PASSWORD)=' "$PROJECT_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d \"\' | head -c 200)"
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
  "generated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
    else
        rm -f "$PROJECT_DIR/backend/shared/version.json"
    fi
}

build_core() {
    # 智能判断：只有"会改变镜像层"的文件变更才触发 build。
    # 触发条件（与 docker/Dockerfile.oss / docker-compose.yml 实际 COPY + build 段对齐）：
    #   1) Dockerfile 自身变更
    #      - docker/Dockerfile*
    #      - docker/*.build-args (buildkit 缓存标记)
    #   2) Dockerfile.oss 实际 COPY 的 3 个 requirements 文件
    #      - requirements.txt / requirements/production.txt / requirements/ai.txt
    #   3) docker-compose.yml 中 quantmind.build 段变更（影响 build 行为）
    #      - build.args（如 TORCH_DEVICE skip → cu121）
    #      - build.context / build.dockerfile
    #      - build.target / build.platform / build.cache_from
    # 不触发 build（绝大多数情况）：
    #   - backend/、config/、scripts/ 等都是 bind mount，**不**需要重 build
    #   - ports/volumes/environment 改了只影响运行时，重启即可（restart_services 会处理）
    #   - 子项目 tools/rd-agent/dashboard/... 下的 requirements.txt 各自独立
    # 这样 95% 的纯代码升级从 5min 缩到 30s。
    if ! $BUILD; then
        log '2/4 跳过镜像构建（--no-build 显式指定）'
        return
    fi

    # 触发源：仅影响镜像层的文件（与 Dockerfile.oss 实际 COPY/ARG 对齐）。
    # 不依赖 git reflog / HEAD@{1}——首次部署、浅克隆、--force 下都有效。
    local trigger files build_blk
    trigger=''
    files=(
        "$PROJECT_DIR/requirements.txt"
        "$PROJECT_DIR/requirements/production.txt"
        "$PROJECT_DIR/requirements/ai.txt"
        "$PROJECT_DIR/docker/Dockerfile.oss"
    )
    # Dockerfile + build-args
    for f in "$PROJECT_DIR"/docker/Dockerfile* "$PROJECT_DIR"/docker/*.build-args; do
        [[ -e "$f" ]] && files+=("$f")
    done
    # 计算文件签名：存在则取 sha256（前 64 位），缺失则记 missing
    for f in "${files[@]}"; do
        if [[ -f "$f" ]]; then
            local h
            h="$(sha256sum "$f" 2>/dev/null | awk '{print $1}' | head -c 64 || echo missing)"
            trigger="${trigger}${f}=${h}\n"
        else
            trigger="${trigger}${f}=missing\n"
        fi
    done

    # docker-compose.yml 仅参与"build 段"签名，不再整文件比对：
    # compose 里端口/环境变量/卷等改动不影响镜像层，改动它们不应触发镜像重建。
    # 用 grep 摘出 build 段相关的行（build/context/dockerfile/args/target/... 含 key），
    # 对该子集取 sha256 作为签名；只保留那些真正改变镜像构建的参数。
    build_blk="$(grep -nE 'build:|context:|dockerfile:|args:|target:|cache_from:|TORCH_DEVICE|TORCH_CPU_INDEX_URL' \
        "$PROJECT_DIR/docker-compose.yml" 2>/dev/null | sha256sum \
        | awk '{print $1}' | head -c 64)"
    build_blk="${build_blk:-missing}"
    trigger="${trigger}docker-compose-build=${build_blk}\n"

    local marker marker_dir
    marker_dir="$PROJECT_DIR/.update"
    marker="$marker_dir/deps.sha256"
    local prev
    prev=''
    [[ -f "$marker" ]] && prev="$(cat "$marker" 2>/dev/null || true)"

    # 判定是否需要重建
    local need_build=false need_seed=false
    if ! docker images --format '{{.Repository}}:{{.Tag}}' | grep -qx 'quantmind-oss:latest'; then
        # 镜像根本不存在 → 必须 build（且本次 build 用于初始化镜像）
        need_build=true; need_seed=false
    elif [[ -n "$prev" && "$prev" != "$trigger" ]]; then
        # 有基线且签名变了 → 依赖/构建配置变更 → 需 build
        need_build=true
    elif [[ -z "$prev" ]]; then
        # 镜像已在用、又无签名基线（首次启用新版脚本）→ 仅建基线，不白 rebuild
        need_build=false; need_seed=true
        log "2/4 首次启用构建基线：镜像已存在，仅记录签名，跳过镜像重建"
    else
        # 签名未变 → 跳过
        log "2/4 跳过镜像构建（依赖/构建配置无变化；后端代码 bind mount 已生效）"
        return
    fi

    if $need_build; then
        log "2/4 重建核心后端镜像（检测到依赖/构建配置变更）"
        docker compose -f "$PROJECT_DIR/docker-compose.yml" build quantmind || {
            die "镜像构建失败，请检查以上日志"
        }
    fi
    # build 成功（或无需 build）才落盘新签名（失败不记录，下次重试）
    mkdir -p "$marker_dir"
    printf '%s' "$trigger" > "$marker"
}

# 关键步骤：只重启 application 层容器，**不**碰 db/redis/qwenpaw 等基础设施
# （db 已 restart: unless-stopped，无需脚本干预；碰它才容易翻车）
restart_services() {
    log '3/4 重启后端服务（quantmind + celery；不动 db/redis/qwenpaw）'
    cd "$PROJECT_DIR"
    local services=(quantmind)
    local service
    for service in celery-worker celery-beat; do
        if docker compose config --services | grep -qx "$service"; then
            services+=("$service")
        fi
    done
    docker compose up -d --no-deps --force-recreate "${services[@]}"
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
        docker compose -f "$PROJECT_DIR/docker-compose.yml" up -d --no-deps db \
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
    backup_database
    sync_code
    build_core
    restart_services
    update_database

    # 健康检查：API + celery worker/beat 均就绪才算升级成功。
    # 仅 curl API 不充分——API 可能 200 而 celery 起崩。
    local attempt
    for attempt in {1..30}; do
        # 容器带 healthcheck 时校验为 healthy；无 healthcheck 的基础设施（db/redis）不校验
        local hk
        api_ok=false; celery_ok=false; beat_ok=false
        curl --fail --silent --max-time 3 http://127.0.0.1:8000/health >/dev/null 2>&1 && api_ok=true
        for svc in quantmind-celery quantmind-celery-beat; do
            hk="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$svc" 2>/dev/null)"
            if [[ "$hk" == "healthy" ]]; then
                [[ "$svc" == "quantmind-celery" ]] && celery_ok=true || beat_ok=true
            fi
        done
        if $api_ok && $celery_ok && $beat_ok; then
            log "升级完成 ✓ (HEAD: $(git -C "$PROJECT_DIR" rev-parse --short HEAD))"
            return
        fi
        sleep 2
    done
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
    die 'API/celery 未在 60s 内就绪，请根据上述日志排查'
}

main
