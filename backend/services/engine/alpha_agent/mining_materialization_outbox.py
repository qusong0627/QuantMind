"""Durable owner-scoped handoff from statistical mining to factor materialization."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.owner_scope import OwnerScope


class MiningMaterializationOutbox:
    async def enqueue_selected(
        self,
        session,
        *,
        owner: OwnerScope,
        task_id: str,
        factors: list[dict[str, Any]],
    ) -> int:
        """Insert child work in the same transaction as mining completion."""
        inserted = 0
        for factor in factors:
            result = await session.execute(
                text(
                    "INSERT INTO alpha_mining_materialization_outbox "
                    "(tenant_id,user_id,task_id,factor_id,universe) "
                    "VALUES (:tenant,:user,:task,:factor,:universe) "
                    "ON CONFLICT (tenant_id,user_id,task_id,factor_id) DO NOTHING"
                ),
                {
                    "tenant": owner.tenant_id,
                    "user": owner.user_id,
                    "task": task_id,
                    "factor": factor["factor_id"],
                    "universe": factor["universe"],
                },
            )
            inserted += int(result.rowcount or 0)
        return inserted

    async def claim(self, *, limit: int = 32, lease_seconds: int = 120) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("Invalid mining materialization outbox limit")
        if type(lease_seconds) is not int or not 30 <= lease_seconds <= 600:
            raise ValueError("Invalid mining materialization claim lease")
        claimed: list[dict] = []
        async with get_session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT tenant_id,user_id,task_id,factor_id,universe,attempts "
                        "FROM alpha_mining_materialization_outbox "
                        "WHERE (status='pending' AND available_at<=now()) "
                        "OR (status='claimed' AND claim_until<now()) "
                        "ORDER BY available_at,created_at,task_id,factor_id "
                        "FOR UPDATE SKIP LOCKED LIMIT :limit"
                    ),
                    {"limit": limit},
                )
            ).mappings().all()
            for row in rows:
                token = uuid.uuid4().hex
                updated = await session.execute(
                    text(
                        "UPDATE alpha_mining_materialization_outbox SET "
                        "status='claimed',attempts=attempts+1,claim_token=:token,"
                        "claim_until=now()+(:lease * interval '1 second'),"
                        "updated_at=now() WHERE tenant_id=:tenant AND user_id=:user "
                        "AND task_id=:task AND factor_id=:factor "
                        "AND (status='pending' OR claim_until<now())"
                    ),
                    {
                        "token": token,
                        "lease": lease_seconds,
                        "tenant": row["tenant_id"],
                        "user": row["user_id"],
                        "task": row["task_id"],
                        "factor": row["factor_id"],
                    },
                )
                if updated.rowcount == 1:
                    claimed.append({**dict(row), "claim_token": token})
        return claimed

    async def acknowledge(self, item: dict, *, factor_job_id: str) -> bool:
        owner = OwnerScope(item.get("tenant_id"), item.get("user_id"))
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE alpha_mining_materialization_outbox SET "
                    "status='submitted',factor_job_id=:job,claim_token=NULL,"
                    "claim_until=NULL,last_error=NULL,updated_at=now() "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id AND task_id=:task "
                    "AND factor_id=:factor AND status='claimed' "
                    "AND claim_token=:token"
                ),
                {
                    **owner.sql_params(),
                    "task": item["task_id"],
                    "factor": item["factor_id"],
                    "token": item["claim_token"],
                    "job": factor_job_id,
                },
            )
            return result.rowcount == 1

    async def retry(self, item: dict, *, error: str, delay_seconds: int) -> bool:
        owner = OwnerScope(item.get("tenant_id"), item.get("user_id"))
        terminal = int(item.get("attempts") or 0) >= 10
        next_status = "failed" if terminal else "pending"
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE alpha_mining_materialization_outbox SET status=:status,"
                    "available_at=now()+(:delay * interval '1 second'),"
                    "claim_token=NULL,claim_until=NULL,last_error=:error,updated_at=now() "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id AND task_id=:task "
                    "AND factor_id=:factor AND status='claimed' AND claim_token=:token"
                ),
                {
                    **owner.sql_params(),
                    "task": item["task_id"],
                    "factor": item["factor_id"],
                    "token": item["claim_token"],
                    "status": next_status,
                    "delay": max(1, min(int(delay_seconds), 3600)),
                    "error": str(error)[:1000],
                },
            )
            return result.rowcount == 1
