from __future__ import annotations

import pandas as pd

import backend.services.engine.data_platform.quantdb_hub as hub_mod
from backend.shared.fundamental_aligner import FundamentalAligner


class _FakeHub:
    """对齐器读取路径的假 QuantDB 中枢：返回预置的 features_daily 行。"""

    available = True
    _inst = None

    def __init__(self, rows: pd.DataFrame):
        self._rows = rows

    @classmethod
    def get_instance(cls) -> _FakeHub:
        return cls._inst

    def fetch_latest_rows(self, view, symbols, dt=None, lookback=100, columns=None):
        if not symbols or df_empty(self._rows):
            return pd.DataFrame()
        need = ["symbol"] + [c for c in (columns or []) if c in self._rows.columns] + ["dt"]
        out = self._rows[self._rows["symbol"].isin(symbols)].copy()
        out["dt"] = dt or 0
        return out[need]


def df_empty(df: pd.DataFrame) -> bool:
    return df is None or df.empty


def test_filter_instruments_reads_features_daily_and_normalizes_symbols(monkeypatch):
    rows = pd.DataFrame(
        {
            "symbol": ["600001.SH", "000002.SZ", "600003.SH"],
            "total_mv": [3e9, 3e9, 3e9],
            "float_mv": [1e9, 1e9, 1e9],
            "pe_ttm": [20.0, -5.0, 30.0],
            "pb": [2.0, 2.0, 5.0],
            "vol_std_20": [0.03, 0.03, 0.08],
        }
    )
    fake = _FakeHub(rows)
    _FakeHub._inst = fake
    monkeypatch.setattr(hub_mod, "QuantDBDataHub", _FakeHub)

    aligner = FundamentalAligner()
    filtered = aligner.filter_instruments(
        "2026-08-21",
        ["SH600001", "SZ000002", "SH600003"],
        {
            "total_mv_min": 2e9,
            "float_mv_min": 5e8,
            "pe_ttm_min": 0,
            "pb_max": 3.5,
            "vol_std_20_max": 0.06,
        },
    )

    assert filtered == ["SH600001"]
