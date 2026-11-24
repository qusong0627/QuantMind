"""Target Contract 端到端（W6，无真训练）：

提交 payload → _normalize_payload（冻结）→ _build_config_yaml（写 spec/hash）
→ 容器侧 validate_training_target_contract（自校验）→ metadata（追溯）。
"""

from __future__ import annotations

import copy

import pytest

from backend.services.api.routers.admin.admin_training_utils import _normalize_payload
from backend.shared.training.target_contract import (
    TargetContractError,
    validate_training_target_contract,
)
from backend.shared.training.target_metadata import build_target_metadata
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
        "description": "e2e",
    }
    raw.update(overrides)
    return raw


def _payload(*, target: bool = True) -> dict:
    spec = spec_from_dict(_spec_raw())
    payload = {
        "job_name": "w6_e2e",
        "model_type": "lightgbm",
        "train_start": "2023-01-11",
        "train_end": "2024-12-31",
        "valid_start": "2025-01-01",
        "valid_end": "2025-06-30",
        "test_start": "2025-07-01",
        "test_end": "2025-12-31",
        "features": [],
        "target_horizon_days": 5,
        "target_mode": "return",
        "execution_price": "open",
        "label_transform": "rank",
        "label_top_pct": 0.2,
        "label_drop_extreme_pct": 0.0,
        "context": {"market": "CN", "benchmark": "SH000300", "deal_price": "close"},
        "factor_source": "l1_factors",
        "factor_selection": {"method": "ic_icir", "n_top": 80},
        "max_time_minutes": 60,
    }
    if target:
        payload["_target_meta"] = {
            "id": spec.target_id,
            "version": spec.version,
            "status": "active",
            "source": "registry",
            "base_hash": spec.spec_hash(),
            "base_spec": spec.canonical_dict(),
        }
    return payload


def _orchestrator():
    from backend.services.engine.training.local_docker_orchestrator import (
        LocalDockerOrchestrator,
    )

    o = LocalDockerOrchestrator.__new__(LocalDockerOrchestrator)
    o.api_base = "http://quantmind:8000"
    o.internal_secret = "dummy"
    return o


@pytest.mark.unit
def test_registry_target_full_chain() -> None:
    normalized = _normalize_payload(_payload(), allowed_features=[])
    cfg = _orchestrator()._build_config_yaml("w6_e2e", normalized)

    assert cfg["label"]["target_id"] == "cn_ret_5d_open_rank"
    assert cfg["label"]["target_version"] == 1
    assert cfg["label"]["target_hash"] == cfg["label"]["target_base_hash"]
    assert cfg["label"]["label_contract_version"] == 1

    contract = validate_training_target_contract(cfg, "lightgbm")
    assert contract.legacy is False
    assert contract.effective_hash == cfg["label"]["target_hash"]

    meta = build_target_metadata(cfg, diagnostics={"gate": "strict"})
    assert meta["target_id"] == "cn_ret_5d_open_rank"
    assert meta["target_hash"] == cfg["label"]["target_hash"]
    assert meta["target_overrides"] == {}
    assert meta["contract_flags"]["diagnostic_gate"] == "strict"


@pytest.mark.unit
def test_legacy_target_full_chain_has_no_new_config_keys() -> None:
    normalized = _normalize_payload(_payload(target=False), allowed_features=[])
    cfg = _orchestrator()._build_config_yaml("w6_e2e_legacy", normalized)
    for key in (
        "target_id",
        "target_version",
        "target_hash",
        "target_spec",
        "label_contract_version",
    ):
        assert key not in cfg["label"]
    contract = validate_training_target_contract(cfg, "lightgbm")
    assert contract.legacy is True
    meta = build_target_metadata(cfg)
    assert meta["target_id"] == "legacy"
    assert len(meta["target_hash"]) == 64


@pytest.mark.unit
def test_registry_overrides_full_chain() -> None:
    payload = _payload()
    payload["label_overrides"] = {"horizon_days": 10}
    normalized = _normalize_payload(payload, allowed_features=[])
    cfg = _orchestrator()._build_config_yaml("w6_e2e_override", normalized)

    assert cfg["label"]["target_horizon_days"] == 10
    assert cfg["label"]["target_overrides"] == {"horizon_days": 10}
    assert cfg["label"]["target_hash"] != cfg["label"]["target_base_hash"]
    contract = validate_training_target_contract(cfg, "lightgbm")
    assert contract.spec.horizon_days == 10
    meta = build_target_metadata(cfg)
    assert meta["target_overrides"] == {"horizon_days": 10}
    assert meta["target_base_hash"] == payload["_target_meta"]["base_hash"]


@pytest.mark.unit
def test_tampered_config_is_rejected_at_container_boundary() -> None:
    normalized = _normalize_payload(_payload(), allowed_features=[])
    cfg = _orchestrator()._build_config_yaml("w6_e2e", normalized)
    bad = copy.deepcopy(cfg)
    bad["label"]["target_horizon_days"] = 10
    with pytest.raises(TargetContractError) as exc:
        validate_training_target_contract(bad, "lightgbm")
    assert exc.value.code == "target_spec_field_mismatch"
