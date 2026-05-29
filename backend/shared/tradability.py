"""Tradability helpers shared by inference/backtest pipelines."""

from __future__ import annotations

import pandas as pd


def filter_tradable_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Filter non-tradable rows by price/volume validity.

    Rule set is intentionally minimal and deterministic:
    - `close` must be positive if present
    - `volume` must be positive if present
    """
    if df.empty:
        return df, 0

    filtered = df.copy()
    removed = 0

    if "close" in filtered.columns:
        valid_close = pd.to_numeric(filtered["close"], errors="coerce") > 0
        removed += int((~valid_close).sum())
        filtered = filtered.loc[valid_close].copy()

    if "volume" in filtered.columns:
        valid_volume = pd.to_numeric(filtered["volume"], errors="coerce") > 0
        removed += int((~valid_volume).sum())
        filtered = filtered.loc[valid_volume].copy()

    return filtered, removed
