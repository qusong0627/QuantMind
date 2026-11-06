"""Authenticated admission/history for private pool orthogonality jobs."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.owner_scope import OwnerScope
from backend.shared.secret_redaction import redact_text
from backend.shared.utc_datetime import to_utc_iso
from backend.services.engine.alpha_agent.factor_job_api import _public
from backend.services.engine.alpha_agent.factor_jobs import (
    AlphaFactorJobPersistence,
    FactorJobIdempotencyConflict,
    FactorJobQueueFull,
)
from backend.services.engine.alpha_agent.factor_visibility import (
    FactorVisibilityConflict,
)
from backend.services.engine.alpha_agent.pool_research import (
    PoolResearchConflict,
    _members,
    _members_statement,
    freeze,
    numeric_context,
    verify_admission,
)
from backend.services.engine.auth_context import get_authenticated_identity

router = APIRouter()
jobs = AlphaFactorJobPersistence()


class PoolResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    universe: str = Field(max_length=128)


def _owner(request):
    user_id, tenant_id = get_authenticated_identity(request)
    if not user_id or not tenant_id:
        raise HTTPException(status_code=401, detail="Authenticated owner required")
    return OwnerScope(str(tenant_id), str(user_id))


def _data(value, *, status=200):
    return JSONResponse(
        content={"code": status, "data": value},
        status_code=status,
        headers={"Cache-Control": "private, no-store"},
    )


async def _existing(owner, key, universe):
    row = await jobs.get_by_request_key(
        key, tenant_id=owner.tenant_id, user_id=owner.user_id
    )
    if row:
        snapshot = row.get("request_snapshot") or {}
        if row["kind"] != "pool_research" or snapshot.get("request", {}).get(
            "submission"
        ) != {"universe": universe}:
            raise HTTPException(status_code=409, detail="幂等键已用于其他作业")
    return row


@router.post("/factors/pool/research/submit", status_code=202)
async def submit_pool_research(payload: PoolResearchRequest, request: Request):
    owner = _owner(request)
    key = request.headers.get("Idempotency-Key", "").strip()
    if not 1 <= len(key) <= 128 or any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise HTTPException(status_code=422, detail="池研究需要有效 Idempotency-Key")
    previous = await _existing(owner, key, payload.universe)
    if previous:
        return _data(_public(previous), status=202)
    try:
        frozen = await freeze(owner, payload.universe)
        job_id = await jobs.create(
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            kind="pool_research",
            request=frozen,
            request_key=key,
            on_created=lambda session, _: verify_admission(session, owner, frozen),
        )
    except FactorJobIdempotencyConflict as exc:
        previous = await _existing(owner, key, payload.universe)
        if previous:
            return _data(_public(previous), status=202)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (PoolResearchConflict, FactorVisibilityConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FactorJobQueueFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="本人池研究暂不可提交，请保留原请求重试"
        ) from exc
    row = await jobs.get(
        job_id, tenant_id=owner.tenant_id, user_id=owner.user_id, consistent=True
    )
    if row is None:
        raise HTTPException(
            status_code=503, detail="作业回执暂不可用，请使用原请求重试"
        )
    return _data(_public(row), status=202)


@router.get("/factors/pool/research/history")
async def pool_research_history(
    request: Request,
    universe: str = Query(max_length=128),
    offset: int = Query(default=0, ge=0, le=1000000),
):
    owner = _owner(request)
    async with get_session() as session:
        await session.execute(text("SET LOCAL statement_timeout='5s'"))
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT job_id,status,created_at,completed_at,error_message,result_snapshot->'stats' AS stats "
                        "FROM alpha_factor_jobs WHERE tenant_id=:tenant_id AND user_id=:user_id "
                        "AND kind='pool_research' AND request_snapshot->'request'->>'universe'=:universe "
                        "ORDER BY created_at DESC,job_id DESC LIMIT 21 OFFSET :offset"
                    ),
                    {**owner.sql_params(), "universe": universe, "offset": offset},
                )
            )
            .mappings()
            .all()
        )
    items = [
        {
            "job_id": row["job_id"],
            "status": row["status"],
            "created_at": to_utc_iso(row["created_at"]),
            "completed_at": to_utc_iso(row["completed_at"])
            if row["completed_at"]
            else None,
            "error_message": redact_text(row["error_message"] or "")[:1000],
            "stats": row["stats"],
        }
        for row in rows[:20]
    ]
    return _data({"items": items, "has_more": len(rows) > 20})


@router.get("/factors/pool/research/{job_id}")
async def pool_research_detail(job_id: str, request: Request):
    owner = _owner(request)
    row = await jobs.get(
        job_id, tenant_id=owner.tenant_id, user_id=owner.user_id, consistent=True
    )
    if row is None or row["kind"] != "pool_research":
        raise HTTPException(status_code=404, detail="池研究作业不存在")
    frozen = row["request_snapshot"]["request"]
    async with get_session() as session:
        await session.execute(text("SET LOCAL statement_timeout='5s'"))
        current = (
            (
                await session.execute(
                    _members_statement(),
                    {**owner.sql_params(), "universe": frozen["universe"]},
                )
            )
            .mappings()
            .all()
        )
    try:
        matches = _members(current) == frozen["factor_items"]
    except (ValueError, PoolResearchConflict):
        matches = False
    return _data(
        {
            "job": _public(row),
            "current_members_match": matches,
            "numeric_context_match": frozen.get("numeric_context")
            == await asyncio.to_thread(numeric_context),
        }
    )
