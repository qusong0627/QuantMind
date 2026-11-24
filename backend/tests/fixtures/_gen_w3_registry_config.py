"""一次性生成 W3 registry config 金样（不参与 pytest 收集）。"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, "/app")
from backend.shared.training.target_spec import spec_from_dict  # noqa: E402

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
        "description": "W3 validate-only golden",
    }
)
label = spec.to_label_fields()
label.update(
    {
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
)
cfg = {
    "run_id": "w3_registry_validate",
    "job_name": "w3_registry_validate",
    "data": {
        "train_start": "2023-01-11",
        "train_end": "2024-12-31",
        "features": [],
        "source_mode": "LOCAL",
    },
    "model": {
        "type": "lightgbm",
        "prediction_mode": "point",
        "params": {},
        "xgb_params": {},
        "catboost_params": {},
        "dl_params": {},
    },
    "label": label,
    "context": {"market": "CN"},
    "output": {"result_path": "/tmp/w3_registry_result.json"},
}
out = Path("/app/backend/tests/fixtures/w3_registry_config.yaml")
out.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
print(out, spec.spec_hash())
