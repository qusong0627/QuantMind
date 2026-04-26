from __future__ import annotations

from pathlib import Path
import types

import pytest

from backend.services.engine.inference.script_runner import ExecutionResult, InferenceScriptRunner


def test_runner_dimension_insufficient_triggers_fallback(monkeypatch, tmp_path: Path):
    model_dir = tmp_path / "model_qlib"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "inference.py").write_text("#!/usr/bin/env python\nprint('main')\n", encoding="utf-8")
    (model_dir / "inference_alpha158.py").write_text("#!/usr/bin/env python\nprint('fb')\n", encoding="utf-8")

    runner = InferenceScriptRunner(models_production=str(model_dir))

    monkeypatch.setattr(runner, "_resolve_expected_feature_dim", lambda: 76)
    monkeypatch.setattr(
        runner,
        "_query_dimension_readiness",
        lambda trade_date, expected_dim: {"ready": False, "detail": "dim_not_ready"},
    )

    fallback_called = {"value": False}

    def _fake_fallback(**kwargs):
        fallback_called["value"] = True
        return ExecutionResult(
            success=True,
            exit_code=0,
            stdout="[]",
            stderr="",
            run_id=kwargs["run_id"],
            fallback_used=True,
            fallback_reason=kwargs["fallback_reason"],
        )

    monkeypatch.setattr(runner, "_execute_fallback", _fake_fallback)

    def _never_run(*args, **kwargs):
        raise AssertionError("main inference.py should not run when dimension gate fails")

    monkeypatch.setattr("backend.services.engine.inference.script_runner.subprocess.run", _never_run)

    result = runner.execute("2026-03-20")
    assert result.success is True
    assert result.fallback_used is True
    assert "dim_not_ready" in result.fallback_reason
    assert fallback_called["value"] is True


def test_runner_missing_primary_script_triggers_fallback(monkeypatch, tmp_path: Path):
    model_dir = tmp_path / "model_qlib"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "inference_alpha158.py").write_text("#!/usr/bin/env python\nprint('fb')\n", encoding="utf-8")

    runner = InferenceScriptRunner(models_production=str(model_dir))

    fallback_called = {"value": False}

    def _fake_fallback(**kwargs):
        fallback_called["value"] = True
        return ExecutionResult(
            success=True,
            exit_code=0,
            stdout="[]",
            stderr="",
            run_id=kwargs["run_id"],
            fallback_used=True,
            fallback_reason=kwargs["fallback_reason"],
            active_model_id="alpha158",
            active_data_source="db/Alpha158_bin",
        )

    monkeypatch.setattr(runner, "_execute_fallback", _fake_fallback)

    result = runner.execute("2026-03-20")
    assert result.success is True
    assert result.fallback_used is True
    assert "主模型推理脚本不存在" in result.fallback_reason
    assert fallback_called["value"] is True


def test_runner_expected_feature_dim_from_metadata_feature_columns(tmp_path: Path):
    model_dir = tmp_path / "model_qlib"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "metadata.json").write_text(
        '{"feature_columns": ["f1", "f2", "f3", "f4", "f5", "f6"]}',
        encoding="utf-8",
    )
    runner = InferenceScriptRunner(models_production=str(model_dir))
    assert runner._resolve_expected_feature_dim() == 6


def test_runner_alpha158_fallback_defaults_to_qlib_data(tmp_path: Path):
    model_dir = tmp_path / "model_qlib"
    model_dir.mkdir(parents=True, exist_ok=True)

    runner = InferenceScriptRunner(models_production=str(model_dir))

    assert runner.fallback_data_dir.endswith("db/qlib_data")


