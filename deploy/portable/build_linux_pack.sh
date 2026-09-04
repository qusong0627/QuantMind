#!/usr/bin/env bash
# ============================================================
# QuantMind 便携版打包脚本（Linux x86_64）
# 产出: deploy/portable/dist/QuantMind-Portable-linux-x64.tar.gz
#
# 包内组件:
#   runtime/  python-build-standalone 内嵌 Python 3.10 + 全部依赖
#   pgsql/    便携 PostgreSQL 15 (zonky 官方二进制)
#   redis/    Redis 7.2 (源码编译, 仅本机回环)
#   backend/ config/ strategy_templates/ web/  源码与前端产物
#   start.sh stop.sh start.bat stop.bat README-portable.md
#
# 可覆盖的环境变量:
#   PIP_INDEX_URL      默认清华源
#   PG_VERSION         默认 15.19.0 (zonky)
#   REDIS_VERSION      默认 7.2.9
#   SKIP_TAR=1         只组装不压缩
# ============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$REPO_ROOT/deploy/portable"
BUILD="$HERE/build"
DIST="$HERE/dist"
STAGE="$BUILD/QuantMind-Portable-linux-x64"

PIP_DEFAULT="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
PIP_FALLBACKS=("$PIP_DEFAULT" "https://pypi.tuna.tsinghua.edu.cn/simple/" "https://pypi.org/simple/")
TORCH_INDEX="https://download.pytorch.org/whl/cpu"
PG_VERSION="${PG_VERSION:-15.19.0}"
REDIS_VERSION="${REDIS_VERSION:-7.2.9}"
NPROC="$(nproc 2>/dev/null || echo 4)"

log()  { echo -e "\033[36m[build]\033[0m $(date '+%H:%M:%S') $*"; }
ok()   { echo -e "\033[32m[build]\033[0m $(date '+%H:%M:%S') $*"; }
fail() { echo -e "\033[31m[build]\033[0m $*" >&2; exit 1; }
dl()   { curl -fL --retry 3 --progress-bar -o "$2" "$1"; }

# 解析 python-build-standalone 最新 release 的 tag 与资产名
# API 限流时自动降级：releases/latest 重定向拿 tag + expanded_assets 页面拿资产名
resolve_pbs_asset() {
    local pattern="$1" tag="" asset=""
    tag="$(curl -fsSL --retry 2 https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest 2>/dev/null \
        | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["tag_name"])
except Exception: pass' 2>/dev/null || true)"
    if [ -z "$tag" ]; then
        tag="$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
            https://github.com/astral-sh/python-build-standalone/releases/latest \
            | sed -E 's#.*/tag/([^/?]+).*#\1#')"
    fi
    [ -n "$tag" ] || fail "无法获取 python-build-standalone 最新版本号"
    asset="$(curl -fsSL "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/$tag" 2>/dev/null \
        | python3 -c '
import json, sys
pattern = sys.argv[1]
try:
    for a in json.load(sys.stdin)["assets"]:
        n = a["name"]
        if n.startswith("cpython-3.10.") and pattern in n and n.endswith(".tar.gz"):
            print(n); break
except Exception: pass' "$pattern" 2>/dev/null || true)"
    if [ -z "$asset" ]; then
        # 结尾引号用于排除 *.tar.gz.sha256 文件
        asset="$(curl -fsSL "https://github.com/astral-sh/python-build-standalone/releases/expanded_assets/$tag" \
            | grep -oE "cpython-3\.10\.[0-9]+[^\"<]*${pattern}\.tar\.gz\"" \
            | sed 's/"$//' | head -1 || true)"
    fi
    [ -n "$asset" ] || fail "未找到 cpython-3.10 资产 (pattern=$pattern, tag=$tag)"
    PBS_TAG="$tag"
    PBS_ASSET="$asset"
}

# ── 前置检查 ────────────────────────────────────────────────
command -v curl >/dev/null || fail "需要 curl"
command -v make >/dev/null || command -v cc >/dev/null || fail "编译 Redis 需要 gcc/make"
[ -f "$REPO_ROOT/electron/dist-react/index.html" ] || fail "缺少前端构建产物 electron/dist-react/，先运行 npm run dashboard:build"

mkdir -p "$BUILD/cache" "$STAGE"
AVAIL_KB=$(df -k "$BUILD" | awk 'NR==2{print $4}')
[ "${AVAIL_KB:-0}" -lt 23000000 ] && fail "磁盘剩余空间不足 23GB，无法打包"

# ── 1. 内嵌 Python (python-build-standalone) ────────────────
if [ ! -x "$STAGE/runtime/python/bin/python3" ]; then
    log "下载 python-build-standalone 运行时 ..."
    resolve_pbs_asset "x86_64-unknown-linux-gnu-install_only"
    log "  $PBS_ASSET (release $PBS_TAG)"
    dl "https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/$PBS_ASSET" \
       "$BUILD/cache/$PBS_ASSET"
    mkdir -p "$STAGE/runtime"
    tar -xzf "$BUILD/cache/$PBS_ASSET" -C "$STAGE/runtime"
    [ -x "$STAGE/runtime/python/bin/python3" ] || fail "Python 运行时解压异常"
