"""Deterministic, owner-input-only factor combination research.

The module consumes already authorized private panel frames and never reads a
path or database itself. Callers must resolve exact panel versions for the job
owner before invoking it.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

MIN_FACTORS = 2
MAX_FACTORS = 12
MIN_DAYS = 20
MIN_NAMES_PER_DAY = 10
MAX_DAYS = 120
MAX_ROWS = 300_000
FRET_TOLERANCE = 1e-5
ZERO_NORM = 1e-12


@dataclass(frozen=True)
class ComboConfig:
    max_days: int = 120
    train_ratio: float = 0.7
    popsize: int = 8
    maxiter: int = 80
    tolerance: float = 1e-6
    seed: int = 42
    time_budget_seconds: float = 90.0
    cost_rate: float | None = None


class ComboOptimizationCancelled(RuntimeError):
    """The durable factor job requested a stop during optimization."""


def _validate_config(config: ComboConfig) -> None:
    if type(config.max_days) is not int or not 20 <= config.max_days <= MAX_DAYS:
        raise ValueError("max_days must be between 20 and 120")
    if type(config.popsize) is not int or not 3 <= config.popsize <= 20:
        raise ValueError("popsize must be between 3 and 20")
    if type(config.maxiter) is not int or not 1 <= config.maxiter <= 500:
        raise ValueError("maxiter must be between 1 and 500")
    if type(config.seed) is not int or not 0 <= config.seed <= 2**32 - 1:
        raise ValueError("seed is outside the supported range")
    if (
        isinstance(config.train_ratio, bool)
        or not isinstance(config.train_ratio, (int, float))
        or not math.isfinite(float(config.train_ratio))
        or not 0.5 <= float(config.train_ratio) <= 0.85
    ):
        raise ValueError("train_ratio must be between 0.5 and 0.85")
    if (
        isinstance(config.time_budget_seconds, bool)
        or not isinstance(config.time_budget_seconds, (int, float))
        or not math.isfinite(float(config.time_budget_seconds))
        or not 1 <= float(config.time_budget_seconds) <= 300
    ):
        raise ValueError("time_budget_seconds must be between 1 and 300")
    if config.cost_rate is not None and (
        isinstance(config.cost_rate, bool)
        or not isinstance(config.cost_rate, (int, float))
        or not math.isfinite(float(config.cost_rate))
        or not 0 <= float(config.cost_rate) <= 0.1
    ):
        raise ValueError("cost_rate must be finite and between 0 and 0.1")


def build_dataset(
    frames: Mapping[str, pd.DataFrame],
    factor_ids: Sequence[str],
    *,
    max_days: int = 120,
) -> pd.DataFrame:
    """Join immutable factor panels by date/symbol and validate label agreement."""
    order = list(factor_ids)
    if not MIN_FACTORS <= len(order) <= MAX_FACTORS:
        raise ValueError(f"Combination requires 2 to {MAX_FACTORS} factors")
    if any(not isinstance(value, str) or not value for value in order):
        raise ValueError("Factor IDs must be non-empty strings")
    if len(set(order)) != len(order):
        raise ValueError("Factor IDs must be unique")
    if type(max_days) is not int or not 20 <= max_days <= MAX_DAYS:
        raise ValueError("max_days must be between 20 and 120")

    merged: pd.DataFrame | None = None
    for index, factor_id in enumerate(order):
        source = frames.get(factor_id)
        required = {"trade_date", "symbol", "zscore", "forward_return"}
        if not isinstance(source, pd.DataFrame) or not required.issubset(
            source.columns
        ):
            raise ValueError(f"Factor {factor_id} lacks a composable return panel")
        part = source[["trade_date", "symbol", "zscore", "forward_return"]].copy()
        part["trade_date"] = pd.to_datetime(
            part["trade_date"], errors="coerce"
        ).dt.strftime("%Y-%m-%d")
        part["zscore"] = pd.to_numeric(part["zscore"], errors="coerce")
        part["forward_return"] = pd.to_numeric(part["forward_return"], errors="coerce")
        part = part.replace([np.inf, -np.inf], np.nan).dropna(
            subset=["trade_date", "symbol", "zscore", "forward_return"]
        )
        part["symbol"] = part["symbol"].astype(str)
        part = part.drop_duplicates(["trade_date", "symbol"], keep="last")
        part = part.rename(
            columns={
                "zscore": f"z{index}",
                "forward_return": f"return{index}",
            }
        )
        merged = (
            part
            if merged is None
            else merged.merge(
                part,
                on=["trade_date", "symbol"],
                how="inner",
                validate="one_to_one",
            )
        )
        if merged.empty:
            raise ValueError("Factor panels have no overlapping date/symbol rows")

    assert merged is not None
    returns = merged[[f"return{i}" for i in range(len(order))]].to_numpy(
        dtype="float64"
    )
    if (
        len(order) > 1
        and float(np.abs(returns - returns[:, [0]]).max()) > FRET_TOLERANCE
    ):
        raise ValueError("Factor panels use inconsistent forward-return labels")
    merged["forward_return"] = returns[:, 0]
    days = sorted(merged["trade_date"].unique())
    if len(days) > max_days:
        positions = np.unique(
            np.linspace(0, len(days) - 1, max_days).round().astype(int)
        )
        days = [days[position] for position in positions]
    merged = merged[merged["trade_date"].isin(set(days))]

    # Bound the matrix passed to the optimizer while preserving deterministic
    # date coverage and symbol order for broad A-share universes.
    per_day_cap = max(1, MAX_ROWS // max(1, len(days)))
    capped: list[pd.DataFrame] = []
    for _, day in merged.groupby("trade_date", sort=True):
        day = day.sort_values("symbol", kind="stable")
        if len(day) > per_day_cap:
            positions = np.unique(np.linspace(0, len(day) - 1, per_day_cap).astype(int))
            day = day.iloc[positions]
        capped.append(day)
    merged = pd.concat(capped, ignore_index=True)
    per_day = merged.groupby("trade_date")["symbol"].nunique()
    merged = merged[
        merged["trade_date"].isin(per_day[per_day >= MIN_NAMES_PER_DAY].index)
    ]
    if merged["trade_date"].nunique() < MIN_DAYS:
        raise ValueError("At least 20 overlapping trading days are required")
    z_cols = [f"z{i}" for i in range(len(order))]
    return (
        merged[["trade_date", "symbol", *z_cols, "forward_return"]]
        .sort_values(["trade_date", "symbol"], kind="stable")
        .reset_index(drop=True)
    )


def _rank_ic_by_day(frame: pd.DataFrame, values: np.ndarray) -> pd.Series:
    pair = pd.DataFrame(
        {
            "trade_date": frame["trade_date"].to_numpy(),
            "factor": values,
            "return": frame["forward_return"].to_numpy(dtype="float64"),
        }
    )
    factor_rank = pair.groupby("trade_date")["factor"].rank(method="average")
    return_rank = pair.groupby("trade_date")["return"].rank(method="average")
    pair["factor_centered"] = factor_rank - factor_rank.groupby(
        pair["trade_date"]
    ).transform("mean")
    pair["return_centered"] = return_rank - return_rank.groupby(
        pair["trade_date"]
    ).transform("mean")
    covariance = (
        (pair["factor_centered"] * pair["return_centered"])
        .groupby(pair["trade_date"])
        .sum()
    )
    factor_variance = (pair["factor_centered"] ** 2).groupby(pair["trade_date"]).sum()
    return_variance = (pair["return_centered"] ** 2).groupby(pair["trade_date"]).sum()
    correlation = covariance / np.sqrt(factor_variance * return_variance)
    return correlation.replace([np.inf, -np.inf], np.nan).dropna()


def _window_metrics(
    frame: pd.DataFrame, weights: np.ndarray, cost_rate: float
) -> dict[str, Any]:
    values = (
        frame[[f"z{i}" for i in range(len(weights))]].to_numpy(dtype="float64")
        @ weights
    )
    ic = _rank_ic_by_day(frame, values)
    pair = frame[["trade_date", "symbol", "forward_return"]].copy()
    pair["factor"] = values
    pair = pair.sort_values(["trade_date", "symbol"], kind="stable")
    pair["rank_pct"] = pair.groupby("trade_date")["factor"].rank(pct=True)
    daily_gross = (
        pair[pair["rank_pct"] >= 0.7].groupby("trade_date")["forward_return"].mean()
    )
    long_names = {
        day: frozenset(group.loc[group["rank_pct"] >= 0.7, "symbol"])
        for day, group in pair.groupby("trade_date", sort=True)
        if group["symbol"].nunique() >= MIN_NAMES_PER_DAY
    }
    turnover: dict[str, float] = {}
    previous: frozenset[str] | None = None
    for day in sorted(long_names):
        current = long_names[day]
        turnover[day] = (
            0.0
            if previous is None or not current
            else len(current - previous) / len(current)
        )
        previous = current
    cost = pd.Series(turnover, dtype="float64").reindex(daily_gross.index).fillna(0.0)
    net = daily_gross - cost * cost_rate
    equity = (1.0 + net).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    vol = float(net.std(ddof=1)) if len(net) > 1 else 0.0
    return {
        "mean_rank_ic": float(ic.mean()) if len(ic) else None,
        "rank_icir": (
            float(ic.mean() / ic.std(ddof=1))
            if len(ic) > 1 and float(ic.std(ddof=1)) > 0
            else None
        ),
        "annual_return": float(net.mean() * 252) if len(net) else None,
        "sharpe_ratio": float(net.mean() / (vol + 1e-8) * np.sqrt(252))
        if len(net) > 1
        else None,
        "max_drawdown": float(drawdown.min()) if len(drawdown) else None,
        "n_days": int(len(net)),
        "n_obs": int(len(frame)),
        "equity_curve": [
            {"date": str(day), "value": float(value)}
            for day, value in equity.iloc[
                np.unique(
                    np.linspace(
                        0, max(0, len(equity) - 1), min(750, len(equity))
                    ).astype(int)
                )
            ].items()
        ],
    }


def optimize_combo(
    frames: Mapping[str, pd.DataFrame],
    factor_ids: Sequence[str],
    config: ComboConfig = ComboConfig(),
    *,
    should_cancel: Callable[[], bool] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Optimize signed L1-normalized weights on a chronological train window."""
    _validate_config(config)
    from scipy.optimize import differential_evolution

    dataset = build_dataset(frames, factor_ids, max_days=config.max_days)
    days = sorted(dataset["trade_date"].unique())
    train_count = max(1, min(len(days) - 1, int(round(len(days) * config.train_ratio))))
    train_days, valid_days = set(days[:train_count]), set(days[train_count:])
    train = dataset[dataset["trade_date"].isin(train_days)]
    valid = dataset[dataset["trade_date"].isin(valid_days)]
    if not len(valid_days):
        raise ValueError("Validation window is empty")
    if config.cost_rate is None:
        from backend.services.engine.inference.trading_cost import CostModel

        cost_rate = CostModel.resolve().round_trip_cost()
    else:
        cost_rate = float(config.cost_rate)

    z = train[[f"z{i}" for i in range(len(factor_ids))]].to_numpy(dtype="float64")
    train_frame = train.reset_index(drop=True)
    deadline = time.monotonic() + float(config.time_budget_seconds)
    best: dict[str, Any] = {}

    def objective(raw: np.ndarray) -> float:
        if should_cancel is not None and should_cancel():
            raise ComboOptimizationCancelled("Factor combination was cancelled")
        if time.monotonic() >= deadline:
            raise StopIteration
        norm = float(np.abs(raw).sum())
        if norm < ZERO_NORM:
            return 1.0
        values = z @ (raw / norm)
        ic = _rank_ic_by_day(train_frame, values)
        mean = float(ic.mean()) if len(ic) else float("nan")
        score = -mean if math.isfinite(mean) else 1.0
        if score < best.get("score", math.inf):
            best["score"] = score
            best["x"] = np.asarray(raw, dtype="float64").copy()
        return score

    def callback(intermediate_result: Any = None) -> bool:
        if should_cancel is not None and should_cancel():
            raise ComboOptimizationCancelled("Factor combination was cancelled")
        iteration = getattr(intermediate_result, "nit", 0)
        if progress is not None:
            progress(
                {
                    "stage": "optimize_weights",
                    "iteration": int(iteration or 0),
                    "progress_pct": min(
                        90,
                        10 + int(80 * min(1.0, float(iteration or 0) / config.maxiter)),
                    ),
                }
            )
        if time.monotonic() >= deadline:
            raise StopIteration
        return False

    converged = False
    evaluations: int | None = None
    try:
        result = differential_evolution(
            objective,
            [(-1.0, 1.0)] * len(factor_ids),
            seed=config.seed,
            popsize=config.popsize,
            maxiter=config.maxiter,
            tol=config.tolerance,
            polish=True,
            workers=1,
            updating="deferred",
            callback=callback,
        )
        raw = np.asarray(result.x, dtype="float64")
        converged = bool(result.success)
        evaluations = int(result.nfev) or None
    except StopIteration:
        if "x" not in best:
            raise RuntimeError(
                "Optimizer budget ended before a candidate was produced"
            ) from None
        raw = best["x"]
    norm = float(np.abs(raw).sum())
    if norm < ZERO_NORM:
        raise RuntimeError("Optimizer produced zero weights")
    weights = raw / norm
    result = {
        "factor_ids": list(factor_ids),
        "weights": {
            factor_id: float(weight)
            for factor_id, weight in zip(factor_ids, weights, strict=True)
        },
        "train_window": {"start_date": min(train_days), "end_date": max(train_days)},
        "validation_window": {
            "start_date": min(valid_days),
            "end_date": max(valid_days),
        },
        "train_metrics": _window_metrics(train, weights, cost_rate),
        "validation_metrics": _window_metrics(valid, weights, cost_rate),
        "config": {
            **asdict(config),
            "cost_rate": cost_rate,
            "converged": converged,
            "n_evaluations": evaluations,
        },
    }
    if progress is not None:
        progress({"stage": "result_persist", "progress_pct": 95})
    return result