def test_runner_can_disable_model_fallback(monkeypatch, tmp_path: Path):
    model_dir = tmp_path / "alpha158"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "inference.py").write_text(
        "#!/usr/bin/env python\nprint('main')\n",
        encoding="utf-8",
    )

    runner = InferenceScriptRunner(
        primary_model_dir=str(model_dir),
        fallback_model_dir=str(model_dir),
        primary_model_id="alpha158",
        fallback_model_id="alpha158",
        enable_fallback=False,
    )

    monkeypatch.setattr(runner, "_resolve_expected_feature_dim", lambda: 158)
    monkeypatch.setattr(
        runner,
        "_query_dimension_readiness",
        lambda trade_date, expected_dim: {"ready": False, "detail": "dim_not_ready"},
    )

    def _unexpected_fallback(**kwargs):
        raise AssertionError("fallback should be disabled for independent models")

    monkeypatch.setattr(runner, "_execute_fallback", _unexpected_fallback)

    result = runner.execute("2026-03-20")

    assert result.success is False
    assert result.fallback_used is False
    assert result.execution_mode == ""
    assert result.model_switch_used is False
    assert result.model_switch_reason == ""
    assert result.active_model_id == "alpha158"
    assert "dim_not_ready" in (result.error or "")


def test_router_service_alpha158_fallback_defaults_to_qlib_data(monkeypatch):
    monkeypatch.delenv("QLIB_FALLBACK_DATA_PATH", raising=False)

    from backend.services.engine.inference.router_service import InferenceRouterService

    service = InferenceRouterService()

    assert service.fallback_data_source == "db/qlib_data"


