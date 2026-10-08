#!/usr/bin/env bash
# QuantMind 前端部署脚本
# 用法: bash scripts/deploy_frontend.sh [--skip-build] [--allow-local-live]
#
# 解决问题: docker cp 不会清理已删除的旧 chunk，且 main 文件名 hash 每次变化，
#           零散 cp 会留下"旧 main 找不到 + 新 main 没复制"的混合状态。
#           本脚本: 1) 先清空容器 assets/，2) 全量复制 dist-react/，3) 校验 main 文件存在
#
# 选项:
#   --skip-build        跳过 npm run build，直接部署当前 dist-react/
#   --allow-local-live  允许部署**实盘 UI 可见**的产物（构建时 VITE_ENABLE_REAL_TRADING=true，
#                       底部栏会出现「实盘交易」入口）。默认拒绝，见第 0 步。
set -euo pipefail

# ── 配置 ──────────────────────────────────────────────────────
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ELECTRON_DIR="${PROJECT_ROOT}/electron"
DIST_DIR="${ELECTRON_DIR}/dist-react"
WEB_CONTAINER="quantmind-web"
NGINX_ROOT="/usr/share/nginx/html"
SKIP_BUILD=""
ALLOW_LOCAL_LIVE="${ALLOW_LOCAL_LIVE:-}"

# ── 工具函数 ──────────────────────────────────────────────────
log()   { echo -e "\033[36m[deploy]\033[0m $*"; }
ok()    { echo -e "\033[32m[ok]\033[0m $*"; }
warn()  { echo -e "\033[33m[warn]\033[0m $*"; }
fail()  { echo -e "\033[31m[fail]\033[0m $*" >&2; exit 1; }

# ── 参数 ──────────────────────────────────────────────────────
for arg in "$@"; do
    case "$arg" in
        --skip-build)        SKIP_BUILD="--skip-build" ;;
        --allow-local-live)  ALLOW_LOCAL_LIVE=1 ;;
        *) fail "未知参数：$arg（可用：--skip-build / --allow-local-live）" ;;
    esac
done

# ── 0. 实盘 UI 可见的构建：默认拒绝，显式放行 ─────────────────
# 栏目源码 electron/src/features/local-live/ 已入仓（2026-10-08），「本机独有目录」
# 这道旧闸门（源码存在即拒绝）随之退役 —— 留着它会拒绝**每一次**部署。
#
# 现在要拦的是**实盘 UI 可见性**：`VITE_ENABLE_REAL_TRADING=true` 构建出来的产物，
# 底部栏会出现「实盘交易」入口、账户概览显示真实券商账户快照（导航项由
# isLiveTradingEnabled() 收敛）。若 quantmind-web 面向公网，等于对外发布了实盘
# 控制台 —— 与「默认构建不显示实盘 UI」的声明相反。
#
# 判据 = 构建开关本身（本脚本自己就是构建入口，env 即是构建期注入值）：
# 默认拒绝，加 --allow-local-live 才放行。拦的依然是**意外**，不是**决定**。
# 启用流程（两端同时打开）见 docs/实盘模块_启用与通道指南.md。
REAL_TRADING_FLAG="$(echo "${VITE_ENABLE_REAL_TRADING:-}" | tr '[:upper:]' '[:lower:]')"
if [[ "${REAL_TRADING_FLAG}" == "true" && -z "${ALLOW_LOCAL_LIVE}" ]]; then
    fail "本次构建打开了实盘 UI（VITE_ENABLE_REAL_TRADING=true）。
       部署后底部栏会出现「实盘交易」入口，账户概览会显示真实券商账户；
       若 ${WEB_CONTAINER} 面向公网即等于公开发布实盘控制台。
       两种出路：
         · 就是要发布实盘 UI：VITE_ENABLE_REAL_TRADING=true bash scripts/deploy_frontend.sh --allow-local-live
         · 部署默认版本（不显示实盘 UI）：去掉 VITE_ENABLE_REAL_TRADING 重新构建
             bash scripts/deploy_frontend.sh"
fi
# （这是**提前**失败：省掉一次白构建。放行的提示与真正权威的判据在第 3 步——
#   那里读的是**产物里的内联标记**，`--skip-build` 与「env 与产物不一致」都跑不掉。）

# ── 1. 检查 web 容器在跑 ───────────────────────────────────────
log "检查 quantmind-web 容器..."
docker inspect "${WEB_CONTAINER}" --format '{{.State.Status}}' 2>/dev/null \
    | grep -q '^running$' \
    || fail "${WEB_CONTAINER} 容器未运行，先 docker compose up -d quantmind-web"

# ── 2. 构建（可选）─────────────────────────────────────────────
if [[ "$SKIP_BUILD" == "--skip-build" ]]; then
    log "跳过 npm run build（--skip-build）"
else
    log "在 ${ELECTRON_DIR} 跑 npm run build..."
    cd "${ELECTRON_DIR}"
    if ! command -v npm >/dev/null 2>&1; then
        fail "未找到 npm 命令"
    fi
    npm run build 2>&1 | tail -10
    cd - >/dev/null
