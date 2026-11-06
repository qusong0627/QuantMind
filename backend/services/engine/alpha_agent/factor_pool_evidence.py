"""Read value edges only while both private panel versions are still current."""

from __future__ import annotations

from sqlalchemy import text

from backend.shared.owner_scope import OwnerScope


async def current_correlations(session, owner: OwnerScope, market: str, universe):
    scope = " AND e.universe=:universe" if universe is not None else ""
    rows = (
        (
            await session.execute(
                text(
                    "SELECT DISTINCT ON (e.universe,ends.factor_id) e.universe,"
                    "ends.factor_id,ends.other_id,ABS(e.weight) AS correlation "
                    "FROM rd_agent_factor_edges e "
                    "JOIN rd_agent_factor_pool src ON src.tenant_id=e.tenant_id "
                    "AND src.user_id=e.user_id AND src.market=e.market "
                    "AND src.universe=e.universe AND src.factor_id=e.src_factor_id "
                    "JOIN rd_agent_factor_pool dst ON dst.tenant_id=e.tenant_id "
                    "AND dst.user_id=e.user_id AND dst.market=e.market "
                    "AND dst.universe=e.universe AND dst.factor_id=e.dst_factor_id "
                    "JOIN rd_agent_factors sf ON sf.tenant_id=src.tenant_id "
                    "AND sf.user_id=src.user_id AND sf.factor_id=src.factor_id "
                    "JOIN rd_agent_factors df ON df.tenant_id=dst.tenant_id "
                    "AND df.user_id=dst.user_id AND df.factor_id=dst.factor_id "
                    "CROSS JOIN LATERAL (VALUES (e.src_factor_id,e.dst_factor_id),"
                    "(e.dst_factor_id,e.src_factor_id)) AS ends(factor_id,other_id) "
                    "WHERE e.tenant_id=:tenant_id AND e.user_id=:user_id "
                    "AND src.tenant_id=:tenant_id AND src.user_id=:user_id "
                    "AND dst.tenant_id=:tenant_id AND dst.user_id=:user_id "
                    "AND sf.tenant_id=:tenant_id AND sf.user_id=:user_id "
                    "AND df.tenant_id=:tenant_id AND df.user_id=:user_id "
                    "AND e.market=:market AND e.method='value' "
                    "AND e.relation='correlated_with' "
                    "AND src.archived_at IS NULL AND dst.archived_at IS NULL "
                    "AND sf.archived_at IS NULL AND df.archived_at IS NULL "
                    "AND sf.status='completed' AND df.status='completed' "
                    "AND src.panel_version=e.extra->>'source_panel_version' "
                    "AND dst.panel_version=e.extra->>'target_panel_version' "
                    "AND ABS(e.weight)<=1 "
                    + scope
                    + " ORDER BY e.universe,ends.factor_id,ABS(e.weight) DESC,ends.other_id"
                ),
                {**owner.sql_params(), "market": market, "universe": universe},
            )
        )
        .mappings()
        .all()
    )
    return {
        (str(row["universe"]), str(row["factor_id"])): (
            float(row["correlation"]),
            str(row["other_id"]),
        )
        for row in rows
    }
