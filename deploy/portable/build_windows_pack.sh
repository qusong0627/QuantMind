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

# 前端产物来源。默认取**本机那次**构建（electron/dist-react），可用 WEB_DIST 指到
# 别处——`scripts/package-for-windows.sh` 就是拿它接一份「干净检出 + 干净构建」的
# 产物（本机构建里带着未跟踪的本机独有栏目，第三方包不能用那一份）。
WEB_DIST="${WEB_DIST:-$REPO_ROOT/electron/dist-react}"
[ -f "$WEB_DIST/index.html" ] || fail "缺少前端构建产物: $WEB_DIST"

# 出厂净化闸门的显式放行开关（自用包）。默认关：第三方包不许带本机独有栏目的产物。
GUARD_EXTRA=()
if [ "${PACK_ALLOW_LOCAL_LIVE:-0}" = "1" ]; then
    GUARD_EXTRA+=(--allow-local-live)
    log "PACK_ALLOW_LOCAL_LIVE=1：允许本机独有实盘栏目的前端产物（自用包形态）"
fi

# 前端产物形态闸：通用便携包必须是**全栏目**形态。
# 实盘节点包（deploy/live-win/build_live_pack.sh，本机专用/未进版本库）要求
# 同一份 dist-react 带 VITE_LIVE_NODE_ONLY=true 构建 —— 只留 QuantBot 与实盘
# 交易两栏。两个包共用这个产物目录，且都从这里取 web/，于是打完实盘包之后
# 直接跑本脚本，就会把「两栏 UI」静默装进通用便携包：用户点不到大盘分析/
# 模型训练/投研，而构建日志里一行异常都没有。这里挡在构建期。
# 判据只看 "true"：该变量不在任何 .env 文件里，全栏目形态下产物中根本没有
# 这个键（Vite 只注入已定义的 VITE_ 变量），传 false 也与源码语义一致（只认 true）。
if grep -rqs 'VITE_LIVE_NODE_ONLY:"true"' "$WEB_DIST/assets"; then
    fail "前端产物是实盘节点形态（VITE_LIVE_NODE_ONLY=true，只剩 QuantBot/实盘交易两栏）。
       多半是刚打完实盘节点包。重新构建全栏目产物:
         npm run dashboard:build   # 在仓库根目录执行，不带 VITE_LIVE_NODE_ONLY"
fi

# 前端产物形态闸之二：**本机独有实盘栏目**的产物（`electron/src/features/local-live/`
# 未跟踪、不开源）。本机 npm run build 会把它整块打进 dist-react，而两份包都从这里
# 取 web/ ——第三方包带上就等于把不开源的部分发出去。
# 判据直接读 pack_rules.PRIVATE_CHUNKS（**不在这儿再写一遍 glob**：闸门、这份脚本、
# scripts/deploy_frontend.sh 三处各写一遍，迟早有一处漂掉）。放在这里而不是只等第 6
# 步的成品校验：那时候 4GB 依赖已经下完了，白等一场。
if [ "${PACK_ALLOW_LOCAL_LIVE:-0}" != "1" ]; then
    PRIVATE_HITS="$(python3 - "$HERE" "$WEB_DIST" <<'PYEOF'
import pathlib, sys
sys.path.insert(0, sys.argv[1])
import pack_rules as R  # noqa: E402

print("\n".join(R.find_private_chunks(pathlib.Path(sys.argv[2]))[:10]))
PYEOF
)"
    if [ -n "$PRIVATE_HITS" ]; then
        fail "前端产物里有本机独有实盘栏目的 chunk（未跟踪、不开源）:
       $(echo "$PRIVATE_HITS" | tr '\n' ' ')
       第三方包不能用这一份。改用干净构建:
         bash scripts/package-for-windows.sh
       自用包要带它，显式放行: PACK_ALLOW_LOCAL_LIVE=1 $0"
    fi
