#!/usr/bin/env bash
# ============================================================
# QuantMind 便携版打包脚本（Windows x64 交叉组装版）
# 在 Linux 上组装 Windows 便携包（不执行任何 Windows 代码，
# 全部使用 win_amd64 wheel + 官方 Windows 二进制）。
#
# 产出: deploy/portable/dist/QuantMind-Portable-win-x64.zip
#
# ⚠️ 交叉组装后必须在真实 Windows 机器上验证一轮再分发。
# ⚠️ 已知限制: pip 按构建机环境解析依赖标记，个别仅 Windows 生效的
#    小依赖可能遗漏（EXTRA_WIN_PKGS 兜底），真机验证时补齐即可。
# ============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$REPO_ROOT/deploy/portable"
BUILD="$HERE/build"
DIST="$HERE/dist"
STAGE="$BUILD/QuantMind-Portable-win-x64"

PIP_DEFAULT="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
PIP_FALLBACKS=("$PIP_DEFAULT" "https://pypi.tuna.tsinghua.edu.cn/simple/" "https://pypi.org/simple/")
TORCH_INDEX="https://download.pytorch.org/whl/cpu"
PG_VERSION="${PG_VERSION:-15.19.0}"
REDIS_WIN_VERSION="${REDIS_WIN_VERSION:-5.0.14.1}"
# 仅 Windows 平台生效、构建机解析标记时会漏掉的小依赖
EXTRA_WIN_PKGS="colorama"

log()  { echo -e "\033[36m[build-win]\033[0m $(date '+%H:%M:%S') $*"; }
ok()   { echo -e "\033[32m[build-win]\033[0m $(date '+%H:%M:%S') $*"; }
fail() { echo -e "\033[31m[build-win]\033[0m $*" >&2; exit 1; }
dl()   { curl -fL --retry 3 --progress-bar -o "$2" "$1"; }

# 解析 python-build-standalone 最新 release（API 限流时降级到重定向 + expanded_assets）
# 可传多个候选 pattern（先旧式 -shared-install_only，再无 shared 的新式），
# 匹配用 endswith(pattern.tar.gz) 精确尾部，天然排除 *_stripped 变体。
resolve_pbs_asset() {
    local tag="" asset=""
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
patterns = sys.argv[1:]
try:
    for a in json.load(sys.stdin)["assets"]:
        n = a["name"]
        if not n.startswith("cpython-3.10."):
            continue
        for p in patterns:
            if n.endswith(p + ".tar.gz"):
                print(n); raise SystemExit
except SystemExit:
    pass
except Exception:
    pass' "$@" 2>/dev/null || true)"
    if [ -z "$asset" ]; then
        for p in "$@"; do
            asset="$(curl -fsSL "https://github.com/astral-sh/python-build-standalone/releases/expanded_assets/$tag" \
                | grep -oE "cpython-3\.10\.[0-9]+[^\"<]*${p}\.tar\.gz\"" \
                | sed 's/"$//' | head -1 || true)"
            [ -n "$asset" ] && break
        done
    fi
    [ -n "$asset" ] || fail "未找到 cpython-3.10 资产 (patterns: $*, tag=$tag)"
    PBS_TAG="$tag"
    PBS_ASSET="$asset"
}

command -v curl >/dev/null || fail "需要 curl"
command -v python3 >/dev/null || fail "需要 python3"
[ -f "$REPO_ROOT/electron/dist-react/index.html" ] || fail "缺少前端构建产物 electron/dist-react/"

mkdir -p "$BUILD/cache" "$STAGE"
AVAIL_KB=$(df -k "$BUILD" | awk 'NR==2{print $4}')
[ "${AVAIL_KB:-0}" -lt 23000000 ] && fail "磁盘剩余空间不足 23GB"

# ── 1. 内嵌 Windows Python (python-build-standalone) ────────
if [ ! -f "$STAGE/runtime/python/python.exe" ]; then
    log "下载 python-build-standalone (windows-x64) ..."
    resolve_pbs_asset "x86_64-pc-windows-msvc-shared-install_only" "x86_64-pc-windows-msvc-install_only"
    log "  $PBS_ASSET (release $PBS_TAG)"
    dl "https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/$PBS_ASSET" \
       "$BUILD/cache/$PBS_ASSET"
    mkdir -p "$STAGE/runtime"
    tar -xzf "$BUILD/cache/$PBS_ASSET" -C "$STAGE/runtime"
