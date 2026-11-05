#!/usr/bin/env bash
# QuantMind 前端发布共享库（2026-11-05）
# ========================================
# 安装（deploy/deploy.sh、deploy/full-deploy.sh）与升级（deploy/update.sh）共用：
#   · 构建 quantmind-web:latest（Vite 产物烘进 nginx 镜像）
#   · 首次安装创建 quantmind-web 容器；已有容器则原地切换静态文件
#
# 开关口径（用户拍板）：
#   · 其它前端功能默认全部安装/更新；
#   · **实盘交易 UI 默认关**——只有部署方自己的 .env 里 ENABLE_REAL_TRADING=true
#     （或显式 VITE_ENABLE_REAL_TRADING=true）才构建实盘入口；
#   · 文档挖掘 UI 出厂默认开，可用 ENABLE_DOC_MINING=false 关闭。
#
# 只定义函数，不产生副作用。调用方需已设置 PROJECT_DIR 并可用 docker CLI。
# 错误一律 return 1（由调用方决定 die/告警），避免 source 时直接退出安装流程。

qm_web_log() { printf '[quantmind-web] %s\n' "$*"; }
qm_web_warn() { printf '[quantmind-web] %s\n' "$*" >&2; }

# 读 .env 布尔开关（同一 key 取最后一次）；缺失/非 true → false。
qm_web_env_flag() {
    local key="$1" default="$2" value
    value="$(grep -E "^[[:space:]]*${key}=" "$PROJECT_DIR/.env" 2>/dev/null \
        | tail -1 | cut -d= -f2- | tr -d "\"'[:space:]" || true)"
    value="$(printf '%s' "${value:-$default}" | tr '[:upper:]' '[:lower:]')"
    if [[ "$value" == "true" ]]; then printf 'true'; else printf 'false'; fi
}

# 实盘 UI 构建开关：显式 VITE_ENABLE_REAL_TRADING > .env ENABLE_REAL_TRADING > false。
qm_web_live_flag() {
    local value
    value="${VITE_ENABLE_REAL_TRADING:-$(qm_web_env_flag ENABLE_REAL_TRADING false)}"
    value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
    if [[ "$value" == "true" ]]; then printf 'true'; else printf 'false'; fi
}

# 文档挖掘 UI 构建开关：显式 VITE_ENABLE_DOC_MINING > .env ENABLE_DOC_MINING > true。
qm_web_doc_flag() {
    local value
    value="${VITE_ENABLE_DOC_MINING:-$(qm_web_env_flag ENABLE_DOC_MINING true)}"
    value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
    if [[ "$value" == "true" ]]; then printf 'true'; else printf 'false'; fi
}

qm_web_container_exists() {
    # docker inspect 对容器名做前缀匹配（quantmind-web 会命中 quantmind-web-xxx），
    # 必须精确比对 Name，避免误判「已存在」。
    [[ "$(docker inspect --format '{{.Name}}' quantmind-web 2>/dev/null || true)" == "/quantmind-web" ]]
}

qm_web_container_running() {
    [[ "$(docker inspect --format '{{.Name}} {{.State.Status}}' quantmind-web 2>/dev/null || true)" \
        == "/quantmind-web running" ]]
}

qm_web_image_commit() {
    docker image inspect quantmind-web:latest \
        --format '{{ index .Config.Labels "qm.git.commit" }}' 2>/dev/null || true
}

# 前端源码身份：electron/ 树 + Dockerfile.web + 构建脚本 + .npmrc 的哈希。
# 用它做镜像复用判据，后端-only 提交不会误判「前端镜像过期」。
qm_web_src_id() {
    local project="${1:-$PROJECT_DIR}" part hash parts=''
    for part in electron docker/Dockerfile.web scripts/frontend .npmrc; do
        hash="$(git -C "$project" rev-parse "HEAD:$part" 2>/dev/null || true)"
        [[ -n "$hash" ]] && parts+="$hash"$'\n'
    done
    if [[ -z "$parts" ]]; then
        git -C "$project" rev-parse HEAD 2>/dev/null || echo unknown
        return
    fi
    printf '%s' "$parts" | sha256sum | cut -c1-16
}

qm_web_image_src() {
    docker image inspect quantmind-web:latest \
        --format '{{ index .Config.Labels "qm.web.src" }}' 2>/dev/null || true
}

