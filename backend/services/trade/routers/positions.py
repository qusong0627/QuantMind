"""
Position API Routes
"""

import logging
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade_shared.portfolio.middleware.auth import (
    get_current_tenant_id,
    get_current_user_id,
)
from backend.services.trade_shared.portfolio.schemas import (
    PositionAdjust,
    PositionCreate,
    PositionHistoryResponse,
    PositionResponse,
    TradeSync,
)
from backend.services.trade_shared.portfolio.services import PortfolioService, PositionService
from backend.services.trade_shared.portfolio.utils import get_db
from backend.services.trade_shared.portfolio.utils.limiter import limiter
from backend.services.trade.routers.internal_strategy_utils import verify_internal_call

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Positions"])


@router.post(
    "/portfolios/{portfolio_id}/positions",
    response_model=PositionResponse,
    status_code=201,
    summary="创建持仓（开仓）",
    description="为投资组合创建新的持仓，即开仓操作。需要 JWT 认证和组合权限。",
    response_description="返回创建的持仓详情",
)
@limiter.limit("20/minute")
async def create_position(
    request: Request,
    portfolio_id: int = Path(..., description="投资组合ID"),
    data: PositionCreate = ...,
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """创建持仓（开仓）"""
    try:
        # 验证用户是否拥有该投资组合
        portfolio = await PortfolioService.get_portfolio(db, portfolio_id, user_id, tenant_id=tenant_id)
        if not portfolio:
            raise HTTPException(status_code=404, detail="投资组合不存在或无权访问")

        position = await PositionService.create_position(db, portfolio_id, data)
        return position
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to create position: {e}")
        raise HTTPException(status_code=500, detail="创建失败")


@router.get(
    "/portfolios/{portfolio_id}/positions",
    response_model=list[PositionResponse],
    summary="查询持仓列表",
    description="查询投资组合的所有持仓，支持状态过滤。需要 JWT 认证和组合权限。",
    response_description="返回持仓列表",
)
@limiter.limit("60/minute")
async def list_positions(
    request: Request,
    portfolio_id: int = Path(..., description="投资组合ID"),
    status: str | None = Query(None, description="状态过滤"),
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """查询持仓列表"""
    try:
        # 验证用户是否拥有该投资组合
        portfolio = await PortfolioService.get_portfolio(db, portfolio_id, user_id, tenant_id=tenant_id)
        if not portfolio:
            raise HTTPException(status_code=404, detail="投资组合不存在或无权访问")

        positions = await PositionService.list_positions(db, portfolio_id, status)
        return positions
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list positions: {e}")
        raise HTTPException(status_code=500, detail="查询失败")


@router.post(
    "/internal/sync-trade",
    response_model=PositionResponse,
    summary="同步交易(内部)",
    description="供 Trading Service 调用，同步成交信息更新持仓",
)
async def sync_trade(
    data: TradeSync,
    db: AsyncSession = Depends(get_db),
    _internal: None = Depends(verify_internal_call),  # H1 加固：内部密钥校验（原注释自认缺此防线）
):
    try:
        position = await PositionService.sync_trade_update(db, data)
        return position
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/portfolios/{portfolio_id}/positions/{position_id}",
    response_model=PositionResponse,
    summary="查询持仓详情",
    description="根据 ID 查询持仓的详细信息，包括持仓数量、成本、市值、盈亏等。需要 JWT 认证。",
    response_description="返回持仓详情",
)
@limiter.limit("60/minute")
async def get_position(
    request: Request,
    position_id: int = Path(..., description="持仓ID"),
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """查询持仓详情"""
    try:
        position = await PositionService.get_position(db, position_id, user_id=user_id, tenant_id=tenant_id)
        if not position:
            raise HTTPException(status_code=404, detail="持仓不存在")
        return position
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get position: {e}")
        raise HTTPException(status_code=500, detail="查询失败")


@router.put(
    "/positions/{position_id}/price",
    response_model=PositionResponse,
    summary="更新持仓价格",
    description="更新持仓的当前价格，系统将自动重新计算市值和盈亏。需要 JWT 认证。",
    response_description="返回更新后的持仓详情",
)
@limiter.limit("100/minute")
async def update_position_price(
    request: Request,
    position_id: int = Path(..., description="持仓ID"),
    current_price: Decimal = Query(..., gt=0, description="当前价格"),
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """更新持仓价格"""
    try:
        position = await PositionService.update_position_price(
            db, position_id, current_price, user_id=user_id, tenant_id=tenant_id
        )
        return position
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to update position price: {e}")
        raise HTTPException(status_code=500, detail="更新失败")


@router.post(
    "/positions/{position_id}/adjust",
    response_model=PositionResponse,
    summary="调整持仓（加仓/减仓）",
    description="调整持仓数量，支持加仓和减仓操作。系统将自动重新计算平均成本。需要 JWT 认证。",
    response_description="返回调整后的持仓详情",
)
@limiter.limit("30/minute")
async def adjust_position(
    request: Request,
    position_id: int = Path(..., description="持仓ID"),
    data: PositionAdjust = ...,
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """调整持仓（加仓/减仓）"""
    try:
        position = await PositionService.adjust_position(db, position_id, data, user_id=user_id, tenant_id=tenant_id)
        return position
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to adjust position: {e}")
        raise HTTPException(status_code=500, detail="调整失败")


@router.post(
    "/positions/{position_id}/close",
    response_model=PositionResponse,
    summary="平仓",
    description="平掉指定的持仓，计算已实现盈亏。需要 JWT 认证。",
    response_description="返回平仓后的持仓详情",
)
@limiter.limit("20/minute")
async def close_position(
    request: Request,
    position_id: int = Path(..., description="持仓ID"),
    price: Decimal = Query(..., gt=0, description="平仓价格"),
    note: str | None = Query(None, description="备注"),
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """平仓"""
    try:
        position = await PositionService.close_position(
            db, position_id, price, note, user_id=user_id, tenant_id=tenant_id
        )
        return position
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to close position: {e}")
        raise HTTPException(status_code=500, detail="平仓失败")


@router.get(
    "/positions/{position_id}/history",
    response_model=list[PositionHistoryResponse],
    summary="查询持仓历史",
    description="查询持仓的所有变更历史记录，包括开仓、加仓、减仓、平仓等操作。需要 JWT 认证。",
    response_description="返回持仓历史列表",
)
@limiter.limit("30/minute")
async def get_position_history(
    request: Request,
    position_id: int = Path(..., description="持仓ID"),
    limit: int = Query(100, ge=1, le=1000, description="限制数量"),
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """查询持仓历史"""
    try:
        history = await PositionService.get_position_history(
            db, position_id, limit, user_id=user_id, tenant_id=tenant_id
        )
        return history
    except Exception as e:
        logger.error(f"Failed to get position history: {e}")
        raise HTTPException(status_code=500, detail="查询失败")