fi
SITE_PKG="$STAGE/runtime/python/Lib/site-packages"
[ -d "$SITE_PKG" ] || fail "Windows Python 解压异常（缺 Lib/site-packages）"

# ── 2. 下载 win_amd64 全量 wheel 并安装到目标 site-packages ──
COMBINED="$BUILD/requirements-win-combined.txt"
{
    cat "$REPO_ROOT/requirements.txt"
    echo "torch==2.9.1+cpu"
    echo "quantdb-sdk==0.3.3"
    for p in $EXTRA_WIN_PKGS; do echo "$p"; done
} > "$COMBINED"

# futu-api 只发布 macosx/linux 预编译包，PyPI 无 win_amd64 wheel；
# 后端 broker 导入为可选（try/except），Windows 包直接剔除，否则整条镜像链必败
WIN_REQ_1="$BUILD/requirements-win-1.txt"
WIN_REQ_2="$BUILD/requirements-win-2.txt"
WIN_REQ_3="$BUILD/requirements-win-3.txt"
sed -e '/^futu-api/d' -e '/^qstock/d' "$COMBINED" > "$WIN_REQ_1"
sed -e '/^futu-api/d' -e '/^qstock/d' "$REPO_ROOT/requirements/production.txt" > "$WIN_REQ_2"
sed -e '/^futu-api/d' -e '/^qstock/d' "$REPO_ROOT/requirements/ai.txt" > "$WIN_REQ_3"
# 本机网络实测阿里云 ≈90kB/s（4GB 需 ~13h），官方源可达数 MB/s，故官方优先
PIP_FALLBACKS=("https://pypi.org/simple/" "https://mirrors.aliyun.com/pypi/simple/" "https://pypi.tuna.tsinghua.edu.cn/simple/")

