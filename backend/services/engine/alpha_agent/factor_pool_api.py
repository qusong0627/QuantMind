"""Authenticated API for the current user's private factor pool."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.services.engine.auth_context import get_authenticated_identity
from backend.services.engine.alpha_agent.factor_pool import FactorPoolPersistence

router = APIRouter()
_pool = FactorPoolPersistence()


class FactorPoolAddRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    universe: str = Field(default="", max_length=128)
    market: str = Field(default="a_share", min_length=1, max_length=32)


class FactorPoolGraphRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: str = Field(default="a_share", min_length=1, max_length=32)
    universe: str | None = Field(default=None, max_length=128)


def _owner(request: Request) -> tuple[str, str]:
    user_id, tenant_id = get_authenticated_identity(request)
    if not tenant_id or not user_id:
        raise HTTPException(status_code=401, detail="Authenticated owner is required")
    return str(tenant_id), str(user_id)


@router.get("/factors/pool")
async def list_factor_pool(
    request: Request,
    universe: str | None = Query(default=None, max_length=128),
    include_archived: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    tenant_id, user_id = _owner(request)
    try:
        rows = await _pool.list_members(
            tenant_id=tenant_id,
            user_id=user_id,
            universe=universe,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    has_more = len(rows) > limit
    return {
        "code": 200,
        "data": {
            "items": rows[:limit],
            "has_more": has_more,
            "limit": limit,
            "offset": offset,
        },
    }


@router.get("/factors/pool/graph/edges")
async def list_factor_pool_edges(
    request: Request,
    universe: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=500, ge=1, le=1000),
) -> dict:
    tenant_id, user_id = _owner(request)
    try:
        rows = await _pool.list_formula_edges(
            tenant_id=tenant_id,
            user_id=user_id,
            universe=universe,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"code": 200, "data": {"items": rows}}


@router.get("/factors/pool/cleanup/suggestions")
async def get_pool_cleanup_suggestions(
    request: Request,
    universe: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1000000),
):
    from backend.services.engine.alpha_agent.pool_cleanup_service import (
        cleanup_suggestions,
    )

    tenant_id, user_id = _owner(request)
    try:
        result = await cleanup_suggestions(
            tenant_id=tenant_id,
            user_id=user_id,
            universe=universe,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="本人因子池建议暂不可用，请稍后刷新"
        ) from exc
    return JSONResponse(
        content={"code": 200, "data": result},
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/factors/pool/{factor_id}/panel")
async def get_factor_pool_panel(
    factor_id: str,
    request: Request,
    universe: str = Query(default="", max_length=128),
    market: str = Query(default="a_share", min_length=1, max_length=32),
) -> dict:
    tenant_id, user_id = _owner(request)
    try:
        panel = await _pool.get_member_panel_summary(
            tenant_id=tenant_id,
            user_id=user_id,
            factor_id=factor_id,
            market=market,
            universe=universe,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if panel is None:
        raise HTTPException(status_code=404, detail="Factor panel not found")
    return {"code": 200, "data": panel}


@router.post("/factors/pool/graph/rebuild")
async def rebuild_factor_pool_edges(
    payload: FactorPoolGraphRequest, request: Request
) -> dict:
    tenant_id, user_id = _owner(request)
    try:
        result = await _pool.rebuild_formula_edges(
            tenant_id=tenant_id,
            user_id=user_id,
            market=payload.market,
            universe=payload.universe,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"code": 200, "data": result}


@router.post("/factors/pool/graph/value-rebuild")
async def rebuild_factor_pool_value_edges(
    payload: FactorPoolGraphRequest, request: Request
) -> dict:
    tenant_id, user_id = _owner(request)
    try:
        result = await _pool.rebuild_value_edges(
            tenant_id=tenant_id,
            user_id=user_id,
            market=payload.market,
            universe=payload.universe,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"code": 200, "data": result}


@router.post("/factors/pool/graph/task-round-rebuild")
async def rebuild_factor_pool_task_round_edges(
    payload: FactorPoolGraphRequest, request: Request
) -> dict:
    tenant_id, user_id = _owner(request)
    try:
        result = await _pool.rebuild_task_round_edges(
            tenant_id=tenant_id,
            user_id=user_id,
            market=payload.market,
            universe=payload.universe,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"code": 200, "data": result}


@router.post("/factors/pool/score/rebuild")
async def rebuild_factor_pool_scores(
    payload: FactorPoolGraphRequest, request: Request
) -> dict:
    tenant_id, user_id = _owner(request)
    try:
        result = await _pool.rebuild_pool_scores(
            tenant_id=tenant_id,
            user_id=user_id,
            market=payload.market,
            universe=payload.universe,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"code": 200, "data": result}


@router.post("/factors/pool/{factor_id}", status_code=201)
async def add_factor_to_pool(
    factor_id: str, payload: FactorPoolAddRequest, request: Request
) -> dict:
    tenant_id, user_id = _owner(request)
    from backend.services.engine.alpha_agent.factor_visibility import (
        FactorVisibilityConflict,
    )

    try:
        added = await _pool.add_member(
            tenant_id=tenant_id,
            user_id=user_id,
            factor_id=factor_id,
            market=payload.market,
            universe=payload.universe,
        )
    except FactorVisibilityConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Factor not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not added:
        raise HTTPException(status_code=404, detail="Factor not found")
    return {"code": 201, "data": {"factor_id": factor_id, "in_pool": True}}


@router.delete("/factors/pool/{factor_id}")
async def remove_factor_from_pool(
    factor_id: str,
    request: Request,
    market: str = Query(default="a_share", min_length=1, max_length=32),
    universe: str = Query(default="", max_length=128),
) -> dict:
    tenant_id, user_id = _owner(request)
    try:
        removed = await _pool.remove_member(
            tenant_id=tenant_id,
            user_id=user_id,
            factor_id=factor_id,
            market=market,
            universe=universe,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="Factor not found in pool")
    return {"code": 200, "data": {"factor_id": factor_id, "in_pool": False}}
