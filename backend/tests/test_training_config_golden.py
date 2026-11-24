"""Legacy 训练配置 label 块金样（W0）。

只钉三件事：
1. 无 `target_ref` 的请求，config.yaml 的 `label` 键集合与当前基线逐键一致；
2. 不出现任何 `target_*` 新键（Target Contract 只在新路径写）；
3. `_build_config_yaml` 的 label 值来自 payload，不被回测 context 污染。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "backend" / "tests" / "fixtures" / "training_label_config_golden_legacy.json"


def _make_orchestrator():
    from backend.services.engine.training.local_docker_orchestrator import (
        LocalDockerOrchestrator,
    )

    o = LocalDockerOrchestrator.__new__(LocalDockerOrchestrator)
    o.api_base = "http://quantmind:8000"
    o.internal_secret = "dummy"
    return o


def _payload() -> dict:
    return {
        "job_name": "w0_legacy_golden",
        "model_type": "lightgbm",
        "train_start": "2023-01-11",
        "train_end": "2024-12-31",
        "valid_start": "2025-01-01",
        "valid_end": "2025-06-30",
        "test_start": "2025-07-01",
        "test_end": "2025-12-31",
        "num_boost_round": 300,
        "early_stopping_rounds": 50,
        "features": [],
        "target_horizon_days": 5,
        "target_mode": "return",
        "execution_price": "open",
        "label_transform": "rank",
        "label_top_pct": 0.2,
        "label_drop_extreme_pct": 0.0,
        "context": {"market": "CN", "benchmark": "SH000300", "deal_price": "close"},
        "factor_selection": {"method": "ic_icir", "n_top": 80},
        "max_time_minutes": 60,
    }


@pytest.mark.unit
def test_legacy_label_block_matches_golden() -> None:
    cfg = _make_orchestrator()._build_config_yaml("w0_legacy_golden", _payload())
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert cfg["label"] == expected
    assert set(cfg["label"]) == set(expected)


@pytest.mark.unit
def test_registry_target_writes_frozen_spec_into_config() -> None:
    from backend.shared.training.target_spec import spec_from_dict

    spec = spec_from_dict(
        {
            "target_id": "cn_ret_5d_open_rank",
            "version": 1,
            "market": "CN",
            "family": "forward_return",
            "mode": "regression",
            "horizon_days": 5,
            "entry": {"lag_days": 1, "price": "open"},
            "exit": {"price": "close"},
            "transform": {"type": "rank", "top_pct": 0.2, "drop_extreme_pct": 0.0},
            "description": "golden",
        }
    )
    payload = _payload()
    payload["target_meta"] = {
        "id": spec.target_id,
        "version": spec.version,
        "status": "active",
        "source": "registry",
        "base_hash": spec.spec_hash(),
        "effective_hash": spec.spec_hash(),
        "spec": spec.canonical_dict(),
        "overrides": {},
    }
    cfg = _make_orchestrator()._build_config_yaml("w3_registry_golden", payload)
    label = cfg["label"]
    assert label["target_id"] == "cn_ret_5d_open_rank"
    assert label["target_version"] == 1
    assert label["target_hash"] == spec.spec_hash()
    assert label["target_base_hash"] == spec.spec_hash()
    assert label["target_status"] == "active"
    assert label["label_contract_version"] == 1
    assert label["target_spec"]["target_id"] == "cn_ret_5d_open_rank"
    # 平铺字段仍保持容器现有读取口径
    assert label["target_horizon_days"] == 5
    assert label["target_mode"] == "return"
    assert label["execution_price"] == "open"


@pytest.mark.unit
def test_legacy_label_block_has_no_target_contract_keys() -> None:
    cfg = _make_orchestrator()._build_config_yaml("w0_legacy_golden", _payload())
    forbidden = {
        "target_id",
        "target_version",
        "target_hash",
        "target_status",
        "target_spec",
        "label_contract_version",
    }
    assert not (set(cfg["label"]) & forbidden)


@pytest.mark.unit
def test_label_block_is_not_derived_from_backtest_context() -> None:
    payload = _payload()
    payload["context"]["deal_price"] = "open"  # 回测口径，不得影响训练标签
    cfg = _make_orchestrator()._build_config_yaml("w0_legacy_golden", payload)
    assert cfg["label"]["execution_price"] == "open"
    assert cfg["context"]["deal_price"] == "open"
    # 反向：训练标签 open，回测 close，两者互不影响
    payload["context"]["deal_price"] = "close"
    payload["execution_price"] = "close"
    cfg2 = _make_orchestrator()._build_config_yaml("w0_legacy_golden", payload)
    assert cfg2["label"]["execution_price"] == "close"
    assert cfg2["context"]["deal_price"] == "close"
