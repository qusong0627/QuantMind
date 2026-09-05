#!/usr/bin/env bash
# QuantMind 完整在线部署（从 CDN 下载「完整业务数据 + 预训练模型 + 镜像包」并一键部署）
#
# 默认 CDN 地址：
#   https://www.quantmindai.cn/downloads
# 可用 QUANTMIND_OFFLINE_BASE_URL 覆盖默认地址。
# 可选环境变量：
#   QUANTMIND_MANIFEST_SHA256  SHA256SUMS 清单的 SHA-256（可选）
#   QUANTMIND_DOCKER_MIRROR  Docker 镜像加速地址
#   QUANTMIND_REPO_URL    代码仓库地址（默认自建 Gitea）
#   QUANTMIND_REF         要部署的 Git 分支或 tag（默认 master）
#   QUANTMIND_REPLACE_QLIB=true       覆盖已有 db/qlib_data（谨慎）
#   QUANTMIND_REPLACE_DATABASE=true   覆盖已有 PostgreSQL 业务数据（谨慎）
#   QUANTMIND_REPLACE_QWENPAW_DATA=true 覆盖已有 QwenPaw 持久化数据（谨慎）
#   QUANTMIND_REBUILD_IMAGE=true 基于最新代码重建 quantmind 镜像（默认复用离线包成品镜像，谨慎）
#   QUANTMIND_COMPOSE_OVERLAY  已验证 docker-compose.yml 的本地路径（可选）
#   QUANTMIND_DEPLOY_OVERLAY_DIR  受控 Dockerfile 覆盖目录（可选）

set -euo pipefail

PROJECT_DIR="${QUANTMIND_PROJECT_DIR:-/opt/quantmind}"
DOWNLOAD_DIR="${QUANTMIND_DOWNLOAD_DIR:-/opt/quantmind-downloads}"
STAGING_DIR="${QUANTMIND_STAGING_DIR:-/opt/quantmind-staging}"
REPO_URL="${QUANTMIND_REPO_URL:-https://quantmindai.cn/gitea/qusong0627/QuantMind.git}"
REF="${QUANTMIND_REF:-master}"
COMPOSE_OVERLAY="${QUANTMIND_COMPOSE_OVERLAY:-}"
DEPLOY_OVERLAY_DIR="${QUANTMIND_DEPLOY_OVERLAY_DIR:-}"
OFFLINE_BASE_URL="${QUANTMIND_OFFLINE_BASE_URL:-https://www.quantmindai.cn/downloads}"
OFFLINE_BASE_URL="${OFFLINE_BASE_URL%/}"
MANIFEST_SHA256="${QUANTMIND_MANIFEST_SHA256:-}"
DOCKER_MIRROR="${QUANTMIND_DOCKER_MIRROR:-https://vmx3wfa8ih592aat3z.xuanyuan.run}"
PACKAGE_DIR="$DOWNLOAD_DIR/quantmind-offline"

log() { printf '[full-deploy] %s\n' "$*"; }
die() { log "错误: $*" >&2; exit 1; }

require_root() { [[ ${EUID} -eq 0 ]] || die '请使用 sudo bash deploy/full-deploy.sh'; }
require_ubuntu() {
    . /etc/os-release
    [[ ${ID:-} == ubuntu ]] || die '仅支持 Ubuntu'
}
require_url() { [[ -n "$1" ]] || die "缺少环境变量 $2"; }

download() {
    local url="$1" destination="$2" expected_sha="${3:-}"
    mkdir -p "$(dirname "$destination")"

    # 已下载且校验通过的包直接复用，避免重跑时 curl -C - 对完整文件
    # 返回 HTTP 416，也避免重复下载数 GB 的镜像包。
    if [[ -n "$expected_sha" && -f "$destination" ]] \
        && echo "${expected_sha}  ${destination}" | sha256sum --check --status; then
        log "复用已校验下载包: $(basename "$destination")"
        return 0
    fi
    log "下载 $(basename "$destination")"
    curl --fail --location --continue-at - --retry 3 --retry-delay 3 \
        "$url" -o "$destination"
    [[ -s "$destination" ]] || die "下载结果为空: $destination"
    if [[ -n "$expected_sha" ]]; then
        echo "${expected_sha}  ${destination}" | sha256sum --check --status \
            || die "SHA-256 校验失败: $destination"
    fi
}

