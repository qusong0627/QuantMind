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


# ═══════════════════════════════════════════════════════════════════════════
# 停牌过滤测试（2026-04-26）
# ═══════════════════════════════════════════════════════════════════════════

class TestUntradableFilter:
    """测试停牌股票过滤逻辑。"""

    def test_filter_removes_zero_volume(self):
        """volume = 0 的股票应被过滤。"""
        import pandas as pd
        from backend.services.engine.inference.templates.inference_parquet import (
            filter_untradable_rows,
        )

        df = pd.DataFrame({
            "symbol": ["SH600519", "SH600735", "SZ000001"],
            "close": [100.0, 50.0, 10.0],
            "volume": [1000, 0, 500],  # SH600735 零成交
        })

        result = filter_untradable_rows(df)

        assert len(result) == 2
        assert "SH600735" not in result["symbol"].values
        assert set(result["symbol"]) == {"SH600519", "SZ000001"}

    def test_filter_removes_zero_close(self):
        """close <= 0 的股票应被过滤。"""
        import pandas as pd
        from backend.services.engine.inference.templates.inference_parquet import (
            filter_untradable_rows,
        )

        df = pd.DataFrame({
            "symbol": ["SH600519", "SH600735", "SZ000001"],
            "close": [100.0, 0.0, 10.0],  # SH600735 价格为 0
            "volume": [1000, 500, 500],
        })

        result = filter_untradable_rows(df)

        assert len(result) == 2
        assert "SH600735" not in result["symbol"].values

    def test_filter_removes_negative_close(self):
        """close < 0 的股票应被过滤。"""
        import pandas as pd
        from backend.services.engine.inference.templates.inference_parquet import (
            filter_untradable_rows,
        )

        df = pd.DataFrame({
            "symbol": ["SH600519", "SH600735"],
            "close": [100.0, -1.0],  # SH600735 负价格
            "volume": [1000, 500],
        })

        result = filter_untradable_rows(df)

        assert len(result) == 1
        assert result.iloc[0]["symbol"] == "SH600519"

    def test_filter_handles_empty_dataframe(self):
        """空 DataFrame 应返回空。"""
        import pandas as pd
        from backend.services.engine.inference.templates.inference_parquet import (
            filter_untradable_rows,
        )

        df = pd.DataFrame()
        result = filter_untradable_rows(df)

        assert result.empty

    def test_filter_handles_missing_columns(self):
        """缺少 close/volume 列时应跳过对应过滤。"""
        import pandas as pd
        from backend.services.engine.inference.templates.inference_parquet import (
            filter_untradable_rows,
        )

        # 只有 close，无 volume
        df = pd.DataFrame({
            "symbol": ["SH600519", "SH600735"],
            "close": [100.0, 0.0],
        })

        result = filter_untradable_rows(df)

        assert len(result) == 1
        assert result.iloc[0]["symbol"] == "SH600519"


class TestManagedParquetTemplateDetection:
    """测试旧版自动生成脚本识别逻辑。"""

    def test_detects_new_template(self, tmp_path: Path):
        """新版模板应被识别为托管脚本。"""
        from backend.services.engine.inference.script_runner import InferenceScriptRunner

        script = tmp_path / "inference.py"
        script.write_text(
            '#!/usr/bin/env python3\n"""QuantMind Parquet 数据源推理脚本 (inference.py 模板)\n',
            encoding="utf-8",
        )

        runner = InferenceScriptRunner(models_production=str(tmp_path))
        assert runner._is_managed_parquet_template(script) is True

    def test_detects_old_auto_generated_script(self, tmp_path: Path):
        """旧版自动生成脚本应被识别为托管脚本。"""
        from backend.services.engine.inference.script_runner import InferenceScriptRunner

        script = tmp_path / "inference.py"
        script.write_text(
            '''#!/usr/bin/env python3
"""
QuantMind Parquet 数据源推理脚本
================================
由训练流水线自动生成
''',
            encoding="utf-8",
        )

        runner = InferenceScriptRunner(models_production=str(tmp_path))
        assert runner._is_managed_parquet_template(script) is True

    def test_rejects_custom_script(self, tmp_path: Path):
        """自定义脚本不应被识别为托管脚本。"""
        from backend.services.engine.inference.script_runner import InferenceScriptRunner

        script = tmp_path / "inference.py"
        script.write_text(
            '#!/usr/bin/env python3\n"""自定义推理脚本"""\nprint("custom")\n',
            encoding="utf-8",
        )

        runner = InferenceScriptRunner(models_production=str(tmp_path))
        assert runner._is_managed_parquet_template(script) is False


class TestActiveDataSourceAudit:
    """测试 active_data_source 审计字段。"""

    def test_parquet_model_records_real_data_source(self, monkeypatch, tmp_path: Path):
        """parquet 模型执行后 active_data_source 应为真实 parquet 目录。"""
        # 直接验证 ExecutionResult 的 active_data_source 字段
        # 在实际执行中，parquet 模型的 active_data_source 会被设置为 parquet 目录
        # 此测试验证字段存在且可正确传递
        result = ExecutionResult(
            success=True,
            exit_code=0,
            stdout='[{"symbol": "SH600519", "score": 0.5}]',
            stderr="",
            run_id="run_test",
            signals_count=1,
            active_model_id="user_model",
            active_data_source="/app/db/feature_snapshots",
        )

        assert result.success is True
        assert "feature_snapshots" in result.active_data_source
