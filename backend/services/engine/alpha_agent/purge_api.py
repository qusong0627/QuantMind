"""Owner-scoped full purge preview and durable job submission."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.shared.owner_path import owner_segment
from backend.shared.owner_scope import OwnerScope
from backend.services.engine.alpha_agent.factor_job_api import (
    _jobs,
    _owner,
    _public,
)
from backend.services.engine.alpha_agent.owner_purge import (
    OwnerPurgeConflict,
    preview as preview_owner,
)

router = APIRouter()


class OwnerPurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal[True]
    preview_token: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    task_id: str | None = Field(default=None, min_length=1, max_length=128)


def _purge_conflict(exc: OwnerPurgeConflict) -> HTTPException:
    """把可预期的清理冲突映射成 409，避免冒泡成 500。

    ``OwnerPurgeConflict`` 覆盖预览后数据变化、预览令牌失效、路径越界等
    情况，都是调用方可处理的状态，不属于服务端故障。
    """
    return HTTPException(status_code=409, detail=str(exc))


def _resolve_scope(task_id: str | None) -> str | None:
    if task_id is None:
        return None
    try:
        return owner_segment(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="任务 ID 非法") from exc


@router.post("/purge/preview")
async def preview_purge(
    request: Request,
    response: Response,
    task_id: str | None = Query(default=None, max_length=128),
) -> dict:
    tenant_id, user_id = _owner(request)
    scope = _resolve_scope(task_id)
    try:
        result = await preview_owner(
            OwnerScope(tenant_id, user_id), scope_task_id=scope
        )
    except OwnerPurgeConflict as exc:
        raise _purge_conflict(exc) from exc
    response.headers["Cache-Control"] = "private, no-store"
    return {"code": 200, "data": result}


@router.post("/purge", status_code=202)
async def submit_purge(
    payload: OwnerPurgeRequest, request: Request, response: Response
) -> dict:
    tenant_id, user_id = _owner(request)
    request_key = request.headers.get("Idempotency-Key")
    if not request_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    response.headers["Cache-Control"] = "private, no-store"

    # A network retry must return the originally admitted purge even if the
    # preview fingerprint has since changed.
    existing = await _jobs.get_by_request_key(
        request_key, tenant_id=tenant_id, user_id=user_id
    )
    if existing is not None:
        return {
            "code": 200,
            "data": {
                "job_id": existing["job_id"],
                "status": existing["status"],
                "reused": True,
            },
        }

    owner = OwnerScope(tenant_id, user_id)
    scope = _resolve_scope(payload.task_id)
    kind = "purge_task" if scope else "purge"
    try:
        current = await preview_owner(owner, scope_task_id=scope)
    except OwnerPurgeConflict as exc:
        raise _purge_conflict(exc) from exc
    if current["preview_token"] != payload.preview_token:
        raise HTTPException(
            status_code=409, detail="数据在预览后发生变化，请重新预览"
        )
    if current["blockers"]:
        if scope:
            detail: str | dict = {
                "message": "该任务仍被引用或有活动作业，请先处理后再清理",
                "blockers": current["blockers"],
            }
        else:
            detail = {
                "message": "仍有活动任务或作业，请先停止后再清理",
                "blockers": current["blockers"],
            }
        raise HTTPException(status_code=409, detail=detail)
    job_request: dict = {
        "action": "purge_task" if scope else "purge_all",
        "preview_token": payload.preview_token,
        "counts": current["counts"],
    }
    if scope:
        job_request["task_id"] = scope
    job_id = await _jobs.create(
        tenant_id=tenant_id,
        user_id=user_id,
        kind=kind,
        request=job_request,
        request_key=request_key,
    )
    return {
        "code": 200,
        "data": {"job_id": job_id, "status": "queued", "reused": False},
    }


@router.get("/purge/{job_id}")
async def get_purge(job_id: str, request: Request, response: Response) -> dict:
    tenant_id, user_id = _owner(request)
    job = await _jobs.get(
        job_id, tenant_id=tenant_id, user_id=user_id, consistent=True
    )
    if job is None or job.get("kind") not in {"purge", "purge_task"}:
        raise HTTPException(status_code=404, detail="Purge job not found")
    response.headers["Cache-Control"] = "private, no-store"
    return {"code": 200, "data": _public(job)}
