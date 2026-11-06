"""Reversible private factor archival and transactional admission fencing."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.owner_scope import OwnerScope
from backend.shared.utc_datetime import to_utc_iso


class FactorVisibilityConflict(ValueError):
    """The requested visibility change conflicts with active work or archival."""


def referenced_factor_ids(
    request: dict[str, Any], *, max_factors: int = 20
) -> list[str]:
    if max_factors not in {20, 100}:
        raise ValueError("Unsupported factor reference limit")
    values = []
    if request.get("factor_id") is not None:
        values.append(request["factor_id"])
    items = request.get("factor_items", [])
    ids = request.get("factor_ids", [])
    if not isinstance(items, list) or not isinstance(ids, list):
        raise ValueError("Invalid factor references")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid frozen factor reference")
        values.append(item.get("factor_id"))
    values.extend(ids)
    if len(values) > 2 * max_factors + 1 or any(
        not isinstance(value, str) or not value or len(value) > 256 for value in values
    ):
        raise ValueError("Invalid factor references")
    result = sorted(set(values))
    if len(result) > max_factors:
        raise ValueError("Too many factor references")
    return result


def _lock_statement():
    return text(
        "SELECT factor_id,archived_at FROM rd_agent_factors "
        "WHERE tenant_id=:tenant_id AND user_id=:user_id "
        "AND factor_id=ANY(CAST(:factor_ids AS TEXT[])) "
        "ORDER BY factor_id FOR UPDATE"
    )


def _require_rows(rows, factor_ids: list[str]) -> None:
    if {row["factor_id"] for row in rows} != set(factor_ids):
        raise LookupError("Factor not found")
    if any(row["archived_at"] is not None for row in rows):
        raise FactorVisibilityConflict("已归档因子需先恢复，才能提交新任务或加入研究池")


def lock_active_factors(session, owner: OwnerScope, factor_ids: list[str]) -> None:
    if not factor_ids:
        return
    rows = (
        session.execute(
            _lock_statement(),
            {**owner.sql_params(), "factor_ids": sorted(set(factor_ids))},
        )
        .mappings()
        .all()
    )
    _require_rows(rows, factor_ids)


async def lock_active_factors_async(
    session, owner: OwnerScope, factor_ids: list[str]
) -> None:
    if not factor_ids:
        return
    rows = (
        (
            await session.execute(
                _lock_statement(),
                {**owner.sql_params(), "factor_ids": sorted(set(factor_ids))},
            )
        )
        .mappings()
        .all()
    )
    _require_rows(rows, factor_ids)


class FactorVisibilityStore:
    async def set_archived(
        self,
        factor_id: str,
        *,
        tenant_id: str,
        user_id: str,
        archived: bool,
        reason: str = "",
    ) -> dict[str, Any]:
        owner = OwnerScope(tenant_id, user_id)
        if not factor_id or len(factor_id) > 256 or type(archived) is not bool:
            raise ValueError("Invalid factor visibility request")
        reason = reason.strip()
        if len(reason) > 500:
            raise ValueError("归档或恢复原因不能超过 500 字")
        params = {**owner.sql_params(), "factor_id": factor_id, "reason": reason}
        async with get_session() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT factor_id,status,archived_at,archive_reason,"
                            "metadata_json->>'task_id' AS source_task_id "
                            "FROM rd_agent_factors WHERE tenant_id=:tenant_id "
                            "AND user_id=:user_id AND factor_id=:factor_id FOR UPDATE"
                        ),
                        params,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise LookupError("Factor not found")
            if (row["archived_at"] is not None) == archived:
                return self._public(row)
            if archived:
                if row["status"] in {"running", "backtesting", "validating"}:
                    raise FactorVisibilityConflict(
                        "因子仍在执行任务，停止确认后才能归档"
                    )
                busy = (
                    await session.execute(
                        text(
                            "SELECT 1 FROM alpha_factor_jobs j "
                            "LEFT JOIN resource_task_leases lease ON lease.pool=j.lease_pool "
                            "AND lease.task_id=j.job_id AND lease.tenant_id=j.tenant_id "
                            "AND lease.user_id=j.user_id "
                            "WHERE j.tenant_id=:tenant_id AND j.user_id=:user_id "
                            "AND (j.status IN ('queued','running','stopping') "
                            "OR lease.state NOT IN ('completed','failed','cancelled')) "
                            "AND (j.request_snapshot->'request' @> CAST(:single_ref AS jsonb) "
                            "OR j.request_snapshot->'request' @> CAST(:batch_ref AS jsonb) "
                            "OR j.request_snapshot->'request' @> CAST(:ids_ref AS jsonb)) LIMIT 1"
                        ),
                        {
                            **params,
                            "single_ref": json.dumps({"factor_id": factor_id}),
                            "batch_ref": json.dumps(
                                {"factor_items": [{"factor_id": factor_id}]}
                            ),
                            "ids_ref": json.dumps({"factor_ids": [factor_id]}),
                        },
                    )
                ).first()
                source_busy = (
                    await session.execute(
                        text(
                            "SELECT 1 FROM alpha_mining_tasks task "
                            "LEFT JOIN alpha_mining_task_specs spec ON spec.task_id=task.task_id "
                            "AND spec.tenant_id=task.tenant_id AND spec.user_id=task.user_id "
                            "LEFT JOIN resource_task_leases lease ON lease.pool='alpha_mining' "
                            "AND lease.task_id=task.task_id AND lease.tenant_id=task.tenant_id "
                            "AND lease.user_id=task.user_id "
                            "WHERE task.tenant_id=:tenant_id AND task.user_id=:user_id "
                            "AND (task.task_id=:task_id OR spec.params_json->'seed_factor_ids' "
                            "@> CAST(:seed_ref AS jsonb)) "
                            "AND (task.status IN ('pending','queued','running','stopping') "
                            "OR lease.state NOT IN ('completed','failed','cancelled')) LIMIT 1"
                        ),
                        {
                            **params,
                            "task_id": row["source_task_id"] or "",
                            "seed_ref": json.dumps([factor_id]),
                        },
                    )
                ).first()
                if busy or source_busy:
                    raise FactorVisibilityConflict(
                        "因子仍有活动任务或生成任务，结束后才能归档"
                    )
                await session.execute(
                    text(
                        "UPDATE rd_agent_factor_pool SET archived_at=now(),updated_at=now() "
                        "WHERE tenant_id=:tenant_id AND user_id=:user_id "
                        "AND factor_id=:factor_id AND archived_at IS NULL"
                    ),
                    params,
                )
            changed = (
                (
                    await session.execute(
                        text(
                            "UPDATE rd_agent_factors SET archived_at="
                            + (
                                "now(),archive_reason=:reason"
                                if archived
                                else "NULL,archive_reason=NULL"
                            )
                            + ",updated_at=now() WHERE tenant_id=:tenant_id AND user_id=:user_id "
                            "AND factor_id=:factor_id RETURNING factor_id,archived_at,archive_reason"
                        ),
                        params,
                    )
                )
                .mappings()
                .one()
            )
            await session.execute(
                text(
                    "INSERT INTO alpha_factor_visibility_events "
                    "(tenant_id,user_id,factor_id,event_id,action,reason) "
                    "VALUES (:tenant_id,:user_id,:factor_id,:event_id,:action,:reason)"
                ),
                {
                    **params,
                    "event_id": uuid.uuid4().hex,
                    "action": "archived" if archived else "restored",
                },
            )
            return self._public(changed)

    @staticmethod
    def _public(row) -> dict[str, Any]:
        return {
            "factor_id": row["factor_id"],
            "archived_at": to_utc_iso(row["archived_at"])
            if row["archived_at"]
            else None,
            "archive_reason": row["archive_reason"],
        }
