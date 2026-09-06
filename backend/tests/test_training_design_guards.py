"""Regression guards for training-time leakage and target semantics.

These assertions intentionally inspect the container entrypoint as source: importing
``train.py`` requires optional GPU/runtime dependencies that are unavailable in the
unit-test image.
"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TRAIN_SCRIPT = ROOT / "docker" / "training" / "train.py"
TRAINING_UTILS = ROOT / "backend" / "services" / "api" / "routers" / "admin" / "admin_training_utils.py"


def test_classification_keeps_binary_labels_and_uses_classification_objectives() -> None:
    source = TRAIN_SCRIPT.read_text(encoding="utf-8")

    assert 'if _target_mode != "classification":\n        df["label"] = df.groupby("trade_date")["label"].rank' in source
    assert 'params["objective"] = "binary"' in source
    assert 'params["objective"] = "binary:logistic"' in source
    assert 'params["loss_function"] = "Logloss"' in source


def test_factor_selection_uses_only_the_training_segment() -> None:
    source = TRAIN_SCRIPT.read_text(encoding="utf-8")

    assert "selection_train_df, _, _ = _split_data(df, cfg)" in source
    assert "selection_train_df, valid_features, label_col=\"label\"" in source


def test_labels_and_split_gap_share_t_plus_one_execution_convention() -> None:
    source = TRAIN_SCRIPT.read_text(encoding="utf-8")
    utils_source = TRAINING_UTILS.read_text(encoding="utf-8")
    # P2 拆包：口径常量与用法已迁入 data/ 包，main 的 metadata 落点仍在 train.py
    splits_source = (ROOT / "docker" / "training" / "data" / "splits.py").read_text(encoding="utf-8")
    loading_source = (ROOT / "docker" / "training" / "data" / "loading.py").read_text(encoding="utf-8")

    assert "_EXECUTION_LAG_DAYS = 1" in splits_source
    assert "shift(-(_horizon + _EXECUTION_LAG_DAYS))" in loading_source
    assert "_embargo_days = _horizon + _EXECUTION_LAG_DAYS" in splits_source
    assert '"execution_lag_days": _EXECUTION_LAG_DAYS' in source
    assert 'gap_days = int(normalized.get("target_horizon_days") or 1) + 1' in utils_source
