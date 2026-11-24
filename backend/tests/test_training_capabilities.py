"""训练目标能力矩阵（W0）单元测试。

护栏目标：
1. 13 模型在 capabilities / request / per_model 三处集合完全一致；
2. 逐模型能力与机构口径表一致（任何一行被误改都会红）；
3. 非法组合在纯函数层就被拒绝，错误码稳定。
"""

from __future__ import annotations

import json

import pytest

from backend.shared.training import capabilities as cap
from backend.shared.training.per_model import (
    MODEL_FRAMEWORK,
    MODEL_PARAMS_FIELD,
    REQUEST_MODELS,
)
from backend.shared.training.request import ALLOWED_MODEL_TYPES

EXPECTED_MODELS = {
    "lightgbm",
    "xgboost",
    "catboost",
    "linear",
    "random_forest",
    "mlp",
    "gru",
    "lstm",
    "alstm",
    "transformer",
    "tabnet",
    "tcn",
    "nativetft",
}


def test_thirteen_models_are_single_sourced() -> None:
    assert cap.ALL_MODEL_TYPES == EXPECTED_MODELS
    assert set(cap.MODEL_CAPABILITIES) == EXPECTED_MODELS
    assert set(cap.MODEL_FAMILIES) == EXPECTED_MODELS
    assert set(cap.MODEL_PARAMS_FIELD) == EXPECTED_MODELS
    assert set(cap.MODEL_FRAMEWORK) == EXPECTED_MODELS
    assert set(ALLOWED_MODEL_TYPES) == EXPECTED_MODELS
    assert set(REQUEST_MODELS) == EXPECTED_MODELS
    assert set(MODEL_PARAMS_FIELD) == EXPECTED_MODELS
    assert set(MODEL_FRAMEWORK) == EXPECTED_MODELS


def test_capability_matrix_contract() -> None:
    matrix = cap.capability_matrix()
    assert matrix["matrix_version"] == cap.CAPABILITY_MATRIX_VERSION
    assert len(matrix["models"]) == 13
    assert {row["model_type"] for row in matrix["models"]} == EXPECTED_MODELS
    # 必须能 JSON 序列化（前端接口直接返回）
    json.dumps(matrix, ensure_ascii=False)


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        (
            "classification",
            {"lightgbm", "xgboost", "catboost", "linear", "random_forest", "mlp"},
        ),
        ("quantile", {"lightgbm"}),
        ("rank_loss", {"lightgbm", "xgboost", "catboost"}),
        (
            "sample_weight",
            {"lightgbm", "xgboost", "catboost", "linear", "random_forest"},
        ),
        (
            "group_by_date",
            {"lightgbm", "xgboost", "catboost"},
        ),
        (
            "sequence",
            {"gru", "lstm", "alstm", "transformer", "tcn", "nativetft"},
        ),
    ],
)
def test_per_capability_sets(attr: str, expected: set[str]) -> None:
    actual = {
        name
        for name, item in cap.MODEL_CAPABILITIES.items()
        if bool(getattr(item, attr))
    }
    assert actual == expected


def test_all_models_regress_by_default() -> None:
    assert {n for n, c in cap.MODEL_CAPABILITIES.items() if c.regression} == EXPECTED_MODELS


def test_intersection_for_mixed_ensemble() -> None:
    inter = cap.intersect_capability(["lightgbm", "gru"])
    assert inter["regression"] is True
    assert inter["classification"] is False
    assert inter["sample_weight"] is False
    with pytest.raises(cap.CapabilityError):
        cap.intersect_capability(["lightgbm", "not_a_model"])


@pytest.mark.parametrize(
    "model_type", ["gru", "lstm", "alstm", "transformer", "tcn", "tabnet", "nativetft"]
)
def test_classification_is_rejected_for_dl(model_type: str) -> None:
    with pytest.raises(cap.CapabilityError) as exc:
        cap.validate_target_model_combo([model_type], target_mode="classification")
    assert exc.value.code == "target_capability_unsupported"
    assert model_type in str(exc.value)


@pytest.mark.parametrize(
    "model_type", ["lightgbm", "xgboost", "catboost", "linear", "random_forest", "mlp"]
)
def test_classification_is_allowed_for_supported_models(model_type: str) -> None:
    cap.validate_target_model_combo([model_type], target_mode="classification")


