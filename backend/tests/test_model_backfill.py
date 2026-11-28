"""串行补推共享核心（``model_backfill``）单元测试。

锁死四类语义：

- 缺口口径：pred 中间空洞要补、QuantDB 未产出日不算缺口、无产物给原因；
- 单日执行：成功合并 pred、锁占用记 skipped_lock、失败落 run 终态、异常不抛；
- 串行编排：缺口少的模型先跑、取消在当前日之后生效、连续失败 3 天止损、
  忙碌模型整只跳过且不阻塞后续；
- 资格判定：系统模型只读、归档/未就绪/无产物各有原因。
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.services.engine.inference import model_backfill as mb


def _write_pred(path, dates: list[str]) -> None:
    rows = [
        {"symbol": "SH600036", "trade_date": day, "pred": 0.5}
        for day in dates
    ]
    pd.DataFrame(rows).to_parquet(path / "pred.parquet", index=False)


def _model(tmp_path, **overrides) -> mb.BackfillModel:
    payload = {
        "model_id": "mdl_lgb",
        "display_name": "LightGBM",
        "model_type": "lightgbm",
        "market": "CN",
        "target_horizon_days": 10,
        "status": "ready",
        "storage_path": str(tmp_path),
        "metadata": {},
    }
    payload.update(overrides)
    return mb.BackfillModel(**payload)


# ── 覆盖与缺口 ────────────────────────────────────────────────────────────


def test_coverage_finds_middle_holes_and_caps_at_data_cutoff(tmp_path):
    _write_pred(tmp_path, ["2026-10-12", "2026-10-14"])
    coverage = mb.compute_coverage(
        _model(tmp_path),
        latest=date(2026, 10, 21),
        cutoff=date(2026, 10, 16),
    )
    assert coverage.source == "pred"
    assert coverage.count == 2
    assert coverage.min_date == "2026-10-12"
    assert coverage.max_date == "2026-10-14"
    assert coverage.data_cutoff_date == "2026-10-16"
    assert list(coverage.gap_dates) == ["2026-10-13", "2026-10-15", "2026-10-16"]


def test_coverage_without_pred_uses_quantdb_range(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mb,
        "_quantdb_available_dates",
        lambda model: ("l1_factors", ["2026-10-12", "2026-10-13"]),
    )
    coverage = mb.compute_coverage(
        _model(tmp_path), latest=date(2026, 10, 21), cutoff=date(2026, 10, 16)
    )
    assert coverage.estimated is True
    assert coverage.source == "quantdb_fallback"
    assert coverage.count == 2
    assert coverage.max_date == "2026-10-13"
    assert coverage.factor_latest_date == "2026-10-13"
    assert coverage.factor_stale is True
    assert coverage.factor_lag_days == 3
    # 因子源只到 10-13，10-14~10-16 不是「补推缺口」，而是因子未物化
    assert coverage.gap_dates == ()


def test_factor_stale_message_explains_its_the_data_not_the_model(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        mb,
        "_quantdb_available_dates",
        lambda model: ("fused189", ["2026-10-12", "2026-10-13"]),
    )
    coverage = mb.compute_coverage(
        _model(tmp_path), latest=date(2026, 10, 21), cutoff=date(2026, 10, 16)
    )
    message = mb.factor_stale_message(coverage)
    assert "fused189" in message
    assert "2026-10-13" in message
    assert "不是模型坏了" in message


def test_coverage_without_any_source_reports_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "_quantdb_available_dates", lambda model: ("l1_factors", []))
    coverage = mb.compute_coverage(
        _model(tmp_path), latest=date(2026, 10, 21), cutoff=date(2026, 10, 21)
    )
    assert coverage.gap_dates == ()
    assert coverage.reason == "pred.parquet not found and quantdb unavailable"


def test_coverage_missing_storage_is_explicit():
    coverage = mb.compute_coverage(
        _model("/definitely/not/here", storage_path=""),
        latest=date(2026, 10, 21),
        cutoff=date(2026, 10, 21),
    )
    assert coverage.reason == "missing_storage"
    assert coverage.latest_trade_date == "2026-10-21"


def test_compute_gap_dates_returns_list(tmp_path):
    _write_pred(tmp_path, ["2026-10-12"])
    assert mb.compute_gap_dates(
        _model(tmp_path), latest=date(2026, 10, 13), cutoff=date(2026, 10, 13)
    ) == ["2026-10-13"]


def test_ineligibility_reasons(tmp_path):
    assert mb.ineligibility_reason(_model(tmp_path)) is None
    assert (
        mb.ineligibility_reason(_model(tmp_path, model_id="sys-native-tft"))
        == "system_readonly"
    )
    assert (
        mb.ineligibility_reason(_model(tmp_path, status="archived")) == "archived"
    )
    assert mb.ineligibility_reason(_model(tmp_path, status="training")) == "not_ready"
    assert (
        mb.ineligibility_reason(_model(tmp_path, storage_path="")) == "missing_storage"
    )


# ── 单日执行 ──────────────────────────────────────────────────────────────


def _result(**overrides):
    payload = {
        "success": True,
        "signals": [{"symbol": "SH600036", "score": 0.9}],
        "signals_count": 1,
        "run_id": "run_1",
        "error": "",
        "failure_stage": "",
        "fallback_reason": "",
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def test_run_one_day_merges_successful_signals(tmp_path, monkeypatch):
    calls: list[tuple] = []

    def fake_execute(model, day, tenant_id, user_id, redis_client):
        calls.append((model.model_id, day, tenant_id, user_id, redis_client))
        return _result(), 1

    monkeypatch.setattr(mb, "_execute_sync", fake_execute)
    result = _run(
        mb.run_one_day(_model(tmp_path), "2026-10-13", "default", "u1", task_id="j1")
    )
    assert result.status == "completed"
    assert result.merged_rows == 1
    assert result.signals_count == 1
    assert calls[0][:4] == ("mdl_lgb", "2026-10-13", "default", "u1")


def test_run_one_day_maps_lock_held_to_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mb,
        "_execute_sync",
        lambda *args: (
            _result(success=False, fallback_reason="LOCK_HELD: 已在执行"),
            0,
        ),
    )
    result = _run(mb.run_one_day(_model(tmp_path), "2026-10-13", "default", "u1"))
    assert result.status == "skipped_lock"
    assert "LOCK_HELD" in result.error


def test_run_one_day_records_failed_run_terminal(tmp_path, monkeypatch):
    recorded: list[tuple] = []

    async def fake_record(model, day, tenant_id, user_id, result, task_id):
        recorded.append((model.model_id, day, result.run_id, task_id))

    monkeypatch.setattr(mb, "_record_failed_run", fake_record)
    monkeypatch.setattr(
        mb,
        "_execute_sync",
        lambda *args: (
            _result(
                success=False,
                run_id="run_bad",
                error="数据不足",
                failure_stage="main_script",
            ),
            0,
        ),
    )
    result = _run(
        mb.run_one_day(_model(tmp_path), "2026-10-13", "default", "u1", task_id="j1")
    )
    assert result.status == "failed"
    assert result.error == "数据不足"
    assert result.failure_stage == "main_script"
    assert recorded == [("mdl_lgb", "2026-10-13", "run_bad", "j1")]


def test_run_one_day_turns_runner_exception_into_failed(tmp_path, monkeypatch):
    def boom(*args):
        raise RuntimeError("进程挂了")

    monkeypatch.setattr(mb, "_execute_sync", boom)
    result = _run(mb.run_one_day(_model(tmp_path), "2026-10-13", "default", "u1"))
    assert result.status == "failed"
    assert "进程挂了" in result.error


# ── 串行编排 ──────────────────────────────────────────────────────────────


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _day(model, day, status="completed", merged=1, error=""):
    return mb.DayResult(
        model_id=model.model_id,
        day=day,
        status=status,
        merged_rows=merged if status == "completed" else 0,
        error=error,
    )


def test_run_serial_orders_by_gap_count_then_model_id(tmp_path):
    calls: list[tuple[str, str]] = []

    async def fake_day(model, day, tenant_id, user_id, **kwargs):
        calls.append((model.model_id, day))
        return _day(model, day)

    models = [
        (_model(tmp_path, model_id="mdl_a"), ["2026-10-12", "2026-10-13"]),
        (_model(tmp_path, model_id="mdl_b"), ["2026-10-12"]),
        (_model(tmp_path, model_id="mdl_c"), ["2026-10-12"]),
    ]
    result = _run(mb.run_serial(models, "default", "u1", run_day=fake_day))
    assert calls == [
        ("mdl_b", "2026-10-12"),
        ("mdl_c", "2026-10-12"),
        ("mdl_a", "2026-10-12"),
        ("mdl_a", "2026-10-13"),
    ]
    assert result.completed_days == 4
    assert [item.status for item in result.model_results] == [
        "completed",
        "completed",
        "completed",
    ]


def test_run_serial_cancel_stops_after_current_day(tmp_path):
    calls: list[str] = []
    state = {"cancel": False}

    async def fake_day(model, day, tenant_id, user_id, **kwargs):
        calls.append(day)
        state["cancel"] = True
        return _day(model, day)

    async def should_cancel():
        return state["cancel"]

    result = _run(
        mb.run_serial(
            [(_model(tmp_path), ["2026-10-12", "2026-10-13"])],
            "default",
            "u1",
            run_day=fake_day,
            should_cancel=should_cancel,
        )
    )
    assert calls == ["2026-10-12"]
    assert result.cancelled is True
    assert result.model_results[0].status == "cancelled"
    assert result.model_results[0].gap_dates == ["2026-10-12", "2026-10-13"]


def test_run_serial_stops_model_after_three_consecutive_failures(tmp_path):
    calls: list[str] = []

    async def fake_day(model, day, tenant_id, user_id, **kwargs):
        calls.append(model.model_id)
        if model.model_id == "mdl_bad":
            return _day(model, day, status="failed", error="数据不足")
        return _day(model, day)

    models = [
        (_model(tmp_path, model_id="mdl_bad"), ["d1", "d2", "d3", "d4", "d5"]),
    ]
    result = _run(mb.run_serial(models, "default", "u1", run_day=fake_day))
    assert calls == ["mdl_bad", "mdl_bad", "mdl_bad"]
    bad = result.model_results[0]
    assert bad.status == "failed"
    assert bad.days_failed == 3
    assert len(bad.errors) == 3
    assert result.failed_days == 3
    assert result.completed_days == 0


def test_run_serial_lock_skip_is_not_a_failure(tmp_path):
    async def fake_day(model, day, tenant_id, user_id, **kwargs):
        return _day(model, day, status="skipped_lock", error="LOCK_HELD")

    result = _run(
        mb.run_serial(
            [(_model(tmp_path), ["d1", "d2", "d3", "d4"])],
            "default",
            "u1",
            run_day=fake_day,
        )
    )
    summary = result.model_results[0]
    assert summary.status == "skipped"
    assert summary.days_skipped == 4
    assert result.failed_days == 0


def test_run_serial_skips_busy_model_and_continues(tmp_path):
    calls: list[str] = []

    async def fake_day(model, day, tenant_id, user_id, **kwargs):
        calls.append(model.model_id)
        return _day(model, day)

    async def should_skip(model_id):
        return model_id == "mdl_busy"

    models = [
        (_model(tmp_path, model_id="mdl_busy"), ["d1", "d2"]),
        (_model(tmp_path, model_id="mdl_ok"), ["d1"]),
    ]
    result = _run(
        mb.run_serial(
            models,
            "default",
            "u1",
            run_day=fake_day,
            should_skip_model=should_skip,
        )
    )
    assert calls == ["mdl_ok"]
    busy = next(item for item in result.model_results if item.model_id == "mdl_busy")
    assert busy.status == "skipped_busy"
    assert busy.days_skipped == 2
    assert result.skipped_days == 2


def test_run_serial_emits_progress_events(tmp_path):
    events: list[dict] = []

    async def fake_day(model, day, tenant_id, user_id, **kwargs):
        return _day(model, day)

    async def progress(event):
        events.append(event)

    _run(
        mb.run_serial(
            [(_model(tmp_path), ["d1"])],
            "default",
            "u1",
            run_day=fake_day,
            progress_cb=progress,
        )
    )
    kinds = [event["type"] for event in events]
    assert kinds == ["model_started", "day", "model_finished"]
    assert events[1]["result"]["status"] == "completed"


# ── 模型解析 / 产物路径 / 依赖降级 ────────────────────────────────────────


def test_to_backfill_model_reads_metadata_context():
    model = mb.to_backfill_model(
        {
            "model_id": " mdl_1 ",
            "status": "READY",
            "storage_path": "/tmp/x",
            "metadata_json": {
                "display_name": "LightGBM 300",
                "model_type": "LightGBM",
                "context": {"market": "hk", "target_horizon_days": "10"},
            },
        }
    )
    assert model.model_id == "mdl_1"
    assert model.display_name == "LightGBM 300"
    assert model.model_type == "lightgbm"
    assert model.market == "HK"
    assert model.target_horizon_days == 10
    assert model.status == "ready"


def test_to_backfill_model_tolerates_bad_horizon_and_missing_meta():
    model = mb.to_backfill_model(
        {"model_id": "m", "metadata_json": {"target_horizon_days": "abc"}}
    )
    assert model.target_horizon_days is None
    assert model.market == "CN"
    assert mb.to_backfill_model({"model_id": "m", "metadata_json": "not-a-dict"}).model_id


def test_resolve_backfill_model_returns_none_or_model(monkeypatch):
    async def no_row(**kwargs):
        return None

    monkeypatch.setattr(
        mb.model_registry_service, "get_model", lambda **kwargs: no_row(**kwargs)
    )
    assert _run(mb.resolve_backfill_model("default", "u1", "m")) is None

    async def row(**kwargs):
        return {"model_id": "m", "status": "ready", "storage_path": "/tmp"}

    monkeypatch.setattr(
        mb.model_registry_service, "get_model", lambda **kwargs: row(**kwargs)
    )
    resolved = _run(mb.resolve_backfill_model("default", "u1", " m "))
    assert resolved is not None and resolved.model_id == "m"

    async def blank(**kwargs):
        return {"model_id": "   ", "status": "ready"}

    monkeypatch.setattr(
        mb.model_registry_service, "get_model", lambda **kwargs: blank(**kwargs)
    )
    assert _run(mb.resolve_backfill_model("default", "u1", "m")) is None


def test_resolve_pred_parquet_prefers_existing_then_default(tmp_path):
    assert mb.resolve_pred_parquet(str(tmp_path)) == tmp_path / "pred.parquet"
    nested = tmp_path / "pred"
    nested.mkdir()
    (nested / "pred.parquet").write_bytes(b"")
    assert mb.resolve_pred_parquet(str(tmp_path)) == nested / "pred.parquet"
    (tmp_path / "pred.parquet").write_bytes(b"")
    assert mb.resolve_pred_parquet(str(tmp_path)) == tmp_path / "pred.parquet"


def test_coverage_pred_without_date_column(tmp_path):
    pd.DataFrame([{"symbol": "SH600036", "score": 0.5}]).to_parquet(
        tmp_path / "pred.parquet", index=False
    )
    coverage = mb.compute_coverage(
        _model(tmp_path), latest=date(2026, 10, 21), cutoff=date(2026, 10, 21)
    )
    assert coverage.reason == "pred.parquet has no trade_date column"
    assert coverage.gap_dates == ()


def test_latest_trading_date_rolls_back_to_session():
    assert mb.latest_trading_date(date(2026, 10, 21)) == date(2026, 10, 21)
    assert mb.latest_trading_date(date(2026, 10, 18)) == date(2026, 10, 16)


def test_quantdb_helpers_degrade_when_data_dir_missing(tmp_path, monkeypatch):
    from backend.services.engine.data_platform import quantdb_factor_reader as qfr

    monkeypatch.setattr(qfr, "market_data_dir", lambda market: tmp_path / "missing")
    assert mb.quantdb_latest_factor_date("CN") is None
    assert mb._quantdb_available_dates(_model(tmp_path)) == ("l1_factors", [])


def test_create_redis_client_respects_flags(monkeypatch):
    monkeypatch.setenv("QM_BACKFILL_INFERENCE_LOCK", "0")
    assert mb.create_redis_client() is None
    monkeypatch.setenv("QM_BACKFILL_INFERENCE_LOCK", "1")
    monkeypatch.delenv("REDIS_HOST", raising=False)
    assert mb.create_redis_client() is None
    monkeypatch.setenv("REDIS_HOST", "127.0.0.1")
    client = mb.create_redis_client()
    assert client is not None
    client.close()


def test_execute_sync_merges_only_successful_signals(tmp_path, monkeypatch):
    calls: list[tuple] = []

    class FakeRunner:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def execute(self, **kwargs):
            calls.append(("execute", self.kwargs["primary_model_id"], kwargs["date"]))
            return _result(signals=[{"symbol": "SH600036"}])

    monkeypatch.setattr(mb, "InferenceScriptRunner", FakeRunner)
    monkeypatch.setattr(
        mb,
        "merge_signals_into_pred",
        lambda path, payload, create_if_missing: 7,
    )
    result, merged = mb._execute_sync(_model(tmp_path), "2026-10-13", "default", "u1", None)
    assert result.success is True and merged == 7
    assert calls == [("execute", "mdl_lgb", "2026-10-13")]

    monkeypatch.setattr(
        FakeRunner,
        "execute",
        lambda self, **kwargs: _result(success=False, signals=[]),
    )
    monkeypatch.setattr(
        mb,
        "merge_signals_into_pred",
        lambda *args, **kwargs: pytest.fail("失败日不得合并"),
    )
    _, merged = mb._execute_sync(_model(tmp_path), "2026-10-14", "default", "u1", None)
    assert merged == 0


def test_execute_sync_success_without_signals_skips_merge(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mb,
        "InferenceScriptRunner",
        lambda **kwargs: SimpleNamespace(execute=lambda **kw: _result(signals=[])),
    )
    monkeypatch.setattr(
        mb,
        "merge_signals_into_pred",
        lambda *args, **kwargs: pytest.fail("无信号不得合并"),
    )
    _, merged = mb._execute_sync(_model(tmp_path), "2026-10-14", "default", "u1", None)
    assert merged == 0


def test_record_failed_run_writes_terminal_state(tmp_path, monkeypatch):
    calls: list[tuple] = []

    class FakePersistence:
        async def create_run(self, **kwargs):
            calls.append(("create", kwargs))

        async def update_run(self, **kwargs):
            calls.append(("update", kwargs))

    monkeypatch.setattr(mb, "model_inference_persistence", FakePersistence())

    async def no_run():
        await mb._record_failed_run(
            _model(tmp_path),
            "2026-10-13",
            "default",
            "u1",
            _result(success=False, run_id=""),
            None,
        )

    _run(no_run())
    assert calls == []

    async def with_run():
        await mb._record_failed_run(
            _model(tmp_path),
            "2026-10-13",
            "default",
            "u1",
            _result(success=False, run_id="run_x", error="坏", failure_stage="main"),
            "job_1",
        )

    _run(with_run())
    assert calls[0][1]["run_id"] == "run_x"
    assert calls[0][1]["request_payload"]["task_id"] == "job_1"
    assert calls[1][1]["status"] == "failed"
    assert calls[1][1]["failure_stage"] == "main"


def test_record_failed_run_swallows_persistence_errors(tmp_path, monkeypatch):
    class BoomPersistence:
        async def create_run(self, **kwargs):
            raise RuntimeError("db down")

    monkeypatch.setattr(mb, "model_inference_persistence", BoomPersistence())

    async def scenario():
        await mb._record_failed_run(
            _model(tmp_path),
            "2026-10-13",
            "default",
            "u1",
            _result(success=False, run_id="run_x"),
            None,
        )

    _run(scenario())  # 不抛


def test_emit_swallows_callback_errors():
    async def boom(event):
        raise RuntimeError("callback down")

    _run(mb._emit(boom, {"type": "day"}))
    _run(mb._emit(None, {"type": "day"}))


def test_model_status_branches():
    assert mb._model_status(mb.ModelRunResult("m", "M"), cancelled=True) == "cancelled"
    assert mb._model_status(mb.ModelRunResult("m", "M"), cancelled=False) == "completed"
    partial = mb.ModelRunResult("m", "M", days_completed=1, days_failed=1)
    assert mb._model_status(partial, cancelled=False) == "partial"
    failed = mb.ModelRunResult("m", "M", days_failed=1)
    assert mb._model_status(failed, cancelled=False) == "failed"
    skipped = mb.ModelRunResult("m", "M", days_skipped=1)
    assert mb._model_status(skipped, cancelled=False) == "skipped"


def test_day_result_ok_and_serial_total_days():
    assert mb.DayResult("m", "2026-10-13", "completed").ok is True
    assert mb.DayResult("m", "2026-10-13", "failed").ok is False
    serial = mb.SerialResult((), completed_days=2, failed_days=1, skipped_days=3, cancelled=False)
    assert serial.total_days == 6


def test_latest_trading_date_falls_back_when_calendar_unavailable(monkeypatch):
    import sys

    def boom(_name):
        raise RuntimeError("calendar unavailable")

    monkeypatch.setitem(
        sys.modules, "exchange_calendars", SimpleNamespace(get_calendar=boom)
    )
    assert mb.latest_trading_date(date(2026, 10, 18)) == date(2026, 10, 16)


def test_quantdb_latest_factor_date_reads_reader(tmp_path, monkeypatch):
    from backend.services.engine.data_platform import quantdb_factor_reader as qfr

    class FakeReader:
        dates: list[str] = ["2026-10-20", "2026-10-21"]

        def __init__(self, qdir, market=None):
            self.qdir = qdir

        def available_dates(self, source):
            return list(self.dates)

    monkeypatch.setattr(qfr, "market_data_dir", lambda market: tmp_path)
    monkeypatch.setattr(qfr, "QuantDBFactorReader", FakeReader)
    assert mb.quantdb_latest_factor_date("CN") == date(2026, 10, 21)
    FakeReader.dates = []
    assert mb.quantdb_latest_factor_date("CN") is None


def test_quantdb_available_dates_reads_factor_source(tmp_path, monkeypatch):
    from backend.services.engine.data_platform import quantdb_factor_reader as qfr

    seen: list[str] = []

    class FakeReader:
        def __init__(self, qdir, market=None):
            pass

        def available_dates(self, source):
            seen.append(source)
            return ["2026-10-20", "2026-10-21"]

    monkeypatch.setattr(qfr, "market_data_dir", lambda market: tmp_path)
    monkeypatch.setattr(qfr, "QuantDBFactorReader", FakeReader)
    model = _model(
        tmp_path,
        metadata={"context": {"market": "hk", "factor_source": "l2_factors"}},
    )
    assert mb._quantdb_available_dates(model) == (
        "l2_factors",
        ["2026-10-20", "2026-10-21"],
    )
    assert seen == ["l2_factors"]


def test_create_redis_client_degrades_on_bad_port(monkeypatch):
    monkeypatch.setenv("QM_BACKFILL_INFERENCE_LOCK", "1")
    monkeypatch.setenv("REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("REDIS_PORT", "not-a-number")
    assert mb.create_redis_client() is None
