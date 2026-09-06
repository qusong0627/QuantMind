"""按模型独立入口：13 schema 键隔离 + 路由工厂 13 端点（本地可跑）。

- 每个模型 schema 只含共享字段 + 自家 params（extra=forbid）：跨模型 params 当场拒收。
- 路由工厂为纯路由装配（submit 懒导入），本地可断言 13 条路径。
- 深度值校验仍在 TrainingRequest 单源，本层不管。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.shared.training.per_model import (
    MODEL_FRAMEWORK,
    MODEL_PARAMS_FIELD,
    REQUEST_MODELS,
)
from backend.shared.training.request import ALLOWED_MODEL_TYPES


def _stub_auth():
    return {"tenant_id": "default", "user_id": "tester", "sub": "tester"}


def test_specs_cover_all_allowed_models():
    assert set(REQUEST_MODELS) == set(ALLOWED_MODEL_TYPES)
    assert set(MODEL_PARAMS_FIELD) == set(ALLOWED_MODEL_TYPES)
    assert len(REQUEST_MODELS) == 13


def test_each_schema_has_own_params_only():
    from backend.shared.training.per_model import _SHARED_FIELDS

    for name, cls in REQUEST_MODELS.items():
        fields = set(cls.model_fields)
        assert fields == set(_SHARED_FIELDS) | {
            "model_type",
            MODEL_PARAMS_FIELD[name],
        }, name
        assert cls.model_fields["model_type"].default == name


def test_cross_model_params_rejected_at_entry():
    # G1 主案：给 lightgbm 传 dl_params，入口即 422（以前进训练后被静默丢弃）
    with pytest.raises(ValidationError):
        REQUEST_MODELS["lightgbm"].model_validate({"dl_params": {"n_epochs": 3}})
    with pytest.raises(ValidationError):
        REQUEST_MODELS["gru"].model_validate({"lgb_params": {"num_leaves": 31}})
    with pytest.raises(ValidationError):
        REQUEST_MODELS["xgboost"].model_validate({"model_type": "lightgbm"})
    # 未知顶层键同样拒绝
    with pytest.raises(ValidationError):
        REQUEST_MODELS["lightgbm"].model_validate({"no_such_key": 1})


def test_own_params_and_minimal_dump():
    req = REQUEST_MODELS["lightgbm"].model_validate({"lgb_params": {"num_leaves": 63}})
    dumped = req.model_dump(mode="json", exclude_none=True)
    assert dumped == {"model_type": "lightgbm", "lgb_params": {"num_leaves": 63}}
    # 空提交只剩 model_type（下游默认值与旧入口一致）
    dumped = (
        REQUEST_MODELS["gru"]
        .model_validate({})
        .model_dump(mode="json", exclude_none=True)
    )
    assert dumped == {"model_type": "gru"}


def test_frameworks_match_runtime_mapping():
    assert MODEL_FRAMEWORK["mlp"] == "sklearn"
    assert MODEL_FRAMEWORK["lightgbm"] == "lightgbm"
    assert MODEL_FRAMEWORK["nativetft"] == "pytorch"


def test_factory_builds_thirteen_routes_with_auth():
    from fastapi import Depends

    from backend.services.api.routers.training_per_model import build_per_model_router

    router = build_per_model_router(_stub_auth)
    paths = sorted(r.path for r in router.routes)
    assert paths == sorted(f"/training/{m}" for m in REQUEST_MODELS)

    # 端点级隔离实测：经 HTTP 递跨模型 params → 422（鉴权用 stub，不进 submit）
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    resp = client.post("/training/lightgbm", json={"dl_params": {"n_epochs": 3}})
    assert resp.status_code == 422
    resp = client.post("/training/gru", json={"lgb_params": {"num_leaves": 31}})
    assert resp.status_code == 422
