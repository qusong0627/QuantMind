#!/usr/bin/env bash
# ============================================================
# QuantMind 便携版打包脚本（macOS Apple Silicon / M 系列）
# 产出: deploy/portable/dist/QuantMind-Portable-macos-arm64.zip
#
# ⚠️ 必须在 macOS(M 系列)上运行：
#   - Redis 无官方 darwin 预编译产物，本脚本用系统 clang 现场编译
#   - 需预先: brew install make 可选（系统自带 clang/make 即可）
#   - PG 用 zonky 官方 darwin 二进制（x86_64, M 系列经 Rosetta 运行，
#     系统首次会提示安装 Rosetta 请允许）
#
# 用法（在 Mac 上）:
#   git clone 仓库 或 从 SMB 把仓库拷到 Mac
#   cd deploy/portable && bash build_macos_pack.sh
# ============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$REPO_ROOT/deploy/portable"
BUILD="$HERE/build"
DIST="$HERE/dist"
STAGE="$BUILD/QuantMind-Portable-macos-arm64"

PIP_FALLBACKS=("https://pypi.org/simple/" "https://mirrors.aliyun.com/pypi/simple/")
TORCH_INDEX="https://download.pytorch.org/whl/cpu"
PG_VERSION="${PG_VERSION:-15.19.0}"
REDIS_VERSION="${REDIS_VERSION:-7.2.9}"

log()  { echo -e "\033[36m[build-mac]\033[0m $(date '+%H:%M:%S') $*"; }
ok()   { echo -e "\033[32m[build-mac]\033[0m $(date '+%H:%M:%S') $*"; }
fail() { echo -e "\033[31m[build-mac]\033[0m $*" >&2; exit 1; }

# 平台自检
[ "$(uname -s)" = "Darwin" ] || fail "本脚本必须在 macOS 上运行（Redis 需要本机编译）"
case "$(uname -m)" in arm64) ;; *) echo "警告: 非 arm64 (Apple Silicon)，请确认意图";; esac

command -v curl >/dev/null || fail "需要 curl"
command -v make  >/dev/null || fail "需要 make"
command -v clang >/dev/null || command -v cc >/dev/null || fail "需要 clang/cc (Xcode 命令行工具: xcode-select --install)"
[ -f "$REPO_ROOT/electron/dist-react/index.html" ] || fail "缺少前端产物 electron/dist-react/，先 npm run build:react"

mkdir -p "$BUILD/cache" "$STAGE"
# Python (aarch64-apple-darwin)
if [ ! -x "$STAGE/runtime/python/bin/python3" ]; then
    log "下载 python-build-standalone (aarch64-apple-darwin) ..."
    PBS_TAG="$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
        https://github.com/astral-sh/python-build-standalone/releases/latest \
        | sed -E 's#.*/tag/([^/?]+).*#\1#')"
    PBS_ASSET="$(curl -fsSL "https://github.com/astral-sh/python-build-standalone/releases/expanded_assets/$PBS_TAG" \
        | grep -oE 'cpython-3\.10\.[0-9]+\+[^"<]*aarch64-apple-darwin-install_only\.tar\.gz' \
        | head -1)"
    [ -n "$PBS_ASSET" ] || fail "未找到 aarch64-apple-darwin 资产 (tag=$PBS_TAG)"
    curl -fL --retry 3 -o "$BUILD/cache/$PBS_ASSET" \
        "https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/$PBS_ASSET"
    mkdir -p "$STAGE/runtime"
    tar -xzf "$BUILD/cache/$PBS_ASSET" -C "$STAGE/runtime"
fi
PY="$STAGE/runtime/python/bin/python3"
"$PY" --version
SITE_PKG="$STAGE/runtime/python/lib/python3.10/site-packages"
[ -d "$SITE_PKG" ] || fail "Python 解压异常"

# 依赖（macosx_11_0_arm64，含 universal2 的 qlib）
if [ ! -f "$SITE_PKG/fastapi/__init__.py" ]; then
    COMBINED="$BUILD/requirements-mac-combined.txt"
    {
        sed '/^futu-api/d;/^qstock/d' "$REPO_ROOT/requirements.txt"
        echo "torch==2.9.1"
        echo "quantdb-sdk==0.3.3"
    } > "$COMBINED"
    DL_OK=0
    for idx in "${PIP_FALLBACKS[@]}"; do
        log "镜像 $idx 下载 macosx_11_0_arm64 wheels ..."
        WHEELS="$BUILD/wheels-mac"; rm -rf "$WHEELS"; mkdir -p "$WHEELS"
        if PIP_INDEX_URL="$idx" python3 -m pip download -d "$WHEELS" \
                --platform macosx_11_0_arm64 --python-version 3.10 --implementation cp \
                --only-binary=:all: --extra-index-url "$TORCH_INDEX" -r "$COMBINED" \
           && PIP_INDEX_URL="$idx" python3 -m pip download -d "$WHEELS" \
                --platform macosx_11_0_arm64 --python-version 3.10 --implementation cp \
                --only-binary=:all: -r "$REPO_ROOT/requirements/production.txt" \
           && PIP_INDEX_URL="$idx" python3 -m pip download -d "$WHEELS" \
                --platform macosx_11_0_arm64 --python-version 3.10 --implementation cp \
                --only-binary=:all: -r "$REPO_ROOT/requirements/ai.txt"; then
            DL_OK=1; break
        fi
    done
    [ "$DL_OK" = "1" ] || fail "所有镜像下载失败"
    python3 -m pip install --no-index --find-links "$WHEELS" --target "$SITE_PKG" \
        --platform macosx_11_0_arm64 --python-version 3.10 --implementation cp \
        --only-binary=:all: -r "$COMBINED"
    python3 -m pip install --no-index --find-links "$WHEELS" --target "$SITE_PKG" \
        --platform macosx_11_0_arm64 --python-version 3.10 --implementation cp \
        --only-binary=:all: -r "$REPO_ROOT/requirements/production.txt"
    python3 -m pip install --no-index --find-links "$WHEELS" --target "$SITE_PKG" \
        --platform macosx_11_0_arm64 --python-version 3.10 --implementation cp \
        --only-binary=:all: -r "$REPO_ROOT/requirements/ai.txt"
    rm -rf "$WHEELS"