download_offline_package() {
    log '步骤 3/8：从 CDN 下载离线包与校验清单'
    mkdir -p "$PACKAGE_DIR"
    download "$OFFLINE_BASE_URL/SHA256SUMS" "$PACKAGE_DIR/SHA256SUMS" \
        "$MANIFEST_SHA256"

    local file
    for file in \
        images.tar.zst data-system.tar.zst postgres-all.sql.zst \
        images.list README.txt; do
        grep -Eq "^[0-9a-f]{64}  ${file}$" "$PACKAGE_DIR/SHA256SUMS" \
            || die "离线包校验清单缺少: $file"
        download "$OFFLINE_BASE_URL/$file" "$PACKAGE_DIR/$file" \
            "$(awk -v name="$file" '$2 == name { print $1 }' "$PACKAGE_DIR/SHA256SUMS")"
    done
    # QwenPaw 卷为可选工件：业务内容只在 data 卷（技能池/工作区）；
    # secrets/backups/shared 通常为空，部署时按需创建空卷即可。
    for file in \
        quantmind_qwenpaw-data.tar.zst quantmind_qwenpaw-secrets.tar.zst \
        quantmind_qwenpaw-backups.tar.zst quantmind_qwenpaw-shared.tar.zst; do
        if grep -Eq "^[0-9a-f]{64}  ${file}$" "$PACKAGE_DIR/SHA256SUMS"; then
            download "$OFFLINE_BASE_URL/$file" "$PACKAGE_DIR/$file" \
                "$(awk -v name="$file" '$2 == name { print $1 }' "$PACKAGE_DIR/SHA256SUMS")"
        fi
    done
    (cd "$PACKAGE_DIR" && sha256sum --check --status SHA256SUMS) \
        || die '离线包 SHA-256 校验失败'
}

install_runtime() {
    log '步骤 1/8：更新系统并安装依赖'
    apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        ca-certificates curl git gnupg lsb-release zstd \
        python3-pip python3-pandas

    # 数据分析与 parquet 读取依赖：
    #   - python3-pandas 已通过 apt 安装（读 parquet 还需 pyarrow 引擎）
    #   - duckdb 用于直接查询 quantdb 的海量 parquet 数据
    #   - pyarrow 补齐 pandas 的 parquet 引擎
    # 离线环境可能无 PyPI 访问，安装失败仅告警，不中断整体部署。
    if [[ ${QUANTMIND_SKIP_ANALYSIS_TOOLS:-false} != true ]]; then
        log '步骤 1/8：安装 parquet 分析工具（pandas/duckdb/pyarrow）'
        python3 -m pip install --break-system-packages duckdb pyarrow \
            || python3 -m pip install duckdb pyarrow --user \
            || log '警告：parquet 分析工具安装失败（可能无外网），已跳过'
    fi

    log '步骤 2/8：安装 Docker 和 Docker Compose'
    if ! command -v docker >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io
    fi
    if ! docker compose version >/dev/null 2>&1; then
        # Ubuntu 源在不同版本中使用过两个包名。
        DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-plugin 2>/dev/null \
            || DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-v2
    fi
    systemctl enable --now docker
    docker compose version >/dev/null || die 'Docker Compose 不可用'

    configure_docker_mirror
}

