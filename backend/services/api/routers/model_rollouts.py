"""模型晋升流程 API（P2 · 设计 §3.1 状态机 / §5.3 观察期）。

- ``GET  /api/v1/models/rollouts``             台账列表（市场/阶段过滤）
- ``POST /api/v1/models/rollouts``             创建 rollout（challenger = 待晋升模型）
- ``GET  /api/v1/models/rollouts/{id}``        详情（含两侧模型记录，治理页证据卡）
- ``POST /api/v1/models/rollouts/{id}/evaluate``   重算 G0-G7（回放入口；观察窗满自动 gate_passed）
- ``POST /api/v1/models/rollouts/{id}/observing``  进观察期（G0/G1 硬闸门 + 开 settings 行）
- ``POST /api/v1/models/rollouts/{id}/promote``    晋升（单事务：市场级默认切换 + 审计，理由必填）
- ``POST /api/v1/models/rollouts/{id}/reject``     拒绝（关 settings 行，理由必填）
- ``POST /api/v1/models/rollouts/{id}/rollback``   回滚（默认切回备任，理由必填）

身份口径与 ``model_training`` 一致（``users.user_id`` 优先，历史别名为 sub）；
阶段冲突 409 / 前置条件 400 / 不存在 404——服务层异常直接映射，不吞不改。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.engine.services.model_rollout_service import (
    RolloutConflict,
    RolloutInvalid,
    RolloutNotFound,
    model_rollout_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/models/rollouts", tags=["Model Rollouts"])


def _owner_scope(current_user: dict[str, Any]) -> tuple[str, str]:
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="用户身份无效")
    return tenant_id, user_id


def _as_http(exc: Exception) -> HTTPException:
    if isinstance(exc, RolloutNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RolloutConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, RolloutInvalid):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=f"晋升流程内部错误: {exc}")


class CreateRolloutBody(BaseModel):
    market: str = Field(default="CN", description="市场（CN/HK/US/...）")
    challenger_model_id: str = Field(..., min_length=1)
    campaign_id: str | None = Field(
        default=None, description="锚定的滚动训练 campaign（vintage 回放来源）"
    )
    notes: str | None = None


class EvaluateBody(BaseModel):
    thresholds: dict[str, float] | None = Field(
        default=None, description="G0-G7 阈值覆盖（缺省用 model_rollout.DEFAULT_THRESHOLDS）"
    )


class ObservingBody(BaseModel):
    schedule_time: str | None = Field(
        default=None, description="日更排班（缺省取冠军同款；再缺省 00:00）"
    )


class DecisionBody(BaseModel):
    notes: str = Field(..., min_length=1, description="审批理由（审计，必填）")


@router.get("")
async def list_rollouts(
    market: str | None = Query(default=None),
    stage: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    items = await model_rollout_service.list_rollouts(
        tenant_id=tenant_id, user_id=user_id, market=market, stage=stage, limit=limit
    )
    return {"items": items, "total": len(items)}


@router.post("")
async def create_rollout(
    body: CreateRolloutBody, current_user: dict = Depends(get_current_user)
):
    tenant_id, user_id = _owner_scope(current_user)
    try:
        rollout = await model_rollout_service.create_rollout(
            tenant_id=tenant_id,
            user_id=user_id,
            market=body.market,
            challenger_model_id=body.challenger_model_id,
            campaign_id=body.campaign_id,
            notes=body.notes,
        )
    except (RolloutNotFound, RolloutConflict, RolloutInvalid) as exc:
        raise _as_http(exc) from exc
    return rollout


@router.get("/{rollout_id}")
async def get_rollout(rollout_id: str, current_user: dict = Depends(get_current_user)):
    owner = _owner_scope(current_user)
    try:
        return await model_rollout_service.get_detail(rollout_id, owner=owner)
    except (RolloutNotFound, RolloutConflict, RolloutInvalid) as exc:
        raise _as_http(exc) from exc


@router.post("/{rollout_id}/evaluate")
async def evaluate_rollout(
    rollout_id: str,
    body: EvaluateBody | None = None,
    current_user: dict = Depends(get_current_user),
):
    """重算 G0-G7 并落台账；observing 且观察窗满 ``g5_min_days`` → 自动 gate_passed。"""
    owner = _owner_scope(current_user)
    try:
        return await model_rollout_service.evaluate(
            rollout_id, thresholds=(body.thresholds if body else None), owner=owner
        )
    except (RolloutNotFound, RolloutConflict, RolloutInvalid) as exc:
        raise _as_http(exc) from exc


@router.post("/{rollout_id}/observing")
async def start_observation(
    rollout_id: str,
    body: ObservingBody | None = None,
    current_user: dict = Depends(get_current_user),
):
    owner = _owner_scope(current_user)
    try:
        return await model_rollout_service.start_observation(
            rollout_id, schedule_time=(body.schedule_time if body else None), owner=owner
        )
    except (RolloutNotFound, RolloutConflict, RolloutInvalid) as exc:
        raise _as_http(exc) from exc


@router.post("/{rollout_id}/promote")
async def promote_rollout(
    rollout_id: str,
    body: DecisionBody,
    current_user: dict = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    try:
        return await model_rollout_service.promote(
            rollout_id, decided_by=user_id, notes=body.notes, owner=(tenant_id, user_id)
        )
    except (RolloutNotFound, RolloutConflict, RolloutInvalid) as exc:
        raise _as_http(exc) from exc


@router.post("/{rollout_id}/reject")
async def reject_rollout(
    rollout_id: str,
    body: DecisionBody,
    current_user: dict = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    try:
        return await model_rollout_service.reject(
            rollout_id, decided_by=user_id, notes=body.notes, owner=(tenant_id, user_id)
        )
    except (RolloutNotFound, RolloutConflict, RolloutInvalid) as exc:
        raise _as_http(exc) from exc


@router.post("/{rollout_id}/rollback")
async def rollback_rollout(
    rollout_id: str,
    body: DecisionBody,
    current_user: dict = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    try:
        return await model_rollout_service.rollback(
            rollout_id, decided_by=user_id, notes=body.notes, owner=(tenant_id, user_id)
        )
    except (RolloutNotFound, RolloutConflict, RolloutInvalid) as exc:
        raise _as_http(exc) from exc
