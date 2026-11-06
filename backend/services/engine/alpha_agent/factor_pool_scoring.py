"""Deterministic pool retrieval scoring, adapted for private owner scopes.

The calculation is a pure port of AutoAlpha's pool scoring contract. It has no
database or filesystem access; callers must load and persist rows for one
authenticated tenant/user scope.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class PoolCandidate:
    factor_id: str
    icir: float | None
    times_retrieved: int = 0
    max_pool_corr: float | None = None
    age_days: float | None = None


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: PoolCandidate
    q_norm: float
    score: float


def icir_percentiles(values: list[float | None]) -> list[float | None]:
    """Return average-rank percentiles; missing and non-finite values stay None."""
    present = [
        (index, float(value))
        for index, value in enumerate(values)
        if value is not None and math.isfinite(float(value))
    ]
    output: list[float | None] = [None] * len(values)
    if not present:
        return output
    ordered = sorted(present, key=lambda item: (item[1], item[0]))
    count = len(ordered)
    start = 0
    while start < count:
        end = start
        while end + 1 < count and ordered[end + 1][1] == ordered[start][1]:
            end += 1
        average_rank = (start + end + 2) / 2.0
        percentile = average_rank / count
        for position in range(start, end + 1):
            output[ordered[position][0]] = percentile
        start = end + 1
    return output


def _candidate_score(candidate: PoolCandidate, q_norm: float) -> float:
    fatigue = 1.0 / (1.0 + 0.5 * max(0, int(candidate.times_retrieved)))
    corr = candidate.max_pool_corr
    redundancy = 1.0 - 0.5 * max(0.0, min(1.0, float(corr))) if corr is not None else 1.0
    age = max(0.0, float(candidate.age_days)) if candidate.age_days is not None else 0.0
    freshness = 0.5 ** (age / 90.0)
    return float(q_norm) * fatigue * redundancy * freshness


def rank_candidates(candidates: Iterable[PoolCandidate]) -> list[ScoredCandidate]:
    """Score one universe independently, excluding factors without finite ICIR."""
    items = list(candidates)
    percentiles = icir_percentiles([item.icir for item in items])
    scored = [
        ScoredCandidate(candidate, float(q_norm), _candidate_score(candidate, q_norm))
        for candidate, q_norm in zip(items, percentiles, strict=True)
        if q_norm is not None
    ]
    return sorted(scored, key=lambda item: (-item.score, item.candidate.factor_id))