fi
PY="$STAGE/runtime/python/bin/python3"
"$PY" --version

# ── 2. Python 依赖 ──────────────────────────────────────────
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_CACHE_DIR=1

if ! "$PY" -c "import fastapi" 2>/dev/null; then
    install_deps_with_index() {
        local idx="$1"
        export PIP_INDEX_URL="$idx"
        "$PY" -m pip install --upgrade pip setuptools wheel cython &&
        # torch CPU 版：本体走官方 CPU 源，传递依赖走默认源（与 Dockerfile.oss 一致）
        "$PY" -m pip install "torch==2.9.1+cpu" --index-url "$idx" --extra-index-url "$TORCH_INDEX" &&
        # 三个文件分开安装：requirements.txt 与 production.txt 存在版本重叠
        # （如 redis 6.4.0 vs 5.0.1），合并解析会冲突；Dockerfile 就是顺序覆盖安装
        "$PY" -m pip install -r "$REPO_ROOT/requirements.txt" &&
        "$PY" -m pip install -r "$REPO_ROOT/requirements/production.txt" &&
        "$PY" -m pip install -r "$REPO_ROOT/requirements/ai.txt" &&
        "$PY" -m pip install "quantdb-sdk==0.3.3"
    }
    PIP_OK=0
    for _idx in "${PIP_FALLBACKS[@]}"; do
        log "使用镜像 $_idx 安装 Python 依赖（约 4-6GB）..."
        if install_deps_with_index "$_idx"; then PIP_OK=1; break; fi
        log "镜像 $_idx 安装失败，切换下一个 ..."
    done
    [ "$PIP_OK" = "1" ] || fail "所有 PyPI 镜像安装依赖均失败"
fi

# ── 2b. rd-agent（因子演化，与镜像安装方式一致）──────────────
# rd-agent 依赖链会同时把 fastapi/pydantic/starlette 等升级到新版
# （生产镜像正是这个状态，代码已按新版演进）
if [ -d "$REPO_ROOT/rd-agent" ] && [ -f "$REPO_ROOT/rd-agent/pyproject.toml" ]; then
    if ! "$PY" -c "import rdagent" 2>/dev/null; then
        log "安装 rd-agent（含 litellm/mlflow 等依赖链，约 1-2GB）..."
        SETUPTOOLS_SCM_PRETEND_VERSION_FOR_RDAGENT=0.1.dev1 \
            "$PY" -m pip install --no-cache-dir "$REPO_ROOT/rd-agent"
    fi
    # litellm 1.98 + py3.10/pydantic 2.13 兼容补丁（与 docker-compose 挂载等效）
    SITE_PKG="$("$PY" -c 'import site; print(site.getsitepackages()[0])')"
    cp "$REPO_ROOT/docker/litellm_sitecustomize.py" "$SITE_PKG/sitecustomize.py"
    log "已写入 sitecustomize 兼容补丁"
else
    log "警告: 仓库中无 rd-agent/ 源码，跳过因子演化模块安装"
fi

# ── 2c. 核心栈版本对齐（与生产镜像实测版本一致，保证确定性）────
"$PY" -m pip install \
    "fastapi==0.141.1" "pydantic==2.13.5" "starlette==1.6.0" \
    "uvicorn[standard]==0.52.4" "httpx==0.28.1" \
    "openai==2.54.0" "anthropic==1.2.0" "litellm==1.98.0"

# qlib 0.9.7 停牌 price=None 补丁（与 Dockerfile.oss 一致）
SITE_PKG="$("$PY" -c 'import site; print(site.getsitepackages()[0])')"
log "应用 qlib position.py 补丁 ..."
python3 "$REPO_ROOT/docker/patch_qlib.py" "$SITE_PKG/qlib/backtest/position.py"

# ── 3. 依赖导入自检 ─────────────────────────────────────────
log "依赖导入自检 ..."
"$PY" -c "
import fastapi, uvicorn, sqlalchemy, asyncpg, redis, celery, duckdb
import pandas, numpy, lightgbm, xgboost, catboost, sklearn, pyarrow
import qlib, torch, transformers, akshare
import litellm  # 验证 sitecustomize 兼容补丁生效
import rdagent
print('  fastapi', fastapi.__version__, '| torch', torch.__version__,
      '| qlib', qlib.__version__, '| pandas', pandas.__version__)
"
ok "依赖自检通过"

