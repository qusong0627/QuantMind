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
BRANCH="${QM_SYNC_BRANCH:-main}"
REPO=""
# auto-detect: env > home > sibling-next-to-package
for cand in "${QM_REPO_ROOT:-}" "$HOME/quantmind-src" "$HOME/QuantMind" "/opt/quantmind-src" "$PACK/../quantmind-src" "$PACK/../QuantMind"; do
    if [ -n "$cand" ] && [ -d "$cand/.git" ]; then REPO="$cand"; break; fi
done

if [ -z "$REPO" ]; then
    echo "[!] no git repo auto-detected."
    echo "    Easiest: clone next to the package, e.g."
    echo "      cd $PACK/.."
    echo "      git clone -b main --single-branch https://gitee.com/qusong0627/QuantMind.git quantmind-src"
    echo "    then run this script again. Or: export QM_REPO_ROOT=/path/to/quantmind"
    exit 1
fi

echo "[sync] repo: $REPO  branch: $BRANCH"
echo "[sync] pulling latest code ..."
git -C "$REPO" fetch origin || { echo "[!] fetch failed"; exit 1; }
git -C "$REPO" checkout "$BRANCH" 2>/dev/null || true
git -C "$REPO" pull origin "$BRANCH" || { echo "[!] pull failed - check network/credentials"; exit 1; }

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

echo
echo "[sync] Done. Restart with: ./start.sh"
echo "[sync] UI updates? sync web/ from the maintainer or rebuild dist-react."
