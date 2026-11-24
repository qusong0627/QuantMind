"""提交链路 Target Contract（W2）：形状校验、解析冻结、能力硬闸、预检。

不连 DB：registry 路径通过 `_target_meta` 注入（与 submit_training_job 一致），
legacy 路径走平铺字段合成。DB 解析本身由 `test_target_registry_integration.py` 覆盖。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.services.api.routers.admin.admin_training_utils import _normalize_payload
from backend.shared.training.request import TrainingRequest
from backend.shared.training.target_spec import spec_from_dict


def _base_payload(**overrides) -> dict:
    payload = {
        "job_name": "w2_target_test",
        "model_type": "lightgbm",
        "train_start": "2023-01-11",
        "train_end": "2024-12-31",
        "features": ["f1"],
        "target_horizon_days": 5,
        "target_mode": "return",
        "execution_price": "open",
        "label_transform": "rank",
        "label_top_pct": 0.2,
        "label_drop_extreme_pct": 0.0,
        "context": {"market": "CN", "benchmark": "SH000300"},
        "factor_source": "l1_factors",
    }
    payload.update(overrides)
    return payload


def _target_meta(raw: dict, *, status: str = "active") -> dict:
    spec = spec_from_dict(raw)
    return {
        "id": spec.target_id,
        "version": spec.version,
        "status": status,
        "source": "registry",
        "base_hash": spec.spec_hash(),
        "base_spec": spec.canonical_dict(),
    }


def _spec_raw(**overrides) -> dict:
    raw = {
        "target_id": "cn_ret_5d_open_rank",
        "version": 1,
        "market": "CN",
        "family": "forward_return",
        "mode": "regression",
        "horizon_days": 5,
        "entry": {"lag_days": 1, "price": "open"},
        "exit": {"price": "close"},
        "transform": {"type": "rank", "top_pct": 0.2, "drop_extreme_pct": 0.0},
        "description": "test",
    }
    raw.update(overrides)
    return raw


def _error_detail(payload: dict) -> str:
    with pytest.raises(HTTPException) as exc:
        _normalize_payload(payload, allowed_features=[])
    return str(exc.value.detail)


# ── 形状校验 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("ref", "prefix"),
    [
        ("nope", "target_ref_invalid"),
        ({}, "target_ref_invalid"),
        ({"id": "x"}, "target_version_required"),
        ({"id": "x", "version": "bad"}, "target_ref_invalid"),
        ({"id": "x", "version": 0}, "target_ref_invalid"),
        ({"id": "x", "version": 1, "extra": 1}, "target_ref_invalid"),
    ],
)
def test_target_ref_shape_errors(ref, prefix: str) -> None:
    payload = _base_payload(target_ref=ref)
    with pytest.raises(HTTPException) as exc:
        TrainingRequest.validate_request(payload)
    assert str(exc.value.detail).startswith(prefix)


def test_label_overrides_must_be_flat_scalars() -> None:
    for bad in ("nope", {"horizon_days": {"v": 1}}, {"horizon_days": [1]}):
        with pytest.raises(HTTPException) as exc:
            TrainingRequest.validate_request(_base_payload(label_overrides=bad))
        assert str(exc.value.detail).startswith("target_override_invalid")


def test_label_formula_override_forbidden_with_target_ref() -> None:
    payload = _base_payload(
        target_ref={"id": "cn_ret_5d_open_rank", "version": 1},
        label_formula="label_transform=zscore",
    )
    with pytest.raises(HTTPException) as exc:
        TrainingRequest.validate_request(payload)
    assert str(exc.value.detail).startswith("target_formula_override_forbidden")


# ── 解析/冻结 ───────────────────────────────────────────────────────────────


def test_registry_meta_derives_flat_fields_and_hash() -> None:
    payload = _base_payload(
        _target_meta=_target_meta(_spec_raw()),
        target_horizon_days=3,  # 必须被 spec 覆盖，而不是信任平铺字段
        execution_price="close",
    )
    out = _normalize_payload(payload, allowed_features=[])
    assert out["target_horizon_days"] == 5
    assert out["execution_price"] == "open"
    assert out["label_transform"] == "rank"
    meta = out["target_meta"]
    assert meta["id"] == "cn_ret_5d_open_rank"
    assert meta["version"] == 1
    assert meta["source"] == "registry"
    assert meta["effective_hash"] == spec_from_dict(_spec_raw()).spec_hash()
    assert meta["base_hash"] == meta["effective_hash"]


def test_expected_target_hash_shape_is_validated() -> None:
    with pytest.raises(HTTPException) as exc:
        TrainingRequest.validate_request(_base_payload(expected_target_hash="bad"))
    assert str(exc.value.detail).startswith("target_hash_invalid")


def test_expected_target_hash_drift_is_rejected() -> None:
    payload = _base_payload(
        _target_meta=_target_meta(_spec_raw()),
        expected_target_hash="0" * 64,
    )
    detail = _error_detail(payload)
    assert detail.startswith("target_hash_drift")


def test_expected_target_hash_matches_effective_spec() -> None:
    meta = _target_meta(_spec_raw())
    payload = _base_payload(
        _target_meta=meta,
        expected_target_hash=meta["base_hash"],
    )
    out = _normalize_payload(payload, allowed_features=[])
    assert out["target_hash"] == meta["base_hash"]


def test_registry_overrides_change_effective_hash_only() -> None:
    base = _target_meta(_spec_raw())
    payload = _base_payload(
        _target_meta=base, label_overrides={"horizon_days": 10}
    )
    out = _normalize_payload(payload, allowed_features=[])
    assert out["target_horizon_days"] == 10
    assert out["target_meta"]["base_hash"] == base["base_hash"]
    assert out["target_meta"]["effective_hash"] != base["base_hash"]
    assert out["target_meta"]["spec"]["horizon_days"] == 10
    assert out["target_meta"]["overrides"] == {"horizon_days": 10}


def test_legacy_payload_is_unchanged_and_gets_legacy_meta() -> None:
    payload = _base_payload()
    out = _normalize_payload(payload, allowed_features=[])
    assert out["target_horizon_days"] == 5
    assert out["target_mode"] == "return"
    assert out["execution_price"] == "open"
    assert out["label_transform"] == "rank"
    assert out["target_meta"]["id"] == "legacy"
    assert out["target_meta"]["version"] == 0
    assert out["target_meta"]["source"] == "legacy"
    assert out["target_meta"]["effective_hash"] == out["target_meta"]["base_hash"]


def test_classification_spec_drives_target_mode() -> None:
    raw = _spec_raw(
        target_id="cn_updown_5d_open",
        family="classification_binary",
        mode="classification",
    )
    payload = _base_payload(
        target_mode="return",  # 平铺字段错误，必须被 spec 纠正
        _target_meta=_target_meta(raw),
    )
    out = _normalize_payload(payload, allowed_features=[])
    assert out["target_mode"] == "classification"


# ── 能力硬闸 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model_type", ["gru", "lstm", "alstm", "transformer", "tcn", "tabnet", "nativetft"]
)
def test_classification_dl_is_rejected(model_type: str) -> None:
    raw = _spec_raw(
        target_id="cn_updown_5d_open",
        family="classification_binary",
        mode="classification",
    )
    payload = _base_payload(
        model_type=model_type, target_mode="classification", _target_meta=_target_meta(raw)
    )
    detail = _error_detail(payload)
    assert detail.startswith("target_capability_unsupported")


def test_mixed_model_types_intersection_is_enforced() -> None:
    raw = _spec_raw(
        target_id="cn_updown_5d_open",
        family="classification_binary",
        mode="classification",
    )
    payload = _base_payload(
        model_type="lightgbm",
        model_types=["lightgbm", "gru"],
        target_mode="classification",
        _target_meta=_target_meta(raw),
    )
    detail = _error_detail(payload)
    assert detail.startswith("target_capability_unsupported")
    assert "gru" in detail


def test_quantile_requires_consistent_prediction_mode_for_registry() -> None:
    raw = _spec_raw()  # forward_return
    payload = _base_payload(
        _target_meta=_target_meta(raw), prediction_mode="quantile"
    )
    detail = _error_detail(payload)
    assert detail.startswith("target_prediction_mode_mismatch")


def test_quantile_spec_requires_quantile_mode_and_lgb() -> None:
    raw = _spec_raw(
        target_id="cn_ret_5d_open_quantile",
        family="quantile_return",
        mode="quantile",
        transform={"type": "raw", "top_pct": 0.2, "drop_extreme_pct": 0.0},
    )
    ok = _base_payload(_target_meta=_target_meta(raw), prediction_mode="quantile")
    out = _normalize_payload(ok, allowed_features=[])
    assert out["prediction_mode"] == "quantile"
    assert out["target_mode"] == "return"

    bad_model = _base_payload(
        model_type="xgboost", _target_meta=_target_meta(raw), prediction_mode="quantile"
    )
    assert _error_detail(bad_model).startswith("target_capability_unsupported")

    bad_mode = _base_payload(_target_meta=_target_meta(raw), prediction_mode="point")
    assert _error_detail(bad_mode).startswith("target_prediction_mode_mismatch")


def test_sample_weight_unsupported_model_is_rejected() -> None:
    payload = _base_payload(
        model_type="mlp",
        sample_weight={"tradeable_mask": True, "st": False, "time_decay_half_life_days": 0},
    )
    assert _error_detail(payload).startswith("target_capability_unsupported")


def test_capability_gate_warn_allows_but_records_flag(monkeypatch) -> None:
    monkeypatch.setenv("QM_TARGET_CAPABILITY_GATE", "warn")
    raw = _spec_raw(
        target_id="cn_updown_5d_open",
        family="classification_binary",
        mode="classification",
    )
    payload = _base_payload(
        model_type="gru", target_mode="classification", _target_meta=_target_meta(raw)
    )
    out = _normalize_payload(payload, allowed_features=[])
    assert out["target_meta"]["contract_flags"]["capability_gate"] == "warn"


# ── 预检 ────────────────────────────────────────────────────────────────────


def test_open_price_requires_factor_source() -> None:
    payload = _base_payload(factor_source="")
    detail = _error_detail(payload)
    assert detail.startswith("target_execution_price_unsupported")


def test_size_excess_requires_total_cap_feature() -> None:
    payload = _base_payload(
        label_transform="size_excess_rank",
    )
    detail = _error_detail(payload)
    assert detail.startswith("target_size_excess_requires_total_cap")


def test_size_excess_with_total_cap_passes() -> None:
    payload = _base_payload(
        features=["f1", "total_cap"], label_transform="size_excess_rank"
    )
    out = _normalize_payload(payload, allowed_features=[])
    assert out["label_transform"] == "size_excess_rank"


def test_drop_extreme_with_classification_is_rejected() -> None:
    payload = _base_payload(
        target_mode="classification",
        label_transform="rank",
        label_drop_extreme_pct=0.1,
    )
    detail = _error_detail(payload)
    assert detail.startswith("target_drop_extreme_unsupported_transform")
