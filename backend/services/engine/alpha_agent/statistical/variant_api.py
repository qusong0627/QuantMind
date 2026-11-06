"""Session-owned access to statistical variant evidence and reversible cooling."""

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.services.engine.auth_context import get_authenticated_identity
from .variant_ledger import context_view, list_contexts, reset_cooling

router = APIRouter(prefix="/research/variant-ledger")


class ResetCoolingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)


def _owner(request: Request) -> dict:
    user_id, tenant_id = get_authenticated_identity(request)
    return {"tenant_id": tenant_id, "user_id": user_id}


@router.get("")
async def get_contexts(
    request: Request,
    universe: str = Query(min_length=1, max_length=128),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=1000000),
):
    return {
        "code": 200,
        "data": await list_contexts(
            **_owner(request), universe=universe, limit=limit, offset=offset
        ),
    }


@router.get("/{context_id}")
async def get_context(context_id: str, request: Request):
    data = await context_view(**_owner(request), context_id=context_id)
    if data is None:
        raise HTTPException(status_code=404, detail="变体台账不存在")
    return {"code": 200, "data": data}


@router.post("/{context_id}/{variant_key}/reset")
async def reset_variant(
    context_id: str, variant_key: str, payload: ResetCoolingRequest, request: Request
):
    try:
        data = await reset_cooling(
            **_owner(request),
            context_id=context_id,
            variant_key=variant_key,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if data is None:
        raise HTTPException(status_code=404, detail="变体台账不存在")
    return {"code": 200, "data": data}
