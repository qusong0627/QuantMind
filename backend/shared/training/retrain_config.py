"""模型 → 「重新训练」配置载荷（模型管理页一键带入训练页）。

口径：

* **config.yaml 是首选来源**——它保存了训练当时的超参数、时间切分、标签口径与
  因子库版本；metadata.json / 数据库 metadata_json 只在缺 config 时兜底。
* **绝不外泄 callback.secret**：训练配置里带有训练完成回调的密钥，返回前端前
  必须剔除；这里按白名单以外的危险键做显式删除，而不是整份透传。
* 本模块只负责读盘与整形，不碰数据库/HTTP，便于单测。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

#: config.yaml 里不得回传给前端的键（回调 URL + 回调密钥）。
_SENSITIVE_CONFIG_KEYS = ("callback",)

#: metadata 里对重训有用的字段（避免把 SHAP/漂移等大块元数据整包回传）。
_METADATA_KEYS = (
    "model_type",
    "market",
    "target_horizon_days",
    # ``features`` = 训练实际使用的有效特征（train.py 的 valid_features），
    # ``requested_features`` = 提交的候选集；两者可能不同（目录缺失/筛选）。
    # 重训必须按有效特征带回，否则会把当年没进模型的特征也勾上。
    "features",
    "requested_features",
    "feature_columns",
    "train_start",
    "train_end",
    "val_start",
    "val_end",
    "test_start",
    "test_end",
    "context",
    "preprocessing",
    "objective",
    "target_mode",
    "target_id",
    "target_version",
    "target_hash",
    "target_base_hash",
    "target_status",
    "target_spec",
    "target_overrides",
    "label_contract_version",
    "label_formula",
    "execution_lag_days",
    "factor_source",
    "display_name",
)


def load_training_config(storage_path: str | None) -> tuple[dict[str, Any] | None, list[str]]:
    """读取模型目录下的 config.yaml；缺失/损坏时返回 (None, 提示)。"""
    if not storage_path:
        return None, ["模型缺少 storage_path，已按 metadata 回退"]

    config_path = Path(storage_path) / "config.yaml"
    if not config_path.is_file():
        return None, ["模型目录缺少 config.yaml，超参数按模型类型默认值带入"]

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 解析失败降级到 metadata
        logger.warning("重训配置解析失败 %s: %s", config_path, exc)
        return None, [f"config.yaml 解析失败：{exc}"]

    if not isinstance(raw, dict):
        return None, ["config.yaml 内容不是对象，已按 metadata 回退"]

    for key in _SENSITIVE_CONFIG_KEYS:
        raw.pop(key, None)
    return raw, []


def build_retrain_payload(
    *,
    model_id: str,
    metadata: dict[str, Any],
    storage_path: str | None,
) -> dict[str, Any]:
    """整形 ``GET /models/{id}/retrain-config`` 的响应体。"""
    config, warnings = load_training_config(storage_path)

    meta_subset = {
        key: metadata[key]
        for key in _METADATA_KEYS
        if metadata.get(key) is not None
    }

    context = config.get("context") if isinstance(config, dict) else None
    market = (
        (context or {}).get("market")
        if isinstance(context, dict)
        else None
    ) or metadata.get("market") or "CN"

    display_name = str(
        metadata.get("display_name")
        or (config or {}).get("job_name")
        or model_id
    )

    return {
        "model_id": model_id,
        "display_name": display_name,
        "market": str(market),
        "config": config,
        "metadata": meta_subset,
        "warnings": warnings,
    }
