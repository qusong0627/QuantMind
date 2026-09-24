#!/usr/bin/env bash
# ============================================================
# QuantMind 便携包**一键出包**（在 Linux 上交叉组装 Windows x64 包）
#
# 用法:
#   bash scripts/package-for-windows.sh                  # 出给别人的包
#   bash scripts/package-for-windows.sh --private        # 自己用的包（带本机独有实盘栏目）
#   bash scripts/package-for-windows.sh --web-dist=DIR   # 用现成的前端产物，不重新构建
#
# 选项:
#   --private         自用包：前端用**本机工作树**的产物（含未跟踪的
#                     electron/src/features/local-live/ 实盘栏目），并显式放行闸门里
#                     那条私有栏目判据。与 --rev / --web-dist 互斥。
#   --real-trading    前端带 VITE_ENABLE_REAL_TRADING=true 构建（实盘 UI 可见）。
#                     这是**双开关的前半**：后端那半由目标机 pack.env 的
#                     ENABLE_REAL_TRADING 决定，两半都开才真的能用。默认关。
#   --web-dist=DIR    直接指定前端产物目录（跳过构建）。
#   --rev=REF         干净检出用哪个修订（默认 HEAD）。
#   --require-clean   工作树有未提交的跟踪改动就拒绝出包（正式发布用）。
#   --keep-worktree   保留干净检出（排障；默认用完就删）。
#   --skip-assemble   只产出前端产物、不组装不出包（调前端 / 调闸门时用）。
#
# 它做的事:
#   ① 前置检查：工具、磁盘、工作树状态（有未提交改动时如实告知并记进 VERSION）
#   ② 前端产物：**默认在干净检出里构建**。本机工作树里有未跟踪的
#      electron/src/features/local-live/（不开源的本机实盘栏目），本机构建会把它
#      整块打进 dist-react/，而两份便携包都从那里取 web/ —— 干净检出里没有那个
#      目录，产物天然是公开形态（与 scripts/deploy_frontend.sh 里
#      「部署公开版本：改用干净检出」同一招，这里把它自动化）。
#   ③ 组装出包：调 deploy/portable/build_windows_pack.sh。源码取**本机工作树**
#      （models/、hpu 缓存这类运行期目录只在工作树里齐），只把前端换成 ② 的产物。
#   ④ 出厂净化闸门随出包一起跑（排除清单 / 必备清单 / 内网地址 / 明文口令 /
#      宿主残留值 / 本机独有栏目产物），有违规就拒绝出包、连 zip 都不写；
#      通过后打印体积与 sha256。
#
# 产物: deploy/portable/dist/QuantMind-Portable-win-x64.zip (+ .sha256)
# 收件人拿到 zip 解压后双击 start.bat 即可，包内 README.md 是安装说明。
# ============================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORTABLE="${PROJECT_ROOT}/deploy/portable"
BUILD_DIR="${PORTABLE}/build"
CLEAN_SRC="${BUILD_DIR}/clean-src"
ZIP_OUT="${PORTABLE}/dist/QuantMind-Portable-win-x64.zip"
ZIP_SHA="${ZIP_OUT}.sha256"

log()  { echo -e "\033[36m[pack]\033[0m $*"; }
ok()   { echo -e "\033[32m[ok]\033[0m $*"; }
warn() { echo -e "\033[33m[warn]\033[0m $*"; }
fail() { echo -e "\033[31m[fail]\033[0m $*" >&2; exit 1; }

# ── 参数 ──────────────────────────────────────────────────────
MODE_PRIVATE=""
REAL_TRADING=""
WEB_DIST_OPT=""
REV="HEAD"
REQUIRE_CLEAN=""
KEEP_WT=""
SKIP_ASSEMBLE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --private)        MODE_PRIVATE=1 ;;
        --real-trading)   REAL_TRADING=1 ;;
        --web-dist)       WEB_DIST_OPT="${2:-}"; [ -n "$WEB_DIST_OPT" ] || fail "--web-dist 缺目录"; shift ;;
        --web-dist=*)     WEB_DIST_OPT="${1#*=}" ;;
        --rev)            REV="${2:-}"; [ -n "$REV" ] || fail "--rev 缺参数"; shift ;;
        --rev=*)          REV="${1#*=}" ;;
        --require-clean)  REQUIRE_CLEAN=1 ;;
        --keep-worktree)  KEEP_WT=1 ;;
        --skip-assemble)  SKIP_ASSEMBLE=1 ;;
        -h|--help)        sed -n '3,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)                fail "未知参数：$1（-h 看用法）" ;;
    esac
    shift