configure_docker_mirror() {
    log "配置 Docker 镜像加速: $DOCKER_MIRROR"
    DOCKER_MIRROR="$DOCKER_MIRROR" python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path("/etc/docker/daemon.json")
try:
    config = json.loads(path.read_text()) if path.exists() else {}
except json.JSONDecodeError as exc:
    raise SystemExit(f"Docker 配置文件格式错误: {exc}")

mirror = os.environ["DOCKER_MIRROR"]
mirrors = [mirror] + [item for item in config.get("registry-mirrors", []) if item != mirror]
config["registry-mirrors"] = mirrors
path.parent.mkdir(parents=True, exist_ok=True)
temporary = path.with_suffix(".json.quantmind-tmp")
temporary.write_text(json.dumps(config, indent=2) + "\n")
temporary.replace(path)
PY
    systemctl restart docker
}

import_images() {
    local archive="$PACKAGE_DIR/images.tar.zst"
    log '步骤 4/8：解压并导入 Docker 镜像'
    zstd --test --quiet "$archive" || die '镜像包 zstd 校验失败'

    local image
    local images_ready=true
    for image in \
        quantmind-oss:latest \
        quantmind-data-gateway:latest \
        postgres:15-alpine redis:7-alpine \
        lcomplete/huntly:latest agentscope/qwenpaw:latest \
        ghcr.io/gnzsnz/ib-gateway:latest \
        python:3.10-slim-bookworm; do
        if ! docker image inspect "$image" >/dev/null 2>&1; then
            images_ready=false
            break
        fi
    done
    if $images_ready; then
        log '复用已导入的 Docker 镜像'
        return 0
    fi

    # 流式导入，不产生同等大小的中间 .tar 文件。
    zstd --decompress --stdout "$archive" | docker load

    for image in \
        quantmind-oss:latest \
        quantmind-data-gateway:latest \
        postgres:15-alpine redis:7-alpine \
        lcomplete/huntly:latest agentscope/qwenpaw:latest \
        ghcr.io/gnzsnz/ib-gateway:latest \
        python:3.10-slim-bookworm; do
        docker image inspect "$image" >/dev/null 2>&1 \
            || die "离线镜像包未包含必需镜像: $image"
    done
}

restore_payload_data() {
    local archive="$PACKAGE_DIR/data-system.tar.zst"
    rm -rf "$STAGING_DIR"
    mkdir -p "$STAGING_DIR"

    log '步骤 6/8：恢复业务数据、模型与 Qlib 数据'
    zstd --test --quiet "$archive" || die '业务数据包 zstd 校验失败'
    zstd --decompress --stdout "$archive" | tar --extract --file - --directory "$STAGING_DIR"
    [[ -f "$STAGING_DIR/db/qlib_data/calendars/day.txt" ]] \
        || die '业务数据包结构异常：缺少 db/qlib_data/calendars/day.txt'
}

has_qlib_features() {
    local qlib_dir="$1"
    [[ -f "$qlib_dir/calendars/day.txt" && -d "$qlib_dir/features" ]] \
        && find "$qlib_dir/features" -type f -print -quit 2>/dev/null | grep -q .
}

