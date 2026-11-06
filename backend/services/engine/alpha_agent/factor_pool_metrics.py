"""Shared daily rank correlation, adapted from frozen AutoAlpha pool_panels.py."""

from __future__ import annotations

import math

from backend.services.engine.alpha_agent.factor_pool_edges import (
    VALUE_CORRELATION_MIN_PAIRS_PER_DAY,
    VALUE_CORRELATION_MIN_SAMPLE_DAYS,
)


def daily_rank_corr(
    left,
    right,
    *,
    min_pairs_per_day=VALUE_CORRELATION_MIN_PAIRS_PER_DAY,
    min_sample_days=VALUE_CORRELATION_MIN_SAMPLE_DAYS,
):
    import numpy as np
    import pandas as pd

    if min_pairs_per_day < 2 or min_sample_days < 1:
        raise ValueError("Invalid daily correlation sample limits")
    merged = left[["trade_date", "symbol", "rank_pct"]].merge(
        right[["trade_date", "symbol", "rank_pct"]],
        on=["trade_date", "symbol"],
        suffixes=("_a", "_b"),
        validate="one_to_one",
    )
    for column in ("rank_pct_a", "rank_pct_b"):
        merged[column] = pd.to_numeric(merged[column], errors="coerce")
    merged = merged.replace([np.inf, -np.inf], np.nan).dropna(
        subset=["rank_pct_a", "rank_pct_b"]
    )
    if merged.empty:
        return None
    day = merged["trade_date"]
    xa, xb = merged["rank_pct_a"], merged["rank_pct_b"]
    xa_c = xa - xa.groupby(day).transform("mean")
    xb_c = xb - xb.groupby(day).transform("mean")
    cov = (xa_c * xb_c).groupby(day).sum()
    va = (xa_c**2).groupby(day).sum()
    vb = (xb_c**2).groupby(day).sum()
    n_pairs = merged.groupby("trade_date").size()
    denom = (va * vb) ** 0.5
    daily = (cov / denom.where(denom > 0)).where(n_pairs >= min_pairs_per_day)
    valid = int(daily.notna().sum())
    if valid < min_sample_days:
        return None
    value = float(daily.mean())
    return (max(-1.0, min(1.0, value)), valid) if math.isfinite(value) else None
