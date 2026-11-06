"""Use immutable completed pool results only while the entire manifest matches."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from sqlalchemy import text

from backend.shared.owner_scope import OwnerScope
from backend.shared.utc_datetime import to_utc_iso
from .factor_pool_diversity import CRITERIA, METHOD
from .pool_research import (
    CONTRACT,
    PoolResearchConflict,
    _members,
    _members_statement,
    number,
    numeric_context,
)


async def current_diversities(session, owner: OwnerScope, universe):
    """Caller holds its primary, read-only repeatable-read transaction.

    No writes to mutable pool rows: a membership, code, ICIR, panel or source-job
    change makes the saved contribution unknown. Do not fall back to an older
    job, an unversioned p.diversity_contrib, or a recomputation during GET.
    """
    if not isinstance(owner, OwnerScope):
        raise ValueError("Authenticated owner required")
    rows = (
        (
            await session.execute(
                _members_statement(all_universes=universe is None),
                {**owner.sql_params(), "universe": universe},
            )
        )
        .mappings()
        .all()
    )
    if len(rows) > 5000:
        raise ValueError("完整本人池超过5000条，不能推断多样性贡献")
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["universe"]].append(row)
    manifests = {}
    for scope, members in grouped.items():
        try:
            manifests[scope] = _members(members)
        except (ValueError, PoolResearchConflict):
            # Oversized/legacy/unverifiable scope remains unknown, never partial.
            continue
    if not manifests:
        return {}, {}
    jobs = (
        (
            await session.execute(
                text(
                    "SELECT DISTINCT ON (request_snapshot->'request'->>'universe') "
                    "job_id,completed_at,request_snapshot->'request' AS request,"
                    "result_snapshot->'diversity' AS diversity "
                    "FROM alpha_factor_jobs WHERE tenant_id=:tenant_id AND user_id=:user_id "
                    "AND kind='pool_research' AND status='completed' "
                    "AND request_snapshot->'request'->>'market'='a_share' "
                    "AND request_snapshot->'request'->>'universe'=ANY(CAST(:universes AS TEXT[])) "
                    "ORDER BY request_snapshot->'request'->>'universe',created_at DESC,job_id DESC"
                ),
                {**owner.sql_params(), "universes": list(manifests)},
            )
        )
        .mappings()
        .all()
    )
    context = await asyncio.to_thread(numeric_context)
    contributions, sources = {}, {}
    for job in jobs:
        request, evidence = job["request"], job["diversity"]
        if not isinstance(request, dict) or not isinstance(evidence, dict):
            continue
        scope = request.get("universe")
        members = manifests.get(scope)
        if (
            members is None
            or request.get("contract") != CONTRACT
            or request.get("numeric_context") != context
            or request.get("diversity_criteria") != CRITERIA
            or request.get("factor_items") != members
            or evidence.get("method") != METHOD
            or evidence.get("criteria") != CRITERIA
            or evidence.get("status") != "computed"
            or evidence.get("factor_ids") != [item["factor_id"] for item in members]
            or evidence.get("factor_count") != len(members)
        ):
            continue
        deltas = evidence.get("contributions")
        if not isinstance(deltas, dict) or set(deltas) != {
            item["factor_id"] for item in members
        }:
            continue
        parsed = {
            key: number(value.get("delta")) if isinstance(value, dict) else None
            for key, value in deltas.items()
        }
        if any(value is None for value in parsed.values()):
            continue
        contributions.update(((scope, key), value) for key, value in parsed.items())
        sources[scope] = {
            "job_id": job["job_id"],
            "completed_at": to_utc_iso(job["completed_at"]),
            "method": METHOD,
            "n_days": evidence.get("n_days"),
            "n_obs": evidence.get("n_obs"),
            "effective_factors": evidence.get("effective_factors"),
        }
    return contributions, sources
