#!/usr/bin/env bash
# QuantMind 发布共享库（P2-6 不可变发布）——update.sh / full-deploy.sh / deploy.sh 共用。
#
# 唯一职责：
#   1) compose 文件组解析（含旧版 compose 的能力探测与 bind-mount 回退告知）；
#   2) 发布断言的原语（镜像身份 / 容器代码挂载面 / 运行态戳），供调用方做晋级与回滚。
# 只定义函数，不产生副作用；除 _qm_release_die 兜底外绝不 exit——流程控制（die/回滚）
# 留在各调用脚本，库只做「判定 + 如实报告」。
#
# 数据类挂载白名单（项目目录下允许出现在生产容器的挂载名）
QM_DATA_MOUNT_NAMES="data models db logs configs user_pools_local alphaagent"

_qm_release_die() {
    if declare -F die >/dev/null 2>&1; then
        die "$@"
    else
        printf '[qm-release] 错误: %s\n' "$*" >&2
        exit 1
    fi
}

# 初始化 compose 文件组。用法: qm_compose_init <project_dir> <bind_mounts:true|false>
#   成功 → 设置 QM_COMPOSE 数组与 QM_PROD_OVERLAY_OK。
#     bind_mounts=true / 覆盖层文件缺失（旧 tag 检出）时：仅用基础 compose（回退模式）。
#   compose 过旧解析不了 !override → 写 stderr 并 return 1（调用方决定 die 或提示 --mounts）。
qm_compose_init() {
    local project_dir="$1" bind_mounts="${2:-false}"
    local base="$project_dir/docker-compose.yml"
    local overlay="$project_dir/docker-compose.prod.yml"
    QM_COMPOSE=()
    QM_PROD_OVERLAY_OK=false
    if [[ ! -f "$base" ]]; then
        _qm_release_die "缺少 $base"
        return 1
    fi
    if [[ "$bind_mounts" == "true" ]]; then
        printf '[qm-release] 警告: 已显式启用 bind-mount 回退模式——不做不可变断言，仅排障用\n' >&2
        QM_COMPOSE=(docker compose -f "$base")
        return 0
    fi
    if [[ ! -f "$overlay" ]]; then
        printf '[qm-release] 警告: 缺少 %s（旧版本/旧 tag 检出？）——本次退回 bind-mount 模式\n' \
            "$overlay" >&2
        QM_COMPOSE=(docker compose -f "$base")
        return 0
    fi
    # 能力探测：!override 需 compose ≥2.24.4。先带覆盖层试解析，失败再试基线——
    # 基线也失败说明是环境/配置问题，不能误诊成「版本太旧」。
    if docker compose -f "$base" -f "$overlay" config -q >/dev/null 2>&1; then
        QM_COMPOSE=(docker compose -f "$base" -f "$overlay")
        QM_PROD_OVERLAY_OK=true
        return 0
    fi
    if docker compose -f "$base" config -q >/dev/null 2>&1; then
        printf '[qm-release] 错误: 当前 docker compose 解析不了 %s（卷替换标签 !override 需 compose ≥2.24.4）。\n' \
            "$overlay" >&2
        printf '[qm-release] 升级: apt-get update && apt-get install -y docker-compose-plugin；\n' >&2
        printf '[qm-release] 临时回退: update.sh --mounts（或 QUANTMIND_BIND_MOUNTS=true）——仅排障用。\n' >&2
        return 1
    fi
    printf '[qm-release] 错误: docker-compose.yml 本身无法解析（.env/语法/compose 版本问题），先跑 docker compose config 排查。\n' >&2
    return 1
}

# 检出 commit（拿不到回退 unknown）。
qm_release_head_sha() {
    git -C "$1" rev-parse HEAD 2>/dev/null || printf 'unknown'
}

# 镜像的 qm.git.commit Label；缺失/unknown → 输出空串（判定归调用方）。
qm_image_git_commit() {
    local image="$1" v
    v="$(docker image inspect "$image" --format '{{ index .Config.Labels "qm.git.commit" }}' 2>/dev/null || true)"
    case "$v" in
        ""|"<no value>"|unknown) printf '' ;;
        *) printf '%s' "$v" ;;
    esac
}

# 挂载源过滤器：stdin 逐行读挂载 Source，输出「越界（代码面）」的源；空=通过。
# 规则：项目目录之外的源一律放行（数据盘/系统路径）；项目目录之内仅白名单名放行
# （QM_DATA_MOUNT_NAMES）；项目目录本身被整体挂入 → 越界。
# 同时按字面与物理路径（pwd -P，解符号链接）两种前缀匹配——PROJECT_DIR 本身是
# 符号链接（如 /opt/quantmind → /mnt/…）时两种形态都可能出现，漏一种就是假通过。
qm_filter_code_mount_sources() {
    local p="$1" pp src name allowed
    pp="$(cd "$p" 2>/dev/null && pwd -P || printf '%s' "$p")"
    while IFS= read -r src; do
        [[ -n "$src" ]] || continue
        case "$src" in
            "$p"|"$pp")
                printf '%s\n' "$src"; continue ;;
            "$p"/*|"$pp"/*) ;;
            *) continue ;;
        esac
        allowed=false
        for name in $QM_DATA_MOUNT_NAMES; do
            case "$src" in
                "$p/$name"|"$p/$name"/*|"$pp/$name"|"$pp/$name"/*) allowed=true; break ;;
            esac
        done
        $allowed || printf '%s\n' "$src"
    done
}

# 容器当前挂载里越界的 Source（空=通过）。
qm_code_mounts_of() {
    local container="$1" project_dir="$2"
    docker inspect --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' "$container" 2>/dev/null \
        | qm_filter_code_mount_sources "$project_dir" || true
}

# 容器内 /app/deploy_stamp.json 的 commit（拿不到输出空串）。
qm_runtime_stamp_commit() {
    local container="$1"
    docker exec "$container" sh -c \
        'sed -n "s/.*\"commit\":\"\([^"]*\)\".*/\1/p" /app/deploy_stamp.json 2>/dev/null' \
        2>/dev/null | head -1
}

# 应用层容器集合（发布断言的作用域）
QM_APP_CONTAINERS="quantmind quantmind-celery quantmind-celery-beat"

# 发布断言核心（组合原语）：三应用容器零代码挂载 + 主容器运行态戳 == head_sha。
# 通过 → stdout 空、rc 0；失败 → stdout 为原因、rc 1。回滚/报错文案由调用方决定。
qm_release_verify_runtime() {
    local project_dir="$1" head_sha="$2"
    local container out
    for container in $QM_APP_CONTAINERS; do
        if ! docker inspect "$container" >/dev/null 2>&1; then
            printf '容器 %s 不存在' "$container"
            return 1
        fi
        out="$(qm_code_mounts_of "$container" "$project_dir")"
        if [[ -n "$out" ]]; then
            printf '容器 %s 仍挂载代码路径：%s' "$container" "$(printf '%s' "$out" | tr '\n' ' ')"
            return 1
        fi
    done
    local stamp
    stamp="$(qm_runtime_stamp_commit quantmind)"
    if [[ -z "$stamp" ]]; then
        printf '容器内 /app/deploy_stamp.json 缺失或无 commit 字段（镜像未按 P2-6 烘焙？）'
        return 1
    fi
    if [[ "$stamp" != "$head_sha" ]]; then
        printf '运行态戳 %s ≠ 检出 %s' "${stamp:0:12}" "${head_sha:0:12}"
        return 1
    fi
    return 0
}
