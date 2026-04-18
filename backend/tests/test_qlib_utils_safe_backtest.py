import pytest
import pandas as pd
import qlib.backtest as qlib_backtest

from backend.services.engine.qlib_app.utils import qlib_utils


def _bench_label(value):
    if isinstance(value, pd.Series):
        return "<series>"
    return value


def test_safe_backtest_retries_with_benchmark_alias(monkeypatch):
    calls: list[str] = []

    def fake_backtest(*args, **kwargs):
        benchmark = kwargs.get("benchmark")
        calls.append(str(_bench_label(benchmark)))
        if benchmark == "SH000300":
            raise ValueError("The benchmark ['SH000300'] does not exist. Please provide the right benchmark")
        if isinstance(benchmark, pd.Series):
            raise ValueError("The benchmark ['SH000300'] does not exist. Please provide the right benchmark")
        if benchmark == "000300.SH":
            return {"ok": True}, {"ok": True}
        raise AssertionError(f"unexpected benchmark: {benchmark}")

    monkeypatch.setattr(qlib_backtest, "backtest", fake_backtest)

    portfolio_dict, indicator_dict = qlib_utils.safe_backtest(
        start_time="2024-01-01",
        end_time="2024-01-31",
        strategy={"class": "Dummy"},
        executor={"class": "Dummy"},
        benchmark="SH000300",
    )

    assert calls == ["SH000300", "<series>", "000300.SH"]
    assert portfolio_dict == {"ok": True}
    assert indicator_dict == {"ok": True}


def test_safe_backtest_disables_benchmark_when_aliases_not_available(monkeypatch):
    calls: list[str] = []

    def fake_backtest(*args, **kwargs):
        benchmark = kwargs.get("benchmark")
        calls.append(str(_bench_label(benchmark)))
        if isinstance(benchmark, pd.Series):
            return {"ok": True}, {"ok": True}
        raise ValueError(f"The benchmark ['{benchmark}'] does not exist. Please provide the right benchmark")

    monkeypatch.setattr(qlib_backtest, "backtest", fake_backtest)

    portfolio_dict, indicator_dict = qlib_utils.safe_backtest(
        start_time="2024-01-01",
        end_time="2024-01-31",
        strategy={"class": "Dummy"},
        executor={"class": "Dummy"},
        benchmark="SH009999",
    )

    assert calls[0] == "SH009999"
    assert calls[-1] == "<series>"
    assert portfolio_dict == {"ok": True}
    assert indicator_dict == {"ok": True}


def test_safe_backtest_does_not_swallow_non_benchmark_errors(monkeypatch):
    def fake_backtest(*args, **kwargs):
        raise RuntimeError("executor init failed")

    monkeypatch.setattr(qlib_backtest, "backtest", fake_backtest)

    with pytest.raises(RuntimeError, match="executor init failed"):
        qlib_utils.safe_backtest(
            start_time="2024-01-01",
            end_time="2024-01-31",
            strategy={"class": "Dummy"},
            executor={"class": "Dummy"},
            benchmark="SH000300",
        )


def test_safe_backtest_disables_default_benchmark_when_not_passed(monkeypatch):
    calls: list[str] = []

    def fake_backtest(*args, **kwargs):
        benchmark = kwargs.get("benchmark")
        calls.append(str(_bench_label(benchmark)))
        if isinstance(benchmark, pd.Series):
            return {"ok": True}, {"ok": True}
        raise AssertionError(f"unexpected benchmark: {benchmark}")

    monkeypatch.setattr(qlib_backtest, "backtest", fake_backtest)

    portfolio_dict, indicator_dict = qlib_utils.safe_backtest(
        start_time="2024-01-01",
        end_time="2024-01-31",
        strategy={"class": "Dummy"},
        executor={"class": "Dummy"},
    )

    assert calls == ["<series>"]
    assert portfolio_dict == {"ok": True}
    assert indicator_dict == {"ok": True}