@pytest.mark.parametrize(
    "model_type", ["xgboost", "catboost", "linear", "random_forest", "mlp", "gru", "nativetft"]
)
def test_quantile_is_lightgbm_only(model_type: str) -> None:
    with pytest.raises(cap.CapabilityError):
        cap.validate_target_model_combo(
            [model_type], target_mode="return", prediction_mode="quantile"
        )


def test_quantile_requires_cn_and_single_lightgbm() -> None:
    cap.validate_target_model_combo(
        ["lightgbm"], target_mode="return", prediction_mode="quantile", market="CN"
    )
    with pytest.raises(cap.CapabilityError):
        cap.validate_target_model_combo(
            ["lightgbm"], target_mode="return", prediction_mode="quantile", market="HK"
        )
    with pytest.raises(cap.CapabilityError):
        cap.validate_target_model_combo(
            ["lightgbm", "xgboost"],
            target_mode="return",
            prediction_mode="quantile",
            market="CN",
        )


@pytest.mark.parametrize("model_type", ["mlp", "gru", "lstm", "nativetft", "tabnet"])
def test_sample_weight_rejected_for_unsupported_models(model_type: str) -> None:
    with pytest.raises(cap.CapabilityError) as exc:
        cap.validate_target_model_combo(
            [model_type], target_mode="return", sample_weight=True
        )
    assert model_type in str(exc.value)


@pytest.mark.parametrize(
    "model_type", ["lightgbm", "xgboost", "catboost", "linear", "random_forest"]
)
def test_sample_weight_allowed_for_supported_models(model_type: str) -> None:
    cap.validate_target_model_combo(
        [model_type], target_mode="return", sample_weight=True
    )


def test_rank_objective_requires_single_tree_model() -> None:
    cap.validate_target_model_combo(
        ["lightgbm"], target_mode="return", objective="lambdarank"
    )
    with pytest.raises(cap.ObjectiveConflictError):
        cap.validate_target_model_combo(
            ["random_forest"], target_mode="return", objective="lambdarank"
        )
    with pytest.raises(cap.ObjectiveConflictError):
        cap.validate_target_model_combo(
            ["lightgbm", "xgboost"], target_mode="return", objective="lambdarank"
        )
    with pytest.raises(cap.ObjectiveConflictError):
        cap.validate_target_model_combo(
            ["lightgbm"], target_mode="classification", objective="lambdarank"
        )


def test_binary_objective_conflicts_with_return_target() -> None:
    with pytest.raises(cap.ObjectiveConflictError) as exc:
        cap.validate_target_model_combo(
            ["lightgbm"], target_mode="return", objective="binary"
        )
    assert exc.value.code == "target_objective_conflict"


def test_classification_rejects_regression_objective() -> None:
    with pytest.raises(cap.ObjectiveConflictError):
        cap.validate_target_model_combo(
            ["lightgbm"], target_mode="classification", objective="regression"
        )
    # 留空或显式二分类均放行（训练器最终会强制覆盖）
    cap.validate_target_model_combo(["lightgbm"], target_mode="classification")
    cap.validate_target_model_combo(
        ["lightgbm"], target_mode="classification", objective="binary"
    )


def test_capability_endpoint_is_registered_before_model_id_fallback() -> None:
    """`GET /{model_id}` 是兜底；静态路由必须前置，否则整组 404。"""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "services"
        / "api"
        / "routers"
        / "model_training.py"
    ).read_text(encoding="utf-8")
    assert '"/training/capabilities"' in source
    assert source.index('"/training/capabilities"') < source.index(
        '@router.get("/{model_id}"'
    )


def test_normalize_model_types_deduplicates_and_lowercases() -> None:
    assert cap.normalize_model_types("LightGBM", None) == ("lightgbm",)
    assert cap.normalize_model_types(
        None, ["lightgbm", "LIGHTGBM", "gru"]
    ) == ("lightgbm", "gru")
    # 空 model_types 回退 model_type；两者都空回退默认 lightgbm（与历史入口一致）
    assert cap.normalize_model_types(None, []) == ("lightgbm",)
    with pytest.raises(cap.CapabilityError):
        cap.normalize_model_types(None, ["not_a_model"])