if [ ! -f "$SITE_PKG/fastapi/__init__.py" ]; then
    WHEELS="$BUILD/wheels-win"
    # jsonpath/jieba(akshare/qstock 依赖)在 PyPI 只有 sdist 无 wheel，
    # --only-binary 下解析必败；本地预构建一次纯 py wheel（幂等缓存），
    # 每次镜像尝试开始时复制进 WHEELS 供 find-links 解析与 no-index 安装使用
    SDIST_WHEELS="$BUILD/cache/wheels-sdist"
    mkdir -p "$SDIST_WHEELS"
    # 注: pyqlib(ai.txt)依赖的 gym 同为 sdist-only，一并预构建
    for _sd_pkg in 'jsonpath==0.82.2' 'jieba==0.42.1' 'PyExecJS==1.5.1' 'gym==0.26.2'; do
        _sd_name="${_sd_pkg%%==*}"
        # pip 产出的 wheel 文件名是规范小写（pyexecjs-*.whl），须大小写不敏感判断
        if ! find "$SDIST_WHEELS" -maxdepth 1 -iname "${_sd_name}-*.whl" | grep -q .; then
            log "预构建 ${_sd_pkg} 纯 py wheel（sdist-only 包）..."
            PIP_INDEX_URL="https://pypi.org/simple/" python3 -m pip wheel \
                --no-deps --no-cache-dir -w "$SDIST_WHEELS" "$_sd_pkg"
        fi
    done
    download_wheels_with_index() {
        local idx="$1"
        rm -rf "$WHEELS"; mkdir -p "$WHEELS"
        cp "$SDIST_WHEELS"/*.whl "$WHEELS/" 2>/dev/null || true
        PIP_INDEX_URL="$idx" python3 -m pip download -d "$WHEELS" \
            --platform win_amd64 --python-version 3.10 --implementation cp \
            --only-binary=:all: --find-links "$WHEELS" \
            --extra-index-url "$TORCH_INDEX" \
            -r "$WIN_REQ_1" &&
        PIP_INDEX_URL="$idx" python3 -m pip download -d "$WHEELS" \
            --platform win_amd64 --python-version 3.10 --implementation cp \
            --only-binary=:all: --find-links "$WHEELS" \
            -r "$WIN_REQ_2" &&
        PIP_INDEX_URL="$idx" python3 -m pip download -d "$WHEELS" \
            --platform win_amd64 --python-version 3.10 --implementation cp \
            --only-binary=:all: --find-links "$WHEELS" \
            -r "$WIN_REQ_3"
    }
    WIN_PIP_OK=0
    for _idx in "${PIP_FALLBACKS[@]}"; do
        log "使用镜像 $_idx 下载 win_amd64 wheels（约 3-4GB）..."
        if download_wheels_with_index "$_idx"; then WIN_PIP_OK=1; break; fi
        log "镜像 $_idx 下载失败，切换下一个 ..."
    done
    [ "$WIN_PIP_OK" = "1" ] || fail "所有 PyPI 镜像下载 wheels 均失败"

    log "安装到目标 site-packages ..."
    # 与 Dockerfile 一致：三个文件顺序覆盖安装（存在 redis 等版本重叠，合并解析会冲突）
    python3 -m pip install --no-index --find-links "$WHEELS" \
        --target "$SITE_PKG" \
        --platform win_amd64 --python-version 3.10 --implementation cp \
        --only-binary=:all: \
        -r "$WIN_REQ_1"
    python3 -m pip install --no-index --find-links "$WHEELS" \
        --target "$SITE_PKG" \
        --platform win_amd64 --python-version 3.10 --implementation cp \
        --only-binary=:all: \
        -r "$WIN_REQ_2"
    python3 -m pip install --no-index --find-links "$WHEELS" \
        --target "$SITE_PKG" \
        --platform win_amd64 --python-version 3.10 --implementation cp \
        --only-binary=:all: \
        -r "$WIN_REQ_3"
    rm -rf "$WHEELS"
fi

# qlib 0.9.7 停牌 price=None 补丁（纯文本补丁，无需导入 qlib）
log "应用 qlib position.py 补丁 ..."
python3 "$REPO_ROOT/docker/patch_qlib.py" \
    "$SITE_PKG/qlib/backtest/position.py"

# 清理 __pycache__（Windows 上由首次运行重新生成）
find "$SITE_PKG" -maxdepth 2 -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

# ── 2b. rd-agent（因子演化；Windows 交叉构建尽力而为，失败则降级）──
if [ -d "$REPO_ROOT/rd-agent" ] && [ -f "$REPO_ROOT/rd-agent/pyproject.toml" ]; then
    if [ ! -f "$SITE_PKG/rdagent/__init__.py" ]; then
        log "构建 rd-agent wheel 并解析 Windows 依赖 ..."
        SETUPTOOLS_SCM_PRETEND_VERSION_FOR_RDAGENT=0.1.dev1 \
            python3 -m pip wheel "$REPO_ROOT/rd-agent" -w "$BUILD/cache/rdagent_wheel" --no-deps
        RD_WHEEL="$(ls "$BUILD/cache/rdagent_wheel"/rdagent-*.whl 2>/dev/null | head -1)"
        if [ -n "$RD_WHEEL" ]; then
            if PIP_INDEX_URL="${PIP_FALLBACKS[0]}" python3 -m pip download \
                -d "$BUILD/cache/rdagent_wheel" \
                --platform win_amd64 --python-version 3.10 --implementation cp \
                --only-binary=:all: --find-links "$BUILD/cache/rdagent_wheel" \
                "rdagent==0.1.dev1" 2>/dev/null; then
                python3 -m pip install --no-index \
                    --find-links "$BUILD/cache/rdagent_wheel" \
                    --target "$SITE_PKG" \
                    --platform win_amd64 --python-version 3.10 --implementation cp \
                    --only-binary=:all: \
                    "rdagent==0.1.dev1" || log "警告: rd-agent 目标安装失败，Windows 包因子演化功能降级"
            else
                log "警告: rd-agent Windows 依赖解析失败，跳过（因子演化功能降级）"
            fi
        else
            log "警告: rd-agent wheel 构建失败，跳过（因子演化功能降级）"
        fi
    fi
    # litellm 1.98 + py3.10/pydantic 2.13 兼容补丁（与 docker-compose 挂载等效）
    cp "$REPO_ROOT/docker/litellm_sitecustomize.py" "$SITE_PKG/sitecustomize.py"
else
    log "警告: 仓库中无 rd-agent/ 源码，Windows 包不含因子演化模块"
fi

# ── 2c. 核心栈版本对齐（与生产镜像实测版本一致）────────────────
# 注意：uvicorn 不用 [standard] extras——uvloop 无 Windows wheel，
# 交叉解析会硬失败；standard 的其余组件（websockets/httptools）显式列出
python3 -m pip install --target "$SITE_PKG" \
    --platform win_amd64 --python-version 3.10 --implementation cp \
    --only-binary=:all: \
    "fastapi==0.141.1" "pydantic==2.13.5" "starlette==1.6.0" \
    "uvicorn==0.52.4" "websockets==16.1.1" "httptools==0.8.0" \
    "httpx==0.28.1" \
    "openai==2.54.0" "anthropic==1.2.0" "litellm==1.98.0" 2>/dev/null \
    || log "警告: 核心栈版本对齐失败，保留 requirements 解析版本（需真机验证）"

# ── 3. 便携 PostgreSQL 15 (zonky windows 二进制) ─────────────
if [ ! -f "$STAGE/pgsql/bin/initdb.exe" ]; then
    log "下载 PostgreSQL $PG_VERSION (windows) ..."
    JAR="$BUILD/cache/embedded-postgres-binaries-windows-amd64-$PG_VERSION.jar"
    dl "https://repo1.maven.org/maven2/io/zonky/test/postgres/embedded-postgres-binaries-windows-amd64/$PG_VERSION/embedded-postgres-binaries-windows-amd64-$PG_VERSION.jar" "$JAR"
    python3 -m zipfile -e "$JAR" "$BUILD/cache/pg_extract_win_$PG_VERSION/"
    TXZ="$(ls "$BUILD/cache/pg_extract_win_$PG_VERSION/"*.txz)"
    mkdir -p "$STAGE/pgsql"
    tar -xJf "$TXZ" -C "$STAGE/pgsql"
fi

# ── 4. Redis for Windows (tporadowski 构建) ──────────────────
if [ ! -f "$STAGE/redis/redis-server.exe" ]; then
    log "下载 Redis for Windows $REDIS_WIN_VERSION ..."
    ZIP="$BUILD/cache/Redis-x64-$REDIS_WIN_VERSION.zip"
    dl "https://github.com/tporadowski/redis/releases/download/v$REDIS_WIN_VERSION/Redis-x64-$REDIS_WIN_VERSION.zip" "$ZIP"
    mkdir -p "$STAGE/redis"
    python3 -m zipfile -e "$ZIP" "$STAGE/redis/"
fi

# ── 5. 源码与前端产物 ────────────────────────────────────────
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
# 便携版 UI 与 API 同源伺服：清掉硬编码的网关地址，改走相对路径
find "$STAGE/web/assets" -name '*.js' -type f \
    -exec sed -i 's#http://127\.0\.0\.1:8000##g' {} +

cp "$REPO_ROOT/docker/training/train.py" "$STAGE/train.py"
cp "$REPO_ROOT/docker/training/preprocessing.py" "$STAGE/preprocessing.py"
cp "$REPO_ROOT/docker/training/parallel_utils.py" "$STAGE/parallel_utils.py"

cp "$HERE/pack_assets/start.sh" "$HERE/pack_assets/stop.sh" "$STAGE/"
cp "$HERE/pack_assets/start.bat" "$HERE/pack_assets/stop.bat" "$STAGE/"
cp "$HERE/pack_assets/pg_setup.py" "$STAGE/"
cp "$HERE/pack_assets/pack.env.example" "$STAGE/pack.env.example"
cp "$HERE/pack_assets/README-portable.md" "$STAGE/README.md"
cp "$REPO_ROOT/LICENSE" "$STAGE/LICENSE" 2>/dev/null || true

mkdir -p "$STAGE/data" "$STAGE/logs" "$STAGE/run"
GIT_REV="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
{
    echo "pack=QuantMind-Portable-win-x64"
    echo "git=$GIT_REV"
    echo "built=$(date '+%Y-%m-%d %H:%M')"
    echo "python=3.10 (python-build-standalone windows)"
    echo "postgres=$PG_VERSION"
    echo "redis=$REDIS_WIN_VERSION (tporadowski)"
    echo "note=cross-assembled on linux; verify on real Windows before distribution"
} > "$STAGE/VERSION"

# ── 6. 打包 ─────────────────────────────────────────────────
mkdir -p "$DIST"
if [ "${SKIP_TAR:-0}" = "1" ]; then
    ok "组装完成（SKIP_TAR=1 跳过压缩）: $STAGE"
else
    log "压缩为 zip（供 Windows 直接解压）..."
    ZIP_OUT="$DIST/QuantMind-Portable-win-x64.zip"
    rm -f "$ZIP_OUT"
    ( cd "$BUILD" && python3 -m zipfile -c "$ZIP_OUT" "$(basename "$STAGE")" )
    ok "打包完成: $ZIP_OUT ($(du -sh "$ZIP_OUT" | cut -f1))"
fi
