"""串行补推任务 API 与旧补推端点兼容层测试。

覆盖：

- ``/api/v1/models/inference/backfill-jobs``：鉴权、预览/创建/列表/详情/取消、
  业务错误映射（409 活跃任务 / 400 校验失败）、路由不被 ``GET /{model_id}`` 遮蔽；
- 旧端点 ``/{model_id}/inference/backfill``：返回 ``task_id + job_id``、
  已是最新时不建任务、活跃任务冲突 409、状态查询映射旧字段；
- coverage 端点保持旧响应键（estimated/source/reason）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.services.api.routers import model_inference_jobs as mj
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.engine.inference.model_backfill import BackfillCoverage
from backend.services.engine.services.model_inference_job_service import (
    JobValidationError,
    job_payload,
)

BASE = "/api/v1/models/inference/backfill-jobs"
USER = {"tenant_id": "default", "user_id": "10000001", "sub": "10000001"}


def _run(coro):
    return asyncio.run(coro)


class _FakePersistence:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.reaped = 0
        self.cancelled: list[str] = []

    async def reap_stale(self, **kwargs):
        self.reaped += 1
        return 0

    async def list_jobs(self, **kwargs):
        return list(self.jobs.values())[: kwargs.get("limit", 20)]

    async def get_job(self, job_id, **kwargs):
        return self.jobs.get(job_id)

    async def request_cancel(self, job_id, **kwargs):
        self.cancelled.append(job_id)
        job = self.jobs.get(job_id)
        if job is not None:
            job["cancel_requested"] = True
        return job


def _job(job_id: str = "job_1", **overrides) -> dict:
    payload = {
        "job_id": job_id,
        "tenant_id": "default",
        "user_id": "10000001",
        "status": "running",
        "mode": "gaps",
        "model_ids": ["m1", "m2"],
        "total_models": 2,
        "total_days": 4,
        "completed_days": 1,
        "failed_days": 0,
        "skipped_days": 0,
        "current_model_id": "m1",
        "current_trade_date": "2026-10-13",
        "model_results": [],
        "logs": ["m1 2026-10-12 完成"],
        "error": None,
        "cancel_requested": False,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
        "heartbeat_at": None,
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def api(monkeypatch):
    persistence = _FakePersistence()
    monkeypatch.setattr(mj, "model_inference_job_persistence", persistence)
    app = FastAPI()
    app.include_router(mj.router)
    app.dependency_overrides[get_current_user] = lambda: dict(USER)
    return TestClient(app), persistence


def test_endpoints_require_auth():
    app = FastAPI()
    app.include_router(mj.router)
    client = TestClient(app)
    assert client.get(BASE).status_code in {401, 403}
    assert client.post(f"{BASE}/preview", json={"model_ids": ["a", "b"]}).status_code in {
        401,
        403,
    }


def test_owner_rejects_missing_user():
    app = FastAPI()
    app.include_router(mj.router)
    app.dependency_overrides[get_current_user] = lambda: {"tenant_id": "default"}
    assert TestClient(app).get(BASE).status_code == 401


def test_preview_returns_service_payload(api, monkeypatch):
    client, _ = api

    async def fake_preview(*, tenant_id, user_id, model_ids):
        assert tenant_id == "default" and user_id == "10000001"
        assert model_ids == ["m1", "m2"]
        return SimpleNamespace(
            as_dict=lambda: {
                "models": [{"model_id": "m1", "eligible": True}],
                "excluded": [],
                "totals": {"models": 2, "eligible_models": 1},
                "warnings": ["串行执行"],
            }
        )

    monkeypatch.setattr(mj, "build_preview", fake_preview)
    resp = client.post(f"{BASE}/preview", json={"model_ids": ["m1", "m2"]})
    assert resp.status_code == 200
    assert resp.json()["warnings"] == ["串行执行"]


def test_preview_maps_validation_error_to_400(api, monkeypatch):
    client, _ = api

    async def fake_preview(**_kwargs):
        raise JobValidationError("too_few_models", "至少选择 2 个模型")

    monkeypatch.setattr(mj, "build_preview", fake_preview)
    resp = client.post(f"{BASE}/preview", json={"model_ids": ["m1"]})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "too_few_models"


def test_create_maps_active_job_conflict_to_409(api, monkeypatch):
    client, _ = api

    async def fake_create(**_kwargs):
        raise JobValidationError("active_job_exists", "已有补推任务在执行")

    monkeypatch.setattr(mj, "create_job", fake_create)
    resp = client.post(f"{BASE}", json={"model_ids": ["m1", "m2"]})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "active_job_exists"


def test_create_returns_job_payload(api, monkeypatch):
    client, _ = api

    async def fake_create(**_kwargs):
        return _job("job_new", status="queued")

    monkeypatch.setattr(mj, "create_job", fake_create)
    resp = client.post(f"{BASE}", json={"model_ids": ["m1", "m2"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == "job_new"
    assert body["status"] == "queued"


def test_list_reaps_stale_and_reports_active(api):
    client, persistence = api
    persistence.jobs["job_1"] = _job("job_1")
    persistence.jobs["job_0"] = _job("job_0", status="completed")
    resp = client.get(BASE, params={"limit": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert persistence.reaped == 1
    assert body["active_job_id"] == "job_1"
    assert [item["job_id"] for item in body["items"]] == ["job_1", "job_0"]


def test_get_job_404_and_cancel(api):
    client, persistence = api
    assert client.get(f"{BASE}/missing").status_code == 404
    persistence.jobs["job_1"] = _job("job_1")
    found = client.get(f"{BASE}/job_1")
    assert found.status_code == 200
    assert found.json()["progress"] == 0.25
    cancel = client.post(f"{BASE}/job_1/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["cancel_requested"] is True
    assert persistence.cancelled == ["job_1"]
    assert client.post(f"{BASE}/missing/cancel").status_code == 404


def test_job_payload_hides_storage_path_and_computes_progress():
    payload = job_payload(_job("j", storage_path="/secret", total_days=4, completed_days=3))
    assert "storage_path" not in payload
    assert payload["progress"] == 0.75
    assert payload["current"] == {"model_id": "m1", "date": "2026-10-13"}
    terminal = job_payload(_job("j", status="completed", total_days=0))
    assert terminal["progress"] == 1.0


def test_jobs_routes_not_shadowed_by_model_id_catchall(api):
    """真实 app：``/api/v1/models/inference/backfill-jobs`` 不得落进 ``GET /{model_id}``。"""
    from backend.services.api.main import app

    app.dependency_overrides[get_current_user] = lambda: dict(USER)
    try:
        resp = TestClient(app).get(BASE)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


# ── 旧端点兼容 ────────────────────────────────────────────────────────────


def _coverage(gaps=(), **overrides) -> BackfillCoverage:
    payload = {
        "min_date": "2026-10-01",
        "max_date": "2026-10-12",
        "gap_dates": tuple(gaps),
        "latest_trade_date": "2026-10-21",
        "data_cutoff_date": "2026-10-16",
        "count": 5,
    }
    payload.update(overrides)
    return BackfillCoverage(**payload)


def _model():
    return SimpleNamespace(model_id="mdl_1", storage_path="/app/models/mdl_1")


def test_legacy_coverage_keeps_old_keys(monkeypatch):
    from backend.services.api.routers import model_training as mt

    async def fake_resolve(tenant_id, user_id, model_id):
        return _model()

    monkeypatch.setattr(mt, "resolve_backfill_model", fake_resolve)
    monkeypatch.setattr(mt, "compute_coverage", lambda model: _coverage(["2026-10-13"]))
    payload = _run(
        mt.get_inference_coverage("mdl_1", {"tenant_id": "default", "user_id": "u1"})
    )
    assert payload["gap_dates"] == ["2026-10-13"]
    assert payload["count"] == 5
    assert payload["data_cutoff_date"] == "2026-10-16"
    assert payload["is_up_to_date"] is False


def test_legacy_coverage_estimated_shape(monkeypatch):
    from backend.services.api.routers import model_training as mt

    async def fake_resolve(tenant_id, user_id, model_id):
        return _model()

    monkeypatch.setattr(mt, "resolve_backfill_model", fake_resolve)
    monkeypatch.setattr(
        mt,
        "compute_coverage",
        lambda model: _coverage(
            [], estimated=True, source="quantdb_fallback", min_date=None
        ),
    )
    payload = _run(
        mt.get_inference_coverage("mdl_1", {"tenant_id": "default", "user_id": "u1"})
    )
    assert payload["estimated"] is True
    assert payload["source"] == "quantdb_fallback"
    assert payload["is_up_to_date"] is False


def test_legacy_coverage_missing_model_404(monkeypatch):
    from backend.services.api.routers import model_training as mt

    async def fake_resolve(tenant_id, user_id, model_id):
        return None

    monkeypatch.setattr(mt, "resolve_backfill_model", fake_resolve)
    with pytest.raises(HTTPException) as exc:
        _run(
            mt.get_inference_coverage(
                "missing", {"tenant_id": "default", "user_id": "u1"}
            )
        )
    assert exc.value.status_code == 404


def test_legacy_backfill_up_to_date_skips_job(monkeypatch):
    from backend.services.api.routers import model_training as mt

    async def fake_resolve(tenant_id, user_id, model_id):
        return _model()

    async def fail_create(**_kwargs):
        raise AssertionError("无缺口时不应创建任务")

    monkeypatch.setattr(mt, "resolve_backfill_model", fake_resolve)
    monkeypatch.setattr(mt, "compute_coverage", lambda model: _coverage([]))
    monkeypatch.setattr(mt, "create_job", fail_create)
    payload = _run(
        mt.trigger_backfill("mdl_1", {"tenant_id": "default", "user_id": "u1"})
    )
    assert payload == {
        "task_id": None,
        "status": "completed",
        "message": "已是最新",
        "gap": 0,
    }


def test_legacy_backfill_creates_single_model_job(monkeypatch):
    from backend.services.api.routers import model_training as mt

    calls: list[dict] = []

    async def fake_resolve(tenant_id, user_id, model_id):
        return _model()

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return _job("job_legacy", total_days=2, model_ids=["mdl_1"])

    monkeypatch.setattr(mt, "resolve_backfill_model", fake_resolve)
    monkeypatch.setattr(
        mt, "compute_coverage", lambda model: _coverage(["2026-10-13", "2026-10-14"])
    )
    monkeypatch.setattr(mt, "create_job", fake_create)
    payload = _run(
        mt.trigger_backfill("mdl_1", {"tenant_id": "default", "user_id": "u1"})
    )
    assert calls[0]["min_models"] == 1
    assert calls[0]["model_ids"] == ["mdl_1"]
    assert payload["task_id"] == "job_legacy"
    assert payload["job_id"] == "job_legacy"
    assert payload["gap"] == 2
    assert payload["target_dates"] == ["2026-10-13", "2026-10-14"]


def test_legacy_backfill_maps_active_conflict_to_409(monkeypatch):
    from backend.services.api.routers import model_training as mt

    async def fake_resolve(tenant_id, user_id, model_id):
        return _model()

    async def fake_create(**_kwargs):
        raise JobValidationError("active_job_exists", "已有补推任务在执行")

    monkeypatch.setattr(mt, "resolve_backfill_model", fake_resolve)
    monkeypatch.setattr(mt, "compute_coverage", lambda model: _coverage(["2026-10-13"]))
    monkeypatch.setattr(mt, "create_job", fake_create)
    with pytest.raises(HTTPException) as exc:
        _run(mt.trigger_backfill("mdl_1", {"tenant_id": "default", "user_id": "u1"}))
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "active_job_exists"


def test_legacy_status_maps_job_fields(monkeypatch):
    from backend.services.api.routers import model_training as mt

    class FakePersistence:
        def __init__(self) -> None:
            self.reaped: list[dict] = []

        async def reap_stale(self, **kwargs):
            self.reaped.append(kwargs)
            return 0

        async def get_job(self, job_id, **kwargs):
            return _job(
                "job_1",
                status="running",
                model_ids=["mdl_1"],
                total_days=4,
                completed_days=2,
                failed_days=1,
                skipped_days=0,
                model_results=[
                    {"model_id": "mdl_1", "days_merged": 2, "days_completed": 2}
                ],
                logs=["a", "b"],
            )

    persistence = FakePersistence()
    monkeypatch.setattr(mt, "model_inference_job_persistence", persistence)
    payload = _run(
        mt.get_backfill_status(
            "mdl_1", "job_1", {"tenant_id": "default", "user_id": "u1"}
        )
    )
    assert payload["status"] == "running"
    assert payload["progress"] == 75
    assert payload["appended"] == 2
    assert payload["merged_days"] == 2
    assert payload["failed"] == 1
    assert payload["logs"] == "a\nb"
    # 轮询侧自愈：读任务前先回收心跳超时的陈旧 running 行
    assert persistence.reaped == [
        {
            "tenant_id": "default",
            "user_id": "u1",
            "heartbeat_timeout_seconds": mt.REAP_TIMEOUT_SECONDS,
        }
    ]


def test_legacy_status_404_for_other_model(monkeypatch):
    from backend.services.api.routers import model_training as mt

    class FakePersistence:
        async def reap_stale(self, **kwargs):
            return 0

        async def get_job(self, job_id, **kwargs):
            return _job("job_1", model_ids=["other"])

    monkeypatch.setattr(mt, "model_inference_job_persistence", FakePersistence())
    with pytest.raises(HTTPException) as exc:
        _run(
            mt.get_backfill_status(
                "mdl_1", "job_1", {"tenant_id": "default", "user_id": "u1"}
            )
        )
    assert exc.value.status_code == 404


def test_progress_writer_throttles_and_reports_counts():
    from backend.services.engine.services import model_inference_job_service as svc

    class FakePersistence:
        def __init__(self) -> None:
            self.writes: list[dict] = []

        async def update_job(self, job_id, **fields):
            self.writes.append(fields)

    persistence = FakePersistence()
    writer = svc._ProgressWriter(
        job_id="j1",
        tenant_id="default",
        user_id="u1",
        persistence=persistence,
        summaries=[
            {
                "model_id": "m1",
                "display_name": "M1",
                "status": "queued",
                "days_total": 2,
                "days_completed": 0,
                "days_failed": 0,
                "days_skipped": 0,
                "days_merged": 0,
                "errors": [],
                "gap_dates": ["d1", "d2"],
            }
        ],
    )

    async def scenario():
        await writer(
            {
                "type": "day",
                "model_id": "m1",
                "day": "2026-10-13",
                "result": {"status": "completed", "signals_count": 3, "merged_rows": 3},
                "model": {
                    "model_id": "m1",
                    "display_name": "M1",
                    "status": "running",
                    "days_total": 2,
                    "days_completed": 1,
                    "days_failed": 0,
                    "days_skipped": 0,
                    "days_merged": 1,
                    "errors": [],
                    "gap_dates": ["d1", "d2"],
                },
            }
        )
        # 1 秒内第二条 day 事件被节流
        await writer(
            {
                "type": "day",
                "model_id": "m1",
                "day": "2026-10-14",
                "result": {"status": "completed", "signals_count": 3, "merged_rows": 3},
                "model": {
                    "model_id": "m1",
                    "display_name": "M1",
                    "status": "running",
                    "days_total": 2,
                    "days_completed": 2,
                    "days_failed": 0,
                    "days_skipped": 0,
                    "days_merged": 2,
                    "errors": [],
                    "gap_dates": ["d1", "d2"],
                },
            }
        )
        # 模型结束事件强制落库
        await writer({"type": "model_finished", "model": {"model_id": "m1", "display_name": "M1", "status": "completed", "days_total": 2, "days_completed": 2, "days_failed": 0, "days_skipped": 0, "days_merged": 2, "errors": [], "gap_dates": ["d1", "d2"]}})

    _run(scenario())
    assert [write["completed_days"] for write in persistence.writes] == [1, 2]