fi

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
# 实测失败原因（2026-09-24 手工复现，`pip download … --only-binary=:all:` 的 stderr）：
#   ERROR: Could not find a version that satisfies the requirement pandarallel (from rdagent)
#          (from versions: none)   ← 该包在 PyPI 上**只有 sdist、没有 wheel**
# `--only-binary=:all:` 是必要的（构建机是 Linux，允许 sdist 会把需要本地编译的包也放进来，
# 那些在 Windows 上装不了）；代价就是「纯 Python 但只发 sdist」的依赖会一票否决整条解析。
# 想要 Windows 侧也有因子演化：把这类依赖在构建机上预先编成 universal wheel
# （`pip wheel <sdist> -w <dir> --no-deps` 产出 py3-none-any.whl）放进 find-links。
# 另：rd-agent 运行期还要本机有 git（它在工作区里 init/commit），便携包不带 git。
if [ -d "$REPO_ROOT/rd-agent" ] && [ -f "$REPO_ROOT/rd-agent/pyproject.toml" ]; then
    if [ ! -f "$SITE_PKG/rdagent/__init__.py" ]; then
        log "构建 rd-agent wheel 并解析 Windows 依赖 ..."
        SETUPTOOLS_SCM_PRETEND_VERSION_FOR_RDAGENT=0.1.dev1 \
            python3 -m pip wheel "$REPO_ROOT/rd-agent" -w "$BUILD/cache/rdagent_wheel" --no-deps
        RD_WHEEL="$(ls "$BUILD/cache/rdagent_wheel"/rdagent-*.whl 2>/dev/null | head -1)"
        if [ -n "$RD_WHEEL" ]; then
            RD_LOG="$BUILD/cache/rdagent_wheel/pip-download.log"
            if PIP_INDEX_URL="${PIP_FALLBACKS[0]}" python3 -m pip download \
                -d "$BUILD/cache/rdagent_wheel" \
                --platform win_amd64 --python-version 3.10 --implementation cp \
                --only-binary=:all: --find-links "$BUILD/cache/rdagent_wheel" \
                "rdagent==0.1.dev1" >"$RD_LOG" 2>&1; then
                python3 -m pip install --no-index \
                    --find-links "$BUILD/cache/rdagent_wheel" \
                    --target "$SITE_PKG" \
                    --platform win_amd64 --python-version 3.10 --implementation cp \
                    --only-binary=:all: \
                    "rdagent==0.1.dev1" || log "警告: rd-agent 目标安装失败，Windows 包因子演化功能降级"
            else
                log "警告: rd-agent Windows 依赖解析失败，跳过（因子演化功能降级）"
                # 把 pip 的真正原因抬到构建日志里：原先这里 `2>/dev/null` 只吞输出，
                # 警告看着像「网络问题」，排查要手工复现一遍才知道是哪个包没有 wheel。
                grep -E "^(ERROR|  ERROR)|No matching distribution" "$RD_LOG" \
                    | tail -3 | sed 's/^/    /' || true
                log "    完整解析日志: $RD_LOG"
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
for d in backend strategy_templates; do
    rm -rf "$STAGE/$d"
    cp -a "$REPO_ROOT/$d" "$STAGE/$d"
done
# config 单独走 tar：必须排除 runtime.env。那份文件是**宿主机上跑着的后端**
# 写入的运行期密钥（容器以 root 落盘，本机是 0600 root:root），两个理由都不能进包：
#   1. 跨节点泄漏 INTERNAL_CALL_SECRET —— 目标机的密钥必须自己生成；
#   2. 读不了会让 cp -a 直接报错、set -e 中断整包构建（2026-09-17 起的实际故障）。
# 目标机首次启动时 backend/main_oss.py 会自行生成强随机并写入该文件。
rm -rf "$STAGE/config"
tar -C "$REPO_ROOT" --exclude='config/runtime.env' -cf - config | tar -C "$STAGE" -xf -
find "$STAGE/backend" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$STAGE/backend/scratch" "$STAGE/backend/htmlcov" "$STAGE/backend/coverage.xml" 2>/dev/null || true

