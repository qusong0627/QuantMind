#!/usr/bin/env bash
# dsh 升级用：把仓库 docker/dsh/profile 的清单（package.json + pnpm-lock.yaml）植入卷。
# 用法（以实盘卷为例；$REPO = 仓库根）：
#   docker run --rm -v quantmind_dsh-data:/root/.dsh \
#     -v $REPO/docker/dsh/profile:/app/profile-src:ro \
#     -v $REPO/scripts/dsh_prime_profile_volume.sh:/tmp/prime.sh:ro \
#     --entrypoint bash quantmind-dsh:latest /tmp/prime.sh
# 排练/拷贝卷另加 -e DISABLE_INTEGRATIONS=1（移走微信集成目录，防抢线上 bot 游标）。
# 脚本内已设 npm 源为 npmmirror（官方源在部分网络下断流）；.manifest.sha 对齐后
# entrypoint 启动时跳过自装，做到离线可复现。
#   ① 备份 memory 插件 mdcg 数据（数据在包目录内，重装可能清掉）
#   ② 禁用微信集成目录（卷拷贝含真实 bot 绑定，防抢线上 getUpdates 游标）
#   ③ 冻结安装（npmmirror 源）→ ④ 恢复检查 → ⑤ 写 .manifest.sha（与 entrypoint 同公式）
set -euo pipefail
export DSH_HOME=/root/.dsh
PROFILE_DIR="$DSH_HOME/profiles/web"
SRC="${SRC:-/app/profile-src}"
MDCG="$PROFILE_DIR/node_modules/@furongjun1999/dsh-memory"

echo "== 1) mdcg 数据备份 =="
mkdir -p "$DSH_HOME/_ops"
if [ -d "$MDCG/data" ]; then
  _bak="$DSH_HOME/_ops/mdcg-backup-$(date +%Y%m%d-%H%M%S).tgz"
  tar -czf "$_bak" -C "$MDCG" data
  echo "已备份: $_bak ($(du -h "$_bak" | cut -f1))"
else
  echo "（无 mdcg/data，跳过）"
fi

echo "== 2) 微信集成处理（DISABLE_INTEGRATIONS=1 才禁用；实盘卷必须保留） =="
if [ "${DISABLE_INTEGRATIONS:-0}" = "1" ] && [ -d "$DSH_HOME/integrations" ]; then
  _off="$DSH_HOME/_ops/integrations.disabled-by-priming"
  rm -rf "$_off"
  mv "$DSH_HOME/integrations" "$_off"
  echo "integrations → _ops/integrations.disabled-by-priming"
else
  echo "（保留 integrations 原样）"
fi

echo "== 3) 植入新清单 =="
mkdir -p "$PROFILE_DIR"
cp "$SRC/package.json" "$PROFILE_DIR/package.json"
cp "$SRC/pnpm-lock.yaml" "$PROFILE_DIR/pnpm-lock.yaml"

echo "== 3.5) 确保 minimumReleaseAgeExclude 覆盖本次钉的版本 =="
WS="$PROFILE_DIR/pnpm-workspace.yaml"
python3 - "$WS" <<'PY'
import sys
ws = sys.argv[1]
want = ["dsh-free-search@0.8.1", "dsh-mcp-connector@0.2.68", "dshmarket@1.66.9",
        "dsh-univer-office@0.3.6", "@furongjun1999/dsh-memory@0.4.8", "@xmanrui/dsh-im@4.21.2"]
text = open(ws, encoding="utf-8").read()
missing = [w for w in want if w not in text]
if not missing:
    print("（无需新增）"); sys.exit(0)
lines = text.splitlines()
for i, line in enumerate(lines):
    if line.strip() == "minimumReleaseAgeExclude:":
        for w in missing:
            lines.insert(i + 1, "  - '" + w + "'")
        break
else:
    lines.append("minimumReleaseAgeExclude:")
    lines.extend("  - '" + w + "'" for w in missing)
open(ws, "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("已新增:", ", ".join(missing))
PY

echo "== 4) 冻结安装 =="
cd "$DSH_HOME"
npm_config_registry=https://registry.npmmirror.com dsh plugin --profile web install --frozen-lockfile

echo "== 5) mdcg 恢复检查 =="
if [ -d "$MDCG/data" ]; then
  echo "mdcg/data 在安装后存活"
else
  _bak=$(ls -t "$DSH_HOME"/_ops/mdcg-backup-*.tgz 2>/dev/null | head -1 || true)
  if [ -n "$_bak" ]; then
    tar -xzf "$_bak" -C "$MDCG"
    echo "mdcg/data 被安装清掉，已从 $_bak 恢复"
  else
    echo "警告：mdcg/data 不存在且无备份"
  fi
fi

echo "== 6) 写 .manifest.sha（entrypoint 同公式） =="
sha256sum "$PROFILE_DIR/package.json" | cut -d' ' -f1 > "$PROFILE_DIR/.manifest.sha"
echo "sha=$(cat "$PROFILE_DIR/.manifest.sha")"
echo "repo sha=$(sha256sum "$SRC/package.json" | cut -d' ' -f1)"

echo "== 7) 验证六插件版本 =="
for p in @furongjun1999/dsh-memory @xmanrui/dsh-im dsh-free-search dsh-mcp-connector dsh-univer-office dshmarket; do
  node -p "'$p => ' + JSON.parse(require('fs').readFileSync('$PROFILE_DIR/node_modules/$p/package.json','utf8')).version" 2>/dev/null || echo "$p => MISSING"
done

echo "== 完成 =="