checkout_code() {
    log '步骤 5/8：下载最新代码'
    # 目录已存在但非 Git 仓库：若是空目录允许 clone（git clone 支持空目标，
    # 常见于上次安装残留的空 /opt/quantmind）；非空则拒绝，避免覆盖已有数据。
    if [[ -e "$PROJECT_DIR" && ! -d "$PROJECT_DIR/.git" ]]; then
        if [[ -n "$(ls -A "$PROJECT_DIR" 2>/dev/null)" ]]; then
            die "部署目录已存在且不是 Git 仓库（非空）: $PROJECT_DIR"
        fi
    fi
    if [[ -d "$PROJECT_DIR/.git" ]]; then
        git -C "$PROJECT_DIR" fetch origin "$REF"
        git -C "$PROJECT_DIR" checkout --detach "origin/$REF" 2>/dev/null \
            || git -C "$PROJECT_DIR" checkout --detach "$REF"
    else
        git clone --branch "$REF" --depth 1 "$REPO_URL" "$PROJECT_DIR"
    fi

    # 发布分支尚未合并部署修复时，允许由受控的本地文件覆盖 Compose。
    # 该入口只覆盖此单一文件，避免把服务器上的任意目录复制进代码仓库。
    if [[ -n "$COMPOSE_OVERLAY" ]]; then
        [[ -f "$COMPOSE_OVERLAY" ]] || die "Compose 覆盖文件不存在: $COMPOSE_OVERLAY"
        cp "$COMPOSE_OVERLAY" "$PROJECT_DIR/docker-compose.yml"
        log "已应用 Compose 覆盖文件"
    fi
    if [[ -n "$DEPLOY_OVERLAY_DIR" ]]; then
        [[ -d "$DEPLOY_OVERLAY_DIR" ]] || die "部署覆盖目录不存在: $DEPLOY_OVERLAY_DIR"
        local relative_path
        for relative_path in \
            docker/Dockerfile.oss \
            docker/Dockerfile.web \
            docker/Dockerfile.data-gateway; do
            if [[ -f "$DEPLOY_OVERLAY_DIR/$relative_path" ]]; then
                install -D -m 0644 "$DEPLOY_OVERLAY_DIR/$relative_path" \
                    "$PROJECT_DIR/$relative_path"
            fi
        done
        log "已应用 Dockerfile 覆盖层"
    fi

}

install_payload_data() {
    local qlib_target="$PROJECT_DIR/db/qlib_data"
    # 只有真实 Qlib 数据才默认保留；仓库中的空目录或损坏数据会被离线包替换。
    if [[ -e "$qlib_target" ]] && has_qlib_features "$qlib_target" \
        && [[ ${QUANTMIND_REPLACE_QLIB:-false} != true ]]; then
        log "检测到有效 Qlib 数据，复用现有目录: $qlib_target"
    else
        rm -rf "$qlib_target"
        mkdir -p "$PROJECT_DIR/db"
        mv "$STAGING_DIR/db/qlib_data" "$qlib_target"
    fi

    for directory in data models; do
        [[ -d "$STAGING_DIR/$directory" ]] || die "业务数据包缺少: $directory"
        if [[ -e "$PROJECT_DIR/$directory" ]] \
            && [[ ${QUANTMIND_REPLACE_BUSINESS_DATA:-false} != true ]]; then
            log "检测到已有 $directory，保留现有数据"
        else
            rm -rf "$PROJECT_DIR/$directory"
            mv "$STAGING_DIR/$directory" "$PROJECT_DIR/$directory"
        fi
    done
    rm -rf "$STAGING_DIR"
}

restore_database() {
    local archive="$PACKAGE_DIR/postgres-all.sql.zst"
    log '步骤 7/8：恢复 PostgreSQL 业务数据'
    cd "$PROJECT_DIR"
    docker compose up -d db
    # 就绪检测（放宽窗口以覆盖首次 initdb / 冷启动还原大库，最多 60×3s=180s）。
    # pg_isready 早于 PG 可接受连接即返回非 0；用 -h localhost 规避 socket 路径差异。
    local attempt=0 ready=0
    while (( attempt < 60 )); do
        if docker exec quantmind-db sh -lc \
            'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" -h localhost' >/dev/null 2>&1; then
            ready=1
            break
        fi
        attempt=$((attempt + 1))
        sleep 3
    done
    if [[ "$ready" != 1 ]]; then
        log "PostgreSQL 在 ${attempt}s 内未就绪，输出容器日志以定位根因："
        docker logs --tail 100 quantmind-db 2>&1 || true
        die 'PostgreSQL 未在规定时间内就绪（详见上方容器日志）'
    fi

    local table_count
    table_count="$(docker exec quantmind-db sh -lc \
        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT count(*) FROM pg_tables WHERE schemaname = '\''public'\''"')"
    if [[ "$table_count" != 0 && ${QUANTMIND_REPLACE_DATABASE:-false} != true ]]; then
        log "检测到已有 PostgreSQL 数据（$table_count 张表），保留现有数据"
        return 0
    fi
    zstd --decompress --stdout "$archive" \
        | docker exec -i quantmind-db sh -lc 'psql -U "$POSTGRES_USER"'
}

