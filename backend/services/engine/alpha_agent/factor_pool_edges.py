"""Deterministic formula-similarity edges for private factor pools."""

from __future__ import annotations

import re
from collections.abc import Sequence

from backend.shared.factor_identity import normalize_formula

_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z_0-9]*|\d+(?:\.\d+)?")
DEFAULT_THRESHOLD = 0.6
DEFAULT_TOP_K = 3
VALUE_CORRELATION_THRESHOLD = 0.5
VALUE_CORRELATION_MIN_PAIRS_PER_DAY = 20
VALUE_CORRELATION_MIN_SAMPLE_DAYS = 5
VALUE_CORRELATION_MAX_OBSERVATIONS = 20_000


def formula_tokens(formula: str | None) -> frozenset[str]:
    normalized = normalize_formula(formula)
    return frozenset(_TOKEN_RE.findall(normalized)) if normalized else frozenset()


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def similar_pairs(
    items: Sequence[tuple[str, frozenset[str]]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    top_k: int = DEFAULT_TOP_K,
) -> list[tuple[str, str, float]]:
    if not 0.0 <= threshold <= 1.0 or top_k < 0:
        raise ValueError("Invalid formula similarity bounds")
    if not items or top_k == 0:
        return []
    neighbors: dict[str, list[tuple[float, str]]] = {
        factor_id: [] for factor_id, _ in items
    }
    for index, (left_id, left_tokens) in enumerate(items):
        for right_id, right_tokens in items[index + 1 :]:
            score = jaccard(left_tokens, right_tokens)
            if score >= threshold and score > 0.0:
                neighbors[left_id].append((score, right_id))
                neighbors[right_id].append((score, left_id))
    edges: set[tuple[str, str, float]] = set()
    for factor_id, candidates in neighbors.items():
        candidates.sort(key=lambda item: (-item[0], item[1]))
        for score, other_id in candidates[:top_k]:
            source, target = sorted((factor_id, other_id))
            edges.add((source, target, score))
    return sorted(edges)


def value_correlated_pairs(
    items: Sequence[tuple[str, object]],
    *,
    threshold: float = VALUE_CORRELATION_THRESHOLD,
    top_k: int = DEFAULT_TOP_K,
    min_pairs_per_day: int = VALUE_CORRELATION_MIN_PAIRS_PER_DAY,
    min_sample_days: int = VALUE_CORRELATION_MIN_SAMPLE_DAYS,
    max_observations: int = VALUE_CORRELATION_MAX_OBSERVATIONS,
) -> list[tuple[str, str, float, float]]:
    """Find bounded, daily cross-sectional rank correlations on panel overlap."""
    import numpy as np
    import pandas as pd

    if (
        not 0.0 <= threshold <= 1.0
        or top_k < 0
        or min_pairs_per_day < 2
        or min_sample_days < 1
        or max_observations < min_pairs_per_day * min_sample_days
    ):
        raise ValueError("Invalid value correlation bounds")
    if not items or top_k == 0:
        return []

    neighbors: dict[str, list[tuple[float, str, float]]] = {
        factor_id: [] for factor_id, _series in items
    }
    for index, (left_id, left) in enumerate(items):
        if not isinstance(left, pd.Series) or not isinstance(left.index, pd.MultiIndex):
            continue
        for right_id, right in items[index + 1 :]:
            if not isinstance(right, pd.Series) or not isinstance(
                right.index, pd.MultiIndex
            ):
                continue
            aligned = pd.concat(
                [left.rename("left"), right.rename("right")], axis=1, join="inner"
            ).dropna()
            if len(aligned) > max_observations:
                positions = np.unique(
                    np.linspace(0, len(aligned) - 1, max_observations).astype(int)
                )
                aligned = aligned.iloc[positions]
            from backend.services.engine.alpha_agent.factor_pool_metrics import (
                daily_rank_corr,
            )

            common = {
                "trade_date": aligned.index.get_level_values(0),
                "symbol": aligned.index.get_level_values(1),
            }
            measured = daily_rank_corr(
                pd.DataFrame({**common, "rank_pct": aligned["left"].to_numpy()}),
                pd.DataFrame({**common, "rank_pct": aligned["right"].to_numpy()}),
                min_pairs_per_day=min_pairs_per_day,
                min_sample_days=min_sample_days,
            )
            if measured is None:
                continue
            coefficient = measured[0]
            if not np.isfinite(coefficient) or abs(coefficient) < threshold:
                continue
            score = abs(coefficient)
            neighbors[left_id].append((score, right_id, coefficient))
            neighbors[right_id].append((score, left_id, coefficient))

    edges: dict[tuple[str, str], tuple[float, float]] = {}
    for factor_id, candidates in neighbors.items():
        candidates.sort(key=lambda item: (-item[0], item[1]))
        for score, other_id, coefficient in candidates[:top_k]:
            source, target = sorted((factor_id, other_id))
            edges[(source, target)] = (score, coefficient)
    return [
        (source, target, score, coefficient)
        for (source, target), (score, coefficient) in sorted(edges.items())
    ]
