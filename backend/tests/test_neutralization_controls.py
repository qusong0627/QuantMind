"""控制变量快照读取测试（临时 QuantDB 目录）。"""

from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest

from backend.shared.neutralization import controls as controls_mod
from backend.shared.neutralization.controls import (
    ControlsUnavailableError,
    load_controls,
)


def _write_partition(root, day: date) -> None:
    part = root / "6_ml_datasets" / "l1_factors" / f"dt={day:%Y%m%d}"
    part.mkdir(parents=True)
    pd.DataFrame(
        {
            "symbol": ["600000.SH", "000001.SZ", "600000.SH"],
            "fun_float_mv": [100.0, 1.0, 999.0],
            "style_beta_60": [1.1, 0.9, 2.2],
            "mom_ret_20d": [0.05, -0.02, 0.30],
        }
    ).to_parquet(part / "data.parquet")


@pytest.fixture
def quantdb(tmp_path, monkeypatch):
    root = tmp_path / "quantdb"
    root.mkdir()
    _write_partition(root, date(2026, 1, 5))
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    controls_mod.clear_cache()
    yield root
    controls_mod.clear_cache()


def test_loads_prefix_symbols_and_log_mv(quantdb):
    snap = load_controls(date(2026, 1, 5))
    assert set(snap.frame.index) == {"SH600000", "SZ000001"}
    assert snap.frame.loc["SH600000", "log_mv"] == pytest.approx(math.log(100.0))
    assert snap.frame.loc["SZ000001", "log_mv"] == pytest.approx(0.0)
    assert list(snap.frame.columns) == ["log_mv", "beta60", "mom20"]


def test_duplicate_symbol_keeps_first(quantdb):
    snap = load_controls(date(2026, 1, 5))
    assert snap.frame.loc["SH600000", "beta60"] == pytest.approx(1.1)


def test_missing_partition_raises(quantdb):
    with pytest.raises(ControlsUnavailableError):
        load_controls(date(2026, 1, 6))


def test_future_date_rejected(quantdb):
    with pytest.raises(ControlsUnavailableError):
        load_controls(date(2026, 1, 5), signal_data_date=date(2026, 1, 4))
