"""B1 验收：TrainingConfig schema（配置层类型化，行为不变）。

覆盖 REFACTOR_TRAINING_B §7：
- 配置往返：legacy 形状 config → TrainingConfig → dump，parsed 相等。
- 注册前置：枚举非法 / xgb max_depth<0 / 显式 split 三例。
- 死配置（optuna/drift/n_folds/meta_alpha/monitor_rank_ic/seed）只建类型不发射。
- 编排器门：_build_config_yaml 输出可被 schema 校验且 parsed 等价。
"""

import pytest
from pydantic import ValidationError

from backend.shared.training.schemas import TrainingConfig, dump_contract_dict


def _full_legacy_config() -> dict:
    """镜像 B0 真实产出形状（含 split/wfa/fs/pp 的全键 config）。"""
    return {
        "run_id": "train_b1_fixture",
        "job_name": "b1test",
        "data": {
            "train_start": "2023-01-11",
            "train_end": "2024-12-31",
            "features": ["mom_ret_1d"],
            "source_mode": "LOCAL",
            "local_dir": "/tmp/feature_snapshots",
            "factor_source": None,
            "factor_catalog_version": None,
            "factor_schema_hash": None,
            "factor_field_sources": {},
            "factor_catalog_published_at": None,
            "factor_coverage": {},
            "quantdb_dir": None,
        },
        "model": {
            "type": "xgboost",
            "types": None,
            "ensemble": "none",
            "prediction_mode": "point",
            "num_boost_round": 500,
            "early_stopping_rounds": 50,
            "val_ratio": None,
            "params": {},
            "xgb_params": {"eta": 0.1},
            "catboost_params": {},
            "dl_params": {},
        },
        "label": {
            "target_horizon_days": 3,
            "target_mode": "return",
            "label_formula": "",
            "effective_trade_date": "",
            "training_window": "",
        },
        "context": {
            "initial_capital": 1000000,
            "benchmark": "SH000300",
            "commission_rate": 0.00025,
            "slippage": 0.0005,
            "deal_price": "close",
            "market": "CN",
            "industry_as_feature": False,
        },
        "explain": {},
        "output": {
            "result_path": "/workspace/result.json",
            "required_artifacts": [
                "model.lgb",
                "pred.pkl",
                "metadata.json",
                "result.json",
            ],
        },
        "callback": {
            "url": "http://quantmind:8000/api/v1/models/training-runs/x/complete",
            "secret": "s",
        },
        "cache": {"dir": "/tmp"},
        "split": {
            "train": ["2023-01-11", "2024-12-31"],
            "valid": ["2025-01-01", "2025-06-30"],
            "test": ["2025-07-01", "2025-12-31"],
        },
        "wfa": {"enabled": True, "strategy": "rolling"},
        "max_time_minutes": 90,
        "factor_selection": {"method": "ic_icir", "n_top": 80},
        "preprocessing": {"enabled": True, "winsor": False},
    }


def test_roundtrip_full_config_parsed_equal():
    raw = _full_legacy_config()
    out = dump_contract_dict(TrainingConfig.from_dict(raw))
    assert out == raw


def test_empty_config_key_surface_snapshot():
    """空输入的 key 集合快照（§3.3.1）：条件键缺失时不发射，死配置不发射。"""
    out = dump_contract_dict(TrainingConfig.from_dict({}))
    for dead in ("split", "wfa", "factor_selection", "preprocessing"):
        assert dead not in out
    assert set(out.keys()) == {
        "run_id",
        "job_name",
        "data",
        "model",
        "label",
        "context",
        "explain",
        "output",
        "callback",
        "cache",
        "max_time_minutes",
    }
    assert set(out["data"].keys()) == {
        "train_start",
        "train_end",
        "features",
        "source_mode",
        "local_dir",
        "factor_source",
        "factor_catalog_version",
        "factor_schema_hash",
        "factor_field_sources",
        "factor_catalog_published_at",
        "factor_coverage",
        "quantdb_dir",
    }
    assert set(out["model"].keys()) == {
        "type",
        "types",
        "ensemble",
        "prediction_mode",
        "num_boost_round",
        "early_stopping_rounds",
        "val_ratio",
        "params",
        "xgb_params",
        "catboost_params",
        "dl_params",
    }


def test_illegal_enums_rejected():
    base = _full_legacy_config()
    for path, bad in [
        (("label", "target_mode"), "bogus"),
        (("context", "deal_price"), "mid"),
        (("model", "prediction_mode"), "median"),
        (("model", "type"), "svm"),
        (("data", "source_mode"), "s3"),
    ]:
        bad_cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
        parent = dict(base[path[0]])
        parent[path[1]] = bad
        bad_cfg[path[0]] = parent
        with pytest.raises(ValidationError):
            TrainingConfig.from_dict(bad_cfg)


