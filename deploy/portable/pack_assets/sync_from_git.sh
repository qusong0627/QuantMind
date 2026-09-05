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
#   copy backend/ config/ strategy_templates/ into the package
#   clear __pycache__ (stale bytecode protection)
#   ask you to restart with start.sh
#
# NOTE: runtime/models/data are NOT in git - code updates only.
# Frontend artifacts (dist-react/web) are NOT in git; sync web/
# separately if an update includes UI changes.
# ============================================================
set -u
cd "$(dirname "$0")" || exit 1
PACK="$(pwd)"
BRANCH="${QM_SYNC_BRANCH:-master}"
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
        git clone -b "$BRANCH" --single-branch "$URL" "$PACK/../quantmind-src" || {
            echo "[!] clone failed - check internet, or repo needs credentials."
            echo "    Private repo? ask the maintainer for access or another URL."
            exit 1; }
        REPO="$PACK/../quantmind-src"
    else
        echo "    Manual clone later:"
        echo "      cd $PACK/.."
        echo "      git clone -b $BRANCH --single-branch $URL quantmind-src"
        echo "    then rerun. Private repo? ask the maintainer."
        exit 1
    fi
fi

echo "[sync] repo: $REPO  branch: $BRANCH"
BEFORE="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)"
echo "[sync] pulling latest code ..."
git -C "$REPO" fetch origin || { echo "[!] fetch failed"; exit 1; }
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

echo "[sync] copying backend / config / strategy_templates ..."
for d in backend config strategy_templates; do
    if [ -d "$REPO/$d" ]; then
        mkdir -p "$PACK/$d"
        cp -a "$REPO/$d/." "$PACK/$d/"
    fi
done

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
echo "[sync] UI updates? sync web/ from the maintainer or rebuild dist-react."
