"""Versioned replacement for AutoAlpha's unavailable factor_quality dependency.

This is deliberately not presented as a copy of the missing implementation.
For the positive semidefinite daily correlation mean C, use its eigenvalues
(which equal its singular values) in Roy & Vetterli's effective rank:
p = lambda / sum(lambda), H = -sum(p log(p)), effective_factors = exp(H).
Reference: https://doi.org/10.5281/zenodo.40328 (2007), definition 1.
The complete-case daily construction and leave-one convention are QuantMind
decisions. No quality/PFS formula is inferred from an unavailable source.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .factor_pool_orthogonality import OrthogonalityCancelled

METHOD = "daily_complete_case_correlation_erank_v1"
REFERENCE = "https://doi.org/10.5281/zenodo.40328"
CRITERIA = {
    "min_stocks_per_day": 30,
    "min_sample_days": 20,
    "max_factors": 100,
    "max_total_rows": 2_000_000,
    "psd_tolerance": 1e-10,
    "contribution_zero_tolerance": 1e-10,
}


def _spectrum(matrix):
    """Only tolerate floating point roundoff; never repair a non-PSD matrix."""
    if not np.isfinite(matrix).all() or not np.allclose(
        matrix, matrix.T, atol=CRITERIA["psd_tolerance"], rtol=0
    ):
        raise ValueError("Invalid symmetric correlation matrix")
    values = np.linalg.eigvalsh(matrix)
    tolerance = CRITERIA["psd_tolerance"] * max(1.0, float(np.max(np.abs(values))))
    if not np.isfinite(values).all() or float(values.min()) < -tolerance:
        raise ValueError("Non-positive-semidefinite correlation matrix")
    values = np.maximum(values, 0.0)
    total = float(values.sum())
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Zero correlation spectrum")
    probabilities = values[values > 0] / total
    entropy = max(0.0, float(-np.sum(probabilities * np.log(probabilities))))
    return entropy, min(float(len(matrix)), max(1.0, math.exp(entropy))), values


def calculate_diversity(factor_ids, frames, *, should_cancel=None):
    """Use every frozen member and the same rows/days for all leave-one scores.

    A missing member makes the whole-pool metric unavailable. We never silently
    drop factors, fill a missing correlation with zero, or sample panel rows.
    Each day uses the intersection of finite observations of *all* factors and
    gets equal weight. Days with a constant member are excluded for everyone.
    Removing a column uses a principal submatrix, never a wider observation set.
    Effective rank is not monotone under column removal: preserve signed deltas.
    """
    ids = list(factor_ids)
    base = {
        "method": METHOD,
        "implementation_origin": "quantmind_reimplementation_source_dependency_missing",
        "reference": REFERENCE,
        "matrix_basis": "equal_day_mean_pearson_zscore_all_members_complete_case",
        "criteria": dict(CRITERIA),
        "factor_ids": ids,
        "factor_count": len(ids),
        "entropy": None,
        "normalized_entropy": None,
        "effective_factors": None,
        "contributions": {},
        "n_days": 0,
        "n_obs": 0,
    }

    def checkpoint():
        if should_cancel is not None and should_cancel():
            raise OrthogonalityCancelled("池多样性计算已停止")

    def unavailable(reason, **extra):
        return {**base, **extra, "status": "unavailable", "reason": reason}

    checkpoint()
    if not 2 <= len(ids) <= CRITERIA["max_factors"] or len(set(ids)) != len(ids):
        return unavailable("requires_2_to_100_unique_members")
    missing = [value for value in ids if value not in frames or frames[value].empty]
    if missing:
        return unavailable("incomplete_member_panels", missing_factor_ids=missing)
    series, total_rows = [], 0
    for factor_id in ids:
        checkpoint()
        frame = frames[factor_id]
        total_rows += len(frame)
        if total_rows > CRITERIA["max_total_rows"]:
            return unavailable("panel_row_limit_exceeded")
        if not {"trade_date", "symbol", "zscore"}.issubset(frame.columns):
            return unavailable("bad_panel_columns")
        if frame.duplicated(["trade_date", "symbol"]).any():
            return unavailable("duplicate_panel_observations")
        values = (
            frame[["trade_date", "symbol", "zscore"]]
            .dropna(subset=["trade_date", "symbol"])
            .copy()
        )
        values["zscore"] = pd.to_numeric(values["zscore"], errors="coerce").replace(
            [np.inf, -np.inf], np.nan
        )
        series.append(
            values.set_index(["trade_date", "symbol"])["zscore"].rename(factor_id)
        )
    checkpoint()
    common = pd.concat(series, axis=1, join="inner").dropna().sort_index()
    checkpoint()
    if common.empty:
        return unavailable("no_complete_case_observations")
    gram = np.zeros((len(ids), len(ids)), dtype=np.float64)
    days, observations = [], 0
    insufficient_stocks = constant_member = 0
    for day, rows in common.groupby(level="trade_date", sort=True):
        checkpoint()
        if len(rows) < CRITERIA["min_stocks_per_day"]:
            insufficient_stocks += 1
            continue
        values = rows.to_numpy(dtype=np.float64)
        centered = values - values.mean(axis=0)
        norms = np.linalg.norm(centered, axis=0)
        if not np.isfinite(norms).all() or bool((norms <= 0).any()):
            constant_member += 1
            continue
        unit = centered / norms
        daily = unit.T @ unit
        if not np.isfinite(daily).all():
            return unavailable("nonfinite_daily_correlation")
        gram += daily
        days.append(str(day))
        observations += len(rows)
    base.update(
        n_days=len(days),
        n_obs=observations,
        complete_case_days=int(common.index.get_level_values("trade_date").nunique()),
        excluded_days={
            "insufficient_stocks": insufficient_stocks,
            "constant_member": constant_member,
        },
        start_date=days[0] if days else None,
        end_date=days[-1] if days else None,
    )
    if len(days) < CRITERIA["min_sample_days"]:
        return unavailable("insufficient_common_days")
    checkpoint()
    matrix = gram / len(days)
    try:
        entropy, effective, eigenvalues = _spectrum(matrix)
        contributions = {}
        for index, factor_id in enumerate(ids):
            checkpoint()
            keep = [i for i in range(len(ids)) if i != index]
            _, without, _ = _spectrum(matrix[np.ix_(keep, keep)])
            delta = effective - without
            # Roundoff-only zeroing; a genuinely negative contribution stays negative.
            contributions[factor_id] = {
                "effective_without": without,
                "delta": 0.0
                if abs(delta) <= CRITERIA["contribution_zero_tolerance"]
                else delta,
            }
    except (ValueError, np.linalg.LinAlgError, FloatingPointError, OverflowError):
        return unavailable("invalid_correlation_spectrum")
    checkpoint()
    return {
        **base,
        "status": "computed",
        "reason": None,
        "entropy": entropy,
        "normalized_entropy": min(1.0, entropy / math.log(len(ids))),
        "effective_factors": effective,
        "eigenvalues": eigenvalues.tolist(),
        "contributions": contributions,
    }
