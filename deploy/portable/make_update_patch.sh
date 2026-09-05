#!/usr/bin/env bash
# ============================================================
# QuantMind 便携包 增量补丁生成器
# 用法: bash deploy/portable/make_update_patch.sh [基线提交]
#   基线默认 HEAD~1;也可传 tag/commit 如 v2.3.3
# 产出: deploy/portable/dist/QuantMind-Update-<日期>.zip
#       (内含 backend/config/strategy_templates/根级脚本 +
#        apply_update.bat,老用户解压覆盖到包根后双击应用)
#
# 说明: 只打包 git 跟踪且属于「代码类」的改动路径;
#   electron/dist-react 前端产物不入 git → 前端改动请同步 web/ 目录
#   (在打包机 npm run build:react 后把 electron/dist-react 拷为 web/)
# ============================================================
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$REPO_ROOT/deploy/portable"
DIST="$HERE/dist"
BASE="${1:-HEAD~1}"

CODE_PREFIXES="backend/ config/ strategy_templates/ scripts/ pack.env.example pg_setup.py train.py preprocessing.py parallel_utils.py parallel_utils.py"
# pack_assets 里的 bat 落到包根(便携包根目录的启动脚本)
BAT_MAP="pack_assets/start.bat:start.bat pack_assets/stop.bat:stop.bat pack_assets/install_gpu.bat:install_gpu.bat"

cd "$REPO_ROOT"
[ -n "$(git rev-parse --verify -q "$BASE" 2>/dev/null || true)" ] || { echo "[!] 基线不存在: $BASE"; exit 1; }

STAGE="$HERE/build/update-patch"
rm -rf "$STAGE"; mkdir -p "$STAGE"

changed=0
# 1) 代码类路径(原样保留相对路径)
for f in $(git diff --name-only "$BASE"..HEAD -- $CODE_PREFIXES); do
    [ -f "$f" ] || continue
    mkdir -p "$STAGE/$(dirname "$f")"
    cp "$f" "$STAGE/$f"
    changed=1
done
# 2) bat 映射到包根
for pair in $BAT_MAP; do
    src="${pair%%:*}"; dst="${pair##*:}"
    if git diff --quiet "$BASE"..HEAD -- "$src" 2>/dev/null; then continue; fi
    mkdir -p "$STAGE"
    cp "$src" "$STAGE/$dst"
    changed=1
done

if [ "$changed" = "0" ]; then
    echo "[!] $BASE..HEAD 没有代码类改动,无需补丁"
    exit 0
fi

# 3) 生成应用脚本(ASCII+CRLF 铁律)
python3 - "$STAGE/apply_update.bat" <<'PYEOF'
import sys
bat = r'''@echo off
rem QuantMind portable update apply
rem Place this zip's content over the package root first, then run me.
rem I stop services, finish copying (idempotent), and tell you to restart.
setlocal
cd /d "%~dp0"
set "ROOT=%CD%"
echo [update] package root: %ROOT%
echo [update] stopping services...
taskkill /FI "WINDOWTITLE eq QuantMind-CeleryBeat*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-CeleryWorker*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-Backend*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-Redis*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-Huntly*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-QwenPaw*" /T /F >nul 2>&1
taskkill /FI "IMAGENAME eq python.exe" /F >nul 2>&1
if exist "%ROOT%\pgdata\PG_VERSION" if exist "%ROOT%\pgsql\bin\pg_ctl.exe" (
    "%ROOT%\pgsql\bin\pg_ctl.exe" -D "%ROOT%\pgdata" stop -m fast >nul 2>&1
)
echo [update] files were merged by your unzip before this step.
echo [update] clearing __pycache__ to avoid stale bytecode...
for /d /r "%ROOT%\backend" %%d in (__pycache__) do rd /s /q "%%d" 2>nul
echo.
echo [update] Done. Start the package with start.bat now.
echo [update] If something fails, send logs\backend.log to the maintainer.
pause
endlocal
'''
open(sys.argv[1], 'w', encoding='ascii', newline='').write(bat.replace('\n','\r\n'))
print('apply_update.bat written')
PYEOF

ZIP="$DIST/QuantMind-Update-$(date +%Y%m%d-%H%M).zip"
rm -f "$ZIP"
(cd "$STAGE" && python3 -m zipfile -c "$ZIP" .)
echo "补丁包: $ZIP ($(du -sh "$ZIP" | cut -f1))"
echo "分发: 老用户解压到便携包根目录(覆盖合并),双击 apply_update.bat,再 start.bat"
