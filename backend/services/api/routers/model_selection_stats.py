"""截面选股 · 单模型 TopK 历史统计端点。

``GET /api/v1/models/{model_id}/selection-stats``：JWT 鉴权，按
``(tenant_id, user_id)`` 隔离；只做纸面统计，不产生任何订单/委托/成交语义。

路由为两段路径，不会被 ``model_training`` 的 ``GET /{model_id}`` 兜底遮蔽；
仍按既有约定在 ``main.py`` 中先于它注册。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.engine.services.model_selection_stats_service import (
    ModelSelectionStatsService,
    SelectionStatsError,
)

router = APIRouter(prefix="/api/v1/models", tags=["Model Selection Stats"])


def _owner(current_user: Mapping[str, Any]) -> tuple[str, str]:
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="用户身份无效")
    return tenant_id, user_id


@router.get("/{model_id}/selection-stats")
async def get_selection_stats(
    model_id: str,
    start: date | None = Query(default=None, description="测试集信号起始日（含）"),
    end: date | None = Query(default=None, description="测试集信号截止日（含）"),
    top_k: int = Query(default=50, ge=1, le=50, description="每期入选数"),
    hold_days: int | None = Query(
        default=None,
        ge=1,
        le=60,
        description="持有交易日数；缺省取模型 target_horizon_days，1=每日滚动",
    ),
    capital: float = Query(default=10_000_000.0, gt=0, le=1e12, description="初始资金"),
    exclude_blacklist: bool = Query(
        default=False, description="按交易黑名单剔除入选标的（推理中心「排除黑名单」开关）"
    ),
    current_user: Mapping[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    tenant_id, user_id = _owner(current_user)
    try:
        return await ModelSelectionStatsService().compute(
            tenant_id=tenant_id,
            user_id=user_id,
            model_id=model_id,
            start=start,
            end=end,
            top_k=top_k,
            hold_days=hold_days,
            capital=capital,
            exclude_blacklist=bool(exclude_blacklist),
        )
    except SelectionStatsError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
