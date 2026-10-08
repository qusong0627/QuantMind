"""Predictions must never be dated by guessing a natural day on calendar failure."""

import exchange_calendars as xcals
import pytest

from backend.services.engine.inference.script_runner import InferenceScriptRunner


@pytest.mark.parametrize(
    ("data_day", "expected"),
    [
        ("2026-09-24", "2026-09-28"),
        ("2026-09-30", "2026-10-08"),
        ("2026-10-09", "2026-10-12"),
    ],
)
def test_china_prediction_skips_holidays_and_makeup_weekend(data_day, expected):
    assert InferenceScriptRunner._resolve_prediction_trade_date(data_day) == expected


@pytest.mark.parametrize(
    "failure", [ImportError("missing"), ValueError("out of range")]
)
def test_calendar_failure_does_not_guess_holiday(monkeypatch, failure):
    def unavailable(*args, **kwargs):
        raise failure

    monkeypatch.setattr(xcals, "get_calendar", unavailable)
    with pytest.raises(RuntimeError, match="停止推理"):
        InferenceScriptRunner._resolve_prediction_trade_date("2026-09-30")


def test_unknown_market_does_not_silently_use_china():
    with pytest.raises(RuntimeError, match="停止推理"):
        InferenceScriptRunner._resolve_prediction_trade_date("2026-09-30", "UNKNOWN")


def test_crypto_still_uses_next_natural_day():
    assert (
        InferenceScriptRunner._resolve_prediction_trade_date("2026-09-30", "CRYPTO")
        == "2026-10-01"
    )
    with pytest.raises(ValueError):
        InferenceScriptRunner._resolve_prediction_trade_date("invalid", "CRYPTO")


def test_execute_stops_before_script_or_persistence_on_calendar_failure(
    monkeypatch, tmp_path
):
    runner = InferenceScriptRunner(
        primary_model_dir=str(tmp_path), primary_model_id="model_test"
    )
    monkeypatch.setattr(
        runner, "_read_primary_metadata", lambda: {"context": {"market": "A"}}
    )

    def unavailable(*args, **kwargs):
        raise ValueError("calendar unavailable")

    def must_not_run(*args, **kwargs):
        pytest.fail("Calendar failure must stop before persistence")

    monkeypatch.setattr(xcals, "get_calendar", unavailable)
    monkeypatch.setattr(runner, "_persist_and_publish", must_not_run)
    result = runner.execute("2026-09-30")
    assert not result.success
    assert result.failure_stage == "trading_calendar"
    assert result.signals_count == 0
    assert result.prediction_trade_date == ""
    assert "停止推理" in result.error
