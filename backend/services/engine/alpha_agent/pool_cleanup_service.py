"""Read-only cleanup evidence for one authenticated owner's complete pool scopes."""

from __future__ import annotations

import asyncio
import math
from collections import Counter, defaultdict
from dataclasses import asdict

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.owner_scope import OwnerScope
from backend.shared.utc_datetime import to_utc_iso
from backend.services.engine.alpha_agent.factor_pool_cleanup import (
    CleanupCandidate,
    CleanupCriteria,
    evaluate_cleanup,
)
from backend.services.engine.alpha_agent.factor_pool_evidence import (
    current_correlations,
)

MAX_MEMBERS = 5000


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _assemble(
    rows,
    correlations,
    *,
    diversities,
    diversity_sources,
    archived_count,
    as_of,
    limit,
    offset,
):
    groups = defaultdict(list)
    missing = Counter()
    for row in rows:
        universe = row["universe"]
        corr, other = correlations.get((universe, row["factor_id"]), (None, None))
        candidate = CleanupCandidate(
            factor_id=row["factor_id"],
            factor_name=row["factor_name"] or row["factor_id"],
            icir=_number(row["icir"]),
            pool_score=_number(row["pool_score"]),
            max_pool_corr=corr,
            max_pool_corr_with=other,
            diversity_contrib=_number(diversities.get((universe, row["factor_id"]))),
        )
        for field in ("icir", "pool_score", "max_pool_corr", "diversity_contrib"):
            if getattr(candidate, field) is None:
                missing[field] += 1
        groups[universe].append(candidate)
    criteria = CleanupCriteria()
    items, scopes = [], []
    summary = Counter()
    for universe, candidates in sorted(groups.items()):
        report = evaluate_cleanup(candidates, criteria)
        scopes.append(
            {
                "universe": universe,
                "pool_size": len(candidates),
                "icir_sample_size": report.icir_sample_size,
                "weak_icir_threshold": report.weak_icir_threshold,
                "diversity_source": diversity_sources.get(universe),
            }
        )
        for suggestion in report.suggestions:
            summary.update(reason.code for reason in suggestion.reasons)
            items.append(
                {
                    **asdict(suggestion.candidate),
                    "universe": universe,
                    "severity": suggestion.severity,
                    "diversity_source": diversity_sources.get(universe),
                    "reasons": [asdict(reason) for reason in suggestion.reasons],
                }
            )
    items.sort(
        key=lambda row: (row["severity"] != "high", row["universe"], row["factor_id"])
    )
    return {
        "items": items[offset : offset + limit],
        "total": len(items),
        "pool_size": len(rows),
        "archived_member_count": archived_count,
        "summary": dict(summary),
        "criteria": asdict(criteria),
        "scopes": scopes,
        "missing_evidence": {
            key: missing[key]
            for key in ("icir", "pool_score", "max_pool_corr", "diversity_contrib")
        },
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < len(items),
        "as_of": to_utc_iso(as_of),
    }


async def cleanup_suggestions(
    *, tenant_id: str, user_id: str, universe: str | None, limit: int, offset: int
):
    owner = OwnerScope(tenant_id, user_id)
    if universe is not None and (not isinstance(universe, str) or len(universe) > 128):
        raise ValueError("无效股票池")
    if not 1 <= limit <= 100 or not 0 <= offset <= 1000000:
        raise ValueError("无效建议分页参数")
    scope = " AND p.universe=:universe" if universe is not None else ""
    params = {**owner.sql_params(), "universe": universe, "limit": MAX_MEMBERS + 1}
    # Use the primary and one read-only repeatable snapshot: a just-removed member
    # must not reappear because a replica or the second SELECT is behind.
    async with get_session() as session:
        await session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        )
        await session.execute(text("SET LOCAL statement_timeout = '5s'"))
        as_of = (await session.execute(text("SELECT now()"))).scalar_one()
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT p.factor_id,p.universe,f.factor_name,"
                        "f.metadata_json->>'icir' AS icir,p.pool_score "
                        "FROM rd_agent_factor_pool p JOIN rd_agent_factors f "
                        "ON f.tenant_id=p.tenant_id AND f.user_id=p.user_id "
                        "AND f.factor_id=p.factor_id "
                        "WHERE p.tenant_id=:tenant_id AND p.user_id=:user_id "
                        "AND f.tenant_id=:tenant_id AND f.user_id=:user_id "
                        "AND p.market='a_share' AND p.archived_at IS NULL "
                        "AND f.archived_at IS NULL AND f.status='completed'"
                        + scope
                        + " ORDER BY p.universe,p.factor_id LIMIT :limit"
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        if len(rows) > MAX_MEMBERS:
            raise ValueError(
                "本人池成员超过5000条，请先选择单个股票池；不会以截断样本代替全池判据"
            )
        archived_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM rd_agent_factor_pool p WHERE "
                    "p.tenant_id=:tenant_id AND p.user_id=:user_id "
                    "AND p.market='a_share' AND p.archived_at IS NOT NULL" + scope
                ),
                params,
            )
        ).scalar_one()
        correlations = await current_correlations(session, owner, "a_share", universe)
        from .pool_diversity_evidence import current_diversities

        diversities, diversity_sources = await current_diversities(
            session, owner, universe
        )
    return await asyncio.to_thread(
        _assemble,
        [dict(row) for row in rows],
        correlations,
        diversities=diversities,
        diversity_sources=diversity_sources,
        archived_count=archived_count,
        as_of=as_of,
        limit=limit,
        offset=offset,
    )
