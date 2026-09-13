import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade_shared.deps import AuthContext, get_auth_context, get_db, get_read_db, get_redis
from backend.services.trade_shared.redis_client import RedisClient
from backend.services.simulation.schemas.trade import (
    SimTradeResponse,
    SimTradeStatsResponse,
)
from backend.services.simulation.services.trade_service import SimTradeService
from backend.services.simulation.services.simulation_manager import require_sim_user_id
from backend.services.trade_shared.utils.stock_lookup import lookup_symbol_name

router = APIRouter()
logger = logging.getLogger(__name__)


def _require_user_id(raw_user_id: str, tenant_id: str = "default") -> int:
    """兼容别名，统一走 require_sim_user_id（OSS admin 归保留账户 0）。"""
    return require_sim_user_id(raw_user_id, tenant_id=tenant_id)


@router.get("/trades", response_model=list[SimTradeResponse])
async def list_trades(
    portfolio_id: int | None = Query(default=None),
    symbol: str | None = Query(default=None),
    market: str | None = Query(
        default=None,
        description="市场过滤 CN/HK/US/FUTURES/CRYPTO（按 symbol 形态判据）；不传 = 全部市场",
    ),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_read_db),
    redis: RedisClient = Depends(get_redis),
):
    user_id = _require_user_id(auth.user_id, auth.tenant_id)
    service = SimTradeService(db, redis)
    trades = await service.list_trades(
        auth.tenant_id,
        user_id,
        portfolio_id=portfolio_id,
        symbol=symbol,
        market=market,
        limit=limit,
        offset=offset,
    )
    # 批量 enrich symbol_name，避免前端 N+1 调用 /stocks/{symbol}
    # trades 可能是 ORM 对象或缓存的 dict，统一处理
    enriched = []
    for t in trades:
        # t 可能是 dict（来自缓存）或 SimTrade ORM
        symbol_val = t["symbol"] if isinstance(t, dict) else getattr(t, "symbol", "")
        name = lookup_symbol_name(symbol_val) if symbol_val else None
        if isinstance(t, dict):
            t["symbol_name"] = name
            enriched.append(t)
        else:
            # ORM 对象 -> 转 dict 并附加
            d = {c.name: getattr(t, c.name) for c in t.__table__.columns}
            d["symbol_name"] = name
            enriched.append(d)
    return enriched


@router.get("/trades/{trade_id}", response_model=SimTradeResponse)
async def get_trade(
    trade_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    user_id = _require_user_id(auth.user_id, auth.tenant_id)
    service = SimTradeService(db)
    trade = await service.get_trade(auth.tenant_id, user_id, trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail="Simulation trade not found")
    return trade


@router.get("/trades/stats/summary", response_model=SimTradeStatsResponse)
async def get_trade_stats(
    portfolio_id: int | None = Query(default=None),
    market: str | None = Query(
        default=None,
        description="市场过滤 CN/HK/US/FUTURES/CRYPTO（按 symbol 形态判据）；不传 = 全部市场",
    ),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_read_db),
    redis: RedisClient = Depends(get_redis),
):
    user_id = _require_user_id(auth.user_id, auth.tenant_id)
    service = SimTradeService(db, redis)
    stats = await service.get_stats(auth.tenant_id, user_id, portfolio_id=portfolio_id, market=market)
    logger.info(
        "simulation trade stats ready: tenant_id=%s user_id=%s portfolio_id=%s market=%s total_trades=%s daily_points=%s",
        auth.tenant_id,
        user_id,
        portfolio_id,
        market,
        stats.get("total_trades", 0),
        len(stats.get("daily_counts", []) or []),
    )
    return SimTradeStatsResponse(**stats)