def test_xgb_negative_max_depth_stripped():
    raw = _full_legacy_config()
    raw["model"] = dict(raw["model"])
    raw["model"]["xgb_params"] = {"max_depth": -1, "eta": 0.1}
    raw["model"]["val_ratio"] = 0.15
    out = dump_contract_dict(TrainingConfig.from_dict(raw))
    assert out["model"]["xgb_params"] == {"eta": 0.1}


def test_split_shape_guarded_val_ratio_none_allowed():
    raw = _full_legacy_config()
    raw["split"] = {"train": ["a"], "valid": ["b", "c"], "test": ["d", "e"]}
    with pytest.raises(ValidationError):
        TrainingConfig.from_dict(raw)
    # 显式 split 下 val_ratio=None 合法（现状强制行为）
    ok = _full_legacy_config()
    assert ok["model"]["val_ratio"] is None
    TrainingConfig.from_dict(ok)


def test_dead_configs_accepted_but_not_emitted():
    raw = _full_legacy_config()
    raw["optuna"] = {"enabled": True, "n_trials": 20}
    raw["drift"] = {"enabled": False}
    raw["model"] = dict(raw["model"])
    raw["model"]["n_folds"] = 5
    raw["model"]["meta_alpha"] = 0.5
    raw["model"]["monitor_rank_ic"] = False
    raw["seed"] = 7
    out = dump_contract_dict(TrainingConfig.from_dict(raw))
    assert "optuna" not in out and "drift" not in out and "seed" not in out
    assert "n_folds" not in out["model"] and "meta_alpha" not in out["model"]
    assert "monitor_rank_ic" not in out["model"]


def test_max_time_clamp_mirrors_legacy():
    assert TrainingConfig.from_dict({}).max_time_minutes == 120
    assert TrainingConfig.from_dict({"max_time_minutes": None}).max_time_minutes == 120
    assert TrainingConfig.from_dict({"max_time_minutes": 5}).max_time_minutes == 10
    assert TrainingConfig.from_dict({"max_time_minutes": "abc"}).max_time_minutes == 120
    assert TrainingConfig.from_dict({"max_time_minutes": 200}).max_time_minutes == 200


def _make_orchestrator():
    from backend.services.engine.training.local_docker_orchestrator import (
        LocalDockerOrchestrator,
    )

    o = LocalDockerOrchestrator.__new__(LocalDockerOrchestrator)
    o.api_base = "http://quantmind:8000"
    o.internal_secret = "b1-test"
    return o


def test_builder_empty_payload_validates_and_roundtrips():
    o = _make_orchestrator()
    out = o._build_config_yaml("b1_empty", {})
    # 门后输出仍可被 schema 校验（幂等）
    assert dump_contract_dict(TrainingConfig.from_dict(out)) == out
    for dead in ("split", "wfa", "preprocessing"):
        assert dead not in out
    # 空 payload 走 auto_feature_filter 默认分支（现状行为）：默认 ic_icir 筛选在
    assert out["factor_selection"]["method"] == "ic_icir"
    assert out["factor_selection"]["n_top"] == 80
    assert out["max_time_minutes"] == 120
    assert out["model"]["val_ratio"] == 0.15


def test_builder_full_payload_split_and_xgb_strip():
    o = _make_orchestrator()
    payload = {
        "job_name": "b1test",
        "model_type": "xgboost",
        "train_start": "2023-01-11",
        "train_end": "2024-12-31",
        "val_ratio": 0.2,
        "num_boost_round": 500,
        "early_stopping_rounds": 50,
        "features": [],
        "target_horizon_days": 3,
        "target_mode": "return",
        "context": {"market": "CN"},
        "xgb_params": {"max_depth": -1, "eta": 0.1},
        "ensemble": "none",
        "prediction_mode": "point",
        "valid_start": "2025-01-01",
        "valid_end": "2025-06-30",
        "test_start": "2025-07-01",
        "test_end": "2025-12-31",
        "wfa": {"enabled": True, "strategy": "rolling"},
        "max_time_minutes": 90,
        "factor_selection": {"method": "ic_icir", "n_top": 80},
        "preprocessing": {"enabled": True, "winsor": False},
    }
    out = o._build_config_yaml("b1_full", payload)
    assert out["split"]["valid"] == ["2025-01-01", "2025-06-30"]
    assert out["model"]["val_ratio"] is None
    assert out["model"]["xgb_params"] == {"eta": 0.1}
    assert out["wfa"]["strategy"] == "rolling"
    assert out["factor_selection"]["n_top"] == 80
    assert dump_contract_dict(TrainingConfig.from_dict(out)) == out
