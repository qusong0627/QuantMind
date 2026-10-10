"""滚动重训参数摘要/构造单测（纯字典变换，无 DB/网络）。"""

import json

import yaml

from backend.services.api.routers.model_rolling_retrain import (
    _build_fallback_payload_from_dir,
    _build_rolling_payload,
    _summarize_params,
)

PAYLOAD = {
    "display_name": "L1L2_ICIR优选",
    "job_name": "job-001",
    "model_type": "lightgbm",
    "train_start": "2016-01-04",
    "train_end": "2024-12-31",
    "valid_start": "2025-01-02",
    "valid_end": "2025-12-31",
    "test_start": "2026-01-05",
    "test_end": "2026-08-31",
    "features": ["F1", "F2", "F3"],
    "num_boost_round": 1000,
    "lgb_params": {"learning_rate": 0.05, "num_leaves": 31},
    "target_horizon_days": 5,
    "target_mode": "return",
    "node_id": "local",
    "factor_source": "l1_factors",
    "factor_catalog_version": "v-old",
    "context": {"market": "CN", "benchmark": "SH000300"},
}

NEW_WINDOW = {
    "train_start": "2016-02-04",
    "train_end": "2025-01-31",
    "valid_start": "2025-02-03",
    "valid_end": "2026-01-30",
    "test_start": "2026-02-05",
    "test_end": "2026-09-30",
}


def test_summarize_params_sections():
    summary = _summarize_params(PAYLOAD, PAYLOAD, "v-old")
    assert summary["window"]["test_end"] == "2026-08-31"
    assert summary["hyperparams"]["learning_rate"] == 0.05
    assert summary["hyperparams"]["num_boost_round"] == 1000
    assert summary["target"]["target_horizon_days"] == 5
    assert summary["feature_count"] == 3
    assert summary["features"] == ["F1", "F2", "F3"]
    assert summary["factor"] == {"source": "l1_factors", "catalog_version": "v-old"}
    assert summary["context"]["market"] == "CN"


def test_build_payload_only_changes_window_names_pin():
    ctx = {
        "payload": dict(PAYLOAD),
        "model": {"model_id": "mdl_x"},
        "current_version": "v-new",
    }
    new_payload = _build_rolling_payload(ctx, NEW_WINDOW)
    # 变的：六边界 + 名称 + pin
    for key, value in NEW_WINDOW.items():
        assert new_payload[key] == value
    assert new_payload["factor_catalog_version"] == "v-new"
    assert "2026-09" in new_payload["display_name"]
    # 不变的：其余全部原样
    for key in ("model_type", "features", "num_boost_round", "lgb_params",
                "target_horizon_days", "target_mode", "node_id",
                "factor_source", "context"):
        assert new_payload[key] == PAYLOAD[key]
    # 易变派生字段被剔除（由提交链路重算）
    for key in ("data_window", "generated_at", "system_notices",
                "factor_field_sources", "factor_schema_hash"):
        assert key not in new_payload
    # 原 payload 未被污染
    assert PAYLOAD["test_end"] == "2026-08-31"


def test_fallback_payload_from_model_dir(tmp_path):
    # 集市模型目录形态：metadata.json + config.yaml
    metadata = {
        "display_name": "Hub 模型",
        "job_name": "hub-job",
        "model_type": "lightgbm",
        "train_start": "2018-01-02",
        "train_end": "2023-06-18",
        "val_start": "2023-06-24",
        "val_end": "2025-01-21",
        "test_start": "2025-01-27",
        "test_end": "2026-08-28",
        "features": ["F1", "F2"],
        "target_horizon_days": 5,
        "target_mode": "return",
        "label_formula": "label = 1",
        "factor_source": "l1_l2_factors",
        "context": {"market": "CN", "benchmark": "SH000300"},
    }
    config = {
        "model": {
            "type": "lightgbm",
            "params": {"learning_rate": 0.01, "num_leaves": 15},
            "num_boost_round": 3000,
            "early_stopping_rounds": 100,
            "ensemble": "none",
            "prediction_mode": "point",
        },
        "label": {"target_horizon_days": 5, "target_mode": "return"},
        "split": {
            "train": ["2018-01-02", "2023-06-18"],
            "valid": ["2023-06-24", "2025-01-21"],
            "test": ["2025-01-27", "2026-08-28"],
        },
        "max_time_minutes": 720,
    }
    (tmp_path / "metadata.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8"
    )

    rebuilt = _build_fallback_payload_from_dir(str(tmp_path))
    assert rebuilt is not None
    assert rebuilt["train_start"] == "2018-01-02"
    assert rebuilt["test_end"] == "2026-08-28"
    assert rebuilt["features"] == ["F1", "F2"]
    assert rebuilt["lgb_params"] == {"learning_rate": 0.01, "num_leaves": 15}
    assert rebuilt["num_boost_round"] == 3000
    assert rebuilt["auto_feature_filter"] is False
    assert rebuilt["factor_source"] == "l1_l2_factors"
    assert rebuilt["context"]["market"] == "CN"

    # 缺文件时返回 None（调用方转 404）
    assert _build_fallback_payload_from_dir(str(tmp_path / "missing")) is None
