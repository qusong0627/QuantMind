#!/usr/bin/env bash
# ============================================================
# QuantMind portable: sync code from local git clone into package
# Ubuntu / Debian / WSL2
# Prereq: this file sits in the package root, and a git clone of
# the QuantMind repo exists on this machine.
# Edit REPO below to that clone path (or export QM_REPO_ROOT).
#
# What it does:
#   git pull (origin <branch>)
#   copy backend/ config/ strategy_templates/ web/ into the package
#   copy data/upgrade_*.sql into the package (startup migrations)
#   clear __pycache__ (stale bytecode protection)
#   ask you to restart with start.sh
#
# NOTE: runtime/models/data are NOT in git - code updates only.
# web/ (frontend build artifacts) IS tracked on the <branch> since
# 2026-09 and synced with rsync --delete (stale chunks removed).
# ============================================================
set -u
cd "$(dirname "$0")" || exit 1
PACK="$(pwd)"
BRANCH="${QM_SYNC_BRANCH:-next}"
URL="${QM_REPO_URL:-https://gitee.com/qusong0627/QuantMind.git}"
BEFORE="" N_COMMITS=0 N_FILES=0
# write permission pre-check
if ! touch "$PACK/.qm_write_test" 2>/dev/null; then
    echo "[!] CANNOT WRITE to package folder - read-only or locked."
    echo "    Move the package to a normal local folder and retry."
    exit 1
fi
rm -f "$PACK/.qm_write_test"
REPO=""
# auto-detect: env > home > sibling-next-to-package
for cand in "${QM_REPO_ROOT:-}" "$HOME/quantmind-src" "$HOME/QuantMind" "/opt/quantmind-src" "$PACK/../quantmind-src" "$PACK/../QuantMind"; do
    if [ -n "$cand" ] && [ -d "$cand/.git" ]; then REPO="$cand"; break; fi
done

if ! command -v git >/dev/null 2>&1; then
    echo "[!] git not found - trying to install it automatically..."
    if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        sudo apt-get update -qq 2>/dev/null
        sudo apt-get install -y git 2>&1 | tail -2
    else
        echo "    Automatic install needs passwordless sudo."
        echo "    Either run:  sudo apt install -y git"
        echo "    and rerun this script, or ask the maintainer for a patch zip."
        exit 1
    fi
fi
command -v git >/dev/null 2>&1 || { echo "[!] git install failed - rerun after installing git."; exit 1; }

if [ -z "$REPO" ]; then
    echo "[sync] no local clone found next to the package."
    read -r -p "Auto-clone now? [y/n]: " DO_CLONE
    if [ "$DO_CLONE" = "y" ] || [ "$DO_CLONE" = "Y" ] || [ "$DO_CLONE" = "yes" ]; then
        echo "[sync] cloning..."
        git clone -b "$BRANCH" "$URL" "$PACK/../quantmind-src" || {
            echo "[!] clone failed - check internet, or repo needs credentials."
            echo "    Private repo? ask the maintainer for access or another URL."
            exit 1; }
        REPO="$PACK/../quantmind-src"
    else
        echo "    Manual clone later:"
        echo "      cd $PACK/.."
        echo "      git clone -b $BRANCH $URL quantmind-src"
        echo "    then rerun. Private repo? ask the maintainer."
        exit 1
    fi
fi

echo "[sync] repo: $REPO  branch: $BRANCH"
BEFORE="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)"
echo "[sync] pulling latest code ..."
git -C "$REPO" fetch origin || { echo "[!] fetch failed"; exit 1; }
git -C "$REPO" fetch origin "$BRANCH:refs/remotes/origin/$BRANCH" 2>/dev/null \
    || echo "[sync] warning: prefetch of $BRANCH failed, checkout will tell if it matters"
git -C "$REPO" checkout "$BRANCH" 2>/dev/null || true
git -C "$REPO" pull origin "$BRANCH" || { echo "[!] pull failed - check network/credentials"; exit 1; }
N_COMMITS="$(git -C "$REPO" rev-list --count "${BEFORE}..HEAD" 2>/dev/null || echo 0)"
N_FILES="$(git -C "$REPO" diff --name-only "${BEFORE}..HEAD" 2>/dev/null | wc -l)"

echo "[sync] stopping services ..."
if [ -x "$PACK/stop.sh" ]; then
    bash "$PACK/stop.sh" >/dev/null 2>&1 || true
fi
pkill -f "backend.main_oss" 2>/dev/null
pkill -f "celery.*qlib_backtest_srv" 2>/dev/null
pkill -f "celery.*celery_app beat" 2>/dev/null
sleep 1