# 镜像是否由当前前端源码构建；无 qm.web.src 的旧镜像退回 commit 精确匹配。
qm_web_image_matches() {
    local project="${1:-$PROJECT_DIR}" want have head current
    want="$(qm_web_src_id "$project")"
    have="$(qm_web_image_src)"
    if [[ -n "$want" && -n "$have" && "$want" != "unknown" ]]; then
        [[ "$want" == "$have" ]]
        return
    fi
    head="$(git -C "$project" rev-parse HEAD 2>/dev/null || true)"
    current="$(qm_web_image_commit)"
    [[ -n "$head" && "$current" == "$head" ]]
}

# 构建前端镜像。实盘开关默认 false；不传 project_dir 时用 PROJECT_DIR。
qm_build_web_image() {
    local project="${1:-$PROJECT_DIR}"
    local live doc head src
    live="$(qm_web_live_flag)"
    doc="$(qm_web_doc_flag)"
    head="$(git -C "$project" rev-parse HEAD 2>/dev/null || echo unknown)"
    src="$(qm_web_src_id "$project")"
    qm_web_log "构建前端镜像（实盘 UI: $live · 文档挖掘 UI: $doc · commit: ${head:0:8}）"
    docker build \
        -f "$project/docker/Dockerfile.web" \
        --build-arg "VITE_ENABLE_REAL_TRADING=$live" \
        --build-arg "VITE_ENABLE_DOC_MINING=$doc" \
        --label "qm.git.commit=$head" \
        --label "qm.web.src=$src" \
        -t quantmind-web:latest "$project" \
        || {
            qm_web_warn '前端镜像构建失败'
            return 1
        }
}

# web 容器所在网络：优先沿用现有 web 容器，其次跟随主容器 quantmind。
qm_web_network() {
    local net=''
    if qm_web_container_exists; then
        net="$(docker inspect quantmind-web \
            --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' \
            2>/dev/null | head -1 || true)"
    fi
    if [[ -z "$net" ]]; then
        net="$(docker inspect quantmind \
            --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' \
            2>/dev/null | head -1 || true)"
    fi
    printf '%s' "${net:-quantmind_quantmind-net}"
}

qm_web_restore_backup() {
    local backup_dir="$1"
    if [[ ! -d "$backup_dir" ]] || [[ -z "$(ls -A "$backup_dir" 2>/dev/null || true)" ]]; then
        return 0
    fi
    qm_web_warn '前端校验失败，回滚上一版静态文件'
    docker exec quantmind-web rm -rf /usr/share/nginx/html/assets >/dev/null 2>&1 || true
    docker cp "$backup_dir/." quantmind-web:/usr/share/nginx/html/ >/dev/null 2>&1 || true
}

# 把 quantmind-web:latest 里的 dist 切换进运行中的容器（清 assets → 全量复制 →
# 校验 main/文件数/HTTP）。任何一步失败先回滚旧静态文件，再 return 1。
qm_deploy_web_dist() {
    if ! qm_web_container_running; then
        qm_web_log '跳过前端静态文件切换（quantmind-web 未运行）'
        return 0
    fi
    local dist_dir=/tmp/quantmind-web-dist
    local backup_dir=/tmp/quantmind-web-backup
    local cid main_ref local_count remote_count http_code
    rm -rf "$dist_dir" "$backup_dir"
    mkdir -p "$dist_dir" "$backup_dir"

    cid="$(docker create quantmind-web:latest)" || {
        qm_web_warn '创建前端产物容器失败'
        return 1
    }
    if ! docker cp "$cid:/usr/share/nginx/html/." "$dist_dir/"; then
        docker rm -f "$cid" >/dev/null 2>&1 || true
        qm_web_warn '从前端镜像提取产物失败'
        return 1
    fi
    docker rm -f "$cid" >/dev/null 2>&1 || true

    main_ref="$(grep -oE 'main-[A-Za-z0-9_-]+\.js' "$dist_dir/index.html" 2>/dev/null | head -1 || true)"
    if [[ -z "$main_ref" || ! -f "$dist_dir/assets/$main_ref" ]]; then
        qm_web_warn "前端产物缺少 main 入口（index.html/main_ref=$main_ref）"
        return 1
    fi

    docker cp quantmind-web:/usr/share/nginx/html/. "$backup_dir/" >/dev/null 2>&1 || true
    qm_web_log "切换 quantmind-web 静态文件 → $main_ref"
    docker exec quantmind-web rm -rf /usr/share/nginx/html/assets >/dev/null 2>&1 || true
    if ! docker exec quantmind-web mkdir -p /usr/share/nginx/html/assets; then
        qm_web_warn '无法创建容器 assets 目录'
        return 1
    fi
    if ! docker cp "$dist_dir/." quantmind-web:/usr/share/nginx/html/; then
        qm_web_restore_backup "$backup_dir"
        qm_web_warn '静态文件复制中断'
        return 1
    fi

    if ! docker exec quantmind-web test -f "/usr/share/nginx/html/assets/$main_ref"; then
        qm_web_restore_backup "$backup_dir"
        qm_web_warn "$main_ref 未复制进容器"
        return 1
    fi
    local_count="$(find "$dist_dir/assets" -type f | wc -l | tr -d ' ' || true)"
    remote_count="$(docker exec quantmind-web sh -c 'ls /usr/share/nginx/html/assets | wc -l' | tr -d ' ' || true)"
    if [[ -z "$local_count" || -z "$remote_count" || "$local_count" != "$remote_count" ]]; then
        qm_web_restore_backup "$backup_dir"
        qm_web_warn "assets 数量不一致：本地 ${local_count:-?} vs 容器 ${remote_count:-?}"
        return 1
    fi
    http_code="$(docker exec quantmind-web curl -s -o /dev/null -w '%{http_code}' \
        "http://127.0.0.1/assets/$main_ref" 2>/dev/null || echo ERR)"
    if [[ "$http_code" != "200" ]]; then
        qm_web_restore_backup "$backup_dir"
        qm_web_warn "nginx GET /assets/$main_ref → $http_code"
        return 1
    fi
    qm_web_log "静态文件切换完成 ✓（$http_code，assets $local_count 个）"
}

