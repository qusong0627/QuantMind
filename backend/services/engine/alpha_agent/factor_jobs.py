"""Owner-scoped durable records for factor research child jobs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.shared.database_manager_v2 import get_session
from backend.shared.owner_scope import OwnerScope
from backend.services.engine.alpha_agent.factor_job_budget import (
    POOL,
    pending_limits,
    require_ready_budget,
)

_KINDS = frozenset(
    {
        "backtest",
        "validate",
        "materialize",
        "serve_features",
        "report",
        "compose",
        "doc_parse",
        "doc_organize",
        "promote",
        "recovery",
        "cleanup",
        "pool_research",
        "purge",
        "purge_task",
    }
)
_ACTIVE = ("queued", "running", "stopping")


class FactorJobQueueFull(RuntimeError):
    """The private factor job queue reached its per-user or global budget."""


class FactorJobIdempotencyConflict(ValueError):
    """An owner reused a request key for different child-job input."""


class FactorJobNotTerminal(RuntimeError):
    """The job is still queued/running/stopping; cancel it before deleting."""


class FactorJobProtected(RuntimeError):
    """Only backtest/validate history is user-deletable."""


def _store_validation_attempt(
    session,
    *,
    owner: OwnerScope,
    factor_id: str,
    validation_id: str,
    job_id: str,
    generation: int,
    submitted_at,
    status: str,
    context: dict[str, Any],
    metrics: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> None:
    encoded_context = json.dumps(
        context,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    encoded_result = (
        json.dumps(
            {"metrics": metrics},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        if metrics is not None
        else None
    )
    session.execute(
        text(
            "INSERT INTO alpha_factor_validation_attempts "
            "(tenant_id,user_id,factor_id,validation_id,job_id,execution_generation,"
            "status,validation_context,result_snapshot,error_code) VALUES "
            "(:tenant_id,:user_id,:factor,:validation,:job,:generation,:status,"
            "CAST(:context AS jsonb),CAST(:result AS jsonb),:error_code)"
        ),
        {
            **owner.sql_params(),
            "factor": factor_id,
            "validation": validation_id,
            "job": job_id,
            "generation": generation,
            "status": status,
            "context": encoded_context,
            "result": encoded_result,
            "error_code": error_code,
        },
    )
    session.execute(
        text(
            "INSERT INTO alpha_factor_validation_latest "
            "(tenant_id,user_id,factor_id,validation_id,job_id,submitted_at) "
            "VALUES (:tenant_id,:user_id,:factor,:validation,:job,:submitted_at) "
            "ON CONFLICT (tenant_id,user_id,factor_id) DO UPDATE SET "
            "validation_id=EXCLUDED.validation_id,job_id=EXCLUDED.job_id,"
            "submitted_at=EXCLUDED.submitted_at "
            "WHERE (EXCLUDED.submitted_at,EXCLUDED.job_id) > "
            "(alpha_factor_validation_latest.submitted_at,"
            "alpha_factor_validation_latest.job_id)"
        ),
        {
            **owner.sql_params(),
            "factor": factor_id,
            "validation": validation_id,
            "job": job_id,
            "submitted_at": submitted_at,
        },
    )


def _record_completed_validation_attempts(
    session,
    *,
    owner: OwnerScope,
    job_id: str,
    generation: int,
    submitted_at,
    request_snapshot: dict[str, Any],
    result_snapshot: dict[str, Any],
) -> None:
    request = request_snapshot.get("request")
    if not isinstance(request, dict):
        raise ValueError("Validation request snapshot is invalid")
    items = request.get("factor_items")
    if isinstance(items, list):
        expected_ids = [
            item.get("factor_id") for item in items if isinstance(item, dict)
        ]
        if len(expected_ids) != len(items):
            raise ValueError("Validation batch manifest is invalid")
    else:
        expected_ids = [request.get("factor_id")]
        items = [request]
    outputs = result_snapshot.get("results")
    if outputs is None:
        outputs = [result_snapshot]
    if (
        not expected_ids
        or len(outputs) != len(expected_ids)
        or [item.get("factor_id") for item in outputs if isinstance(item, dict)]
        != expected_ids
    ):
        raise ValueError("Validation results do not match the frozen factor selection")
    for frozen, output in zip(items, outputs, strict=True):
        if not isinstance(output, dict):
            raise ValueError("Validation result entry is invalid")
        context = output.get("validation") or frozen.get("validation_context")
        metrics = output.get("metrics")
        if not isinstance(context, dict) or not isinstance(metrics, dict):
            raise ValueError("Completed validation requires its window and metrics")
        _store_validation_attempt(
            session,
            owner=owner,
            factor_id=output["factor_id"],
            validation_id=uuid.uuid4().hex,
            job_id=job_id,
            generation=generation,
            submitted_at=submitted_at,
            status="completed",
            context=context,
            metrics=metrics,
        )


def _record_failed_validation_attempt(
    session,
    *,
    owner: OwnerScope,
    job_id: str,
    generation: int,
    submitted_at,
    request_snapshot: dict[str, Any],
    progress_snapshot: dict[str, Any],
    error_code: str | None,
) -> None:
    request = request_snapshot.get("request")
    if not isinstance(request, dict):
        return
    items = request.get("factor_items")
    if isinstance(items, list):
        factor_id = progress_snapshot.get("factor_id")
        frozen = next(
            (
                item
                for item in items
                if isinstance(item, dict) and item.get("factor_id") == factor_id
            ),
            None,
        )
    else:
        factor_id = request.get("factor_id")
        frozen = request
    if not isinstance(factor_id, str) or not factor_id or not isinstance(frozen, dict):
        return
    context = frozen.get("validation_context")
    if not isinstance(context, dict):
        return
    _store_validation_attempt(
        session,
        owner=owner,
        factor_id=factor_id,
        validation_id=uuid.uuid4().hex,
        job_id=job_id,
        generation=generation,
        submitted_at=submitted_at,
        status="failed",
        context=context,
        error_code=error_code,
    )


def _record_materialization_outcome(
    session,
    *,
    owner,
    job_id: str,
    request_body: dict,
    generation_id: str,
    feature_key: str,
    ledger_status: str,
    corr,
    corr_against,
    evidence: dict,
) -> None:
    """Owner-scoped durable materialization outcome for saturation evidence.

    Direction is attributed through the factor's originating mining task at
    write time, so quota and memory reads stay single-table and owner-filtered;
    a factor without a traceable task records an empty direction.
    """
    direction_row = (
        session.execute(
            text(
                "SELECT left(COALESCE(t.direction,''),2000) AS direction "
                "FROM rd_agent_factors f "
                "LEFT JOIN alpha_mining_tasks t "
                "ON t.tenant_id=f.tenant_id AND t.user_id=f.user_id "
                "AND t.task_id=f.metadata_json->>'task_id' "
                "WHERE f.tenant_id=:tenant AND f.user_id=:user "
                "AND f.factor_id=:factor LIMIT 1"
            ),
            {
                "tenant": owner.tenant_id,
                "user": owner.user_id,
                "factor": request_body.get("factor_id"),
            },
        )
        .first()
    )
    session.execute(
        text(
            "INSERT INTO alpha_factor_materializations "
            "(tenant_id,user_id,job_id,factor_id,generation_id,feature_key,"
            "direction,status,corr,corr_against,evidence) "
            "VALUES (:tenant,:user,:job,:factor,:generation,:feature,"
            "left(:direction,2000),:status,:corr,:against,CAST(:evidence AS jsonb))"
        ),
        {
            "tenant": owner.tenant_id,
            "user": owner.user_id,
            "job": job_id,
            "factor": request_body.get("factor_id"),
            "generation": generation_id,
            "feature": feature_key,
            "direction": str(direction_row.direction) if direction_row else "",
            "status": ledger_status,
            "corr": corr,
            "against": corr_against,
            "evidence": json.dumps(
                evidence,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ),
        },
    )


def _canonical_snapshot(kind: str, request: dict[str, Any]) -> tuple[str, str]:
    if kind not in _KINDS or not isinstance(request, dict):
        raise ValueError("Invalid factor job kind or request")
    value = {"schema_version": 1, "kind": kind, "request": request}
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > 256 * 1024:
        raise ValueError("Factor job request exceeds 256 KiB")
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class AlphaFactorJobPersistence:
    """PG-owned child job queue; every public operation requires full OwnerScope."""

    @staticmethod
    async def ensure_tables() -> None:
        from backend.services.engine.alpha_agent.schema import task_schema_statements

        async with get_session() as session:
            for statement in task_schema_statements():
                await session.execute(text(statement))

    async def create(
        self,
        *,
        tenant_id: str,
        user_id: str,
        kind: str,
        request: dict[str, Any],
        parent_task_id: str | None = None,
        request_key: str | None = None,
        on_created: Callable[[Any, str], None] | None = None,
        max_pending_per_user: int | None = None,
        max_pending_global: int | None = None,
    ) -> str:
        owner = OwnerScope(tenant_id, user_id)
        if kind not in _KINDS:
            raise ValueError("Unsupported factor job kind")
        if request_key is not None and (
            not isinstance(request_key, str)
            or not request_key.strip()
            or len(request_key) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in request_key)
        ):
            raise ValueError("Invalid factor job idempotency key")
        snapshot, fingerprint = _canonical_snapshot(kind, request)
        from backend.services.engine.alpha_agent.factor_visibility import (
            lock_active_factors,
            referenced_factor_ids,
        )

        factor_ids = referenced_factor_ids(
            request, max_factors=100 if kind == "pool_research" else 20
        )
        job_id = uuid.uuid4().hex
        budget = require_ready_budget()
        default_user_limit, default_global_limit = pending_limits()
        user_queue_limit = (
            default_user_limit if max_pending_per_user is None else max_pending_per_user
        )
        global_queue_limit = (
            default_global_limit if max_pending_global is None else max_pending_global
        )
        if (
            type(user_queue_limit) is not int
            or type(global_queue_limit) is not int
            or min(user_queue_limit, global_queue_limit) <= 0
        ):
            raise ValueError("Factor job pending limits must be positive integers")

        def prepare(session):
            if request_key is not None:
                existing = (
                    session.execute(
                        text(
                            "SELECT job_id,request_fingerprint "
                            "FROM alpha_factor_job_request_keys "
                            "WHERE tenant_id=:tenant AND user_id=:user "
                            "AND request_key=:key"
                        ),
                        {
                            "tenant": owner.tenant_id,
                            "user": owner.user_id,
                            "key": request_key,
                        },
                    )
                    .mappings()
                    .first()
                )
                if existing:
                    if existing["request_fingerprint"] != fingerprint:
                        raise FactorJobIdempotencyConflict(
                            "Idempotency key belongs to different factor job input"
                        )
                    return "existing", existing["job_id"]
            existing = (
                session.execute(
                    text(
                        "SELECT job_id FROM alpha_factor_jobs WHERE tenant_id=:tenant "
                        "AND user_id=:user AND kind=:kind "
                        "AND request_fingerprint=:fingerprint "
                        "AND status IN ('queued','running','stopping') "
                        "ORDER BY created_at LIMIT 1"
                    ),
                    {
                        "tenant": owner.tenant_id,
                        "user": owner.user_id,
                        "kind": kind,
                        "fingerprint": fingerprint,
                    },
                )
                .mappings()
                .first()
            )
            if existing:
                if request_key is not None:
                    session.execute(
                        text(
                            "INSERT INTO alpha_factor_job_request_keys "
                            "(tenant_id,user_id,request_key,request_fingerprint,job_id) "
                            "VALUES (:tenant,:user,:key,:fingerprint,:job)"
                        ),
                        {
                            "tenant": owner.tenant_id,
                            "user": owner.user_id,
                            "key": request_key,
                            "fingerprint": fingerprint,
                            "job": existing["job_id"],
                        },
                    )
                return "existing", existing["job_id"]
            if kind != "serve_features":
                lock_active_factors(session, owner, factor_ids)
            from backend.services.engine.alpha_agent.artifact_cleanup import (
                lock_factor_origins,
            )

            if kind != "serve_features":
                lock_factor_origins(session, owner, factor_ids)
            return None

        def persist(session):
            session.execute(
                text(
                    "INSERT INTO alpha_factor_jobs "
                    "(tenant_id,user_id,job_id,lease_pool,parent_task_id,kind,status,"
                    "request_fingerprint,request_snapshot) VALUES "
                    "(:tenant,:user,:job,:pool,:parent,:kind,'queued',:fingerprint,"
                    "CAST(:snapshot AS jsonb))"
                ),
                {
                    "tenant": owner.tenant_id,
                    "user": owner.user_id,
                    "job": job_id,
                    "pool": POOL,
                    "parent": parent_task_id,
                    "kind": kind,
                    "fingerprint": fingerprint,
                    "snapshot": snapshot,
                },
            )
            if on_created is not None:
                on_created(session, job_id)
            if request_key is not None:
                session.execute(
                    text(
                        "INSERT INTO alpha_factor_job_request_keys "
                        "(tenant_id,user_id,request_key,request_fingerprint,job_id) "
                        "VALUES (:tenant,:user,:key,:fingerprint,:job)"
                    ),
                    {
                        "tenant": owner.tenant_id,
                        "user": owner.user_id,
                        "key": request_key,
                        "fingerprint": fingerprint,
                        "job": job_id,
                    },
                )
            session.execute(
                text(
                    "INSERT INTO alpha_factor_job_outbox "
                    "(tenant_id,user_id,job_id,event_type) "
                    "VALUES (:tenant,:user,:job,'run_factor_job')"
                ),
                {"tenant": owner.tenant_id, "user": owner.user_id, "job": job_id},
            )

        outcome, admitted_id = await asyncio.to_thread(
            budget.store.enqueue_transactional,
            POOL,
            job_id,
            owner.tenant_id,
            owner.user_id,
            user_budget=user_queue_limit,
            total_budget=global_queue_limit,
            prepare=prepare,
            persist=persist,
        )
        if outcome in {"user_full", "global_full"}:
            raise FactorJobQueueFull("Factor research job queue is full")
        if outcome not in {"queued", "existing"}:
            raise RuntimeError("Factor research job admission did not complete")
        return admitted_id

    async def get_by_request_key(
        self, request_key: str, *, tenant_id: str, user_id: str
    ) -> dict[str, Any] | None:
        owner = OwnerScope(tenant_id, user_id)
        async with get_session() as session:
            found = (
                await session.execute(
                    text(
                        "SELECT j.job_id FROM alpha_factor_job_request_keys k "
                        "JOIN alpha_factor_jobs j ON j.job_id=k.job_id "
                        "AND j.tenant_id=k.tenant_id AND j.user_id=k.user_id "
                        "WHERE k.tenant_id=:tenant_id AND k.user_id=:user_id "
                        "AND k.request_key=:key"
                    ),
                    {**owner.sql_params(), "key": request_key},
                )
            ).scalar_one_or_none()
        return (
            await self.get(found, tenant_id=tenant_id, user_id=user_id, consistent=True)
            if found
            else None
        )

    async def get(
        self, job_id: str, *, tenant_id: str, user_id: str, consistent: bool = False
    ) -> dict[str, Any] | None:
        owner = OwnerScope(tenant_id, user_id)
        async with get_session(read_only=not consistent) as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT job_id, parent_task_id, kind, status, "
                            "execution_generation, request_fingerprint, request_snapshot, "
                            "progress_snapshot, result_snapshot, error_code, error_message, "
                            "created_at, updated_at, completed_at FROM alpha_factor_jobs "
                            "WHERE job_id=:job AND tenant_id=:tenant_id AND user_id=:user_id"
                        ),
                        {"job": job_id, **owner.sql_params()},
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def delete_terminal(
        self, job_id: str, *, tenant_id: str, user_id: str
    ) -> str | None:
        """Hard-delete one finished backtest/validate job with its job-scoped rows.

        回测/验证的结果整份存在 ``result_snapshot`` 里，没有外部产物需要回收，所以
        「清除」就是真删——留一行软删标记只会让它永远占着用户的记录列表。所有语句
        都带 ``(tenant_id, user_id)``，跨属主只会 404 而不是删到别人的数据。

        Returns the deleted job status, or ``None`` when the owner has no such job.
        """
        owner = OwnerScope(tenant_id, user_id)
        params = {"job": job_id, **owner.sql_params()}
        async with get_session() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT status, kind FROM alpha_factor_jobs "
                            "WHERE job_id=:job AND tenant_id=:tenant_id "
                            "AND user_id=:user_id FOR UPDATE"
                        ),
                        params,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            if row["status"] in _ACTIVE:
                raise FactorJobNotTerminal(row["status"])
            if row["kind"] not in {"backtest", "validate"}:
                # 其它 kind（materialize/compose/doc_parse/...）带着产物与下游引用，
                # 由各自的回收链路处理，不在这里删。
                raise FactorJobProtected(str(row["kind"]))
            conquered = [
                # 验证历史通过 job_id 挂在任务上，必须先摘掉再删任务。
                "DELETE FROM alpha_factor_validation_latest WHERE job_id=:job "
                "AND tenant_id=:tenant_id AND user_id=:user_id",
                "DELETE FROM alpha_factor_validation_attempts WHERE job_id=:job "
                "AND tenant_id=:tenant_id AND user_id=:user_id",
                # 因子的生命周期事件属于因子本身，只摘掉对这条任务的外键引用。
                "UPDATE rd_agent_factor_lifecycle_events SET evidence_job_id=NULL "
                "WHERE evidence_job_id=:job AND tenant_id=:tenant_id AND user_id=:user_id",
                "DELETE FROM alpha_factor_job_request_keys WHERE job_id=:job "
                "AND tenant_id=:tenant_id AND user_id=:user_id",
                "DELETE FROM alpha_factor_job_outbox WHERE job_id=:job "
                "AND tenant_id=:tenant_id AND user_id=:user_id",
            ]
            try:
                for statement in conquered:
                    await session.execute(text(statement), params)
                deleted = await session.execute(
                    text(
                        "DELETE FROM alpha_factor_jobs WHERE job_id=:job "
                        "AND tenant_id=:tenant_id AND user_id=:user_id "
                        "AND status IN ('completed','failed','cancelled')"
                    ),
                    params,
                )
                if deleted.rowcount != 1:
                    raise FactorJobNotTerminal(str(row["status"]))
                # 租约由任务引用（jobs -> resource_task_leases），任务删完才轮到它。
                await session.execute(
                    text(
                        "DELETE FROM resource_task_leases WHERE pool='alpha_factor_jobs' "
                        "AND task_id=:job AND tenant_id=:tenant_id AND user_id=:user_id "
                        "AND state IN ('completed','failed','cancelled')"
                    ),
                    params,
                )
            except IntegrityError as exc:
                raise FactorJobProtected("still referenced") from exc
            return str(row["status"])

    async def list(
        self, *, tenant_id: str, user_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        owner = OwnerScope(tenant_id, user_id)
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("Factor job pagination is out of range")
        async with get_session(read_only=True) as session:
            rows = await session.execute(
                text(
                    "SELECT job_id, parent_task_id, kind, status, "
                    "execution_generation, request_fingerprint, request_snapshot, "
                    "progress_snapshot, result_snapshot, error_code, error_message, "
                    "created_at, updated_at, completed_at FROM alpha_factor_jobs "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id "
                    "ORDER BY created_at DESC, job_id DESC LIMIT :limit OFFSET :offset"
                ),
                {**owner.sql_params(), "limit": limit, "offset": offset},
            )
            return [dict(row) for row in rows.mappings().all()]

    async def history_page(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Read a bounded page of this owner's factor jobs and a has-more flag."""
        owner = OwnerScope(tenant_id, user_id)
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("Factor job pagination is out of range")
        allowed_statuses = {
            "pending",
            "queued",
            "running",
            "stopping",
            "completed",
            "failed",
            "cancelled",
        }
        if status is not None and status not in allowed_statuses:
            raise ValueError("Unsupported factor job status filter")
        conditions = ["tenant_id=:tenant_id", "user_id=:user_id"]
        params: dict[str, Any] = {
            **owner.sql_params(),
            "limit": limit + 1,
            "offset": offset,
        }
        if status is not None:
            conditions.append("status=:status")
            params["status"] = status
        async with get_session(read_only=True) as session:
            rows = await session.execute(
                text(
                    "SELECT job_id, parent_task_id, kind, status, "
                    "execution_generation, request_fingerprint, request_snapshot, "
                    "progress_snapshot, result_snapshot, error_code, error_message, "
                    "created_at, updated_at, completed_at FROM alpha_factor_jobs "
                    f"WHERE {' AND '.join(conditions)} "
                    "ORDER BY created_at DESC, job_id DESC LIMIT :limit OFFSET :offset"
                ),
                params,
            )
            page = [dict(row) for row in rows.mappings().all()]
        return {
            "tasks": page[:limit],
            "has_more": len(page) > limit,
            "limit": limit,
            "offset": offset,
        }

    async def validation_history(
        self,
        factor_id: str,
        *,
        tenant_id: str,
        user_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        owner = OwnerScope(tenant_id, user_id)
        if not isinstance(factor_id, str) or not factor_id.strip():
            raise ValueError("Factor ID is required")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Validation history limit must be between 1 and 100")
        async with get_session(read_only=True) as session:
            result = await session.execute(
                text(
                    "SELECT validation_id,factor_id,job_id,execution_generation,status,"
                    "validation_context,result_snapshot,error_code,created_at "
                    "FROM alpha_factor_validation_attempts "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id AND factor_id=:factor_id "
                    "ORDER BY created_at DESC,validation_id DESC LIMIT :limit"
                ),
                {**owner.sql_params(), "factor_id": factor_id, "limit": limit},
            )
            return [dict(row) for row in result.mappings().all()]

    async def first_completed_backtest_at(
        self,
        factor_ids: list[str],
        *,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Return first successful backtest timestamps for this owner's factors."""
        owner = OwnerScope(tenant_id, user_id)
        if not isinstance(factor_ids, list) or len(factor_ids) > 500:
            raise ValueError("Factor lifecycle selection exceeds 500 IDs")
        if any(not isinstance(item, str) or not item for item in factor_ids):
            raise ValueError("Factor lifecycle selection contains an invalid ID")
        if not factor_ids:
            return {}
        async with get_session(read_only=True) as session:
            result = await session.execute(
                text(
                    "WITH factor_refs AS ("
                    "SELECT job.tenant_id,job.user_id,job.completed_at,"
                    "job.request_snapshot #>> '{request,factor_id}' AS factor_id "
                    "FROM alpha_factor_jobs AS job "
                    "WHERE job.tenant_id=:tenant_id AND job.user_id=:user_id "
                    "AND job.kind='backtest' AND job.status='completed' "
                    "AND job.result_snapshot->>'factor_id'=ANY(CAST(:factor_ids AS TEXT[])) "
                    "UNION ALL "
                    "SELECT job.tenant_id,job.user_id,job.completed_at,"
                    "member.value->>'factor_id' AS factor_id "
                    "FROM alpha_factor_jobs AS job CROSS JOIN LATERAL jsonb_array_elements("
                    "CASE WHEN jsonb_typeof(job.request_snapshot #> '{request,factor_items}')='array' "
                    "THEN job.request_snapshot #> '{request,factor_items}' ELSE '[]'::jsonb END"
                    ") AS member(value) "
                    "WHERE job.tenant_id=:tenant_id AND job.user_id=:user_id "
                    "AND job.kind='backtest' AND job.status='completed' "
                    "AND member.value->>'factor_id'=ANY(CAST(:factor_ids AS TEXT[]))"
                    ") SELECT factor_id,MIN(completed_at) AS first_completed_at "
                    "FROM factor_refs WHERE factor_id=ANY(CAST(:factor_ids AS TEXT[])) "
                    "GROUP BY factor_id"
                ),
                {**owner.sql_params(), "factor_ids": factor_ids},
            )
            return {
                str(row["factor_id"]): row["first_completed_at"]
                for row in result.mappings().all()
                if row.get("factor_id") and row.get("first_completed_at") is not None
            }

    async def latest_completed_backtest_evidence(
        self,
        factor_ids: list[str],
        *,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, dict[str, Any]]:
        """Return each factor's latest completed result from this owner's jobs."""
        owner = OwnerScope(tenant_id, user_id)
        if not isinstance(factor_ids, list) or len(factor_ids) > 500:
            raise ValueError("Factor lifecycle selection exceeds 500 IDs")
        if any(not isinstance(item, str) or not item for item in factor_ids):
            raise ValueError("Factor lifecycle selection contains an invalid ID")
        if not factor_ids:
            return {}
        async with get_session(read_only=True) as session:
            result = await session.execute(
                text(
                    "WITH evidence AS ("
                    "SELECT job.tenant_id,job.user_id,job.job_id,job.completed_at,"
                    "job.result_snapshot->>'factor_id' AS factor_id,"
                    "job.result_snapshot AS factor_result "
                    "FROM alpha_factor_jobs AS job "
                    "WHERE job.tenant_id=:tenant_id AND job.user_id=:user_id "
                    "AND job.kind='backtest' AND job.status='completed' "
                    "AND job.result_snapshot->>'factor_id'=ANY(CAST(:factor_ids AS TEXT[])) "
                    "UNION ALL "
                    "SELECT job.tenant_id,job.user_id,job.job_id,job.completed_at,"
                    "item.value->>'factor_id' AS factor_id,item.value AS factor_result "
                    "FROM alpha_factor_jobs AS job CROSS JOIN LATERAL jsonb_array_elements("
                    "CASE WHEN jsonb_typeof(job.result_snapshot->'results')='array' "
                    "THEN job.result_snapshot->'results' ELSE '[]'::jsonb END"
                    ") AS item(value) "
                    "WHERE job.tenant_id=:tenant_id AND job.user_id=:user_id "
                    "AND job.kind='backtest' AND job.status='completed' "
                    "AND item.value->>'factor_id'=ANY(CAST(:factor_ids AS TEXT[]))"
                    "), latest_revisions AS ("
                    "SELECT tenant_id,user_id,factor_id,MAX(revision_no) AS revision_no "
                    "FROM rd_agent_factor_revisions WHERE tenant_id=:tenant_id "
                    "AND user_id=:user_id AND factor_id=ANY(CAST(:factor_ids AS TEXT[])) "
                    "GROUP BY tenant_id,user_id,factor_id"
                    ") SELECT DISTINCT ON (evidence.factor_id) evidence.factor_id,"
                    "evidence.job_id,evidence.completed_at,evidence.factor_result "
                    "FROM evidence JOIN rd_agent_factors AS factor "
                    "ON factor.tenant_id=evidence.tenant_id "
                    "AND factor.user_id=evidence.user_id "
                    "AND factor.factor_id=evidence.factor_id "
                    "JOIN latest_revisions AS revision "
                    "ON revision.tenant_id=evidence.tenant_id "
                    "AND revision.user_id=evidence.user_id "
                    "AND revision.factor_id=evidence.factor_id "
                    "AND evidence.factor_result->>'factor_revision_no'=revision.revision_no::text "
                    "WHERE evidence.tenant_id=:tenant_id AND evidence.user_id=:user_id "
                    "AND evidence.factor_id=ANY(CAST(:factor_ids AS TEXT[])) "
                    "ORDER BY evidence.factor_id,evidence.completed_at DESC,evidence.job_id DESC"
                ),
                {**owner.sql_params(), "factor_ids": factor_ids},
            )
            return {
                str(row["factor_id"]): {
                    "job_id": str(row["job_id"]),
                    "completed_at": row["completed_at"],
                    "result": row["factor_result"],
                }
                for row in result.mappings().all()
                if row.get("factor_id")
            }

    async def active_validation_factor_ids(
        self,
        factor_ids: list[str],
        *,
        tenant_id: str,
        user_id: str,
    ) -> set[str]:
        """Return only this owner's factors with a currently running validation."""
        owner = OwnerScope(tenant_id, user_id)
        if not isinstance(factor_ids, list) or len(factor_ids) > 500:
            raise ValueError("Factor lifecycle selection exceeds 500 IDs")
        if any(not isinstance(item, str) or not item for item in factor_ids):
            raise ValueError("Factor lifecycle selection contains an invalid ID")
        if not factor_ids:
            return set()
        async with get_session(read_only=True) as session:
            result = await session.execute(
                text(
                    "SELECT DISTINCT factor_id FROM alpha_factor_validation_attempts "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id "
                    "AND factor_id=ANY(CAST(:factor_ids AS TEXT[])) AND status='running'"
                ),
                {**owner.sql_params(), "factor_ids": factor_ids},
            )
            return {
                str(row["factor_id"])
                for row in result.mappings().all()
                if row.get("factor_id")
            }

    async def latest_validation_id(
        self,
        factor_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> str | None:
        owner = OwnerScope(tenant_id, user_id)
        async with get_session(read_only=True) as session:
            result = await session.execute(
                text(
                    "SELECT validation_id FROM alpha_factor_validation_latest "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id AND factor_id=:factor_id"
                ),
                {**owner.sql_params(), "factor_id": factor_id},
            )
            row = result.mappings().first()
            return str(row["validation_id"]) if row else None

    async def get_for_dispatch(self, job_id: str) -> dict[str, Any] | None:
        """Resolve a queued job's persisted owner for trusted internal dispatch only."""
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT job.tenant_id,job.user_id,job.job_id,job.kind,"
                            "job.status,job.execution_generation,job.request_snapshot "
                            "FROM alpha_factor_jobs AS job JOIN resource_task_leases AS lease "
                            "ON lease.pool=job.lease_pool AND lease.task_id=job.job_id "
                            "AND lease.tenant_id=job.tenant_id AND lease.user_id=job.user_id "
                            "WHERE job.job_id=:job AND job.status='queued' "
                            "AND lease.state='queued'"
                        ),
                        {"job": job_id},
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def get_for_replay(
        self, job_id: str, *, dispatch_token: str
    ) -> dict[str, Any] | None:
        """Resolve only an active execution already bound to this delivery token."""
        if not dispatch_token or len(dispatch_token) > 128:
            return None
        # A claim may have committed milliseconds ago; a lagging replica could
        # otherwise hide it and acknowledge the only in-flight delivery.
        async with get_session() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT job.tenant_id,job.user_id,job.job_id,job.kind,"
                            "job.status,job.execution_generation,job.request_snapshot "
                            "FROM alpha_factor_jobs AS job JOIN resource_task_leases AS lease "
                            "ON lease.pool=job.lease_pool AND lease.task_id=job.job_id "
                            "AND lease.tenant_id=job.tenant_id AND lease.user_id=job.user_id "
                            "WHERE job.job_id=:job AND job.status IN ('running','stopping') "
                            "AND lease.state='running' "
                            "AND lease.executor->>'dispatch_token'=:dispatch_token"
                        ),
                        {"job": job_id, "dispatch_token": dispatch_token},
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def claim_outbox(self, *, limit: int = 32) -> list[dict[str, Any]]:
        """Lease a bounded batch of durable dispatch events for a publisher."""
        if not 1 <= limit <= 128:
            raise ValueError("Factor job dispatch batch is out of range")
        claimed: list[dict[str, str]] = []
        async with get_session() as session:
            rows = await session.execute(
                text(
                    "SELECT outbox.tenant_id,outbox.user_id,outbox.job_id,job.kind "
                    "FROM alpha_factor_job_outbox AS outbox "
                    "JOIN alpha_factor_jobs AS job ON job.tenant_id=outbox.tenant_id "
                    "AND job.user_id=outbox.user_id AND job.job_id=outbox.job_id "
                    "JOIN resource_task_leases AS lease ON lease.pool=job.lease_pool "
                    "AND lease.task_id=job.job_id AND lease.tenant_id=job.tenant_id "
                    "AND lease.user_id=job.user_id "
                    "WHERE outbox.event_type='run_factor_job' AND job.status='queued' "
                    "AND lease.state='queued' AND outbox.available_at<=now() "
                    "AND (outbox.claim_until IS NULL OR outbox.claim_until<now()) "
                    "ORDER BY outbox.available_at,outbox.created_at,outbox.job_id "
                    "FOR UPDATE OF outbox SKIP LOCKED LIMIT :limit"
                ),
                {"limit": limit},
            )
            for row in rows.mappings().all():
                token = uuid.uuid4().hex
                result = await session.execute(
                    text(
                        "UPDATE alpha_factor_job_outbox SET dispatch_token=:token, "
                        "claim_until=now()+make_interval(secs=>60),attempts=attempts+1 "
                        "WHERE tenant_id=:tenant AND user_id=:user AND job_id=:job "
                        "AND event_type='run_factor_job' "
                        "RETURNING tenant_id,user_id,job_id,attempts"
                    ),
                    {
                        "token": token,
                        "tenant": row["tenant_id"],
                        "user": row["user_id"],
                        "job": row["job_id"],
                    },
                )
                updated = result.mappings().first()
                if updated:
                    claimed.append(
                        {
                            "tenant_id": updated["tenant_id"],
                            "user_id": updated["user_id"],
                            "job_id": updated["job_id"],
                            "dispatch_token": token,
                            "attempts": updated["attempts"],
                            "kind": row.get("kind", ""),
                        }
                    )
        return claimed

    async def acknowledge_outbox(self, item: dict[str, Any]) -> bool:
        owner = OwnerScope(item.get("tenant_id"), item.get("user_id"))
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE alpha_factor_job_outbox SET published_at=now(),"
                    "claim_until=now()+make_interval(secs=>60),last_error=NULL "
                    "WHERE tenant_id=:tenant_id "
                    "AND user_id=:user_id AND job_id=:job_id "
                    "AND event_type='run_factor_job' AND dispatch_token=:token"
                ),
                {
                    **owner.sql_params(),
                    "job_id": item.get("job_id"),
                    "token": item.get("dispatch_token"),
                },
            )
            return result.rowcount == 1

    async def retry_outbox(
        self, item: dict[str, Any], *, delay_seconds: int, error: str
    ) -> bool:
        owner = OwnerScope(item.get("tenant_id"), item.get("user_id"))
        delay = min(max(int(delay_seconds), 1), 900)
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE alpha_factor_job_outbox SET available_at="
                    "now()+make_interval(secs=>:delay),claim_until=NULL,"
                    "published_at=NULL,last_error=:error "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id AND job_id=:job_id "
                    "AND event_type='run_factor_job' AND dispatch_token=:token"
                ),
                {
                    **owner.sql_params(),
                    "job_id": item.get("job_id"),
                    "token": item.get("dispatch_token"),
                    "delay": delay,
                    "error": error[:1000],
                },
            )
            return result.rowcount == 1

    async def claim(
        self,
        job_id: str,
        *,
        tenant_id: str,
        user_id: str,
        executor: dict[str, Any] | None = None,
    ) -> int | None:
        """Atomically reserve a queued job and return its fresh execution generation."""
        owner = OwnerScope(tenant_id, user_id)
        budget = require_ready_budget()
        from backend.shared.resource_lease import LeaseUnavailable

        generation: int | None = None

        def persist(session) -> None:
            nonlocal generation
            row = (
                session.execute(
                    text(
                        "UPDATE alpha_factor_jobs SET status='running', "
                        "execution_generation=execution_generation+1,updated_at=now() "
                        "WHERE job_id=:job AND tenant_id=:tenant_id AND user_id=:user_id "
                        "AND status='queued' RETURNING execution_generation"
                    ),
                    {"job": job_id, **owner.sql_params()},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise RuntimeError("Factor job changed before lease reservation")
            generation = int(row["execution_generation"])

        lease = await asyncio.to_thread(
            budget.store.reserve_transactional,
            POOL,
            job_id,
            owner.tenant_id,
            owner.user_id,
            per_user=budget.projection.per_user,
            capacity=budget.projection.capacity,
            persist=persist,
            executor=executor,
        )
        if lease is None:
            return None
        projection_state = await asyncio.to_thread(budget.projection.acquire, lease)
        if projection_state not in {"acquired", "held"}:
            # The durable reserved lease and running job remain occupied for recovery.
            raise LeaseUnavailable("Factor job lease projection could not be acquired")
        transitioned = await asyncio.to_thread(
            budget.store.transition, POOL, lease, "running"
        )
        if not transitioned:
            if generation is not None:
                await self.confirm_cancelled(
                    job_id,
                    tenant_id=owner.tenant_id,
                    user_id=owner.user_id,
                    generation=generation,
                )
            raise LeaseUnavailable(
                "Factor job durable lease could not enter running state"
            )
        return generation

    async def update_progress(
        self,
        job_id: str,
        *,
        tenant_id: str,
        user_id: str,
        generation: int,
        progress: dict[str, Any],
    ) -> bool:
        owner = OwnerScope(tenant_id, user_id)
        if type(generation) is not int or generation <= 0:
            raise ValueError("Factor job generation must be a positive integer")
        if not isinstance(progress, dict):
            raise ValueError("Factor job progress must be an object")
        encoded = json.dumps(
            progress,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise ValueError("Factor job progress exceeds 16 KiB")
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE alpha_factor_jobs SET progress_snapshot=CAST(:progress AS jsonb), "
                    "updated_at=now() WHERE job_id=:job AND tenant_id=:tenant_id "
                    "AND user_id=:user_id AND execution_generation=:generation "
                    "AND status='running'"
                ),
                {
                    "progress": encoded,
                    "job": job_id,
                    **owner.sql_params(),
                    "generation": generation,
                },
            )
            return result.rowcount == 1

    async def request_cancel(
        self,
        job_id: str,
        *,
        tenant_id: str,
        user_id: str,
        generation: int,
    ) -> str | None:
        owner = OwnerScope(tenant_id, user_id)
        if type(generation) is not int or generation < 0:
            raise ValueError("Factor job generation must be a nonnegative integer")
        from backend.services.engine.alpha_agent.factor_job_budget import get_budget

        budget = get_budget()

        def on_transition(session, status: str) -> None:
            result = session.execute(
                text(
                    "UPDATE alpha_factor_jobs SET status=:status,updated_at=now(), "
                    "completed_at=CASE WHEN :status='cancelled' THEN now() ELSE completed_at END "
                    "WHERE job_id=:job AND tenant_id=:tenant AND user_id=:user "
                    "AND execution_generation=:generation "
                    "AND status IN ('queued','running','stopping')"
                ),
                {
                    "status": status,
                    "job": job_id,
                    "tenant": owner.tenant_id,
                    "user": owner.user_id,
                    "generation": generation,
                },
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    "Factor job generation or status changed during cancel"
                )

        outcome = await asyncio.to_thread(
            budget.store.request_stop_transactional,
            POOL,
            job_id,
            owner.tenant_id,
            owner.user_id,
            on_transition=on_transition,
        )
        return outcome

    async def reconcile_uncertain_document_parse(
        self,
        job_id: str,
        *,
        doc_id: str,
        tenant_id: str,
        user_id: str,
        batch_id: str,
    ) -> bool:
        """Attach a provider-verified batch and resume its stopped owner job atomically."""
        owner = OwnerScope(tenant_id, user_id)
        if not batch_id or len(batch_id) > 256:
            raise ValueError("Invalid MinerU batch ID")
        from backend.services.engine.alpha_agent.factor_job_budget import get_budget

        budget = get_budget()
        with budget.store.session_factory() as session, session.begin():
            if not session.execute(
                text(
                    "SELECT pool FROM resource_lease_pools WHERE pool=:pool FOR UPDATE"
                ),
                {"pool": POOL},
            ).first():
                return False
            lease = (
                session.execute(
                    text(
                        "SELECT state,token,epoch,executor FROM resource_task_leases "
                        "WHERE pool=:pool AND task_id=:job AND tenant_id=:tenant_id "
                        "AND user_id=:user_id FOR UPDATE"
                    ),
                    {"pool": POOL, "job": job_id, **owner.sql_params()},
                )
                .mappings()
                .first()
            )
            job = (
                session.execute(
                    text(
                        "SELECT kind,status,execution_generation,request_snapshot "
                        "FROM alpha_factor_jobs WHERE job_id=:job AND tenant_id=:tenant_id "
                        "AND user_id=:user_id FOR UPDATE"
                    ),
                    {"job": job_id, **owner.sql_params()},
                )
                .mappings()
                .first()
            )
            executor = (lease or {}).get("executor") or {}
            if (
                not lease
                or not job
                or job["kind"] != "doc_parse"
                or job["status"] != "stopping"
                or lease["state"] != "running"
                or not executor.get("stop_requested")
                or executor.get("engine_request_state") != "finished"
                or not executor.get("engine_claim_token")
                or not executor.get("engine_request_id")
            ):
                return False
            snapshot = job["request_snapshot"]
            if isinstance(snapshot, str):
                snapshot = json.loads(snapshot)
            request = snapshot.get("request") if isinstance(snapshot, dict) else None
            if not isinstance(request, dict) or request.get("doc_id") != doc_id:
                return False

            updated_doc = session.execute(
                text(
                    "UPDATE rd_agent_docs SET parser_batch_id=:batch_id,"
                    "parse_state='pending',error=NULL,updated_at=now() "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id AND doc_id=:doc "
                    "AND parse_job_id=:job AND status='parsing' "
                    "AND parse_state='uncertain' AND parser_batch_id IS NULL"
                ),
                {
                    **owner.sql_params(),
                    "doc": doc_id,
                    "job": job_id,
                    "batch_id": batch_id,
                },
            )
            if updated_doc.rowcount != 1:
                return False
            resumed_lease = session.execute(
                text(
                    "UPDATE resource_task_leases SET executor="
                    "jsonb_set(executor - 'stop_requested','{engine_request_state}',"
                    "'\"finished\"'::jsonb,true),updated_at=now() "
                    "WHERE pool=:pool AND task_id=:job AND tenant_id=:tenant_id "
                    "AND user_id=:user_id AND state='running' AND token=:token "
                    "AND epoch=:epoch AND executor->>'stop_requested'='true' "
                    "AND executor->>'engine_request_state'='finished' "
                    "AND executor->>'engine_claim_token'=:claim_token "
                    "AND executor->>'engine_request_id'=:request_id "
                    "AND executor->>'execution_generation'=:generation_text"
                ),
                {
                    "pool": POOL,
                    "job": job_id,
                    **owner.sql_params(),
                    "token": lease["token"],
                    "epoch": lease["epoch"],
                    "claim_token": executor["engine_claim_token"],
                    "request_id": executor["engine_request_id"],
                    "generation_text": str(job["execution_generation"]),
                },
            )
            resumed_job = session.execute(
                text(
                    "UPDATE alpha_factor_jobs SET status='running',updated_at=now() "
                    "WHERE job_id=:job AND tenant_id=:tenant_id AND user_id=:user_id "
                    "AND execution_generation=:generation AND kind='doc_parse' "
                    "AND status='stopping'"
                ),
                {
                    "job": job_id,
                    **owner.sql_params(),
                    "generation": job["execution_generation"],
                },
            )
            if resumed_lease.rowcount != 1 or resumed_job.rowcount != 1:
                raise RuntimeError("Document parse changed during reconciliation")
            return True

    async def finish(
        self,
        job_id: str,
        *,
        tenant_id: str,
        user_id: str,
        generation: int,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        on_finish: Callable[[Any], None] | None = None,
    ) -> bool:
        owner = OwnerScope(tenant_id, user_id)
        if type(generation) is not int or generation <= 0:
            raise ValueError("Factor job generation must be a positive integer")
        if (result is None) == (error_code is None and error_message is None):
            raise ValueError(
                "A factor job must finish with either a result or an error"
            )
        encoded = None
        if result is not None:
            if not isinstance(result, dict):
                raise ValueError("Factor job result must be an object")
            encoded = json.dumps(
                result,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            if len(encoded.encode("utf-8")) > 4 * 1024 * 1024:
                raise ValueError("Factor job result exceeds 4 MiB")
        if error_code is not None and (
            not isinstance(error_code, str) or len(error_code) > 128
        ):
            raise ValueError("Invalid factor job error code")
        if error_message is not None and (
            not isinstance(error_message, str) or len(error_message) > 2000
        ):
            raise ValueError("Invalid factor job error message")
        status = "completed" if result is not None else "failed"
        from backend.services.engine.alpha_agent.factor_job_budget import get_budget

        budget = get_budget()
        lease = await asyncio.to_thread(
            budget.store.get_lease, POOL, job_id, owner.tenant_id, owner.user_id
        )
        if lease is None:
            return False

        def persist(session) -> None:
            if status in {"completed", "failed"}:
                locked_job = (
                    session.execute(
                        text(
                            "SELECT kind,request_snapshot,progress_snapshot,created_at "
                            "FROM alpha_factor_jobs "
                            "WHERE job_id=:job AND tenant_id=:tenant AND user_id=:user "
                            "AND execution_generation=:generation AND status='running' FOR UPDATE"
                        ),
                        {
                            "job": job_id,
                            "tenant": owner.tenant_id,
                            "user": owner.user_id,
                            "generation": generation,
                        },
                    )
                    .mappings()
                    .first()
                )
                if locked_job is None:
                    raise RuntimeError(
                        "Factor job generation or status changed before finish"
                    )
                if locked_job["kind"] == "validate":
                    request_snapshot = locked_job["request_snapshot"]
                    if isinstance(request_snapshot, str):
                        request_snapshot = json.loads(request_snapshot)
                    if not isinstance(request_snapshot, dict):
                        raise ValueError("Validation request snapshot is invalid")
                    if status == "completed":
                        _record_completed_validation_attempts(
                            session,
                            owner=owner,
                            job_id=job_id,
                            generation=generation,
                            submitted_at=locked_job["created_at"],
                            request_snapshot=request_snapshot,
                            result_snapshot=result,
                        )
                    else:
                        progress_snapshot = locked_job["progress_snapshot"] or {}
                        if isinstance(progress_snapshot, str):
                            progress_snapshot = json.loads(progress_snapshot)
                        if isinstance(progress_snapshot, dict):
                            _record_failed_validation_attempt(
                                session,
                                owner=owner,
                                job_id=job_id,
                                generation=generation,
                                submitted_at=locked_job["created_at"],
                                request_snapshot=request_snapshot,
                                progress_snapshot=progress_snapshot,
                                error_code=error_code,
                            )
                if locked_job["kind"] == "materialize":
                    request_snapshot = locked_job["request_snapshot"]
                    if isinstance(request_snapshot, str):
                        request_snapshot = json.loads(request_snapshot)
                    request_body = (
                        request_snapshot.get("request")
                        if isinstance(request_snapshot, dict)
                        else None
                    )
                    if not isinstance(request_body, dict):
                        raise ValueError("Factor materialization request is invalid")
                    generation_id = request_body.get("generation_id")
                    if not isinstance(generation_id, str) or not generation_id:
                        raise ValueError("Factor materialization generation is missing")
                    if status == "completed" and isinstance(
                        result, dict
                    ) and result.get("status") == "rejected_duplicate":
                        if (
                            result.get("generation_id") != generation_id
                            or result.get("factor_id")
                            != request_body.get("factor_id")
                            or result.get("factor_revision_no")
                            != request_body.get("factor_revision_no")
                            or result.get("factor_code_sha256")
                            != request_body.get("factor_code_sha256")
                            or result.get("factor_kind")
                            != request_body.get("factor_kind")
                            or not isinstance(result.get("feature_key"), str)
                            or not result["feature_key"]
                            or len(result["feature_key"]) > 128
                            or type(result.get("corr")) is not float
                            or not 0.5 <= result["corr"] <= 1.0
                            or not isinstance(result.get("corr_against"), str)
                            or not result["corr_against"]
                            or len(result["corr_against"]) > 128
                            or not isinstance(result.get("evidence"), dict)
                        ):
                            raise ValueError(
                                "Factor materialization rejection is invalid"
                            )
                        # Duplicate outcomes never publish a generation, switch
                        # the active pointer, or retire other published columns.
                        archived_generation = session.execute(
                            text(
                                "UPDATE alpha_user_factor_generations SET "
                                "status='archived' "
                                "WHERE tenant_id=:tenant AND user_id=:user "
                                "AND generation_id=:generation AND job_id=:job "
                                "AND status='building' AND dataset_key='rd_mined'"
                            ),
                            {
                                "tenant": owner.tenant_id,
                                "user": owner.user_id,
                                "generation": generation_id,
                                "job": job_id,
                            },
                        )
                        if archived_generation.rowcount != 1:
                            raise RuntimeError(
                                "Factor generation is no longer building"
                            )
                        _record_materialization_outcome(
                            session,
                            owner=owner,
                            job_id=job_id,
                            request_body=request_body,
                            generation_id=generation_id,
                            feature_key=result["feature_key"],
                            ledger_status="rejected_duplicate",
                            corr=result["corr"],
                            corr_against=result["corr_against"],
                            evidence=result["evidence"],
                        )
                    elif status == "completed":
                        if isinstance(result, dict) and result.get("status") == "replaced":
                            from backend.services.engine.alpha_agent.factor_materialize_gate import (
                                REPLACE_IC_FLOOR,
                                REPLACE_IC_RATIO,
                            )

                            replacement_evidence = (
                                result.get("library_gate") or {}
                            ).get("evidence") or {}
                            replacement_detail = (
                                replacement_evidence.get("replacement") or {}
                            )
                            new_ic = result.get("new_ic")
                            incumbent_ic = result.get("incumbent_ic")
                            if (
                                not isinstance(result.get("replaced_generation_id"), str)
                                or not result["replaced_generation_id"]
                                or len(result["replaced_generation_id"]) > 64
                                or result["replaced_generation_id"] == generation_id
                                or not isinstance(result.get("replaced_factor_id"), str)
                                or not result["replaced_factor_id"]
                                or result["replaced_factor_id"]
                                == request_body.get("factor_id")
                                or type(new_ic) is not float
                                or not 0 <= abs(new_ic) <= 1
                                or type(incumbent_ic) is not float
                                or not 0 <= abs(incumbent_ic) <= 1
                                or abs(new_ic) < REPLACE_IC_FLOOR
                                or abs(new_ic) < REPLACE_IC_RATIO * abs(incumbent_ic)
                                or replacement_detail.get("decision") != "ok"
                                or replacement_detail.get("replaced_generation_id")
                                != result["replaced_generation_id"]
                            ):
                                raise ValueError(
                                    "Factor materialization replacement is invalid"
                                )
                            # The incumbent must still be published: a concurrent
                            # materialization may have archived it already, in
                            # which case the replacement basis is gone and the
                            # job fails closed instead of publishing blindly.
                            incumbent = session.execute(
                                text(
                                    "SELECT status FROM alpha_user_factor_generations "
                                    "WHERE tenant_id=:tenant AND user_id=:user "
                                    "AND generation_id=:generation "
                                    "AND dataset_key='rd_mined'"
                                ),
                                {
                                    "tenant": owner.tenant_id,
                                    "user": owner.user_id,
                                    "generation": result["replaced_generation_id"],
                                },
                            ).first()
                            if incumbent is None or incumbent.status != "published":
                                raise RuntimeError(
                                    "Superseded generation is no longer published"
                                )
                        if (
                            not isinstance(result, dict)
                            or result.get("generation_id") != generation_id
                            or result.get("dataset_key") != "rd_mined"
                            or result.get("factor_id") != request_body.get("factor_id")
                            or result.get("factor_revision_no")
                            != request_body.get("factor_revision_no")
                            or result.get("factor_code_sha256")
                            != request_body.get("factor_code_sha256")
                            or result.get("factor_kind")
                            != request_body.get("factor_kind")
                            or not isinstance(result.get("feature_key"), str)
                            or type(result.get("row_count")) is not int
                            or result["row_count"] <= 0
                            or type(result.get("byte_count")) is not int
                            or result["byte_count"] <= 0
                            or not isinstance(result.get("library_gate"), dict)
                            or (
                                result["library_gate"].get("corr") is not None
                                and type(result["library_gate"].get("corr"))
                                is not float
                            )
                            or not (
                                result["library_gate"].get("corr_against") is None
                                or (
                                    isinstance(
                                        result["library_gate"].get("corr_against"),
                                        str,
                                    )
                                    and len(result["library_gate"]["corr_against"])
                                    <= 128
                                )
                            )
                            or not isinstance(
                                result["library_gate"].get("evidence"), dict
                            )
                        ):
                            raise ValueError("Factor materialization result is invalid")
                        updated_generation = session.execute(
                            text(
                                "UPDATE alpha_user_factor_generations SET "
                                "status='published',manifest_json=CAST(:manifest AS jsonb),"
                                "row_count=:rows,byte_count=:bytes,published_at=now() "
                                "WHERE tenant_id=:tenant AND user_id=:user "
                                "AND generation_id=:generation AND job_id=:job "
                                "AND status='building' AND dataset_key='rd_mined'"
                            ),
                            {
                                "manifest": json.dumps(
                                    {
                                        key: result[key]
                                        for key in (
                                            "dataset_key",
                                            "feature_key",
                                            "factor_id",
                                            "factor_code_sha256",
                                            "factor_revision_no",
                                            "factor_kind",
                                            "partition_count",
                                            "universe",
                                            "start_date",
                                            "end_date",
                                        )
                                    } | {"input_evidence": result.get("input_evidence")},
                                    sort_keys=True,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                    allow_nan=False,
                                ),
                                "rows": result["row_count"],
                                "bytes": result["byte_count"],
                                "tenant": owner.tenant_id,
                                "user": owner.user_id,
                                "generation": generation_id,
                                "job": job_id,
                            },
                        )
                        if updated_generation.rowcount != 1:
                            raise RuntimeError(
                                "Factor generation is no longer building"
                            )
                        session.execute(
                            text(
                                "INSERT INTO alpha_user_factor_generation_features "
                                "(tenant_id,user_id,generation_id,feature_key,factor_id,"
                                "factor_code_sha256,factor_revision_no,row_count) VALUES "
                                "(:tenant,:user,:generation,:feature,:factor,:sha,:revision,:rows)"
                            ),
                            {
                                "tenant": owner.tenant_id,
                                "user": owner.user_id,
                                "generation": generation_id,
                                "feature": result["feature_key"],
                                "factor": result["factor_id"],
                                "sha": result["factor_code_sha256"],
                                "revision": result["factor_revision_no"],
                                "rows": result["row_count"],
                            },
                        )
                        session.execute(
                            text(
                                "INSERT INTO alpha_user_factor_dataset_active "
                                "(tenant_id,user_id,dataset_key,generation_id) "
                                "VALUES (:tenant,:user,'rd_mined',:generation) "
                                "ON CONFLICT (tenant_id,user_id,dataset_key) DO UPDATE "
                                "SET generation_id=EXCLUDED.generation_id,updated_at=now()"
                            ),
                            {
                                "tenant": owner.tenant_id,
                                "user": owner.user_id,
                                "generation": generation_id,
                            },
                        )
                        session.execute(
                            text(
                                "UPDATE alpha_user_factor_generations SET status='archived' "
                                "WHERE tenant_id=:tenant AND user_id=:user "
                                "AND dataset_key='rd_mined' AND status='published' "
                                "AND generation_id<>:generation"
                            ),
                            {
                                "tenant": owner.tenant_id,
                                "user": owner.user_id,
                                "generation": generation_id,
                            },
                        )
                        _record_materialization_outcome(
                            session,
                            owner=owner,
                            job_id=job_id,
                            request_body=request_body,
                            generation_id=generation_id,
                            feature_key=result["feature_key"],
                            ledger_status="materialized",
                            corr=result["library_gate"]["corr"],
                            corr_against=result["library_gate"]["corr_against"],
                            evidence=result["library_gate"]["evidence"],
                        )
                    else:
                        session.execute(
                            text(
                                "UPDATE alpha_user_factor_generations SET status='failed' "
                                "WHERE tenant_id=:tenant AND user_id=:user "
                                "AND generation_id=:generation AND job_id=:job "
                                "AND status='building'"
                            ),
                            {
                                "tenant": owner.tenant_id,
                                "user": owner.user_id,
                                "generation": generation_id,
                                "job": job_id,
                            },
                        )
            if on_finish is not None:
                if status != "completed":
                    raise ValueError(
                        "on_finish is only valid for completed factor jobs"
                    )
                on_finish(session)
            updated = session.execute(
                text(
                    "UPDATE alpha_factor_jobs SET status=:status, "
                    "result_snapshot=CAST(:result AS jsonb),error_code=:error_code, "
                    "error_message=:error_message,updated_at=now(),completed_at=now() "
                    "WHERE job_id=:job AND tenant_id=:tenant AND user_id=:user "
                    "AND execution_generation=:generation AND status='running'"
                ),
                {
                    "status": status,
                    "result": encoded,
                    "error_code": error_code,
                    "error_message": error_message,
                    "job": job_id,
                    "tenant": owner.tenant_id,
                    "user": owner.user_id,
                    "generation": generation,
                },
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    "Factor job generation or status changed before finish"
                )

        transitioned = await asyncio.to_thread(
            budget.store.transition_transactional,
            POOL,
            lease,
            status,
            expected_states=("reserved", "running", "suspect"),
            persist=persist,
        )
        if transitioned:
            try:
                budget.projection.release(lease)
            except Exception:  # noqa: BLE001
                # PG terminal state is authoritative; projection recovery removes it.
                pass
        return transitioned

    async def confirm_cancelled(
        self,
        job_id: str,
        *,
        tenant_id: str,
        user_id: str,
        generation: int,
    ) -> bool:
        """Publish cancelled only after the trusted executor proves it stopped."""
        owner = OwnerScope(tenant_id, user_id)
        if type(generation) is not int or generation <= 0:
            raise ValueError("Factor job generation must be a positive integer")
        from backend.services.engine.alpha_agent.factor_job_budget import get_budget

        budget = get_budget()
        lease = await asyncio.to_thread(
            budget.store.get_lease, POOL, job_id, owner.tenant_id, owner.user_id
        )
        if lease is None:
            return False

        def persist(session) -> None:
            result = session.execute(
                text(
                    "UPDATE alpha_factor_jobs SET status='cancelled',updated_at=now(), "
                    "completed_at=now() WHERE job_id=:job AND tenant_id=:tenant "
                    "AND user_id=:user AND execution_generation=:generation "
                    "AND status='stopping'"
                ),
                {
                    "job": job_id,
                    "tenant": owner.tenant_id,
                    "user": owner.user_id,
                    "generation": generation,
                },
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    "Factor job generation or status changed before cancellation"
                )
            session.execute(
                text(
                    "UPDATE alpha_user_factor_generations SET status='archived' "
                    "WHERE tenant_id=:tenant AND user_id=:user AND job_id=:job "
                    "AND dataset_key='rd_mined' AND status='building'"
                ),
                {"tenant": owner.tenant_id, "user": owner.user_id, "job": job_id},
            )

        transitioned = await asyncio.to_thread(
            budget.store.transition_transactional,
            POOL,
            lease,
            "cancelled",
            expected_states=("reserved", "running", "suspect"),
            persist=persist,
        )
        if transitioned:
            try:
                budget.projection.release(lease)
            except Exception:  # noqa: BLE001
                pass
        return transitioned