restore_qwenpaw_volumes() {
    local volume file
    for volume in data secrets backups shared; do
        file="$PACKAGE_DIR/quantmind_qwenpaw-$volume.tar.zst"
        docker volume create "quantmind_qwenpaw-$volume" >/dev/null
        # 离线包未提供该卷工件时创建空卷即可（secrets 由用户部署后自行配置，
        # backups/shared 为运行时产物；业务内容只在 data 卷）。
        if [[ ! -f "$file" ]]; then
            log "离线包未包含 $volume 卷，使用空卷"
            continue
        fi
        if docker run --rm -v "quantmind_qwenpaw-$volume":/target \
            --entrypoint sh postgres:15-alpine \
            -c 'find /target -mindepth 1 -print -quit | grep -q .'; then
            if [[ ${QUANTMIND_REPLACE_QWENPAW_DATA:-false} != true ]]; then
                log "检测到已有 QwenPaw 卷，保留现有数据: $volume"
                continue
            fi
        fi
        zstd --decompress --stdout "$file" | docker run --rm -i \
            -v "quantmind_qwenpaw-$volume":/target \
            --entrypoint sh postgres:15-alpine -c 'tar -C /target -xf -'
    done
}

configure_qwenpaw_runtime() {
    log '步骤 8/8：配置 QwenPaw 运行时（reportlab / docker CLI）'
    # 技能运行环境契约依赖：
    #   - reportlab：QwenPaw venv 内做研报级 MD→PDF 转换（md_to_pdf_report.py）
    #   - docker CLI：QwenPaw 容器内经 docker exec quantmind 执行重依赖取数脚本
    # 离线/无外网环境安装失败仅告警，不中断部署。
    if ! docker ps --format '{{.Names}}' | grep -qx qwenpaw; then
        log '警告：qwenpaw 容器未运行，跳过 QwenPaw 运行时配置'
        return 0
    fi

    if docker exec qwenpaw /app/venv/bin/python3 -c 'import reportlab' >/dev/null 2>&1; then
        log 'QwenPaw venv 已包含 reportlab，跳过安装'
    else
        docker exec qwenpaw sh -c \
            '/app/venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple reportlab' \
            || docker exec qwenpaw sh -c \
            '/app/venv/bin/pip install -q -i https://mirrors.aliyun.com/pypi/simple/ reportlab' \
            || docker exec qwenpaw sh -c '/app/venv/bin/pip install -q reportlab' \
            || log '警告：reportlab 安装失败（可能无外网），技能 PDF 生成将降级为仅 MD 输出'
    fi

    if docker exec qwenpaw sh -c 'command -v docker' >/dev/null 2>&1; then
        log 'QwenPaw 容器已包含 docker CLI'
    elif [[ -x /usr/bin/docker ]] \
        && docker cp /usr/bin/docker qwenpaw:/usr/local/bin/docker 2>/dev/null \
        && docker exec qwenpaw sh -c \
            'chmod +x /usr/local/bin/docker && docker --version >/dev/null' 2>/dev/null; then
        # 首选：宿主机 docker CLI 为静态二进制，直接复制进容器（离线可用，秒级完成）
        log '已从宿主机复制 docker CLI 到 QwenPaw 容器'
    else
        # 兜底：容器内 apt 安装（需外网且容器有软件源）
        docker exec qwenpaw sh -c \
            'command -v apt-get >/dev/null 2>&1 && apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -q docker.io' \
            || log '警告：QwenPaw 容器内 docker CLI 安装失败，重依赖取数脚本无法经 docker exec 在 quantmind 内执行（技能将走内置 pdf 技能兜底）'
    fi
}