rm -rf "$STAGE/web"
mkdir -p "$STAGE/web"
cp -a "$WEB_DIST/." "$STAGE/web/"
# 便携版 UI 与 API 同源伺服：清掉硬编码的网关地址，改走相对路径
find "$STAGE/web/assets" -name '*.js' -type f \
    -exec sed -i 's#http://127\.0\.0\.1:8000##g' {} +

# docker/training 整目录(与编排器 /app/docker/training 布局对齐): train.py 顶层
# import model_trainers/diagnostics/data 同级包; 代码包 data 与包根数据目录 data/
# 同名冲突, 不能拉平到包根, 必须整目录拷贝保相对布局
rm -rf "$STAGE/docker/training"
mkdir -p "$STAGE/docker"
cp -a "$REPO_ROOT/docker/training" "$STAGE/docker/training"

cp "$HERE/pack_assets/start.sh" "$HERE/pack_assets/stop.sh" "$HERE/pack_assets/sync_from_git.sh" "$HERE/pack_assets/restore_backup.sh" "$STAGE/"
cp "$HERE/pack_assets/start.bat" "$HERE/pack_assets/stop.bat" "$HERE/pack_assets/sync_from_git.bat" "$HERE/pack_assets/restore_backup.bat" "$STAGE/"
# 一键安装：install.bat 是入口（双击），install.ps1 是真正的检查实现。
# 两个都要进包——闸门的 REQUIRED_FILES 里也钉着这两条，漏拷会被拦下（不给用户
# 一个「双击了没反应」的包）。
cp "$HERE/pack_assets/install.bat" "$HERE/pack_assets/install.ps1" "$STAGE/"
# .bat 必须 ASCII+CRLF：中文系统 cmd 以 GBK 解析 UTF-8+LF 的 bat 会整段错乱闪退
python3 - "$STAGE" <<'PYEOF'
import glob, sys
stage = sys.argv[1]
for f in glob.glob(stage + "/*.bat"):
    raw = open(f, "rb").read()
    text = raw.decode("utf-8", "replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    text = text.encode("ascii", "replace").decode("ascii")
    open(f, "wb").write(text.encode("ascii"))
    print("  [bat-crlf]", f.split("/")[-1])
PYEOF
cp "$HERE/pack_assets/pg_setup.py" "$STAGE/"
cp "$HERE/pack_assets/pack.env.example" "$STAGE/pack.env.example"
cp "$HERE/pack_assets/README-portable.md" "$STAGE/README.md"
cp "$REPO_ROOT/LICENSE" "$STAGE/LICENSE" 2>/dev/null || true

mkdir -p "$STAGE/data" "$STAGE/logs" "$STAGE/run"
# 增量升级 SQL（data/upgrade_*.sql）：容器版挂 /data，便携包为 <包根>/data，
# main_oss._upgrade_sql_files() 按候选目录探测执行（含 <backend>/../data）。
# 缺这些文件则 system_events 等增量迁移永不执行——历史 bug，勿删。
if ls "$REPO_ROOT"/data/upgrade_*.sql >/dev/null 2>&1; then
    cp -f "$REPO_ROOT"/data/upgrade_*.sql "$STAGE/data/"
    ok "增量升级 SQL 已打包: $(ls "$STAGE"/data/upgrade_*.sql | wc -l) 个"
else
    fail "仓库 data/upgrade_*.sql 缺失（system_events 等增量迁移将不执行）"
fi
# 静态数据：股票名称索引（stock_name_mapper 唯一查表源）。容器版靠 /data 挂载
# 天然可见，便携包必须随包——缺了只有一条 WARNING，中文名会静默退化成代码。
if [ -f "$REPO_ROOT/data/stocks/stocks_index.json" ]; then
    mkdir -p "$STAGE/data/stocks"
    cp -f "$REPO_ROOT/data/stocks/stocks_index.json" "$STAGE/data/stocks/"
    ok "股票名称索引已打包: data/stocks/stocks_index.json ($(du -h "$STAGE/data/stocks/stocks_index.json" | cut -f1))"
else
    fail "仓库 data/stocks/stocks_index.json 缺失（股票中文名将全部为空）"
fi
GIT_REV="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
# 工作树的跟踪文件与 HEAD 不一致时如实标注：源码是**从工作树**拷的，不标的话
# VERSION 会让人以为这份包等于那个修订（追溯时差得最远的就是这一类）。
# 排除 data/：那是本仓已知形态（data/ 是指向 /media/zbox/data/quantmind 的符号
# 链接，git 恒把 12 个已跟踪文件报成已删除），不排除就永远亮着。
if [ "$GIT_REV" != "unknown" ] && \
        [ "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=no -- . ':(exclude)data' 2>/dev/null | grep -c . || true)" != "0" ]; then
    GIT_REV="${GIT_REV}-dirty"
fi
{
    echo "pack=QuantMind-Portable-win-x64"
    echo "git=$GIT_REV"
    echo "built=$(date '+%Y-%m-%d %H:%M')"
    echo "python=3.10 (python-build-standalone windows)"
    echo "postgres=$PG_VERSION"
    echo "redis=$REDIS_WIN_VERSION (tporadowski)"
    echo "note=cross-assembled on linux; verify on real Windows before distribution"
} > "$STAGE/VERSION"

# ── 5b. 与 Linux 包对齐的可选组件 ────────────────────────────
LINUX_STAGE="$BUILD/QuantMind-Portable-linux-x64"
# models(预置 A股模型+FinBERT)为纯数据,直接复用 Linux 包内容
if [ -d "$LINUX_STAGE/models" ] && [ ! -e "$STAGE/models/.qm_models_ok" ]; then
    log "对齐 Linux 包: 复制 models (预置模型+FinBERT) ..."
    rm -rf "$STAGE/models"; mkdir -p "$STAGE/models"
    cp -a "$LINUX_STAGE/models/." "$STAGE/models/"
    [ -d "$STAGE/models/production" ] && touch "$STAGE/models/.qm_models_ok"
fi
# huntly(RSS 阅读): server.jar 平台无关(缓存优先,Linux 包兜底);JRE 用 Adoptium Windows x64。
# 「装好了没」按**内容**判，不按可执行位：解压走 `python3 -m zipfile -e`，它不还原 POSIX
# 权限位（实测 java.exe / jvm.dll 落地都是 664），拿 `-x` 当判据会每次构建都重下一遍
# 240MB、还会在最后一行谎报「组装失败」。Windows 不看权限位；真正的判据是 jvm.dll——
# 少了它 java.exe 只是个空壳（点开即报错，比整块没有更难排查，闸门 REQUIRED_PAIRS 同款判据）。
huntly_jre_ok() { [ -s "$1/bin/java.exe" ] && [ -s "$1/bin/server/jvm.dll" ]; }
huntly_ready() { [ -s "$STAGE/huntly/server.jar" ] && huntly_jre_ok "$STAGE/huntly/jre"; }
HUNTLY_JAR="$BUILD/cache/huntly_server.jar"
[ -f "$HUNTLY_JAR" ] || HUNTLY_JAR="$LINUX_STAGE/huntly/server.jar"
if [ -f "$HUNTLY_JAR" ] && ! huntly_ready; then
    log "组装 Huntly (jar + Windows JRE) ..."
    mkdir -p "$STAGE/huntly"
    cp -a "$HUNTLY_JAR" "$STAGE/huntly/server.jar"
    JRE_ZIP="$BUILD/cache/temurin-jre17-win-x64.zip"
    [ -f "$JRE_ZIP" ] || dl "https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/jre/hotspot/normal/eclipse" "$JRE_ZIP"
    rm -rf "$BUILD/cache/jre-extract"; mkdir -p "$BUILD/cache/jre-extract"
    python3 -m zipfile -e "$JRE_ZIP" "$BUILD/cache/jre-extract/"
    JRE_DIR="$(find "$BUILD/cache/jre-extract" -maxdepth 1 -type d -name 'jdk-*' -o -maxdepth 1 -type d -name 'jre-*' | head -1)"
    [ -n "$JRE_DIR" ] && [ -d "$JRE_DIR/bin" ] || JRE_DIR="$(find "$BUILD/cache/jre-extract" -mindepth 1 -maxdepth 1 -type d | head -1)"
    rm -rf "$STAGE/huntly/jre"; mkdir -p "$STAGE/huntly/jre"
    cp -a "$JRE_DIR/." "$STAGE/huntly/jre/"
    huntly_jre_ok "$STAGE/huntly/jre" || fail "Huntly JRE 组装失败：$JRE_ZIP 里没有 bin/server/jvm.dll（新闻聚合会点开即报错）"
    ok "Huntly 已就绪 ($(du -sh "$STAGE/huntly" | cut -f1))"
elif [ ! -f "$HUNTLY_JAR" ]; then
    log "警告: 没有 Huntly server.jar（$BUILD/cache 与 Linux 包都没有）：新闻聚合降级"
fi
# qwenpaw(Win 运行时由 build_win_qwenpaw_runtime.sh 预构建到 build/qwenpaw-runtime-win)
if [ -d "$BUILD/qwenpaw-runtime-win/python" ] && [ ! -e "$STAGE/qwenpaw_runtime/.qm_ok" ]; then
    log "对齐 Linux 包: 组装 QwenPaw Win 运行时 ..."
    rm -rf "$STAGE/qwenpaw_runtime"; mkdir -p "$STAGE/qwenpaw_runtime"
    cp -a "$BUILD/qwenpaw-runtime-win/." "$STAGE/qwenpaw_runtime/"
    touch "$STAGE/qwenpaw_runtime/.qm_ok"
fi

# ── 6. 打包（出厂净化闸门 + 压缩）────────────────────────────
# 排除清单**只在写 zip 那一步生效**，不在 staging 上删文件：这份 staging 是两个
# 构建器共用的（deploy/live-win/build_live_pack.sh 往同一份里覆盖 pack.env/bridge/
# live/），在 staging 上删等于偷改别人要打的包。于是顺序是：
#   ① --make-zip 先复验 staging（有违规就拒绝出包，产物一个字节都不写）
#   ② 按清单排除着写 zip     ③ --zip 对**产物**再核一遍（确认排除真生效、形状完整）
mkdir -p "$DIST"
ZIP_OUT="$DIST/QuantMind-Portable-win-x64.zip"
if [ "${SKIP_TAR:-0}" = "1" ]; then
    log "SKIP_TAR=1：只做 staging 校验，不压缩 ..."
    python3 "$HERE/pack_guard.py" --stage "$STAGE" "${GUARD_EXTRA[@]}"
    ok "组装完成: $STAGE"
else
    log "出厂净化闸门：校验 staging ..."
    python3 "$HERE/pack_guard.py" --make-zip "$STAGE" "$ZIP_OUT" "${GUARD_EXTRA[@]}"
    log "出厂净化闸门：复核产物 ..."
    python3 "$HERE/pack_guard.py" --zip "$ZIP_OUT" "${GUARD_EXTRA[@]}"
    sha256sum "$ZIP_OUT" | sed 's# .*/#  #' > "$ZIP_OUT.sha256"
    ok "打包完成: $ZIP_OUT ($(du -sh "$ZIP_OUT" | cut -f1))"
    ok "sha256: $(cut -d' ' -f1 "$ZIP_OUT.sha256")  （$ZIP_OUT.sha256）"
fi