# 确保 quantmind-web 容器存在且在跑；不存在则按固定配置创建。
qm_run_web_container() {
    local net port
    net="$(qm_web_network)"
    port="${QUANTMIND_WEB_PORT:-3080}"
    if qm_web_container_exists; then
        if ! qm_web_container_running; then
            qm_web_log '启动已存在的 quantmind-web 容器'
            docker start quantmind-web >/dev/null || return 1
        fi
        return 0
    fi
    qm_web_log "创建 quantmind-web 容器（${port}:80 · network=$net）"
    docker run -d --name quantmind-web \
        -p "${port}:80" \
        --network "$net" \
        --restart unless-stopped \
        --memory 2g --memory-swap 2g \
        quantmind-web:latest \
        >/dev/null \
        || {
            qm_web_warn "创建 quantmind-web 失败（端口 ${port} 是否被占用？）"
            return 1
        }
}

# 安装入口：构建/复用镜像 → 保证容器 → 切换静态文件 → 健康检查。
# 第二参数 reuse：false=总是构建（在线安装/更新）；auto=镜像存在且 commit 匹配则复用
# （离线整机安装，包里带 quantmind-web:latest）；true=只要镜像存在就复用。
qm_install_web() {
    local project="${1:-$PROJECT_DIR}" reuse="${2:-false}"
    local reuse_image=false
    if [[ "$reuse" == "offline" ]] \
        && ! docker image inspect quantmind-web:latest >/dev/null 2>&1; then
        qm_web_warn '离线包未包含 quantmind-web:latest，尝试本地构建（需要 node 基础镜像与 npm 网络）'
    fi
    if docker image inspect quantmind-web:latest >/dev/null 2>&1; then
        case "$reuse" in
            true) reuse_image=true ;;
            auto) qm_web_image_matches "$project" && reuse_image=true ;;
            offline)
                # 离线整机安装：包里带 quantmind-web:latest 就直接用（离线机无法
                # npm install）。commit label 不匹配时只告警——后续一键更新会重建。
                reuse_image=true
                if ! qm_web_image_matches "$project"; then
                    qm_web_warn '离线包内的 quantmind-web 镜像与当前检出 commit 不一致，按包内版本安装（后续可一键更新重建）'
                fi
                ;;
        esac
    fi
    if $reuse_image; then
        qm_web_log '复用现有 quantmind-web:latest（commit/离线包匹配）'
    else
        qm_build_web_image "$project" || return 1
    fi

    if qm_web_container_exists; then
        qm_run_web_container || return 1
        qm_deploy_web_dist || return 1
    else
        qm_run_web_container || return 1
    fi

    local code
    code="$(docker exec quantmind-web curl -s -o /dev/null -w '%{http_code}' \
        http://127.0.0.1/ 2>/dev/null || echo ERR)"
    if [[ "$code" != "200" ]]; then
        qm_web_warn "前端健康检查失败：GET / → $code"
        return 1
    fi
    qm_web_log '前端安装/更新完成 ✓'
}
