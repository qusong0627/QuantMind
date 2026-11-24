"""按模型独立入口的请求契约（主入口共享层 + 模型独立配置）。

设计：
- 共享层（提交/编排/容器/回调/注册）保持单核：submit_training_job。
- 每个模型一个请求模型：仅含共享字段 + 自家 params 容器，extra="forbid" ——
  给 lightgbm 传 dl_params 在入口即 422，而不是进训练后被静默丢弃。
- 深度校验（范围/枚举/422 文案）仍在 TrainingRequest 单源，本层只做键隔离；
  字段一律宽松类型，避免两层报不同文案。
- model_types（多模型）不在独立入口开放，多模型/ensemble 走共享 /run-training。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, create_model

from backend.shared.training.capabilities import (
    MODEL_FRAMEWORK,
    MODEL_PARAMS_FIELD,
)

# 共享字段：各模型通用（键存在性由本层保证，值校验由 TrainingRequest 保证）。
_SHARED_FIELDS: dict[str, Any] = {
    "job_name": (Any, None),
    "display_name": (Any, None),
    "train_start": (Any, None),
    "train_end": (Any, None),
    "val_ratio": (Any, None),
    "num_boost_round": (Any, None),
    "early_stopping_rounds": (Any, None),
    "features": (Any, None),
    "feature_categories": (Any, None),
    "target_horizon_days": (Any, None),
    "target_mode": (Any, None),
    "target_ref": (Any, None),
    "label_overrides": (Any, None),
    "expected_target_hash": (Any, None),
    "execution_price": (Any, None),
    "label_transform": (Any, None),
    "label_top_pct": (Any, None),
    "label_drop_extreme_pct": (Any, None),
    "label_formula": (Any, None),
    "effective_trade_date": (Any, None),
    "training_window": (Any, None),
    "context": (Any, None),
    "explain": (Any, None),
    "ensemble": (Any, None),
    "prediction_mode": (Any, None),
    "n_folds": (Any, None),
    "meta_alpha": (Any, None),
    "optuna": (Any, None),
    "pause_others": (Any, None),
    "preprocessing": (Any, None),
    "sample_weight": (Any, None),
    "factor_selection": (Any, None),
    "auto_feature_filter": (Any, None),
    "max_time_minutes": (Any, None),
    "wfa": (Any, None),
    "rolling_meta": (Any, None),
    "factor_source": (Any, None),
    "factor_catalog_version": (Any, None),
    "factor_catalog_versions": (Any, None),
    "factor_field_sources": (Any, None),
    "factor_schema_hash": (Any, None),
    "factor_catalog_published_at": (Any, None),
    "factor_coverage": (Any, None),
    "valid_start": (Any, None),
    "valid_end": (Any, None),
    "test_start": (Any, None),
    "test_end": (Any, None),
    "required_artifacts": (Any, None),
    "deploy_to_production": (Any, None),
    "generated_at": (Any, None),
    "node_id": (Any, None),
    "data_source_mode": (Any, None),
}

# MODEL_PARAMS_FIELD / MODEL_FRAMEWORK 的单一事实源在 capabilities.py；
# 这里只 re-export，保持既有 import 路径兼容（per_model.MODEL_FRAMEWORK 等）。



def _make_request_model(model_name: str, params_field: str) -> type[BaseModel]:
    """生成单个模型的请求模型：共享字段 + model_type 常量 + 自家 params。"""
    fields: dict[str, Any] = dict(_SHARED_FIELDS)
    fields["model_type"] = (Literal[model_name], model_name)  # type: ignore[valid-type]
    fields[params_field] = (Any, None)
    return create_model(
        f"{model_name.title().replace('_', '')}TrainingRequest",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


REQUEST_MODELS: dict[str, type[BaseModel]] = {
    name: _make_request_model(name, field) for name, field in MODEL_PARAMS_FIELD.items()
}