build_and_start() {
    log '步骤 8/8：基于最新代码重新构建并启动服务'
    cd "$PROJECT_DIR"
    # rsshub 不在离线包内（避免历史损坏镜像），此处在线拉取健康镜像。
    # 仅拉 rsshub，绝不触碰离线包内 qwenpaw 定制镜像。
    if ! docker image inspect diygod/rsshub:latest >/dev/null 2>&1; then
        log 'rsshub 镜像不可用，在线拉取（不影响离线包内其他镜像）...'
        docker compose pull rsshub \
            || docker pull diygod/rsshub:latest \
            || log '警告：rsshub 拉取失败（不影响核心服务，RSS 源功能将不可用）'
    fi
    # 核心镜像按需重建：默认直接复用离线包内已导入、校验过的 quantmind-oss 成品镜像，
    # 避免每次部署重复构建/联网拉取（离线包镜像与最新代码一致时重建纯属浪费）。
    # 只有 QUANTMIND_REBUILD_IMAGE=true 才基于最新代码重建。web/data-gateway/dashboard
    # 均已在离线包中提供成品镜像，直接复用可避免为可选服务拉取额外构建基础镜像。
    if [[ ${QUANTMIND_REBUILD_IMAGE:-false} == true ]]; then
        log '按 QUANTMIND_REBUILD_IMAGE=true 基于最新代码重建 quantmind 镜像...'
        docker compose build --pull=false quantmind
    else
        log '复用离线包内 quantmind-oss 成品镜像（跳过重建；QUANTMIND_REBUILD_IMAGE=true 可强制重建）'
    fi
    docker compose up -d --pull never
    configure_qwenpaw_runtime
    docker compose ps
}

main() {
    require_root
    require_ubuntu
    require_url "$OFFLINE_BASE_URL" QUANTMIND_OFFLINE_BASE_URL
    echo "========================================================================="
    echo " 🚀 QuantMind 完整部署即将开始"
    echo " -------------------------------------------------------------------------"
    echo " ⏱️  预计耗时（依服务器性能与网络波动）:"
    echo "     1. 安装依赖与 Docker        ~2-5 分钟"
    echo "     2. 下载离线包（约 5GB）     ~5-20 分钟"
    echo "     3. 导入 Docker 镜像         ~3-10 分钟"
    echo "     4. 下载最新代码             ~1-3 分钟"
    echo "     5. 恢复业务数据与数据库     ~2-5 分钟"
    echo "     6. 复用成品镜像并启动服务   ~1-5 分钟（QUANTMIND_REBUILD_IMAGE=true 重建则另加构建时间）"
    echo "     合计                       约 15-50 分钟"
    echo " -------------------------------------------------------------------------"
    echo " 💡 如遇系统组件下载缓慢，请切换至国内加速源以提升速度。"
    echo "========================================================================="
    install_runtime
    download_offline_package
    import_images
    checkout_code
    restore_payload_data
    install_payload_data
    restore_database
    restore_qwenpaw_volumes
    build_and_start
    log "完成：代码=$PROJECT_DIR，Qlib 数据=$PROJECT_DIR/db/qlib_data"
    echo ""
    echo "========================================================================="
    echo " 🎉 QuantMind 完整部署成功！"
    echo " -------------------------------------------------------------------------"
    echo " 🖥️  桌面客户端: https://oss.quantmindai.cn/desktop-download.html"
    echo " 📖 API 文档    : http://<服务器 IP>:8000/docs"
    echo " 👤 默认账号    : admin / admin123"
    echo " -------------------------------------------------------------------------"
    echo " 💡 【数据更新与扩展提示】"
    echo " 1. QuantDB 在线下载及日常增量更新（推荐）："
    echo "    在客户端【个人中心】->【数据平台】配置 API Key，"
    echo "    或在终端执行: docker exec quantmind python backend/scripts/quantdb_daily_sync.py"
    echo " 2. 百度网盘完整历史数据包（备选）："
    echo "    链接: https://pan.baidu.com/s/5IT4p5nFlglZ7zu_0H_fA8Q"
    echo "========================================================================="
    echo ""
}

main "$@"

