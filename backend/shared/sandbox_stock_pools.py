"""Prepare visible stock pools before user code runs without database access."""

from __future__ import annotations

from sqlalchemy import text

from backend.shared.database_pool import get_db
from backend.shared.stock_pool.materializer import (
    pool_dir,
    read_pool_txt,
    resolve_pool_txt,
)
from backend.shared.stock_pool.resolver import _build_snapshot
from backend.shared.user_code_sandbox import checked_input
from backend.shared.utils import normalize_user_id


def visible_pool_catalog(tenant_id: str, user_id: str) -> dict:
    with get_db() as session:
        rows = (
            session.execute(
                text(
                    "SELECT pool_id, code, scope, tenant_id, owner_user_id, file_path "
                    "FROM qm_stock_pool WHERE status = 'active' AND market IN ('CN', 'A') "
                    "AND (scope = 'global' OR (scope = 'tenant' AND tenant_id = :tenant) "
                    "OR (scope = 'user' AND tenant_id = :tenant AND owner_user_id = :user)) "
                    "ORDER BY CASE scope WHEN 'global' THEN 0 WHEN 'tenant' THEN 1 ELSE 2 END "
                    "LIMIT 1001"
                ),
                {"tenant": tenant_id, "user": normalize_user_id(user_id)},
            )
            .mappings()
            .all()
        )
    if len(rows) > 1000:
        raise ValueError("Too many visible pools for a sandbox task")
    catalog = {}
    for row in rows:
        path = resolve_pool_txt(
            row["scope"],
            row["code"],
            file_path=row["file_path"],
            tenant_id=row["tenant_id"],
            owner_user_id=row["owner_user_id"],
        )
        try:
            safe_path = checked_input(path, pool_dir())
        except FileNotFoundError:
            continue
        members = read_pool_txt(safe_path)
        snapshot = _build_snapshot(
            pool_id=row["pool_id"],
            code=row["code"],
            market="CN",
            warnings=[],
            api_symbols=members,
            source="sandbox_snapshot",
        ).model_dump()
        catalog[f"pool_id:{row['pool_id']}"] = snapshot
        catalog[f"pool:{row['code']}"] = snapshot
        catalog[row["code"]] = snapshot
    return catalog