fi

# Redis：官方无 darwin 预编译，本机源码编译（mac 自带 clang）
if [ ! -x "$STAGE/redis/redis-server" ]; then
    log "编译 Redis $REDIS_VERSION (本机 clang) ..."
    curl -fL --retry 3 -o "$BUILD/cache/redis-$REDIS_VERSION.tar.gz" \
        "https://download.redis.io/releases/redis-$REDIS_VERSION.tar.gz"
    rm -rf "$BUILD/cache/redis-src"; mkdir -p "$BUILD/cache/redis-src"
    tar -xzf "$BUILD/cache/redis-$REDIS_VERSION.tar.gz" -C "$BUILD/cache/redis-src"
    (cd "$BUILD/cache/redis-src/redis-$REDIS_VERSION" && make -j4 MALLOC=libc >/dev/null)
    mkdir -p "$STAGE/redis"
    cp "$BUILD/cache/redis-src/redis-$REDIS_VERSION/src/redis-server" \
       "$BUILD/cache/redis-src/redis-$REDIS_VERSION/src/redis-cli" "$STAGE/redis/"
    log "Redis 编译完成"
fi

# PostgreSQL：zonky 官方 darwin 二进制（x86_64, M 系列 Rosetta 运行）
if [ ! -x "$STAGE/pgsql/bin/postgres" ]; then
    log "下载 zonky PostgreSQL $PG_VERSION (darwin) ..."
    mkdir -p "$BUILD/cache/pg" "$STAGE/pgsql"
    PG_JAR="$BUILD/cache/embedded-postgres-binaries-darwin-amd64-$PG_VERSION.jar"
    curl -fL --retry 3 -o "$PG_JAR" \
        "https://github.com/zonkyio/embedded-postgres-binaries/releases/download/$PG_VERSION/embedded-postgres-binaries-darwin-amd64-$PG_VERSION.jar"
    rm -rf "$BUILD/cache/pg-unpack"; mkdir -p "$BUILD/cache/pg-unpack"
    (cd "$BUILD/cache/pg-unpack" && jar xf "$PG_JAR" 2>/dev/null) || \
        (cd "$BUILD/cache/pg-unpack" && unzip -o -q "$PG_JAR")
    # jar 内布局含平台目录(binaries/darwin-amd64/...)，定位真实 bin/ 与 lib/
    PG_BIN_DIR="$(dirname "$(find "$BUILD/cache/pg-unpack" -path '*/bin/postgres' | head -1)")"
    [ -n "$PG_BIN_DIR" ] || fail "zonky PG 解包后未找到 bin/postgres，请手动检查 $BUILD/cache/pg-unpack"
    cp -R "$PG_BIN_DIR" "$STAGE/pgsql/bin"
    PG_LIB="$(dirname "$PG_BIN_DIR")/lib"
    [ -d "$PG_LIB" ] && cp -R "$PG_LIB" "$STAGE/pgsql/lib"
    [ -x "$STAGE/pgsql/bin/postgres" ] || fail "zonky PG 复制异常"
fi

# 目录骨架 + 源码/配置/前端
mkdir -p "$STAGE/data" "$STAGE/logs" "$STAGE/run" "$STAGE/models" "$STAGE/strategy_templates"
cp -a "$REPO_ROOT/backend" "$STAGE/"
cp -a "$REPO_ROOT/electron/dist-react" "$STAGE/web"
cp -a "$REPO_ROOT/strategy_templates/." "$STAGE/strategy_templates/" 2>/dev/null || true
# 训练脚本三件套复制到包根（与 Windows/Linux 包对齐；直跑训练需要新版 train.py）
for f in train.py preprocessing.py parallel_utils.py; do
    if [ -f "$REPO_ROOT/docker/training/$f" ]; then
        cp -f "$REPO_ROOT/docker/training/$f" "$STAGE/$f"
    fi
done
cp "$HERE/pack_assets/start.command" "$HERE/pack_assets/stop.command" "$HERE/pack_assets/pg_setup.py" "$STAGE/"
cp "$HERE/pack_assets/pack.env.example" "$STAGE/"
chmod +x "$STAGE/start.command" "$STAGE/stop.command"
# 占位数据目录（与 linux/win 包一致）
for D in uploads strategies reports backtest_results hf qlib_data quantdb quantus quanthk quantbc quantfutures; do
    mkdir -p "$STAGE/data/$D"
done

# 打包
cd "$BUILD"
rm -f "$DIST/QuantMind-Portable-macos-arm64.zip"
zip -qr "$DIST/QuantMind-Portable-macos-arm64.zip" QuantMind-Portable-macos-arm64
ok "macOS 便携包完成: $DIST/QuantMind-Portable-macos-arm64.zip"
echo "  分发前提醒: 首次打开 start.command 若被 Gatekeeper 拦截,"
echo "  执行: xattr -dr com.apple.quarantine QuantMind-Portable-macos-arm64"
echo "  建议至少在一台 M 系列 Mac 上完整跑通一轮再分发。"
