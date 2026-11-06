"""Authenticated sampled-direction batches with immutable selection and readback."""

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from backend.shared.owner_scope import OwnerScope
from backend.services.engine.auth_context import get_authenticated_identity
from .api_models import SampledDirectionBatchRequest
from .direction_batches import (
    DirectionBatchConflict,
    detail,
    dispatch_payload,
    freeze_batch,
    get_batch,
    history,
)

router = APIRouter()


def _owner(request):
    user, tenant = get_authenticated_identity(request)
    if not tenant or not user:
        raise HTTPException(status_code=401, detail="Authenticated owner required")
    return OwnerScope(tenant, user)


def _data(value, *, status=200):
    return JSONResponse(
        content={"code": status, "data": value},
        status_code=status,
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/directions/sample-dispatch", status_code=202)
async def submit_batch(payload: SampledDirectionBatchRequest, request: Request):
    from backend.services.engine.routers.alpha_agent import (
        _dispatch_mining_directions,
        _universe_is_valid,
    )

    owner = _owner(request)
    if not _universe_is_valid(
        payload.universe, tenant_id=owner.tenant_id, user_id=owner.user_id
    ):
        raise HTTPException(status_code=400, detail="本人股票池不存在或不可用")
    try:
        batch = await freeze_batch(owner, payload)
    except DirectionBatchConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="批次保存结果暂不可用，请保留原批次ID和完整请求重试"
        ) from exc
    # The committed batch precedes every child admission. Concurrent retries
    # use the winning selection and the shared queue's owned child request keys.
    receipts = await _dispatch_mining_directions(
        dispatch_payload(batch), request, selection_batch=batch
    )
    result = await detail(owner, batch)
    result["dispatch"] = receipts["data"]
    return _data(result, status=202)


@router.get("/directions/batches/history")
async def batch_history(
    request: Request, offset: int = Query(default=0, ge=0, le=1000000)
):
    return _data(await history(_owner(request), offset))


@router.get("/directions/batches/{batch_id}")
async def batch_detail(batch_id: str, request: Request):
    owner = _owner(request)
    batch = await get_batch(owner, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="本人批次不存在")
    return _data(await detail(owner, batch))
