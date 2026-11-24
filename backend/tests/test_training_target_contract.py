"""容器侧 Target Contract 自校验（W3）。"""

from __future__ import annotations

import pytest

from backend.shared.training.target_contract import (
    TargetContractError,
    load_target_contract,
    validate_training_target_contract,
)
from backend.shared.training.target_spec import spec_from_dict


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


def _cfg(*, target: bool = True, model_type: str = "lightgbm", **label_overrides) -> dict:
    spec = spec_from_dict(_spec_raw())
    label = {
        "target_horizon_days": 5,
        "target_mode": "return",
        "execution_price": "open",
        "label_transform": "rank",
        "label_top_pct": 0.2,
        "label_drop_extreme_pct": 0.0,
        "label_formula": "",
        "effective_trade_date": "",
        "training_window": "",
    }
    if target:
        label.update(
            {
                "target_id": spec.target_id,
                "target_version": spec.version,
                "target_hash": spec.spec_hash(),
                "target_base_hash": spec.spec_hash(),
                "target_status": "active",
                "target_spec": spec.canonical_dict(),
                "label_contract_version": 1,
            }
        )
    label.update(label_overrides)
    return {
        "run_id": "w3",
        "model": {"type": model_type, "types": None, "prediction_mode": "point",
                  "params": {}, "xgb_params": {}, "catboost_params": {}, "dl_params": {}},
        "label": label,
        "context": {"market": "CN"},
    }


def _code(cfg: dict, model_type: str = "lightgbm") -> str:
    with pytest.raises(TargetContractError) as exc:
        validate_training_target_contract(cfg, model_type)
    return exc.value.code


def test_valid_registry_contract_passes() -> None:
    contract = validate_training_target_contract(_cfg(), "lightgbm")
    assert contract.legacy is False
    assert contract.target_id == "cn_ret_5d_open_rank"
    assert contract.version == 1
    assert len(contract.effective_hash) == 64


def test_tampered_hash_is_rejected() -> None:
    cfg = _cfg()
    cfg["label"]["target_hash"] = "0" * 64
    assert _code(cfg) == "target_spec_hash_mismatch"


def test_tampered_flat_field_is_rejected() -> None:
    cfg = _cfg()
    cfg["label"]["target_horizon_days"] = 10
    assert _code(cfg) == "target_spec_field_mismatch"


def test_missing_spec_is_rejected() -> None:
    cfg = _cfg()
    cfg["label"].pop("target_spec")
    assert _code(cfg) == "target_spec_missing"


def test_unknown_contract_version_is_rejected() -> None:
    cfg = _cfg()
    cfg["label"]["label_contract_version"] = 99
    assert _code(cfg) == "target_contract_version_unsupported"


def test_label_formula_override_is_rejected_with_spec() -> None:
    cfg = _cfg()
    cfg["label"]["label_formula"] = "label_transform=zscore"
    assert _code(cfg) == "target_formula_override_forbidden"


def test_registry_classification_dl_is_rejected() -> None:
    spec = spec_from_dict(
        _spec_raw(
            target_id="cn_updown_5d_open",
            family="classification_binary",
            mode="classification",
        )
    )
    cfg = _cfg(target=False, model_type="gru")
    cfg["label"] = {
        **spec.to_label_fields(),
        "label_formula": "",
        "effective_trade_date": "",
        "training_window": "",
        "target_id": spec.target_id,
        "target_version": spec.version,
        "target_hash": spec.spec_hash(),
        "target_base_hash": spec.spec_hash(),
        "target_status": "active",
        "target_spec": spec.canonical_dict(),
        "label_contract_version": 1,
    }
    assert _code(cfg, "gru") == "target_capability_unsupported"


def test_legacy_config_passes_and_synthesizes_contract() -> None:
    cfg = _cfg(target=False)
    contract = validate_training_target_contract(cfg, "lightgbm")
    assert contract.legacy is True
    assert contract.target_id == "legacy"
    assert contract.version == 0
    assert contract.effective_hash == contract.base_hash


def test_legacy_classification_dl_only_warns() -> None:
    cfg = _cfg(target=False, model_type="gru")
    cfg["label"].update(
        {"target_mode": "classification", "label_transform": "rank"}
    )
    contract = validate_training_target_contract(cfg, "gru")
    assert contract.legacy is True


def test_load_legacy_uses_close_default() -> None:
    cfg = _cfg(target=False)
    cfg["label"].pop("execution_price")
    contract = load_target_contract(cfg)
    assert contract.spec.entry.price == "close"
