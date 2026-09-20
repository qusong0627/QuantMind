#!/usr/bin/env bash
# 把训练产物目录打成可交付的模型包（tar.gz）并落到 NFS。
#
# 包内结构与 register_models_from_dir.py 的扫描口径一致：
#   models/users/<tenant>/<user>/<model_dir>/{metadata.json, model.cbm, ...}
# 对方解压后跑：
#   mkdir -p models && tar xzf <包名>.tar.gz -C models
#   python scripts/register_models_from_dir.py --user-id <其用户ID>
#
# 用法：bash scripts/pack_model_for_handoff.sh <model_dir> <输出目录> [包名前缀]
set -euo pipefail

MODEL_DIR="${1:?用法: $0 <model_dir> <输出目录> [包名前缀]}"
OUT_DIR="${2:?用法: $0 <model_dir> <输出目录> [包名前缀]}"
PREFIX="${3:-}"

MODEL_DIR="${MODEL_DIR%/}"
[ -d "$MODEL_DIR" ] || { echo "模型目录不存在: $MODEL_DIR" >&2; exit 1; }

MODEL_BASENAME="$(basename "$MODEL_DIR")"
[ -f "$MODEL_DIR/metadata.json" ] || { echo "缺 metadata.json，注册脚本会跳过: $MODEL_DIR" >&2; exit 1; }

# 模型文件按 register_models_from_dir.py 的优先级挑，挑不到直接失败（比交付空壳强）
MODEL_FILE=""
for f in model.lgb model.xgb model.cbm model.pkl model.pth model.onnx model.pt; do
  if [ -f "$MODEL_DIR/$f" ]; then MODEL_FILE="$f"; break; fi
done
[ -n "$MODEL_FILE" ] || { echo "目录内找不到任何模型文件: $MODEL_DIR" >&2; exit 1; }

# tenant/user 从路径里取（.../models/users/<tenant>/<user>/<model_dir>）
USER_ID="$(basename "$(dirname "$MODEL_DIR")")"
TENANT_ID="$(basename "$(dirname "$(dirname "$MODEL_DIR")")")"

PKG_NAME="${PREFIX}${MODEL_BASENAME}.tar.gz"
mkdir -p "$OUT_DIR"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

REL="users/$TENANT_ID/$USER_ID/$MODEL_BASENAME"
mkdir -p "$STAGE/models/$(dirname "$REL")"
cp -r "$MODEL_DIR" "$STAGE/models/$REL"

# 交付说明：对方拿到包不用猜怎么用
cat > "$STAGE/README_注册说明.md" <<EOF
# 模型交付包：$MODEL_BASENAME

## 内容
- \`models/$REL/\` —— 模型目录（$MODEL_FILE + metadata.json + inference.py + config.yaml + pred.parquet + result.json）

## 注册到你的 QuantMind
\`\`\`bash
# 1) 解压到 models 根目录
mkdir -p models && tar xzf $PKG_NAME -C models --strip-components=0

# 2) 注册到指定用户名下（user_id 是 users 表主键）
python scripts/register_models_from_dir.py --user-id <你的用户ID> --dry-run   # 预览
python scripts/register_models_from_dir.py --user-id <你的用户ID>            # 执行
\`\`\`

注册脚本按 metadata.json 里的 metrics 做软门禁：\`test_rank_icir >= 0.05\` 且
\`test_rank_ic > 0\` 记为 \`ready\`，否则记 \`candidate\`（仍可推理，只是不带 ready 标记）。

## 模型信息
\`\`\`
$(python3 - "$MODEL_DIR/metadata.json" "$MODEL_DIR/config.yaml" "$MODEL_BASENAME" <<'PY'
import json, sys
from pathlib import Path

meta = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
model_dir_name = sys.argv[3]

# valid 窗口不在 metadata 里，从 config.yaml 的 split 取（同一份任务配置）
split = {}
cfg_path = Path(sys.argv[2])
if cfg_path.is_file():
    try:
        import yaml
        split = (yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}).get("split") or {}
    except Exception:  # noqa: BLE001 - 拿不到就不印，不阻断打包
        split = {}

ctx = meta.get("context") or {}
win = lambda p: " ~ ".join(str(x) for x in split[p]) if isinstance(split.get(p), list) else "N/A"
rows = [
    ("model_id", meta.get("model_id") or model_dir_name),
    ("display_name", meta.get("display_name") or meta.get("job_name") or model_dir_name),
    ("market", meta.get("market") or ctx.get("market")),
    ("model_type", meta.get("model_type") or meta.get("framework")),
    ("target_horizon_days", meta.get("target_horizon_days")),
    ("target_mode", meta.get("target_mode")),
    ("n_features", len(meta.get("features") or [])),
    ("train", win("train")),
    ("valid", win("valid")),
    ("test", win("test")),
    ("data_source", meta.get("data_source")),
]
for k, v in rows:
    print(f"{k:22s} = {v}")

# metadata.metrics 是平铺键（train_ic / val_rank_icir / …），不是按 split 嵌套
met = meta.get("metrics") or {}
print()
for label, prefix in (("train", "train"), ("valid", "val"), ("test", "test")):
    bits = [
        f"{short}={met[key]:.4f}"
        for key, short in ((f"{prefix}_ic", "IC"), (f"{prefix}_rank_ic", "RankIC"), (f"{prefix}_rank_icir", "ICIR"))
        if isinstance(met.get(key), (int, float))
    ]
    if bits:
        print(f"metrics.{label:5s} = " + ", ".join(bits))
PY
)
\`\`\`

（\`valid\` 是早停/调参集，\`test\` 才是样本外；上面 test 一段即未参与训练与早停的
真实表现，对照 \`eval_report.json\` 的 \`by_split\` 可复核。）
EOF

tar czf "$OUT_DIR/$PKG_NAME" -C "$STAGE" .

echo "已打包: $OUT_DIR/$PKG_NAME"
echo "大小  : $(du -h "$OUT_DIR/$PKG_NAME" | cut -f1)"
echo "模型文件: $MODEL_FILE"
echo "包内模型目录: models/$REL"
