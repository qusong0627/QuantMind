#!/usr/bin/env bash
# ============================================================
# QuantMind portable: restore last-known-good code backup
# Ubuntu / Debian / WSL2
# Put this file in the package root next to start.sh
# Run it when start.sh fails AFTER a sync_from_git.sh update.
# Restores the newest backups/code-backup-* snapshot.
# ============================================================
set -u
cd "$(dirname "$0")" || exit 1
PACK="$(pwd)"

NEWEST="$(ls -1dt "$PACK"/backups/code-backup-* 2>/dev/null | head -1)"
if [ -z "${NEWEST:-}" ] || [ ! -e "$NEWEST" ]; then
    echo "[!] no backup found under $PACK/backups - nothing to restore."
    echo "    Nothing was changed. Run ./start.sh and check logs."
    exit 1
fi
echo "[restore] source: $NEWEST"

echo "[restore] stopping services ..."
if [ -x "$PACK/stop.sh" ]; then
    bash "$PACK/stop.sh" >/dev/null 2>&1 || true
fi
pkill -f "backend.main_oss" 2>/dev/null
pkill -f "celery" 2>/dev/null
sleep 1

echo "[restore] replacing backend / config / strategy_templates ..."
rm -rf "$PACK/backend" "$PACK/config" "$PACK/strategy_templates"
# web(前端)随 2026-09 起的备份一并恢复;旧备份无 web 时保持现有前端不动
HAS_WEB=0
if [ -f "$NEWEST/web/index.html" ]; then
    HAS_WEB=1
elif [ "${NEWEST##*.}" = "gz" ] && tar tzf "$NEWEST" web/index.html >/dev/null 2>&1; then
    HAS_WEB=1
fi
if [ "$HAS_WEB" = "1" ]; then
    echo "[restore] replacing web (frontend) ..."
    rm -rf "$PACK/web"
fi
case "$NEWEST" in
    *.tar.gz)
        tar xzf "$NEWEST" -C "$PACK" || { echo "[!] extract failed"; exit 1; }
        ;;
    *)
        for d in backend config strategy_templates; do
            [ -d "$NEWEST/$d" ] && cp -a "$NEWEST/$d" "$PACK/"
        done
        if [ "$HAS_WEB" = "1" ]; then
            [ -d "$NEWEST/web" ] && cp -a "$NEWEST/web" "$PACK/"
        fi
        ;;
esac

find "$PACK/backend" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

echo
echo "[restore] DONE - the previous working code is back."
echo "    Run ./start.sh now."
echo "    Still failing? The problem is then outside the code"
echo "    (runtime or data) - see logs/backend.log and ask the"
echo "    maintainer with the log content."
