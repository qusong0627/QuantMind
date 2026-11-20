"""模型「重新训练」配置载荷：config.yaml 优先、metadata 兜底、密钥不外泄。"""

from __future__ import annotations

from pathlib import Path

from backend.shared.training.retrain_config import (
    build_retrain_payload,
    load_training_config,
)

_CONFIG = """\
callback:
  url: http://quantmind:8000/api/v1/models/training-runs/x/complete
  secret: topsecret-token
context:
  market: CN
  benchmark: SH000300
model:
  type: lightgbm
  params:
    learning_rate: 0.03
    num_leaves: 31
split:
  train: ['2016-01-04', '2025-10-17']
  valid: ['2025-11-03', '2026-04-16']
  test: ['2026-05-06', '2026-09-30']
data:
  features: [f1, f2]
  factor_source: l1_factors
"""


def _write_config(tmp_path: Path, text: str = _CONFIG) -> Path:
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    return tmp_path


def test_load_training_config_strips_callback_secret(tmp_path):
    config, warnings = load_training_config(str(_write_config(tmp_path)))

    assert warnings == []
    assert config is not None
    assert config["model"]["type"] == "lightgbm"
    # 回调密钥绝不回传前端
    assert "callback" not in config


def test_build_retrain_payload_prefers_config_yaml(tmp_path):
    payload = build_retrain_payload(
        model_id="m1",
        metadata={
            "display_name": "My Model",
            "model_type": "lightgbm",
            "market": "CN",
            "features": ["f1"],
            "requested_features": ["f1", "f2"],
        },
        storage_path=str(_write_config(tmp_path)),
    )

    assert payload["display_name"] == "My Model"
    assert payload["market"] == "CN"
    assert payload["config"]["data"]["features"] == ["f1", "f2"]
    assert payload["config"]["split"]["train"] == ["2016-01-04", "2025-10-17"]
    assert payload["metadata"]["model_type"] == "lightgbm"
    # 有效特征与候选集都要回传：前端按有效特征带入（100 选 82 的场景）
    assert payload["metadata"]["features"] == ["f1"]
    assert payload["metadata"]["requested_features"] == ["f1", "f2"]
    assert payload["warnings"] == []


def test_build_retrain_payload_falls_back_to_metadata(tmp_path):
    payload = build_retrain_payload(
        model_id="m2",
        metadata={
            "display_name": "Fallback",
            "market": "HK",
            "requested_features": ["a", "b"],
            "train_start": "2020-01-01",
        },
        storage_path=str(tmp_path),
    )

    assert payload["config"] is None
    assert payload["market"] == "HK"
    assert payload["metadata"]["requested_features"] == ["a", "b"]
    assert payload["metadata"]["train_start"] == "2020-01-01"
    assert payload["warnings"]


def test_build_retrain_payload_handles_broken_yaml(tmp_path):
    dirp = _write_config(tmp_path, "model: [unclosed\n")

    payload = build_retrain_payload(
        model_id="m3", metadata={"market": "CN"}, storage_path=str(dirp)
    )

    assert payload["config"] is None
    assert payload["warnings"]
