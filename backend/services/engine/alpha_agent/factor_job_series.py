"""Bounded multi-series payloads for owner-scoped factor backtest results."""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.shared.factor_lifecycle import MAX_LIFECYCLE_POINTS


def _json_values(series: pd.Series, positions: np.ndarray) -> list[float | None]:
    values: list[float | None] = []
    for position in positions:
        value = float(series.iloc[position])
        values.append(value if np.isfinite(value) else None)
    return values


def build_factor_series(
    factors: pd.Series,
    forward_returns: pd.Series,
    *,
    cost_rate: float,
    max_points: int = 750,
) -> dict:
    """Build gross/net portfolio, IC, quantile and turnover series.

    Inputs use a two-level ``(date, symbol)`` index. Costs follow the statistical
    mining rule: round-trip cost is charged on the fraction of the current long
    book that entered since the previous eligible rebalance date.
    """
    if not isinstance(max_points, int) or not 2 <= max_points <= 750:
        raise ValueError("max_points must be an integer between 2 and 750")
    if not np.isfinite(cost_rate) or cost_rate < 0:
        raise ValueError("cost_rate must be a finite non-negative number")
    if (
        not isinstance(factors.index, pd.MultiIndex)
        or factors.index.nlevels != 2
        or not isinstance(forward_returns.index, pd.MultiIndex)
        or forward_returns.index.nlevels != 2
    ):
        raise ValueError("Factor series require a two-level date/symbol index")

    from backend.services.engine.alpha_agent.statistical.costs import (
        daily_turnover_long,
    )
    from backend.services.engine.alpha_agent.statistical.ic_stats import daily_ic

    frame = pd.concat(
        {"factor": factors.astype(float), "return": forward_returns.astype(float)},
        axis=1,
    )
    finite = np.isfinite(frame["factor"]) & np.isfinite(frame["return"])
    frame = frame.loc[finite].sort_index()
    counts = frame.groupby(level=0).size()
    eligible = counts[counts >= 10].index
    frame = frame.loc[frame.index.get_level_values(0).isin(eligible)]
    empty: dict = {
        "dates": [],
        "nav_long": [],
        "nav_long_gross": [],
        "nav_bench": [],
        "nav_excess_pct": [],
        "drawdown": [],
        "ic": [],
        "rank_ic": [],
        "turnover": [],
        "coverage": [],
        "q_curves": {f"q{i}": [] for i in range(1, 6)},
        "basis": "equal_weight_top_30pct_net_next_day_returns",
        "cost_rate": float(cost_rate),
        "turnover_basis": "new_names_fraction_of_current_long_book",
    }
    if frame.empty:
        return empty

    rank = frame.groupby(level=0)["factor"].rank(pct=True, method="average")
    frame["rank"] = rank
    long_members = frame["rank"] >= 0.7
    short_members = frame["rank"] <= 0.3
    daily_gross = frame.loc[long_members].groupby(level=0)["return"].mean()
    daily_bench = frame.groupby(level=0)["return"].mean().reindex(daily_gross.index)
    daily_short = frame.loc[short_members].groupby(level=0)["return"].mean()
    turnover = daily_turnover_long(factors.reindex(frame.index), min_names=10).reindex(
        daily_gross.index
    )
    daily_net = daily_gross - turnover.fillna(0.0) * cost_rate
    nav_net = (1.0 + daily_net).cumprod()
    nav_gross = (1.0 + daily_gross).cumprod()
    nav_bench = (1.0 + daily_bench).cumprod()
    nav_excess = (nav_net / nav_bench - 1.0) * 100.0
    drawdown = nav_net / nav_net.cummax() - 1.0
    daily_ic_frame = daily_ic(frame["factor"], frame["return"], min_names=10).reindex(
        daily_gross.index
    )
    q = np.minimum((rank * 5).astype(int) + 1, 5)
    frame["bucket"] = q
    bucket_returns = frame.groupby([frame.index.get_level_values(0), "bucket"])[
        "return"
    ].mean()
    bucket_returns.index.names = [frame.index.names[0], "bucket"]
    bucket_navs = {
        f"q{i}": (
            1.0
            + bucket_returns.xs(i, level="bucket")
            .reindex(daily_gross.index)
            .fillna(0.0)
        ).cumprod()
        for i in range(1, 6)
    }

    full_dates = daily_gross.index
    positions = np.arange(len(full_dates))
    if len(positions) > max_points:
        positions = np.unique(
            np.linspace(0, len(positions) - 1, max_points).astype(int)
        )
    draw = {
        "dates": [
            day.date().isoformat() if hasattr(day, "date") else str(day)[:10]
            for day in full_dates[positions]
        ],
        "nav_long": _json_values(nav_net, positions),
        "nav_long_gross": _json_values(nav_gross, positions),
        "nav_bench": _json_values(nav_bench, positions),
        "nav_excess_pct": _json_values(nav_excess, positions),
        "drawdown": _json_values(drawdown, positions),
        "ic": _json_values(daily_ic_frame["ic"], positions),
        "rank_ic": _json_values(daily_ic_frame["rank_ic"], positions),
        "turnover": _json_values(turnover.fillna(0.0), positions),
        "coverage": [int(counts.get(day, 0)) for day in full_dates[positions]],
        "q_curves": {
            key: _json_values(values, positions) for key, values in bucket_navs.items()
        },
        "basis": "equal_weight_top_30pct_net_next_day_returns",
        "cost_rate": float(cost_rate),
        "turnover_basis": "new_names_fraction_of_current_long_book",
    }
    # Lifecycle decisions need an exact daily IC tail. The chart series above is
    # downsampled for bounded UI payloads and must never be used for classification.
    lifecycle_positions = np.arange(
        max(0, len(full_dates) - MAX_LIFECYCLE_POINTS), len(full_dates)
    )
    draw["lifecycle_ic_series"] = {
        "dates": [
            day.date().isoformat() if hasattr(day, "date") else str(day)[:10]
            for day in full_dates[lifecycle_positions]
        ],
        "ic": _json_values(daily_ic_frame["ic"], lifecycle_positions),
    }
    # Retain a commonly inspected spread series without representing it as a
    # tradable short portfolio in the cost model.
    if len(daily_short):
        nav_ls = (1.0 + daily_gross - daily_short.reindex(daily_gross.index)).cumprod()
        draw["nav_long_short_gross"] = _json_values(nav_ls, positions)
    return draw