def test_router_service_explicit_alpha158_runs_independently(monkeypatch, tmp_path: Path):
    from backend.services.engine.inference import router_service as router_module

    primary_dir = tmp_path / "model_qlib"
    primary_dir.mkdir(parents=True, exist_ok=True)
    alpha_dir = tmp_path / "alpha158"
    alpha_dir.mkdir(parents=True, exist_ok=True)
    (alpha_dir / "metadata.json").write_text(
        '{"data_source": "qlib", "qlib_data_path": "db/qlib_data"}',
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    class _FakeRunner:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def execute(self, date, tenant_id=None, user_id=None, redis_client=None):
            return ExecutionResult(
                success=True,
                exit_code=0,
                stdout="[]",
                stderr="",
                run_id="run_x",
                active_model_id=str(captured.get("primary_model_id")),
                active_data_source=str(captured.get("primary_data_dir")),
            )

    monkeypatch.setattr(router_module, "InferenceScriptRunner", _FakeRunner)

    service = router_module.InferenceRouterService()
    service.primary_model_dir = str(primary_dir)
    service.fallback_model_dir = str(alpha_dir)

    result = service.run_daily_inference_script(
        date="2026-03-20",
        tenant_id="default",
        user_id="system",
        resolved_model={
            "effective_model_id": "alpha158",
            "model_source": "explicit_system_model",
            "storage_path": "",
            "fallback_reason": "",
        },
    )

    assert captured["primary_model_id"] == "alpha158"
    assert str(captured["primary_model_dir"]).endswith("alpha158")
    assert captured["primary_data_dir"] == "db/qlib_data"
    assert captured["enable_fallback"] is False
    assert result.active_model_id == "alpha158"
    assert result.execution_mode == "independent_model"
    assert result.model_switch_used is False
    assert result.model_switch_reason == ""


def test_runner_ready_threshold_is_adaptive(monkeypatch, tmp_path: Path):
    model_dir = tmp_path / "model_qlib"
    model_dir.mkdir(parents=True, exist_ok=True)
    runner = InferenceScriptRunner(models_production=str(model_dir))

    monkeypatch.setattr(
        "backend.services.engine.inference.script_runner._MIN_READY_SYMBOLS",
        3000,
        raising=False,
    )
    monkeypatch.setattr(
        "backend.services.engine.inference.script_runner._MIN_READY_RATIO",
        0.9,
        raising=False,
    )
    monkeypatch.setattr(
        "backend.services.engine.inference.script_runner._MIN_READY_FLOOR",
        100,
        raising=False,
    )

    assert runner._resolve_ready_threshold(1000) == 900
    assert runner._resolve_ready_threshold(4000) == 3000


class _FakeRedis:
    def __init__(self):
        self.deleted = []

    def set(self, key, value, ex=None, nx=None):
        return True

    def get(self, key):
        return ""

    def delete(self, key):
        self.deleted.append(key)
        return 1


@pytest.mark.anyio
async def test_run_inference_failure_releases_lock_and_returns_standard_fields(monkeypatch):
    import sys

    fake_cal = types.SimpleNamespace(
        get_calendar=lambda *_args, **_kwargs: types.SimpleNamespace(
            sessions=[],
            is_session=lambda _d: True,
            previous_session=lambda _d: types.SimpleNamespace(date=lambda: _d),
        )
    )
    monkeypatch.setitem(sys.modules, "exchange_calendars", fake_cal)
    from backend.services.api.routers.admin import model_management as mm

    fake_redis = _FakeRedis()
    monkeypatch.setattr(mm, "get_redis_sentinel_client", lambda: fake_redis)

    class _FakeRouterService:
        def __init__(self, *_args, **_kwargs):
            pass

        def run_daily_inference_script(self, **_kwargs):
            return ExecutionResult(
                success=False,
                exit_code=2,
                stdout="",
                stderr="dim insufficient",
                run_id="run_20260320_test",
                error="fallback failed",
                fallback_used=True,
                fallback_reason="维度不足",
                failure_stage="fallback_script",
                active_model_id="alpha158",
                active_data_source="db/Alpha158_bin",
            )

    monkeypatch.setattr(mm, "InferenceRouterService", _FakeRouterService)

    resp = await mm.run_inference(current_user={"tenant_id": "t1", "user_id": "u1"})

    assert resp["success"] is False
    assert resp["fallback_used"] is True
    assert resp["fallback_reason"] == "维度不足"
    assert resp["failure_stage"] == "fallback_script"
    assert resp["active_model_id"] == "alpha158"
    assert resp["active_data_source"] == "db/Alpha158_bin"
    assert any(k.startswith("qm:lock:inference:daily:") for k in fake_redis.deleted)


@pytest.mark.anyio
async def test_run_inference_success_releases_lock_and_returns_standard_fields(monkeypatch):
    import sys

    fake_cal = types.SimpleNamespace(
        get_calendar=lambda *_args, **_kwargs: types.SimpleNamespace(
            sessions=[],
            is_session=lambda _d: True,
            previous_session=lambda _d: types.SimpleNamespace(date=lambda: _d),
        )
    )
    monkeypatch.setitem(sys.modules, "exchange_calendars", fake_cal)
    from backend.services.api.routers.admin import model_management as mm

    fake_redis = _FakeRedis()
    monkeypatch.setattr(mm, "get_redis_sentinel_client", lambda: fake_redis)

    class _FakeRouterService:
        def __init__(self, *_args, **_kwargs):
            pass

        def run_daily_inference_script(self, **_kwargs):
            return ExecutionResult(
                success=True,
                exit_code=0,
                stdout="[]",
                stderr="",
                run_id="run_20260320_ok",
                signals_count=10,
                fallback_used=False,
                fallback_reason="",
                active_model_id="model_qlib",
                active_data_source="db/qlib_data",
            )

    monkeypatch.setattr(mm, "InferenceRouterService", _FakeRouterService)

    resp = await mm.run_inference(current_user={"tenant_id": "t1", "user_id": "u1"})

    assert resp["success"] is True
    assert resp["signals_count"] == 10
    assert resp["fallback_used"] is False
    assert "fallback_reason" in resp
    assert "failure_stage" in resp
    assert resp["active_model_id"] == "model_qlib"
    assert resp["active_data_source"] == "db/qlib_data"
    assert any(k.startswith("qm:lock:inference:daily:") for k in fake_redis.deleted)
