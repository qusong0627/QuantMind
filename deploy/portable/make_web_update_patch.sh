#!/usr/bin/env bash
# ============================================================
# QuantMind 便携包 前端(web)增量包生成器
# 用法: bash deploy/portable/make_web_update_patch.sh
#   前置: 打包机已 npm run build:react（electron/dist-react 为最新产物）
# 产出: deploy/portable/dist/QuantMind-WebUpdate-<日期>.zip
#   含 apply_update_web.bat + newweb/（完整前端构建产物）
# 分发: 老用户把 zip 解压到便携包根目录（不覆盖任何已有文件），
#   双击 apply_update_web.bat 自动清旧 assets → 拷贝 newweb → 完成。
# 说明: make_update_patch.sh 的 CODE_PREFIXES 不含 web/（前端产物不入
#   update 补丁），本脚本补足「老用户单独升级前端 UI」的场景；
#   Linux 老用户可跳过 bat：停服后手动执行
#     rm -rf web/assets && cp -r newweb/. web/ && rm -rf newweb
# ============================================================
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$REPO_ROOT/deploy/portable"
DIST="$HERE/dist"
STAGE="$HERE/build/web-update"
SRC="$REPO_ROOT/electron/dist-react"

[ -f "$SRC/index.html" ] || { echo "[!] 缺少前端构建产物 electron/dist-react/index.html，请先 npm run build:react"; exit 1; }

TS=$(date +%Y%m%d-%H%M)
OUT="$DIST/QuantMind-WebUpdate-$TS.zip"

rm -rf "$STAGE"
mkdir -p "$STAGE/newweb"

echo "[build] $TS 复制前端产物 electron/dist-react -> newweb/ ..."
cp -a "$SRC/." "$STAGE/newweb/"

python3 - "$STAGE/apply_update_web.bat" <<'PYEOF'
import sys

bat = r'''@echo off
rem QuantMind frontend-only update helper.
rem Replace the packaged web/ contents with the new build shipped in newweb/.
cd /d "%~dp0"
if not exist "web" (
  echo [!] 'web' directory not found. Extract this zip to the portable package root first.
  pause
  exit /b 1
)
if not exist "newweb\index.html" (
  echo [!] 'newweb\index.html' not found. Package is incomplete.
  pause
  exit /b 1
)
echo [update] Removing old web\assets ...
if exist "web\assets" rd /s /q "web\assets"
echo [update] Copying new frontend into web\ ...
xcopy /e /y /q "newweb\." "web\" >nul
echo [update] Cleaning updater payload ...
rd /s /q "newweb"
echo [update] Done. Refresh the browser page or restart with start.bat.
pause
'''
open(sys.argv[1], 'w', encoding='ascii', newline='').write(bat.replace('\n', '\r\n'))
print('apply_update_web.bat written')
PYEOF

echo "[build] 压缩 web 更新包 ..."
python3 - "$OUT" "$STAGE" <<'PYEOF'
import os
import sys
import zipfile

out, src = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
    for root, _dirs, files in os.walk(src):
        for f in files:
            p = os.path.join(root, f)
            z.write(p, os.path.relpath(p, src))
print(f'zip written: {out}')
PYEOF

rm -rf "$STAGE"
echo "前端更新包: $OUT"
echo "分发: 老用户解压到便携包根目录(覆盖合并)后双击 apply_update_web.bat; Linux 老用户按脚本头注释手动替换"
