"""中性化 API 路由测试（依赖注入覆盖，不连真库）。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.api.routers import neutralization as nl_router
from backend.services.api.routers.neutralization import router
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.engine.inference import neutralization_service as svc


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _client(user: dict) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _admin() -> dict:
    return {"user_id": "10000001", "tenant_id": "default", "is_admin": True}


def _normal_user() -> dict:
    return {"user_id": "10000001", "tenant_id": "default", "is_admin": False}


def test_overview_returns_settings(monkeypatch):
    monkeypatch.setattr(svc, "get_session_factory", lambda: _FakeSession)
    monkeypatch.setattr(
        svc,
        "overview",
        lambda session, tenant_id, user_id: [
            {"model_id": "m1", "mode": "shadow", "last_status": "ok"}
        ],
    )
    resp = _client(_admin()).get("/api/v1/models/neutralization/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"][0]["model_id"] == "m1"


def test_put_rejects_unknown_controls():
    resp = _client(_admin()).put(
        "/api/v1/models/m1/neutralization",
        json={"mode": "shadow", "controls": ["log_mv", "unknown"]},
    )
    assert resp.status_code == 422


def test_put_enforce_requires_admin():
    resp = _client(_normal_user()).put(
        "/api/v1/models/m1/neutralization",
        json={"mode": "enforce", "controls": ["log_mv"]},
    )
    assert resp.status_code == 403


def test_recompute_404_when_no_run(monkeypatch):
    monkeypatch.setattr(svc, "get_session_factory", lambda: _FakeSession)
    monkeypatch.setattr(
        svc,
        "latest_run_id",
        lambda session, tenant_id, user_id, model_id, data_trade_date: None,
    )
    resp = _client(_admin()).post(
        "/api/v1/models/m1/neutralization/recompute",
        json={"data_trade_date": "2026-10-29"},
    )
    assert resp.status_code == 404


def test_scores_requires_date():
    resp = _client(_admin()).get("/api/v1/models/m1/neutralization/scores")
    assert resp.status_code == 422


def test_put_accepts_extended_controls(monkeypatch):
    monkeypatch.setattr(svc, "get_session_factory", lambda: _FakeSession)
    monkeypatch.setattr(
        svc,
        "upsert_settings",
        lambda session, **kwargs: {
            "mode": kwargs["mode"],
            "controls": kwargs["controls"],
            "config_version": "test",
            "exists": True,
        },
    )
    resp = _client(_admin()).put(
        "/api/v1/models/m1/neutralization",
        json={"mode": "shadow", "controls": ["log_mv", "vol20", "turn20"]},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["controls"] == ["log_mv", "vol20", "turn20"]
