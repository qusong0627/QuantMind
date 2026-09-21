#!/usr/bin/env bash
# QuantMind 前端部署脚本
# 用法: bash scripts/deploy_frontend.sh [--skip-build]
#
# 解决问题: docker cp 不会清理已删除的旧 chunk，且 main 文件名 hash 每次变化，
#           零散 cp 会留下"旧 main 找不到 + 新 main 没复制"的混合状态。
#           本脚本: 1) 先清空容器 assets/，2) 全量复制 dist-react/，3) 校验 main 文件存在
#
# 选项:
#   --skip-build   跳过 npm run build，直接部署当前 dist-react/
set -euo pipefail

# ── 配置 ──────────────────────────────────────────────────────
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ELECTRON_DIR="${PROJECT_ROOT}/electron"
DIST_DIR="${ELECTRON_DIR}/dist-react"
WEB_CONTAINER="quantmind-web"
NGINX_ROOT="/usr/share/nginx/html"
SKIP_BUILD="${1:-}"

# ── 工具函数 ──────────────────────────────────────────────────
log()   { echo -e "\033[36m[deploy]\033[0m $*"; }
ok()    { echo -e "\033[32m[ok]\033[0m $*"; }
fail()  { echo -e "\033[31m[fail]\033[0m $*" >&2; exit 1; }

# ── 0. 拒绝部署「含本机独有实盘栏目」的产物 ────────────────────
# electron/src/features/local-live/ 是开发者本机独有的实盘交易栏目，被 .gitignore
# 排除、不开源。本机 `npm run build` 会把它打进 dist-react/，而本脚本会 `docker cp`
# 到 quantmind-web —— 等于把不开源的部分部署到公网。宁可拒绝，不可误发。
#
# 这里只做**源码存在性**检查（能给出最清楚的提示）；产物层面还有第 3 步的第二道闸，
# 拦的是「源码已移走但 dist-react/ 是旧的本地构建」这种绕过。
LOCAL_LIVE_SRC="${ELECTRON_DIR}/src/features/local-live"
if [[ -d "${LOCAL_LIVE_SRC}" ]]; then
    fail "本机存在 ${LOCAL_LIVE_SRC}（不开源的本机实盘栏目）。
       本机构建会把实盘 UI 打进 dist-react/，部署即等于公开发布。
       要部署公开版本，请改用干净检出：
         git worktree add /tmp/qm-public next && cd /tmp/qm-public && bash scripts/deploy_frontend.sh"
fi

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

# 第二道闸：产物层面。拦「源码已移走 / 在干净检出里跑但 dist-react/ 是旧的本地构建」
# 这种绕过第 0 步的情况。判据是**本机栏目的 chunk 是否存在**，不是 grep 中文文案——
# 「实盘交易」四个字本来就在公开产物里（modeCopy 的 full 字段），拿它当判据永不成立。
LOCAL_LIVE_CHUNKS=$(find "${DIST_DIR}/assets" -maxdepth 1 -name 'LiveTradingPage*.js' | head -5)
[[ -z "${LOCAL_LIVE_CHUNKS}" ]] || fail "dist-react/ 里有本机实盘栏目的 chunk，拒绝部署：
${LOCAL_LIVE_CHUNKS}
       请删掉 dist-react/ 后在干净检出里重新构建。"

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