done

[ -n "$MODE_PRIVATE" ] && [ -n "$WEB_DIST_OPT" ] && fail "--private（本机产物）与 --web-dist 互斥"
[ -n "$MODE_PRIVATE" ] && [ "$REV" != "HEAD" ] && fail "--private（本机产物）与 --rev 互斥：干净检出不带未跟踪的本机栏目"

# ── 1. 前置检查 ───────────────────────────────────────────────
for tool in git python3 npm curl; do
    command -v "$tool" >/dev/null || fail "需要 $tool（便携包在 Linux 上交叉组装）"
done
[ -f "${PORTABLE}/build_windows_pack.sh" ] || fail "缺组装脚本：${PORTABLE}/build_windows_pack.sh"
[ -f "${PORTABLE}/pack_guard.py" ] || fail "缺出厂闸门：${PORTABLE}/pack_guard.py"
mkdir -p "$BUILD_DIR"
AVAIL_GB=$(( $(df -k "$BUILD_DIR" | awk 'NR==2{print $4}') / 1024 / 1024 ))
[ "$AVAIL_GB" -ge 23 ] || fail "磁盘剩余 ${AVAIL_GB}GB，组装需要 23GB 以上（存量缓存可复用）"

GIT_REV="$(git -C "$PROJECT_ROOT" rev-parse --short "$REV" 2>/dev/null)" \
    || fail "取不到修订 $REV（先 git fetch？）"
# 未提交的跟踪改动。**排除 data/**：那是本仓的已知形态——data/ 在本机是指向
# /media/zbox/data/quantmind 的符号链接，git 恒把里面 12 个已跟踪文件报成已删除。
# 不排除的话这条提示永远亮着，很快就会被人无视——一条会误报的护栏等于没有护栏。
DIRTY_LIST="$(git -C "$PROJECT_ROOT" status --porcelain --untracked-files=no -- . ':(exclude)data' || true)"
DIRTY_COUNT="$(printf '%s' "$DIRTY_LIST" | grep -c . || true)"
GIT_REV_TAG="$GIT_REV"
if [ "$DIRTY_COUNT" -gt 0 ]; then
    GIT_REV_TAG="${GIT_REV}-dirty"
    warn "工作树有 ${DIRTY_COUNT} 处未提交的跟踪改动 —— 源码那一半出的是**工作树**，不是 $GIT_REV"
    printf '%s\n' "$DIRTY_LIST" | head -10 | sed 's/^/        /'
    [ "$DIRTY_COUNT" -gt 10 ] && echo "        … 另有 $((DIRTY_COUNT - 10)) 处"
    if [ -n "$REQUIRE_CLEAN" ]; then
        fail "--require-clean：工作树不干净，这份包追不回任何修订。先提交（或 stash）再来"
    fi
    warn "产物的 VERSION 会记作 ${GIT_REV_TAG} 以示区别；正式发布请加 --require-clean"
fi

# ── 2. 前端产物 ───────────────────────────────────────────────
WT_ACTIVE=""
remove_worktree() {
    if [ -d "$CLEAN_SRC" ] || git -C "$PROJECT_ROOT" worktree list --porcelain 2>/dev/null \
            | grep -qF "worktree ${CLEAN_SRC}"; then
        git -C "$PROJECT_ROOT" worktree remove --force "$CLEAN_SRC" >/dev/null 2>&1 \
            || rm -rf "$CLEAN_SRC"
    fi
    git -C "$PROJECT_ROOT" worktree prune >/dev/null 2>&1 || true
}
cleanup() {
    local rc=$?
    if [ -n "$WT_ACTIVE" ] && [ -z "$KEEP_WT" ]; then
        remove_worktree
    fi
    return $rc
}
trap cleanup EXIT

build_react() {  # $1 = 构建用的 electron/ 目录
    if [ -n "$REAL_TRADING" ]; then
        log "  VITE_ENABLE_REAL_TRADING=true（前端实盘 UI 可见；后端那半看目标机 pack.env）"
        ( cd "$1" && VITE_ENABLE_REAL_TRADING=true npm run build:react )
    else
        ( cd "$1" && npm run build:react )
    fi
}

if [ -n "$WEB_DIST_OPT" ]; then
    WEB_DIST="$(cd "$WEB_DIST_OPT" 2>/dev/null && pwd)" || fail "--web-dist 目录不存在：$WEB_DIST_OPT"
    [ -f "${WEB_DIST}/index.html" ] || fail "--web-dist 里没有 index.html：$WEB_DIST"
    FRONTEND_DESC="指定产物（未重新构建）"
    log "前端产物：$WEB_DIST（--web-dist 指定）"
