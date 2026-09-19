"""模型产物资产的健全性检查（单一实现）。

用途：在**选中模型之前**回答「这个模型的产物还在不在」。注册表（`qm_user_models`）
里的行可能指向已被清理、迁移或从未落盘的目录——这类模型一旦被选为推理目标，
得到的是一次语焉不详的失败。资产检查把它们提前暴露成可筛可列的字段。

边界（刻意不做的事）：
- **不校验因子 schema**。那要全量扫 parquet（秒级/数据集），且属于推理前检
  （precheck 的 `market_data_ready`）的职责。本模块只做存在性检查
  （每个模型几次 stat），成本可忽略，可以放心挂在列表接口上。
- **不判定「能不能跑」**，只判定「够不够得着」。schema 漂移、版本不兼容这类
  语义问题留给 precheck 用具体原因拒绝。

两个消费方：
- `backend/services/api/routers/research_service.py` → `/research/models`
- `backend/services/api/routers/model_training.py` → `GET /models`（治理面板数据源）
"""

from __future__ import annotations

from pathlib import Path

# 权重产物后缀。**按后缀而非固定文件名匹配**——实测同一批训练产出的权重名各异
# （`model.lgb`、`model_lgb.lgb`、`pytorch_model.bin`…），写死文件名会把一批
# 健康模型误报成「缺权重」。
MODEL_WEIGHT_SUFFIXES: frozenset[str] = frozenset(
    {".lgb", ".xgb", ".cbm", ".pkl", ".pt", ".pth", ".onnx", ".ckpt", ".joblib", ".bin"}
)

# 预测转储不是权重：`pred.pkl` 的后缀与权重集合重合，必须按文件名前缀排除。
# （平台只认 `pred.parquet` 作为读分来源，`pred.pkl` 仅训练期遗留。）
_PRED_STEM_PREFIX = "pred"

# 推理链路真正依赖的固定文件：脚本是执行入口，pred.parquet 是读分与共识的数据面
MODEL_REQUIRED_FILES: tuple[str, ...] = ("inference.py", "pred.parquet")


def _has_weight_file(model_dir: Path) -> bool:
    try:
        entries = list(model_dir.iterdir())
    except OSError:
        return False
    for f in entries:
        if not f.is_file():
            continue
        if f.suffix.lower() not in MODEL_WEIGHT_SUFFIXES:
            continue
        if f.stem.lower().startswith(_PRED_STEM_PREFIX):
            continue
        return True
    return False


def model_asset_gaps(model_dir: str | None) -> list[str]:
    """模型目录缺哪些推理必需产物；空列表 = 资产健全。"""
    if not model_dir:
        return ["注册表无 storage_path"]
    d = Path(model_dir)
    if not d.is_dir():
        return ["模型目录不存在"]
    gaps = [name for name in MODEL_REQUIRED_FILES if not (d / name).is_file()]
    if not _has_weight_file(d):
        gaps.append("模型权重")
    return gaps
