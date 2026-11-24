"""metadata target 构造（W3）：新路径/legacy/诊断字段。"""

from __future__ import annotations

from backend.shared.training.target_metadata import build_target_metadata
from backend.shared.training.target_spec import spec_from_dict


def _spec() -> dict:
    return {
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


def _label(**overrides) -> dict:
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
    label.update(overrides)
    return label


def test_registry_metadata() -> None:
    spec = spec_from_dict(_spec())
    label = _label(
        target_id=spec.target_id,
        target_version=spec.version,
        target_hash=spec.spec_hash(),
        target_base_hash=spec.spec_hash(),
        target_status="active",
        target_spec=spec.canonical_dict(),
        label_contract_version=1,
    )
    meta = build_target_metadata({"label": label, "context": {"market": "CN"}}, lag_days=1)
    assert meta["target_id"] == "cn_ret_5d_open_rank"
    assert meta["target_version"] == 1
    assert meta["target_hash"] == spec.spec_hash()
    assert meta["label_contract_version"] == 1
    assert meta["target_spec"]["target_id"] == "cn_ret_5d_open_rank"
    assert meta["execution_lag_days"] == 1


def test_legacy_metadata_synthesizes_contract() -> None:
    meta = build_target_metadata(
        {"label": _label(), "context": {"market": "CN"}}, lag_days=1
    )
    assert meta["target_id"] == "legacy"
    assert meta["target_version"] == 0
    assert meta["target_status"] == "legacy"
    assert meta["target_spec"]["family"] == "forward_return"
    assert len(meta["target_hash"]) == 64
    assert meta["target_hash"] == meta["target_base_hash"]


def test_diagnostics_and_flags_are_recorded() -> None:
    stats = {"rows": 100, "nan_ratio": 0.01, "gate": "strict", "warnings": []}
    meta = build_target_metadata(
        {"label": _label(), "context": {"market": "CN"}},
        lag_days=1,
        diagnostics=stats,
        env={"QM_TARGET_CAPABILITY_GATE": "strict"},
    )
    assert meta["label_stats"] == stats
    assert meta["contract_flags"] == {
        "capability_gate": "strict",
        "diagnostic_gate": "strict",
    }