elif [ -n "$MODE_PRIVATE" ]; then
    WEB_DIST="${PROJECT_ROOT}/electron/dist-react"
    FRONTEND_DESC="本机工作树产物（自用包）"
    log "自用包：在本机工作树构建前端（含未跟踪的 local-live 实盘栏目）..."
    build_react "${PROJECT_ROOT}/electron"
else
    WEB_DIST="${CLEAN_SRC}/electron/dist-react"
    FRONTEND_DESC="干净检出 $GIT_REV 的产物"
    log "干净检出 $REV（$GIT_REV）→ ${CLEAN_SRC} ..."
    remove_worktree
    git -C "$PROJECT_ROOT" worktree add --detach --quiet "$CLEAN_SRC" "$REV" \
        || fail "git worktree add 失败（${CLEAN_SRC}）"
    WT_ACTIVE=1
    # 本机独有栏目在干净检出里必须**不存在**（它在 .gitignore 里，没提交过）。
    # 真出现了说明有人把它提交了 —— 那正是「不开源的部分」要出事的那一天，当场停。
    if [ -e "${CLEAN_SRC}/electron/src/features/local-live" ]; then
        fail "干净检出里出现了 local-live/：它被提交进版本库了（.gitignore 被绕过？）。
       这份检出不能用来做公开形态的产物，先把它从版本库里清掉"
    fi
    # 依赖树（根 1.4G + electron 80M）不重装，软链本机那份：vite/pnpm 这套（依赖
    # 本来就是软链）一直这么工作，构建的读写都落在工作树的输出目录里。
    ln -sfn "${PROJECT_ROOT}/node_modules" "${CLEAN_SRC}/node_modules"
    ln -sfn "${PROJECT_ROOT}/electron/node_modules" "${CLEAN_SRC}/electron/node_modules"
    log "类型检查（干净检出）..."
    ( cd "${CLEAN_SRC}/electron" && npm run typecheck ) \
        || fail "干净检出的前端类型检查没过 —— 这个修订不适合出包（vite 不做类型检查，这里才拦得住）"
    log "构建前端（干净检出）..."
    build_react "${CLEAN_SRC}/electron"
fi
[ -f "${WEB_DIST}/index.html" ] || fail "前端产物不完整（缺 index.html）：$WEB_DIST"
ok "前端就绪：$WEB_DIST（$(ls "${WEB_DIST}/assets" | wc -l) 个 assets）"

if [ -n "$SKIP_ASSEMBLE" ]; then
    ok "--skip-assemble：到此为止（前端产物已就绪，未组装未出包）"
    exit 0
fi

# ── 3. 组装 + 出包（闸门在里面跑）─────────────────────────────
log "组装便携包（源码取本机工作树；闸门随出包一起跑）..."
export WEB_DIST
if [ -n "$MODE_PRIVATE" ]; then
    export PACK_ALLOW_LOCAL_LIVE=1
    warn "自用包：显式放行本机独有实盘栏目（这一包**不要**发给第三方）"
fi
bash "${PORTABLE}/build_windows_pack.sh"

# ── 4. 交付摘要 ───────────────────────────────────────────────
[ -f "$ZIP_OUT" ] || fail "组装脚本跑完却没有产物：$ZIP_OUT"
SIZE="$(du -h "$ZIP_OUT" | cut -f1)"
SHA="$(cut -d' ' -f1 "$ZIP_SHA" 2>/dev/null || sha256sum "$ZIP_OUT" | cut -d' ' -f1)"
echo
ok "出包完成"
echo "  产物   : $ZIP_OUT"
echo "  体积   : $SIZE"
echo "  sha256 : $SHA"
echo "  修订   : $GIT_REV_TAG"
echo "  前端   : $FRONTEND_DESC"
echo "            $WEB_DIST"
if [ -n "$MODE_PRIVATE" ]; then
    echo "  形态   : 自用（含本机独有实盘栏目）—— 不经核验不要外发"
else
    echo "  形态   : 第三方（全栏目、无本机独有内容）"
fi
echo
echo "  发给别人时附上："
echo "    · 解压后双击 start.bat —— 首次启动自动建库 / 生成密钥 / 初始化数据目录"
echo "    · 需要 64 位 Windows 10/11 与 15GB 以上空闲磁盘"
echo "    · 默认密码与端口改法见包内 README.md（pack.env）"
echo "    · 校验下载完整性:  sha256sum -c $(basename "$ZIP_SHA")"
