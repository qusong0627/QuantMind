"""Owner-scoped factor research pool persistence."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.owner_scope import OwnerScope
from backend.services.engine.alpha_agent.factor_pool_edges import (
    DEFAULT_THRESHOLD,
    DEFAULT_TOP_K,
    VALUE_CORRELATION_THRESHOLD,
    formula_tokens,
    similar_pairs,
    value_correlated_pairs,
)

logger = logging.getLogger(__name__)
MAX_VALUE_EDGE_FACTORS = 100


class FactorPoolPersistence:
    """Manage a user's private pool membership and research annotations."""

    async def get_seed_factors(
        self,
        factor_ids: list[str],
        *,
        tenant_id: str,
        user_id: str,
        market: str = "a_share",
        universe: str,
    ) -> list[dict[str, Any]]:
        """Resolve selected mutation parents from the caller's active pool only."""
        owner = OwnerScope(tenant_id, user_id)
        if not isinstance(factor_ids, list) or not 1 <= len(factor_ids) <= 3:
            raise ValueError("Select between one and three parent factors")
        if any(not isinstance(factor_id, str) for factor_id in factor_ids):
            raise ValueError("Invalid parent factor ID")
        if len(set(factor_ids)) != len(factor_ids):
            raise ValueError("Parent factor IDs must be unique")
        for factor_id in factor_ids:
            self._validate_member(factor_id, market, universe)
        async with get_session(read_only=True) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT p.factor_id,f.factor_name,f.factor_formulation "
                            "FROM rd_agent_factor_pool p JOIN rd_agent_factors f "
                            "ON f.tenant_id=p.tenant_id AND f.user_id=p.user_id "
                            "AND f.factor_id=p.factor_id "
                            "WHERE p.tenant_id=:tenant_id AND p.user_id=:user_id "
                            "AND f.tenant_id=:tenant_id AND f.user_id=:user_id "
                            "AND p.market=:market AND p.universe=:universe "
                            "AND p.archived_at IS NULL AND f.archived_at IS NULL "
                            "AND f.status='completed' "
                            "AND p.factor_id = ANY(CAST(:factor_ids AS TEXT[]))"
                        ),
                        {
                            **owner.sql_params(),
                            "market": market,
                            "universe": universe,
                            "factor_ids": factor_ids,
                        },
                    )
                )
                .mappings()
                .all()
            )
        by_id = {str(row["factor_id"]): dict(row) for row in rows}
        if set(by_id) != set(factor_ids):
            raise LookupError("Parent factor not found in the active owner pool")
        return [by_id[factor_id] for factor_id in factor_ids]

    async def list_members(
        self,
        *,
        tenant_id: str,
        user_id: str,
        market: str = "a_share",
        universe: str | None = None,
        include_archived: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        owner = OwnerScope(tenant_id, user_id)
        if market != "a_share":
            raise ValueError("Only A-share factor pools are supported")
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("Factor pool limit must be between 1 and 500")
        where = ["p.tenant_id=:tenant_id", "p.user_id=:user_id", "p.market=:market"]
        params: dict[str, Any] = {
            **owner.sql_params(),
            "market": market,
            "limit": limit + 1,
            "offset": offset,
        }
        if universe is not None:
            where.append("p.universe=:universe")
            params["universe"] = universe
        if not include_archived:
            where.append("p.archived_at IS NULL")
            where.append("f.archived_at IS NULL")
        async with get_session(read_only=True) as session:
            result = await session.execute(
                text(
                    "SELECT p.factor_id,p.market,p.universe,p.pool_score,p.novelty,"
                    "p.max_pool_corr,p.max_pool_corr_with,p.diversity_contrib,"
                    "p.times_retrieved,p.panel_version,p.created_at,p.updated_at,"
                    "f.factor_name,f.status,f.ic_value,f.rank_ic,f.sharpe_ratio "
                    "FROM rd_agent_factor_pool p JOIN rd_agent_factors f "
                    "ON f.tenant_id=p.tenant_id AND f.user_id=p.user_id "
                    "AND f.factor_id=p.factor_id WHERE "
                    + " AND ".join(where)
                    + " ORDER BY p.updated_at DESC,p.factor_id LIMIT :limit OFFSET :offset"
                ),
                params,
            )
            return [dict(row) for row in result.mappings().all()]

    async def list_factor_miner_expressions(
        self, *, tenant_id: str, user_id: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Read FM-DSL formulas from the caller's active A-share pool only."""
        owner = OwnerScope(tenant_id, user_id)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Factor Miner structure screen limit must be 1..500")
        async with get_session(read_only=True) as session:
            result = await session.execute(
                text(
                    "SELECT p.factor_id,f.factor_name,f.factor_code,"
                    "f.factor_formulation FROM rd_agent_factor_pool p "
                    "JOIN rd_agent_factors f ON f.tenant_id=p.tenant_id "
                    "AND f.user_id=p.user_id AND f.factor_id=p.factor_id "
                    "WHERE p.tenant_id=:tenant_id AND p.user_id=:user_id "
                    "AND f.tenant_id=:tenant_id AND f.user_id=:user_id "
                    "AND p.market='a_share' AND p.archived_at IS NULL AND f.archived_at IS NULL "
                    "ORDER BY p.factor_id LIMIT :limit"
                ),
                {**owner.sql_params(), "limit": limit + 1},
            )
            return [dict(row) for row in result.mappings().all()]

    async def add_member(
        self,
        *,
        tenant_id: str,
        user_id: str,
        factor_id: str,
        market: str = "a_share",
        universe: str = "",
    ) -> bool:
        owner = OwnerScope(tenant_id, user_id)
        self._validate_member(factor_id, market, universe)
        async with get_session() as session:
            from backend.services.engine.alpha_agent.factor_visibility import (
                lock_active_factors_async,
            )

            await lock_active_factors_async(session, owner, [factor_id])
            # INSERT ... SELECT makes factor existence and owner validation part
            # of the write itself; the composite FK is the final integrity guard.
            result = await session.execute(
                text(
                    "INSERT INTO rd_agent_factor_pool "
                    "(tenant_id,user_id,factor_id,market,universe) "
                    "SELECT f.tenant_id,f.user_id,f.factor_id,:market,:universe "
                    "FROM rd_agent_factors f WHERE f.tenant_id=:tenant_id "
                    "AND f.user_id=:user_id AND f.factor_id=:factor_id AND f.archived_at IS NULL "
                    "ON CONFLICT (tenant_id,user_id,factor_id,market,universe) "
                    "DO UPDATE SET archived_at=NULL,updated_at=now()"
                ),
                {
                    **owner.sql_params(),
                    "factor_id": factor_id,
                    "market": market,
                    "universe": universe,
                },
            )
            return result.rowcount == 1

    async def remove_member(
        self,
        *,
        tenant_id: str,
        user_id: str,
        factor_id: str,
        market: str = "a_share",
        universe: str = "",
    ) -> bool:
        owner = OwnerScope(tenant_id, user_id)
        self._validate_member(factor_id, market, universe)
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE rd_agent_factor_pool SET archived_at=now(),updated_at=now() "
                    "WHERE tenant_id=:tenant_id "
                    "AND user_id=:user_id AND factor_id=:factor_id "
                    "AND market=:market AND universe=:universe AND archived_at IS NULL"
                ),
                {
                    **owner.sql_params(),
                    "factor_id": factor_id,
                    "market": market,
                    "universe": universe,
                },
            )
            return result.rowcount == 1

    async def save_panel_if_member(
        self,
        *,
        tenant_id: str,
        user_id: str,
        factor_id: str,
        market: str,
        universe: str,
        values,
        forward_returns=None,
        factor_revision_no: int | None = None,
        source_job_id: str | None = None,
        source_generation: int | None = None,
    ) -> bool:
        """Persist a sampled panel only for an active, owner-matched pool member."""
        owner = OwnerScope(tenant_id, user_id)
        self._validate_member(factor_id, market, universe)
        if (
            type(factor_revision_no) is not int
            or factor_revision_no < 1
            or not isinstance(source_job_id, str)
            or not source_job_id
            or type(source_generation) is not int
            or source_generation < 1
        ):
            return False
        from backend.services.engine.alpha_agent.factor_pool_panels import (
            delete_panel_version,
            write_panel,
        )

        new_panel: dict[str, str] | None = None
        old_version: str | None = None
        try:
            async with get_session() as session:
                factor = (
                    await session.execute(
                        text(
                            "SELECT f.factor_id FROM rd_agent_factors f "
                            "WHERE f.tenant_id=:tenant_id AND f.user_id=:user_id "
                            "AND f.factor_id=:factor_id AND f.archived_at IS NULL "
                            "AND (SELECT max(r.revision_no) FROM rd_agent_factor_revisions r "
                            "WHERE r.tenant_id=f.tenant_id AND r.user_id=f.user_id "
                            "AND r.factor_id=f.factor_id)=:revision FOR UPDATE OF f"
                        ),
                        {
                            **owner.sql_params(),
                            "factor_id": factor_id,
                            "revision": factor_revision_no,
                        },
                    )
                ).first()
                if factor is None:
                    return False
                current = (
                    (
                        await session.execute(
                            text(
                                "SELECT panel_version FROM rd_agent_factor_pool "
                                "WHERE tenant_id=:tenant_id AND user_id=:user_id "
                                "AND factor_id=:factor_id AND market=:market "
                                "AND universe=:universe AND archived_at IS NULL FOR UPDATE"
                            ),
                            {
                                **owner.sql_params(),
                                "factor_id": factor_id,
                                "market": market,
                                "universe": universe,
                            },
                        )
                    )
                    .mappings()
                    .first()
                )
                if current is None:
                    return False
                old_version = current.get("panel_version")
                new_panel = await asyncio.to_thread(
                    write_panel,
                    tenant_id,
                    user_id,
                    market,
                    factor_id,
                    values,
                    forward_returns,
                )
                result = await session.execute(
                    text(
                        "UPDATE rd_agent_factor_pool SET panel_ref=:panel_ref, "
                        "panel_version=:panel_version,extra=COALESCE(extra,'{}'::jsonb)||"
                        "jsonb_build_object('panel_source',jsonb_build_object("
                        "'factor_revision_no',CAST(:revision AS bigint),"
                        "'panel_version',CAST(:panel_version AS text),"
                        "'job_id',CAST(:source_job AS text),'execution_generation',CAST(:source_generation AS bigint))),updated_at=now() "
                        "WHERE tenant_id=:tenant_id AND user_id=:user_id "
                        "AND factor_id=:factor_id AND market=:market "
                        "AND universe=:universe AND archived_at IS NULL"
                    ),
                    {
                        **owner.sql_params(),
                        "factor_id": factor_id,
                        "market": market,
                        "universe": universe,
                        "revision": factor_revision_no,
                        "source_job": source_job_id,
                        "source_generation": source_generation,
                        **new_panel,
                    },
                )
                if result.rowcount != 1:
                    raise LookupError("Factor pool owner scope no longer matches")
        except Exception:
            if new_panel is not None:
                try:
                    await asyncio.to_thread(
                        delete_panel_version,
                        tenant_id,
                        user_id,
                        market,
                        factor_id,
                        new_panel["panel_version"],
                    )
                except Exception as cleanup_error:  # noqa: BLE001
                    logger.warning(
                        "Could not clean unreferenced private factor panel: %s",
                        type(cleanup_error).__name__,
                    )
            raise

        if old_version and old_version != new_panel["panel_version"]:
            try:
                await asyncio.to_thread(
                    delete_panel_version,
                    tenant_id,
                    user_id,
                    market,
                    factor_id,
                    old_version,
                )
            except Exception as exc:  # noqa: BLE001 - ledger reconciles old versions
                logger.warning(
                    "Could not retire old private factor panel: %s", type(exc).__name__
                )
        return True

    async def get_member_panel_summary(
        self,
        *,
        tenant_id: str,
        user_id: str,
        factor_id: str,
        market: str = "a_share",
        universe: str = "",
    ) -> dict[str, Any] | None:
        """Read a bounded daily summary after resolving the owner's pool row."""
        owner = OwnerScope(tenant_id, user_id)
        self._validate_member(factor_id, market, universe)
        from backend.services.engine.alpha_agent.factor_pool_panels import read_panel

        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT panel_version FROM rd_agent_factor_pool "
                            "WHERE tenant_id=:tenant_id AND user_id=:user_id "
                            "AND factor_id=:factor_id AND market=:market "
                            "AND universe=:universe AND archived_at IS NULL"
                        ),
                        {
                            **owner.sql_params(),
                            "factor_id": factor_id,
                            "market": market,
                            "universe": universe,
                        },
                    )
                )
                .mappings()
                .first()
            )
        if row is None or not row.get("panel_version"):
            return None

        frame = await asyncio.to_thread(
            read_panel,
            tenant_id,
            user_id,
            market,
            factor_id,
            expected_version=row["panel_version"],
        )
        if frame is None or frame.empty:
            return None
        summary = (
            frame.groupby("trade_date", sort=True)
            .agg(
                coverage=("symbol", "count"),
                mean_rank_pct=("rank_pct", "mean"),
                mean_zscore=("zscore", "mean"),
            )
            .reset_index()
        )
        return {
            "factor_id": factor_id,
            "market": market,
            "universe": universe,
            "panel_version": row["panel_version"],
            "points": summary.to_dict(orient="records"),
        }

    async def rebuild_pool_scores(
        self,
        *,
        tenant_id: str,
        user_id: str,
        market: str = "a_share",
        universe: str | None = None,
    ) -> dict[str, Any]:
        """Recompute retrieval scores independently per private stock universe."""
        owner = OwnerScope(tenant_id, user_id)
        if market != "a_share":
            raise ValueError("Only A-share factor pools are supported")
        if universe is not None and (
            not isinstance(universe, str) or len(universe) > 128
        ):
            raise ValueError("Invalid factor pool universe")

        from backend.services.engine.alpha_agent.factor_pool_scoring import (
            PoolCandidate,
            rank_candidates,
        )
        from backend.services.engine.alpha_agent.factor_pool_evidence import (
            current_correlations,
        )

        params: dict[str, Any] = {**owner.sql_params(), "market": market}
        pool_scope = [
            "p.tenant_id=:tenant_id",
            "p.user_id=:user_id",
            "p.market=:market",
            "p.archived_at IS NULL",
            "f.archived_at IS NULL",
            "f.status='completed'",
        ]
        if universe is not None:
            pool_scope.append("p.universe=:universe")
            params["universe"] = universe

        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT p.factor_id,p.universe,p.times_retrieved,"
                            "NULLIF(f.metadata_json->>'icir','') AS icir,"
                            "EXTRACT(EPOCH FROM (now()-f.created_at))/86400.0 AS age_days "
                            "FROM rd_agent_factor_pool p JOIN rd_agent_factors f "
                            "ON f.tenant_id=p.tenant_id AND f.user_id=p.user_id "
                            "AND f.factor_id=p.factor_id WHERE "
                            + " AND ".join(pool_scope)
                            + " ORDER BY p.universe,p.factor_id"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            max_corr = await current_correlations(session, owner, market, universe)

            grouped: dict[str, list[PoolCandidate]] = {}
            for row in rows:
                factor_id = str(row["factor_id"])
                try:
                    icir = float(row["icir"]) if row["icir"] is not None else None
                except (TypeError, ValueError):
                    icir = None
                if icir is not None and not math.isfinite(icir):
                    icir = None
                try:
                    age_days = (
                        float(row["age_days"]) if row["age_days"] is not None else None
                    )
                except (TypeError, ValueError):
                    age_days = None
                candidate = PoolCandidate(
                    factor_id=factor_id,
                    icir=icir,
                    times_retrieved=int(row["times_retrieved"] or 0),
                    max_pool_corr=max_corr.get(
                        (str(row["universe"] or ""), factor_id), (None, None)
                    )[0],
                    age_days=age_days,
                )
                grouped.setdefault(str(row["universe"] or ""), []).append(candidate)

            scored = {
                (group_universe, item.candidate.factor_id): item
                for group_universe, candidates in grouped.items()
                for item in rank_candidates(candidates)
            }
            updated = 0
            for group_universe, candidates in grouped.items():
                for candidate in candidates:
                    score = scored.get((group_universe, candidate.factor_id))
                    corr, corr_with = max_corr.get(
                        (group_universe, candidate.factor_id), (None, None)
                    )
                    result = await session.execute(
                        text(
                            "UPDATE rd_agent_factor_pool SET pool_score=:pool_score,"
                            "novelty=:novelty,max_pool_corr=:max_pool_corr,"
                            "max_pool_corr_with=:max_pool_corr_with,"
                            "updated_at=now() WHERE tenant_id=:tenant_id "
                            "AND user_id=:user_id AND factor_id=:factor_id "
                            "AND market=:market AND universe=:universe "
                            "AND archived_at IS NULL"
                        ),
                        {
                            **owner.sql_params(),
                            "factor_id": candidate.factor_id,
                            "market": market,
                            "universe": group_universe,
                            "pool_score": score.score if score else None,
                            "novelty": 1.0 - corr if corr is not None else None,
                            "max_pool_corr": corr,
                            "max_pool_corr_with": corr_with,
                        },
                    )
                    updated += int(result.rowcount or 0)

        return {
            "member_count": len(rows),
            "scored_count": len(scored),
            "missing_icir_count": len(rows) - len(scored),
            "updated_count": updated,
            "universe_count": len(grouped),
        }

    async def rebuild_formula_edges(
        self,
        *,
        tenant_id: str,
        user_id: str,
        market: str = "a_share",
        universe: str | None = None,
    ) -> dict[str, Any]:
        owner = OwnerScope(tenant_id, user_id)
        if market != "a_share":
            raise ValueError("Only A-share factor pools are supported")
        if universe is not None and (
            not isinstance(universe, str) or len(universe) > 128
        ):
            raise ValueError("Invalid factor pool universe")
        scope_params: dict[str, Any] = {**owner.sql_params(), "market": market}
        scope = ["p.tenant_id=:tenant_id", "p.user_id=:user_id", "p.market=:market"]
        if universe is not None:
            scope.append("p.universe=:universe")
            scope_params["universe"] = universe
        async with get_session() as session:
            rows = await session.execute(
                text(
                    "SELECT f.factor_id,f.factor_formulation,p.universe "
                    "FROM rd_agent_factor_pool p JOIN rd_agent_factors f "
                    "ON f.tenant_id=p.tenant_id AND f.user_id=p.user_id "
                    "AND f.factor_id=p.factor_id WHERE "
                    + " AND ".join(scope)
                    + " AND p.archived_at IS NULL "
                    "ORDER BY p.universe,f.factor_id LIMIT 501"
                ),
                scope_params,
            )
            factors = [dict(row) for row in rows.mappings().all()]
            if len(factors) > 500:
                raise ValueError("Formula edge rebuild is limited to 500 pool factors")

            groups: dict[str, list[tuple[str, frozenset[str]]]] = {}
            for factor in factors:
                group = factor.get("universe")
                groups.setdefault(group or "", []).append(
                    (
                        factor["factor_id"],
                        formula_tokens(factor.get("factor_formulation")),
                    )
                )

            delete_sql = (
                "DELETE FROM rd_agent_factor_edges WHERE tenant_id=:tenant_id "
                "AND user_id=:user_id AND market=:market "
                "AND relation='similar_to' AND method='formula'"
            )
            delete_params: dict[str, Any] = {**owner.sql_params(), "market": market}
            if universe is not None:
                delete_sql += " AND universe=:universe"
                delete_params["universe"] = universe
            await session.execute(text(delete_sql), delete_params)

            edge_count = 0
            for group_universe, items in groups.items():
                for source_id, target_id, weight in similar_pairs(
                    items, threshold=DEFAULT_THRESHOLD, top_k=DEFAULT_TOP_K
                ):
                    await session.execute(
                        text(
                            "INSERT INTO rd_agent_factor_edges "
                            "(tenant_id,user_id,market,universe,src_factor_id,dst_factor_id,"
                            "relation,method,weight) VALUES (:tenant_id,:user_id,:market,"
                            ":universe,:source,:target,'similar_to','formula',:weight) "
                            "ON CONFLICT (tenant_id,user_id,market,universe,src_factor_id,"
                            "dst_factor_id,relation,method) DO UPDATE SET weight=EXCLUDED.weight"
                        ),
                        {
                            **owner.sql_params(),
                            "market": market,
                            "universe": group_universe,
                            "source": source_id,
                            "target": target_id,
                            "weight": weight,
                        },
                    )
                    edge_count += 1
            return {
                "factor_count": len(factors),
                "edge_count": edge_count,
                "threshold": DEFAULT_THRESHOLD,
                "top_k": DEFAULT_TOP_K,
            }

    async def rebuild_task_round_edges(
        self,
        *,
        tenant_id: str,
        user_id: str,
        market: str = "a_share",
        universe: str | None = None,
    ) -> dict[str, Any]:
        """Link adjacent completed pool factors from the same owned mining task."""
        owner = OwnerScope(tenant_id, user_id)
        if market != "a_share":
            raise ValueError("Only A-share factor pools are supported")
        if universe is not None and (
            not isinstance(universe, str) or len(universe) > 128
        ):
            raise ValueError("Invalid factor pool universe")
        params: dict[str, Any] = {**owner.sql_params(), "market": market}
        scope = ["p.tenant_id=:tenant_id", "p.user_id=:user_id", "p.market=:market"]
        if universe is not None:
            scope.append("p.universe=:universe")
            params["universe"] = universe
        async with get_session() as session:
            result = await session.execute(
                text(
                    "SELECT p.factor_id,p.universe,f.metadata_json->>'task_id' AS task_id,"
                    "f.created_at FROM rd_agent_factor_pool p JOIN rd_agent_factors f "
                    "ON f.tenant_id=p.tenant_id AND f.user_id=p.user_id "
                    "AND f.factor_id=p.factor_id WHERE "
                    + " AND ".join(scope)
                    + " AND p.archived_at IS NULL AND f.status='completed' "
                    "AND NULLIF(f.metadata_json->>'task_id','') IS NOT NULL "
                    "ORDER BY p.universe,f.metadata_json->>'task_id',f.created_at,f.factor_id "
                    "LIMIT 501"
                ),
                params,
            )
            factors = [dict(row) for row in result.mappings().all()]
            if len(factors) > 500:
                raise ValueError(
                    "Task-round edge rebuild is limited to 500 pool factors"
                )

            groups: dict[tuple[str, str], list[str]] = {}
            for factor in factors:
                groups.setdefault(
                    (str(factor.get("universe") or ""), str(factor["task_id"])), []
                ).append(str(factor["factor_id"]))
            delete_sql = (
                "DELETE FROM rd_agent_factor_edges WHERE tenant_id=:tenant_id "
                "AND user_id=:user_id AND market=:market "
                "AND relation='same_task' AND method='task_round'"
            )
            delete_params: dict[str, Any] = {**owner.sql_params(), "market": market}
            if universe is not None:
                delete_sql += " AND universe=:universe"
                delete_params["universe"] = universe
            await session.execute(text(delete_sql), delete_params)

            edge_count = 0
            for (group_universe, _task_id), factor_ids in groups.items():
                for left, right in zip(factor_ids, factor_ids[1:], strict=False):
                    source, target = sorted((left, right))
                    await session.execute(
                        text(
                            "INSERT INTO rd_agent_factor_edges "
                            "(tenant_id,user_id,market,universe,src_factor_id,dst_factor_id,"
                            "relation,method,weight) VALUES (:tenant_id,:user_id,:market,"
                            ":universe,:source,:target,'same_task','task_round',1.0) "
                            "ON CONFLICT (tenant_id,user_id,market,universe,src_factor_id,"
                            "dst_factor_id,relation,method) DO UPDATE SET weight=1.0"
                        ),
                        {
                            **owner.sql_params(),
                            "market": market,
                            "universe": group_universe,
                            "source": source,
                            "target": target,
                        },
                    )
                    edge_count += 1
            return {
                "factor_count": len(factors),
                "task_count": len(groups),
                "edge_count": edge_count,
            }

    async def list_formula_edges(
        self,
        *,
        tenant_id: str,
        user_id: str,
        market: str = "a_share",
        universe: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        owner = OwnerScope(tenant_id, user_id)
        if market != "a_share" or not 1 <= limit <= 1000:
            raise ValueError("Invalid factor graph query bounds")
        scope = ["e.tenant_id=:tenant_id", "e.user_id=:user_id", "e.market=:market"]
        params: dict[str, Any] = {
            **owner.sql_params(),
            "market": market,
            "limit": limit,
        }
        if universe is not None:
            scope.append("e.universe=:universe")
            params["universe"] = universe
        async with get_session(read_only=True) as session:
            result = await session.execute(
                text(
                    "SELECT e.universe,e.src_factor_id,e.dst_factor_id,e.relation,"
                    "e.method,e.weight,NULLIF(e.extra->>'signed_correlation','')::"
                    "double precision AS signed_correlation,"
                    "src.factor_name AS src_factor_name,"
                    "dst.factor_name AS dst_factor_name FROM rd_agent_factor_edges e "
                    "JOIN rd_agent_factors src ON src.tenant_id=e.tenant_id "
                    "AND src.user_id=e.user_id AND src.factor_id=e.src_factor_id "
                    "JOIN rd_agent_factors dst ON dst.tenant_id=e.tenant_id "
                    "AND dst.user_id=e.user_id AND dst.factor_id=e.dst_factor_id "
                    "JOIN rd_agent_factor_pool src_pool ON src_pool.tenant_id=e.tenant_id "
                    "AND src_pool.user_id=e.user_id AND src_pool.factor_id=e.src_factor_id "
                    "AND src_pool.market=e.market AND src_pool.universe=e.universe "
                    "AND src_pool.archived_at IS NULL "
                    "JOIN rd_agent_factor_pool dst_pool ON dst_pool.tenant_id=e.tenant_id "
                    "AND dst_pool.user_id=e.user_id AND dst_pool.factor_id=e.dst_factor_id "
                    "AND dst_pool.market=e.market AND dst_pool.universe=e.universe "
                    "AND dst_pool.archived_at IS NULL WHERE "
                    + " AND ".join(scope)
                    + " ORDER BY e.universe,e.weight DESC,e.src_factor_id,e.dst_factor_id "
                    "LIMIT :limit"
                ),
                params,
            )
            return [dict(row) for row in result.mappings().all()]

    async def rebuild_value_edges(
        self,
        *,
        tenant_id: str,
        user_id: str,
        market: str = "a_share",
        universe: str | None = None,
    ) -> dict[str, Any]:
        """Rebuild value-correlation edges from this owner's saved panels."""
        owner = OwnerScope(tenant_id, user_id)
        if market != "a_share":
            raise ValueError("Only A-share factor pools are supported")
        if universe is not None and (
            not isinstance(universe, str) or len(universe) > 128
        ):
            raise ValueError("Invalid factor pool universe")

        from backend.services.engine.alpha_agent.factor_pool_panels import read_panel

        params: dict[str, Any] = {**owner.sql_params(), "market": market}
        scope = ["tenant_id=:tenant_id", "user_id=:user_id", "market=:market"]
        if universe is not None:
            params["universe"] = universe
            scope.append("universe=:universe")
        async with get_session(read_only=True) as session:
            rows = await session.execute(
                text(
                    "SELECT factor_id,universe,panel_version FROM rd_agent_factor_pool "
                    "WHERE "
                    + " AND ".join(scope)
                    + " AND archived_at IS NULL AND panel_version IS NOT NULL "
                    "ORDER BY universe,factor_id LIMIT :limit"
                ),
                {**params, "limit": MAX_VALUE_EDGE_FACTORS + 1},
            )
            panel_members = [dict(row) for row in rows.mappings().all()]
        if len(panel_members) > MAX_VALUE_EDGE_FACTORS:
            raise ValueError(
                f"Value edge rebuild is limited to {MAX_VALUE_EDGE_FACTORS} panels"
            )

        groups: dict[str, list[tuple[str, Any]]] = {}
        panel_versions: dict[tuple[str, str], str] = {}
        usable_panels = 0
        for member in panel_members:
            factor_id = member["factor_id"]
            group_universe = member.get("universe") or ""
            version = member["panel_version"]
            frame = await asyncio.to_thread(
                read_panel,
                tenant_id,
                user_id,
                market,
                factor_id,
                expected_version=version,
            )
            if frame is None or frame.empty:
                continue
            import pandas as pd

            values = pd.Series(
                frame["rank_pct"].to_numpy(),
                index=pd.MultiIndex.from_frame(frame[["trade_date", "symbol"]]),
            )
            groups.setdefault(group_universe, []).append((factor_id, values))
            panel_versions[(group_universe, factor_id)] = version
            usable_panels += 1

        edges = []
        for group_universe, items in groups.items():
            group_edges = await asyncio.to_thread(
                value_correlated_pairs,
                items,
                threshold=VALUE_CORRELATION_THRESHOLD,
                top_k=DEFAULT_TOP_K,
            )
            edges.extend(
                (group_universe, source, target, weight, coefficient)
                for source, target, weight, coefficient in group_edges
            )

        import json

        delete_sql = (
            "DELETE FROM rd_agent_factor_edges WHERE tenant_id=:tenant_id "
            "AND user_id=:user_id AND market=:market "
            "AND relation='correlated_with' AND method='value'"
        )
        delete_params: dict[str, Any] = {**owner.sql_params(), "market": market}
        if universe is not None:
            delete_sql += " AND universe=:universe"
            delete_params["universe"] = universe
        async with get_session() as session:
            await session.execute(text(delete_sql), delete_params)
            edge_count = 0
            for group_universe, source, target, weight, coefficient in edges:
                result = await session.execute(
                    text(
                        "INSERT INTO rd_agent_factor_edges "
                        "(tenant_id,user_id,market,universe,src_factor_id,dst_factor_id,"
                        "relation,method,weight,extra) SELECT :tenant_id,:user_id,:market,"
                        ":universe,:source,:target,'correlated_with','value',:weight,"
                        "CAST(:extra AS jsonb) WHERE EXISTS (SELECT 1 FROM "
                        "rd_agent_factor_pool src WHERE src.tenant_id=:tenant_id "
                        "AND src.user_id=:user_id AND src.factor_id=:source "
                        "AND src.market=:market AND src.universe=:universe "
                        "AND src.archived_at IS NULL AND src.panel_version=:source_version) "
                        "AND EXISTS (SELECT 1 FROM rd_agent_factor_pool dst WHERE "
                        "dst.tenant_id=:tenant_id AND dst.user_id=:user_id "
                        "AND dst.factor_id=:target AND dst.market=:market "
                        "AND dst.universe=:universe AND dst.archived_at IS NULL "
                        "AND dst.panel_version=:target_version) ON CONFLICT "
                        "(tenant_id,user_id,market,universe,src_factor_id,dst_factor_id,"
                        "relation,method) DO UPDATE SET weight=EXCLUDED.weight,"
                        "extra=EXCLUDED.extra"
                    ),
                    {
                        **owner.sql_params(),
                        "market": market,
                        "universe": group_universe,
                        "source": source,
                        "target": target,
                        "weight": weight,
                        "source_version": panel_versions[(group_universe, source)],
                        "target_version": panel_versions[(group_universe, target)],
                        "extra": json.dumps(
                            {
                                "signed_correlation": coefficient,
                                "source_panel_version": panel_versions[
                                    (group_universe, source)
                                ],
                                "target_panel_version": panel_versions[
                                    (group_universe, target)
                                ],
                            }
                        ),
                    },
                )
                edge_count += int(result.rowcount or 0)
        return {
            "panel_count": usable_panels,
            "edge_count": edge_count,
            "threshold": VALUE_CORRELATION_THRESHOLD,
            "top_k": DEFAULT_TOP_K,
        }

    @staticmethod
    def _validate_member(factor_id: str, market: str, universe: str) -> None:
        if (
            not isinstance(factor_id, str)
            or not factor_id.strip()
            or len(factor_id) > 256
        ):
            raise ValueError("Invalid factor ID")
        if market != "a_share":
            raise ValueError("Only A-share factor pools are supported")
        if not isinstance(universe, str) or len(universe) > 128:
            raise ValueError("Invalid factor pool universe")