# ── 4. Redis（源码编译，回环使用）────────────────────────────
if [ ! -x "$STAGE/redis/redis-server" ]; then
    log "编译 Redis $REDIS_VERSION ..."
    if [ ! -f "$BUILD/cache/redis-$REDIS_VERSION.tar.gz" ]; then
        dl "https://download.redis.io/releases/redis-$REDIS_VERSION.tar.gz" \
           "$BUILD/cache/redis-$REDIS_VERSION.tar.gz"
    fi
    tar -xzf "$BUILD/cache/redis-$REDIS_VERSION.tar.gz" -C "$BUILD"
    make -C "$BUILD/redis-$REDIS_VERSION" -j"$NPROC" MALLOC=libc BUILD_TLS=no >/dev/null
    mkdir -p "$STAGE/redis"
    cp "$BUILD/redis-$REDIS_VERSION/src/redis-server" "$STAGE/redis/"
    cp "$BUILD/redis-$REDIS_VERSION/src/redis-cli" "$STAGE/redis/"
    rm -rf "$BUILD/redis-$REDIS_VERSION"
fi
"$STAGE/redis/redis-server" --version

# ── 5. 便携 PostgreSQL 15 (zonky 官方二进制) ─────────────────
if [ ! -x "$STAGE/pgsql/bin/initdb" ]; then
    log "下载 PostgreSQL $PG_VERSION 便携二进制 ..."
    JAR="$BUILD/cache/embedded-postgres-binaries-linux-amd64-$PG_VERSION.jar"
    dl "https://repo1.maven.org/maven2/io/zonky/test/postgres/embedded-postgres-binaries-linux-amd64/$PG_VERSION/embedded-postgres-binaries-linux-amd64-$PG_VERSION.jar" "$JAR"
    python3 -m zipfile -e "$JAR" "$BUILD/cache/pg_extract_$PG_VERSION/"
    TXZ="$(ls "$BUILD/cache/pg_extract_$PG_VERSION/"*.txz)"
    mkdir -p "$STAGE/pgsql"
    tar -xJf "$TXZ" -C "$STAGE/pgsql"
fi
"$STAGE/pgsql/bin/initdb" --version

# ── 6. 源码与前端产物 ────────────────────────────────────────
log "复制源码与前端产物 ..."
for d in backend config strategy_templates; do
    rm -rf "$STAGE/$d"
    cp -a "$REPO_ROOT/$d" "$STAGE/$d"
done
find "$STAGE/backend" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$STAGE/backend/scratch" "$STAGE/backend/htmlcov" "$STAGE/backend/coverage.xml" 2>/dev/null || true

rm -rf "$STAGE/web"
mkdir -p "$STAGE/web"
cp -a "$REPO_ROOT/electron/dist-react/." "$STAGE/web/"

# 远程训练节点脚本（与镜像 /app 对齐）
cp "$REPO_ROOT/docker/training/train.py" "$STAGE/train.py"
cp "$REPO_ROOT/docker/training/preprocessing.py" "$STAGE/preprocessing.py"
cp "$REPO_ROOT/docker/training/parallel_utils.py" "$STAGE/parallel_utils.py"

# ── 7. 启动脚本与文档 ────────────────────────────────────────
log "写入启动脚本与说明 ..."
cp "$HERE/pack_assets/start.sh" "$HERE/pack_assets/stop.sh" "$STAGE/"
cp "$HERE/pack_assets/start.bat" "$HERE/pack_assets/stop.bat" "$STAGE/"
cp "$HERE/pack_assets/pg_setup.py" "$STAGE/"
cp "$HERE/pack_assets/pack.env.example" "$STAGE/pack.env.example"
cp "$HERE/pack_assets/README-portable.md" "$STAGE/README.md"
cp "$REPO_ROOT/LICENSE" "$STAGE/LICENSE" 2>/dev/null || true

mkdir -p "$STAGE/data" "$STAGE/logs" "$STAGE/run"
GIT_REV="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
{
    echo "pack=QuantMind-Portable-linux-x64"
    echo "git=$GIT_REV"
    echo "built=$(date '+%Y-%m-%d %H:%M')"
    echo "python=$("$PY" --version 2>&1)"
    echo "postgres=$PG_VERSION"
    echo "redis=$("$STAGE/redis/redis-server" --version | awk '{print $2, $3}')"
} > "$STAGE/VERSION"

# ── 8. 打包 ─────────────────────────────────────────────────
mkdir -p "$DIST"
if [ "${SKIP_TAR:-0}" = "1" ]; then
    ok "组装完成（SKIP_TAR=1 跳过压缩）: $STAGE"
else
    log "压缩整包（约 5-15 分钟）..."
    TAROUT="$DIST/QuantMind-Portable-linux-x64.tar.gz"
    rm -f "$TAROUT"
    if command -v pigz >/dev/null 2>&1; then
        tar -C "$BUILD" -cf - "$(basename "$STAGE")" | pigz > "$TAROUT"
    else
        tar -C "$BUILD" -czf "$TAROUT" "$(basename "$STAGE")"
    fi
    ok "打包完成: $TAROUT ($(du -sh "$TAROUT" | cut -f1))"
fi