echo "[sync] backing up the currently working code ..."
mkdir -p "$PACK/backups"
BK_FILE=""
if command -v tar >/dev/null 2>&1; then
    BK_FILE="$PACK/backups/code-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
    tar czf "$BK_FILE" -C "$PACK" --exclude='__pycache__' backend config strategy_templates web 2>/dev/null
    [ -f "$BK_FILE" ] || BK_FILE=""
fi
if [ -z "$BK_FILE" ]; then
    BK_FILE="$PACK/backups/code-backup-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$BK_FILE"
    for d in backend config strategy_templates web; do
        [ -d "$PACK/$d" ] && cp -a "$PACK/$d" "$BK_FILE/"
    done
fi
# keep only the 5 newest backups
ls -1dt "$PACK"/backups/code-backup-* 2>/dev/null | tail -n +6 | xargs -r rm -rf 2>/dev/null || true
echo "[sync] backup OK: $BK_FILE"

echo "[sync] copying backend / config / strategy_templates / web ..."
for d in backend config strategy_templates; do
    if [ -d "$REPO/$d" ]; then
        mkdir -p "$PACK/$d"
        cp -a "$REPO/$d/." "$PACK/$d/"
    fi
done
# docker/training 整目录（train.py 顶层 import model_trainers/diagnostics/data 同级包；
# 代码包 data 与包根数据目录 data/ 同名，不能拉平到包根 → 保相对布局整目录镜像）。
# 顺带清掉旧拉平布局残留（包根单文件 train.py/model_trainers 会误导脚本探测
# 优先命中而 import 失败）。
if [ -d "$REPO/docker/training" ]; then
    rm -rf "$PACK/docker/training"
    mkdir -p "$PACK/docker"
    cp -a "$REPO/docker/training" "$PACK/docker/training"
    rm -f "$PACK/train.py"
    rm -rf "$PACK/model_trainers"
    echo "[sync] docker/training refreshed (train.py + model_trainers/diagnostics/data)"
else
    echo "[sync] note: repo docker/training missing - package training scripts NOT refreshed"
fi
# data/upgrade_*.sql（增量迁移 SQL，随 git 跟踪）：镜像到包根 data/。
# main_oss._upgrade_sql_files() 启动时按候选目录探测执行（含 <backend>/../data），
# 缺这些文件则 system_events 等增量迁移永不执行——历史 bug，勿删。
if ls "$REPO"/data/upgrade_*.sql >/dev/null 2>&1; then
    mkdir -p "$PACK/data"
    cp -f "$REPO"/data/upgrade_*.sql "$PACK/data/"
    echo "[sync] upgrade SQL refreshed ($(ls "$PACK"/data/upgrade_*.sql 2>/dev/null | wc -l) files)"
else
    echo "[sync] note: repo data/upgrade_*.sql missing - startup migrations will NOT run"
fi

# web/（前端构建产物，随 git 跟踪）：镜像覆盖并清掉旧 chunk，避免 UI 残留
if [ -f "$REPO/web/index.html" ]; then
    if command -v rsync >/dev/null 2>&1; then
        mkdir -p "$PACK/web"
        rsync -a --delete "$REPO/web/" "$PACK/web/"
    else
        rm -rf "$PACK/web"
        mkdir -p "$PACK/web"
        cp -a "$REPO/web/." "$PACK/web/"
    fi
    echo "[sync] web assets updated (前端 UI 变更已随本次同步生效)"
else
    echo "[sync] note: repo has no web/index.html - frontend sync skipped"
fi

# 客户更新件（增量补丁 zip，随仓库 updates/ 分发）：镜像到包内 updates/
# 目录，部署机可直接取件发客户，无需从 git 深处翻找
mkdir -p "$PACK/updates"
if ls "$REPO"/deploy/portable/updates/*.zip >/dev/null 2>&1; then
    cp -f "$REPO"/deploy/portable/updates/*.zip "$PACK/updates/" 2>/dev/null
    echo "[sync] customer update zips mirrored ($(ls "$PACK"/updates/*.zip 2>/dev/null | wc -l) files)"
else
    echo "[sync] note: repo deploy/portable/updates has no zip"
fi

echo "[sync] clearing __pycache__ ..."
find "$PACK/backend" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

mkdir -p "$PACK/logs"
echo "$(date '+%F %T') | commits=$N_COMMITS files=$N_FILES" >> "$PACK/logs/sync_history.log"
echo
echo "============================================"
echo "[sync] UPDATE SUMMARY"
if [ "$N_COMMITS" = "0" ]; then echo "  No new commits - already up to date."; fi
if [ "$N_COMMITS" != "0" ]; then echo "  New commits : $N_COMMITS"; fi
echo "  Files changed : $N_FILES"
echo "  History      : logs/sync_history.log"
echo "============================================"
echo "[sync] Done. Restart with: ./start.sh"
