from pathlib import Path

import pandas as pd
import pytest

from backend.services.engine.qlib_app.utils.simple_signal import SimpleSignal


def test_simple_signal_loads_first_column_from_qlib_instrument_file(tmp_path: Path):
    instrument_file = tmp_path / "margin.txt"
    instrument_file.write_text(
        "SH600000\t2005-01-01\t2099-12-31\nSZ000001\t2005-01-01\t2099-12-31\n",
        encoding="utf-8",
    )

    signal = SimpleSignal(universe=str(instrument_file))

    assert signal._load_instruments_from_file(instrument_file) == ["SH600000", "SZ000001"]


def test_simple_signal_lags_pred_series_to_next_available_date(tmp_path: Path):
    pred_path = tmp_path / "pred.pkl"
    idx = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2025-01-02"), "SH600000"),
            (pd.Timestamp("2025-01-03"), "SH600000"),
        ],
        names=["datetime", "instrument"],
    )
    pd.DataFrame({"score": [0.1, 0.2]}, index=idx).to_pickle(pred_path)

    signal = SimpleSignal(universe="all", pred_path=str(pred_path), signal_lag_days=1)
    series = signal._load_pred_series(pd.Timestamp("2025-01-03"), pd.Timestamp("2025-01-03"))

    assert series is not None
    assert series.loc[(pd.Timestamp("2025-01-03"), "SH600000")] == 0.1


def test_simple_signal_with_pred_path_returns_empty_when_slice_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pred_path = tmp_path / "pred.pkl"
    idx = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2025-01-02"), "SH600000"),
            (pd.Timestamp("2025-01-03"), "SH600000"),
        ],
        names=["datetime", "instrument"],
    )
    pd.DataFrame({"score": [0.1, 0.2]}, index=idx).to_pickle(pred_path)

    signal = SimpleSignal(universe="all", pred_path=str(pred_path), signal_lag_days=1)
    monkeypatch.setattr(
        signal,
        "_get_universe_instruments",
        lambda: (_ for _ in ()).throw(AssertionError("pred-backed signal should not fallback to feature loading")),
    )
    result = signal.get_signal(pd.Timestamp("2025-01-06"), pd.Timestamp("2025-01-06"))

    assert result.empty
