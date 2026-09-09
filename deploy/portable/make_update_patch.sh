#!/usr/bin/env bash
# ============================================================
# QuantMind 便携包 增量补丁生成器
# 用法: bash deploy/portable/make_update_patch.sh [基线提交]
#   基线默认 HEAD~1;也可传 tag/commit 如 v2.3.3
# 产出: deploy/portable/dist/QuantMind-Update-<日期>.zip
#       (内含 backend/config/strategy_templates/根级脚本 + data/upgrade_*.sql +
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

CODE_PREFIXES="backend/ config/ strategy_templates/ scripts/ pack.env.example pg_setup.py"
# pack_assets 里的启动/运维脚本与示例配置落到包根(便携包根目录同名文件)。
# 覆盖 .sh/.command 而不只是 .bat：此前这两条链路只同步 backend/config/web，
# 启动脚本改了老包拿不到（依赖新环境变量的更新会静默失效）。
# pack.env.example 只更新模板说明, 不会覆盖用户自己的 pack.env。
# 路径必须是仓库相对路径(脚本已 cd 到 REPO_ROOT), 否则 git diff 匹配不到、静默跳过。
ASSET_MAP="
deploy/portable/pack_assets/start.sh:start.sh
deploy/portable/pack_assets/stop.sh:stop.sh
deploy/portable/pack_assets/start.command:start.command
deploy/portable/pack_assets/stop.command:stop.command
deploy/portable/pack_assets/start.bat:start.bat
deploy/portable/pack_assets/stop.bat:stop.bat
deploy/portable/pack_assets/sync_from_git.sh:sync_from_git.sh
deploy/portable/pack_assets/sync_from_git.bat:sync_from_git.bat
deploy/portable/pack_assets/restore_backup.sh:restore_backup.sh
deploy/portable/pack_assets/restore_backup.bat:restore_backup.bat
deploy/portable/pack_assets/install_gpu.sh:install_gpu.sh
deploy/portable/pack_assets/install_gpu.bat:install_gpu.bat
deploy/portable/pack_assets/pg_setup.py:pg_setup.py
deploy/portable/pack_assets/pack.env.example:pack.env.example
"

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
# 2) pack_assets → 包根同名文件(start/stop/sync/restore/install_gpu/pg_setup/pack.env.example)
for pair in $ASSET_MAP; do
    src="${pair%%:*}"; dst="${pair##*:}"
    [ -f "$src" ] || { echo "[!] 缺启动脚本: $src"; continue; }
    if git diff --quiet "$BASE"..HEAD -- "$src" 2>/dev/null; then continue; fi
    mkdir -p "$STAGE"
    cp "$src" "$STAGE/$dst"
    changed=1
done
# 2b) docker/training 整目录 → 补丁内 docker/training/(保相对布局: train.py 顶层
#     import model_trainers/diagnostics/data 同级包; 代码包 data 与包根数据目录
#     data/ 同名, 不能拉平到补丁根)。apply_update 需先删包内旧 docker/training
#     与旧拉平残留(根级 train.py/model_trainers)。
if ! git diff --quiet "$BASE"..HEAD -- docker/training 2>/dev/null; then
    rm -rf "$STAGE/docker/training"
    mkdir -p "$STAGE/docker"
    cp -a docker/training "$STAGE/docker/training"
    changed=1
fi

# 2c) 增量升级 SQL（data/upgrade_*.sql → 补丁内 data/，解压到包根后由
#     main_oss._upgrade_sql_files() 启动时自动执行幂等迁移）。整组打包而非只打
#     改动文件：修复「SQL 从未随包分发」之前构建的旧包一个都没带，靠每次补丁
#     全量补齐才会收敛；4 个文件共 ~35KB，代价可忽略。
if ls data/upgrade_*.sql >/dev/null 2>&1; then
    mkdir -p "$STAGE/data"
    cp -f data/upgrade_*.sql "$STAGE/data/"
    echo "[i] 增量升级 SQL 随补丁分发: $(ls data/upgrade_*.sql | wc -l) 个"
fi

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
if exist "%ROOT%\train.py" echo [update] NOTE: old flattened layout detected (root train.py). Delete it and package-root model_trainers, then re-extract this zip's docker\ folder if missing - or use sync_from_git.bat on next runs.
if exist "%ROOT%\model_trainers" echo [update] NOTE: old flattened layout detected (root model_trainers). Delete it, then re-extract this zip's docker\ folder if missing - or use sync_from_git.bat on next runs.
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
