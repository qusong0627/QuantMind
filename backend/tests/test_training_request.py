"""收尾2：TrainingRequest 纯校验与现状 422 逐字相同（本地可跑，无 admin 链依赖）。

多错误并发时的报错优先级以现状为准，不在此锁定（单错误逐字相同由本文件锁定，
28 例全量快照见服务器 /tmp/norm_snap_base.json 对比）。
"""

import pytest
from fastapi import HTTPException

from backend.shared.training.request import ContextRequest, TrainingRequest


def _detail(fn, *args):
    with pytest.raises(HTTPException) as exc_info:
        fn(*args)
    assert exc_info.value.status_code == 422
    return exc_info.value.detail


def test_minimal_valid():
    req = TrainingRequest.validate_request({"model_type": "lightgbm"})
    assert req.model_type == "lightgbm"
    assert req.train_start == "2023-01-11" and req.train_end == "2024-12-31"
    assert req.val_ratio == 0.15 and req.features == []
    assert req.context["market"] == "CN" and req.ensemble == "none"


def test_model_type_errors_verbatim():
    assert "Unsupported model_type: svm" in _detail(
        TrainingRequest.validate_request, {"model_type": "svm"}
    )
    assert "Unsupported model_type in model_types: lgb" in _detail(
        TrainingRequest.validate_request, {"model_types": ["lgb", "nope"]}
    )


def test_silent_normalizers():
    req = TrainingRequest.validate_request(
        {"ensemble": "weird", "prediction_mode": "weird"}
    )
    assert req.ensemble == "none" and req.prediction_mode == "point"
    req = TrainingRequest.validate_request({"model_types": ["xgboost", "catboost"]})
    assert req.model_types == ["xgboost", "catboost"]


def test_display_and_dates_verbatim():
    assert "display_name must be at most 128" in _detail(
        TrainingRequest.validate_request, {"display_name": "x" * 129}
    )
    assert "train_start must be earlier" in _detail(
        TrainingRequest.validate_request,
        {"train_start": "2024-12-31", "train_end": "2023-01-11"},
    )
    assert "Invalid date for train_start: not-a-date" in _detail(
        TrainingRequest.validate_request, {"train_start": "not-a-date"}
    )


def test_numeric_ranges_verbatim():
    assert "val_ratio must be between 0.01 and 0.5" in _detail(
        TrainingRequest.validate_request, {"val_ratio": 0.9}
    )
    assert "num_boost_round must be between 10 and 20000" in _detail(
        TrainingRequest.validate_request, {"num_boost_round": 5}
    )
    assert "early_stopping_rounds must be between 1 and 5000" in _detail(
        TrainingRequest.validate_request, {"early_stopping_rounds": 0}
    )
    assert "target_horizon_days must be between 1 and 30" in _detail(
        TrainingRequest.validate_request, {"target_horizon_days": 31}
    )


def test_features_and_params_shapes_verbatim():
    assert "features must be a string array" in _detail(
        TrainingRequest.validate_request, {"features": "nope"}
    )
    assert "features length cannot exceed 600" in _detail(
        TrainingRequest.validate_request, {"features": [f"f{i}" for i in range(601)]}
    )
    assert "lgb_params must be an object" in _detail(
        TrainingRequest.validate_request, {"lgb_params": "x"}
    )
    # or {} 吞掉 falsy（现状行为）：None/[] 静默回 {}，不报错
    req = TrainingRequest.validate_request({"lgb_params": [], "xgb_params": None})
    assert req.lgb_params == {} and req.xgb_params == {}


def test_horizons_target_mode_wfa_verbatim():
    # horizons 条目校验在 schema；“至少 2 个不同周期”需去重后判定，留函数推导侧（原样）
    req = TrainingRequest.validate_request({"horizons": [3]})
    assert req.horizons == [3]
    assert "non-integer value" in _detail(
        TrainingRequest.validate_request, {"horizons": ["x"]}
    )
    assert "target_mode must be one of" in _detail(
        TrainingRequest.validate_request, {"target_mode": "x"}
    )
    assert "wfa must be an object" in _detail(
        TrainingRequest.validate_request, {"wfa": "x"}
    )
    assert "wfa.strategy must be one of" in _detail(
        TrainingRequest.validate_request, {"wfa": {"strategy": "x"}}
    )
    # NOTE：显式 split 缺字段检查仍在 _normalize_payload（推导耦合），schema 不拦截
    req = TrainingRequest.validate_request({"valid_start": "2025-01-01"})
    assert req.train_start == "2023-01-11"


def test_context_exact_messages_and_camel_fallback():
    assert "context must be an object" in _detail(
        TrainingRequest.validate_request, {"context": "x"}
    )
    assert "context.initial_capital must be > 0" in _detail(
        TrainingRequest.validate_request, {"context": {"initial_capital": -1}}
    )
    assert "context.deal_price must be one of" in _detail(
        TrainingRequest.validate_request, {"context": {"deal_price": "mid"}}
    )
    # [] 被 or {} 吞掉（现状行为）
    req = TrainingRequest.validate_request({"context": []})
    assert req.context["market"] == "CN"
    req = TrainingRequest.validate_request(
        {"context": {"initialCapital": 500000, "benchmark": "HSI", "dealPrice": "OPEN"}}
    )
    assert req.context["initial_capital"] == 500000
    assert req.context["market"] == "HK"
    assert req.context["deal_price"] == "open"


def test_context_request_direct():
    ctx = ContextRequest.model_validate({"initial_capital": "abc"}).cleaned()
    assert ctx["initial_capital"] == 1_000_000.0
    with pytest.raises(HTTPException) as exc_info:
        ContextRequest.model_validate({"commission_rate": -1}).cleaned()
    assert "context.commission_rate must be >= 0" in exc_info.value.detail