fi

# ── 3. 校验本地构建产物 ──────────────────────────────────────
[[ -d "${DIST_DIR}" ]] || fail "${DIST_DIR} 不存在"
[[ -f "${DIST_DIR}/index.html" ]] || fail "${DIST_DIR}/index.html 不存在，build 失败？"
[[ -d "${DIST_DIR}/assets" ]] || fail "${DIST_DIR}/assets 不存在"

MAIN_REF=$(grep -oE 'main-[A-Za-z0-9_-]+\.js' "${DIST_DIR}/index.html" | head -1)
[[ -n "${MAIN_REF}" ]] || fail "index.html 找不到 main-*.js 引用"
[[ -f "${DIST_DIR}/assets/${MAIN_REF}" ]] || fail "${MAIN_REF} 在 dist-react/assets/ 中不存在"
ok "本地构建: index.html → ${MAIN_REF}"

# ── 3b. 实盘 UI 判据（权威）：读产物里的**内联开关标记** ─────────
# 旧判据「产物里有没有本机栏目 chunk」已退役：源码入仓后 LiveTradingPage*.js 在
# **每一次**构建里都存在，chunk 存在性不再是信号。但开关本身是**可反推**的：
# Vite 会把构建期 env 以对象字面量内联进 bundle（实测 `VITE_ENABLE_REAL_TRADING:"true"`
# 逐字可见，形如 pack_rules 的 VITE_LIVE_NODE_ONLY 判据）。所以这里直接读产物——
# env 与产物不一致、--skip-build 拿旧产物，都拦得住。**拦的是意外，不是决定**。
if grep -rqs 'VITE_ENABLE_REAL_TRADING:"true"' "${DIST_DIR}/assets"; then
    if [[ -z "${ALLOW_LOCAL_LIVE}" ]]; then
        fail "dist-react/ 是**实盘 UI 可见**的构建（产物内联标记 VITE_ENABLE_REAL_TRADING:\"true\"），拒绝部署。
       这就是「底部栏出现实盘交易入口、账户概览显示真实券商账户」的那一类产物。
       两种出路：
         · 就是要发布实盘 UI：VITE_ENABLE_REAL_TRADING=true bash scripts/deploy_frontend.sh --allow-local-live
         · 部署默认版本：删掉 dist-react/ 后不带该 env 重新构建"
    fi
    warn "部署产物**实盘 UI 可见**（产物内联 VITE_ENABLE_REAL_TRADING=true）——底部栏会出现「实盘交易」入口。"
    warn "容器：${WEB_CONTAINER}（$(docker port "${WEB_CONTAINER}" 80/tcp 2>/dev/null | head -1 || echo '端口未知')）"
fi

# ── 4. 清空容器旧 assets ─────────────────────────────────────
log "清空 ${WEB_CONTAINER}:${NGINX_ROOT}/assets ..."
docker exec "${WEB_CONTAINER}" rm -rf "${NGINX_ROOT}/assets"
docker exec "${WEB_CONTAINER}" mkdir -p "${NGINX_ROOT}/assets"

# ── 5. 全量复制 dist-react 到容器 ────────────────────────────
log "复制 ${DIST_DIR}/ → ${WEB_CONTAINER}:${NGINX_ROOT}/ ..."
docker cp "${DIST_DIR}/." "${WEB_CONTAINER}:${NGINX_ROOT}/"

# ── 6. 校验容器内 main 文件存在 ─────────────────────────────
docker exec "${WEB_CONTAINER}" test -f "${NGINX_ROOT}/assets/${MAIN_REF}" \
    || fail "${MAIN_REF} 未成功复制到容器"
ok "容器已部署: ${MAIN_REF}"

# ── 7. 看下 assets 数量对得上 ────────────────────────────────
LOCAL_COUNT=$(find "${DIST_DIR}/assets" -type f | wc -l)
REMOTE_COUNT=$(docker exec "${WEB_CONTAINER}" sh -c "ls ${NGINX_ROOT}/assets | wc -l")
if [[ "${LOCAL_COUNT}" != "${REMOTE_COUNT}" ]]; then
    fail "assets 数量不一致：本地 ${LOCAL_COUNT}, 容器 ${REMOTE_COUNT}"
fi
ok "assets 文件数 ${LOCAL_COUNT} 一致"

# ── 8. 健康检查：nginx 能 serve 一个 JS 文件 ─────────────────
WEB_PORT=$(docker port "${WEB_CONTAINER}" 80/tcp 2>/dev/null | awk -F: '{print $NF}' | head -1)
WEB_PORT="${WEB_PORT:-3080}"
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${WEB_PORT}/assets/${MAIN_REF}" || echo "ERR")
[[ "${HTTP_CODE}" == "200" ]] || fail "nginx 没 serve ${MAIN_REF}（HTTP ${HTTP_CODE}）"
ok "nginx 健康：GET /assets/${MAIN_REF} → 200"

echo ""
ok "前端部署完成。浏览器强刷 Ctrl+Shift+R 即可。"
ok "地址：http://localhost:${WEB_PORT}/"
