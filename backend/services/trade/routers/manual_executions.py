from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.services.trade.deps import AuthContext, get_auth_context
from backend.services.trade.services.manual_execution_service import manual_execution_service

router = APIRouter(prefix="/manual-executions", tags=["Manual Executions"])


class ManualExecutionCreateRequest(BaseModel):
    model_id: str = Field(..., description="模型 ID")
    run_id: str = Field(..., description="推理批次 run_id")
    strategy_id: str = Field(..., description="策略 ID")
    trading_mode: str = Field("REAL", description="REAL / SHADOW / SIMULATION")
    preview_hash: str | None = Field(None, description="调仓预案摘要哈希")
    note: str | None = Field(None, description="备注")


class ManualExecutionPreviewRequest(BaseModel):
    model_id: str = Field(..., description="模型 ID")
    run_id: str = Field(..., description="推理批次 run_id")
    strategy_id: str = Field(..., description="策略 ID")
    trading_mode: str = Field("REAL", description="REAL / SHADOW / SIMULATION")
    note: str | None = Field(None, description="备注")


@router.post("/preview")
async def preview_manual_execution(
    payload: ManualExecutionPreviewRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    return await manual_execution_service.build_execution_preview(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        model_id=payload.model_id,
        run_id=payload.run_id,
        strategy_id=payload.strategy_id,
        trading_mode=payload.trading_mode,
        note=payload.note,
    )


@router.post("")
async def create_manual_execution(
    payload: ManualExecutionCreateRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    result = await manual_execution_service.create_manual_task(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        model_id=payload.model_id,
        run_id=payload.run_id,
        strategy_id=payload.strategy_id,
        trading_mode=payload.trading_mode,
        preview_hash=payload.preview_hash,
        note=payload.note,
    )
    return {"status": "success", **result}


@router.get("")
async def list_manual_executions(
    limit: int = Query(20, ge=1, le=100),
    task_type: str | None = Query(None),
    task_source: str | None = Query(None),
    active_runtime_id: str | None = Query(None),
    auth: AuthContext = Depends(get_auth_context),
):
    return await manual_execution_service.list_tasks(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        limit=limit,
        task_type=task_type,
        task_source=task_source,
        active_runtime_id=active_runtime_id,
    )


@router.delete("")
async def clear_manual_executions(
    auth: AuthContext = Depends(get_auth_context),
):
    """清除手动执行历史记录。"""
    return await manual_execution_service.clear_history(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
    )


@router.get("/{task_id}")
async def get_manual_execution(
    task_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    task = await manual_execution_service.get_task(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        task_id=task_id,
    )
    if not task:
        raise HTTPException(status_code=404, detail="手动执行任务不存在")
    return task


@router.get("/{task_id}/logs")
async def get_manual_execution_logs(
    task_id: str,
    after_id: str = Query("0-0"),
    limit: int = Query(200, ge=1, le=500),
    auth: AuthContext = Depends(get_auth_context),
):
    return await manual_execution_service.get_logs(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        task_id=task_id,
        after_id=after_id,
        limit=limit,
    )
