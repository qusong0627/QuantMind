"""
Portfolio API Routes
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade_shared.deps import AuthContext, get_auth_context, get_db
from backend.services.trade_shared.portfolio.schemas import (
    BindStrategyRequest,
    MessageResponse,
    PortfolioCreate,
    PortfolioResponse,
    PortfolioSummary,
    PortfolioUpdate,
    RealTradingResponse,
    SnapshotResponse,
)
from backend.services.trade_shared.portfolio.services import PortfolioService
from backend.services.trade_shared.portfolio.utils.limiter import limiter


async def get_current_user_id(auth: AuthContext = Depends(get_auth_context)) -> str:
    """获取当前用户ID (字符串类型，兼容 'admin' 等非数字ID)"""
    return auth.user_id


async def get_current_tenant_id(
    auth: AuthContext = Depends(get_auth_context),
) -> str:
    tenant_id = str(auth.tenant_id or "").strip()
    return tenant_id or "default"


security = HTTPBearer()


def get_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    return credentials.credentials


logger = logging.getLogger(__name__)
router = APIRouter(tags=["Portfolios"])


@router.post(
    "",
    response_model=PortfolioResponse,
    status_code=201,
    summary="创建投资组合",
    description="创建新的投资组合，初始化资金 and 配置。需要 JWT 认证。",
    response_description="返回创建的投资组合详情",
)
@limiter.limit("10/minute")
async def create_portfolio(
    request: Request,
    data: PortfolioCreate,
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """创建投资组合"""
    try:
        # 强制使用 Token 中的 user_id，防止越权创建
        data.tenant_id = tenant_id
        data.user_id = user_id
        portfolio = await PortfolioService.create_portfolio(db, data)
        return portfolio
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to create portfolio: {e}")
        raise HTTPException(status_code=500, detail="创建失败")


@router.get(
    "",
    response_model=list[PortfolioSummary],
    summary="查询投资组合列表",
    description="查询当前用户的所有投资组合，支持分页和状态过滤。需要 JWT 认证。",
    response_description="返回投资组合列表",
)
@limiter.limit("30/minute")
async def list_portfolios(
    request: Request,
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    skip: int = Query(0, ge=0, description="跳过数量"),
    limit: int = Query(100, ge=1, le=1000, description="限制数量"),
    status: str | None = Query(None, description="状态过滤"),
    db: AsyncSession = Depends(get_db),
):
    """查询投资组合列表 (优化版：解决 N+1 问题)"""
    try:
        from sqlalchemy import and_, func, select

        from backend.services.trade_shared.portfolio.models import Portfolio, Position

        # 使用外连接和聚合一次性查询组合及其持仓数
        count_subquery = (
            select(Position.portfolio_id, func.count(Position.id).label("pos_count"))
            .where(Position.status == "holding")
            .group_by(Position.portfolio_id)
            .subquery()
        )

        stmt = (
            select(Portfolio, count_subquery.c.pos_count)
            .outerjoin(count_subquery, Portfolio.id == count_subquery.c.portfolio_id)
            .where(
                and_(
                    Portfolio.tenant_id == tenant_id,
                    Portfolio.user_id == user_id,
                    Portfolio.is_deleted == False,
                )
            )
        )

        if status:
            stmt = stmt.where(Portfolio.status == status)

        stmt = stmt.order_by(Portfolio.created_at.desc()).offset(skip).limit(limit)
        result = await db.execute(stmt)
        rows = result.all()

        summaries = []
        for portfolio, position_count in rows:
            summaries.append(
                PortfolioSummary(
                    id=portfolio.id,
                    name=portfolio.name,
                    total_value=portfolio.total_value,
                    total_pnl=portfolio.total_pnl,
                    total_return=portfolio.total_return,
                    status=portfolio.status,
                    position_count=position_count or 0,
                )
            )

        return summaries
    except Exception as e:
        logger.error(f"Failed to list portfolios: {e}")
        raise HTTPException(status_code=500, detail="查询失败")


@router.get(
    "/distribution",
    summary="获取全量资产分布",
    description="获取当前用户所有投资组合的资产分布比例（行业、资产类型等）。",
)
async def get_all_portfolios_distribution(
    trading_mode: str | None = Query(None, description="交易模式过滤: REAL/SIMULATION"),
    market: str | None = Query(
        None, description="按市场过滤持仓: CN/HK/US/FUTURES/CRYPTO/CUSTOM"
    ),
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """汇总资产分布"""
    try:
        from sqlalchemy import and_, select

        from backend.services.trade_shared.portfolio.models import Portfolio, Position
        from backend.shared.symbol_market import symbol_market_clause

        # 查询该用户所有持仓
        stmt = (
            select(Position)
            .join(Portfolio)
            .where(
                and_(
                    Portfolio.tenant_id == tenant_id,
                    Portfolio.user_id == user_id,
                    Position.status == "holding",
                )
            )
        )

        if trading_mode:
            stmt = stmt.where(Portfolio.trading_mode == trading_mode.upper())
        if market:
            stmt = stmt.where(symbol_market_clause(Position.symbol, market))

        result = await db.execute(stmt)
        positions = result.scalars().all()
        logger.info(
            "portfolio distribution query done: tenant_id=%s user_id=%s positions=%s",
            tenant_id,
            user_id,
            len(positions),
        )

        # 简单聚合逻辑
        sectors = {}
        assets = {"Stock": 0.0}
        total_market_value = sum(float(p.market_value) for p in positions)

        if total_market_value > 0:
            for p in positions:
                # 假设 symbol_name 或其他字段包含行业信息，这里暂用 Mock 行业
                # 实际生产中可能需要关联外部数据源获取行业信息
                sector = (
                    "Tech"
                    if "IT" in (p.symbol_name or "")
                    else "Finance" if "Bank" in (p.symbol_name or "") else "Other"
                )
                val = float(p.market_value)
                sectors[sector] = sectors.get(sector, 0.0) + (val / total_market_value)
                assets["Stock"] += val / total_market_value
        else:
            logger.info(
                "portfolio distribution empty: tenant_id=%s user_id=%s reason=no_holding_positions",
                tenant_id,
                user_id,
            )

        # 确保返回 float 类型以便前端解析
        sectors = {k: round(v, 4) for k, v in sectors.items()}
        assets = {k: round(v, 4) for k, v in assets.items()}
        logger.info(
            "portfolio distribution aggregated: tenant_id=%s user_id=%s sectors=%s total_market_value=%s",
            tenant_id,
            user_id,
            len(sectors),
            round(total_market_value, 4),
        )

        return {"data": {"sectors": sectors, "assets": assets}}
    except Exception as e:
        logger.error(f"Failed to get distribution: {e}")
        return {"data": {"sectors": {}, "assets": {}}}


@router.get(
    "/performance",
    summary="获取全量业绩曲线",
    description="汇总当前用户所有投资组合的每日收益率。",
)
async def get_all_portfolios_performance(
    type: str = Query("daily_return", description="数据类型: daily_return (收益率), daily_pnl (盈亏金额)"),
    trading_mode: str | None = Query(None, description="交易模式过滤: REAL/SIMULATION"),
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """汇总业绩曲线"""
    try:
        from sqlalchemy import select

        from backend.services.trade_shared.portfolio.models import Portfolio, PortfolioSnapshot

        # 查询该用户下所有组合的快照
        stmt = (
            select(PortfolioSnapshot)
            .join(Portfolio)
            .where(
                Portfolio.tenant_id == tenant_id,
                Portfolio.user_id == user_id,
            )
        )

        if trading_mode:
            stmt = stmt.where(Portfolio.trading_mode == trading_mode.upper())

        stmt = stmt.order_by(PortfolioSnapshot.snapshot_date.asc())

        result = await db.execute(stmt)
        snapshots = result.scalars().all()
        logger.info(
            "portfolio performance query done: tenant_id=%s user_id=%s snapshots=%s type=%s",
            tenant_id,
            user_id,
            len(snapshots),
            type,
        )

        # 按日期汇总
        daily_data = {}
        for s in snapshots:
            date_str = s.snapshot_date.date().isoformat()
            # 根据请求类型选择字段：daily_pnl (绝对金额) 或 daily_return (收益率)
            val = float(s.daily_pnl if type == "daily_pnl" else s.daily_return)
            daily_data[date_str] = daily_data.get(date_str, 0.0) + val
        logger.info(
            "portfolio performance aggregated: tenant_id=%s user_id=%s days=%s",
            tenant_id,
            user_id,
            len(daily_data),
        )
        if not daily_data:
            logger.info(
                "portfolio performance empty: tenant_id=%s user_id=%s reason=no_snapshots",
                tenant_id,
                user_id,
            )

        return {
            "data": {
                "daily_returns": [round(v, 4) for v in daily_data.values()],
                "dates": list(daily_data.keys()),
                "total_return": (round(sum(daily_data.values()), 4) if daily_data else 0.0),
            }
        }
    except Exception as e:
        logger.error(f"Failed to get performance: {e}")
        return {"data": {"daily_returns": [], "total_return": 0}}


@router.get(
    "/{portfolio_id}",
    response_model=PortfolioResponse,
    summary="查询投资组合详情",
    description="根据 ID 查询投资组合的详细信息，包括资金、绩效、风险指标等。需要 JWT 认证。",
    response_description="返回投资组合详情",
)
@limiter.limit("60/minute")
async def get_portfolio(
    request: Request,
    portfolio_id: int,
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """查询投资组合详情"""
    try:
        portfolio = await PortfolioService.get_portfolio(db, portfolio_id, user_id, tenant_id=tenant_id)
        if not portfolio:
            raise HTTPException(status_code=404, detail="投资组合不存在")
        return portfolio
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get portfolio: {e}")
        raise HTTPException(status_code=500, detail="查询失败")


@router.put(
    "/{portfolio_id}",
    response_model=PortfolioResponse,
    summary="更新投资组合",
    description="更新投资组合的基本信息，如名称、描述等。需要 JWT 认证。",
    response_description="返回更新后的投资组合详情",
)
@limiter.limit("20/minute")
async def update_portfolio(
    request: Request,
    portfolio_id: int,
    data: PortfolioUpdate,
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """更新投资组合"""
    try:
        portfolio = await PortfolioService.update_portfolio(db, portfolio_id, tenant_id, user_id, data)
        return portfolio
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to update portfolio: {e}")
        raise HTTPException(status_code=500, detail="更新失败")


@router.delete(
    "/{portfolio_id}",
    response_model=MessageResponse,
    summary="删除投资组合",
    description="删除指定的投资组合，注意该操作不可恢复。需要 JWT 认证。",
    response_description="返回删除结果消息",
)
@limiter.limit("10/minute")
async def delete_portfolio(
    request: Request,
    portfolio_id: int,
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """删除投资组合"""
    try:
        await PortfolioService.delete_portfolio(db, portfolio_id, tenant_id, user_id)
        return MessageResponse(message="删除成功")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to delete portfolio: {e}")
        raise HTTPException(status_code=500, detail="删除失败")


@router.post(
    "/{portfolio_id}/calculate",
    response_model=PortfolioResponse,
    summary="计算投资组合指标",
    description="计算投资组合的各项绩效和风险指标，包括收益率、最大回撤、夏普比率等。需要 JWT 认证。",
    response_description="返回更新后的投资组合详情",
)
@limiter.limit("30/minute")
async def calculate_portfolio_metrics(
    request: Request,
    portfolio_id: int,
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """计算投资组合指标"""
    try:
        portfolio = await PortfolioService.get_portfolio(db, portfolio_id, user_id, tenant_id=tenant_id)
        if not portfolio:
            raise HTTPException(status_code=404, detail="投资组合不存在")

        portfolio = await PortfolioService.calculate_portfolio_metrics(db, portfolio)
        return portfolio
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to calculate portfolio: {e}")
        raise HTTPException(status_code=500, detail="计算失败")


@router.post(
    "/{portfolio_id}/snapshot",
    response_model=SnapshotResponse,
    status_code=201,
    summary="创建投资组合快照",
    description="为投资组合创建当前状态的快照，用于历史记录和绩效分析。需要 JWT 认证。",
    response_description="返回创建的快照详情",
)
@limiter.limit("20/minute")
async def create_portfolio_snapshot(
    request: Request,
    portfolio_id: int,
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """创建投资组合快照"""
    try:
        portfolio = await PortfolioService.get_portfolio(db, portfolio_id, user_id, tenant_id=tenant_id)
        if not portfolio:
            raise HTTPException(status_code=404, detail="投资组合不存在")

        snapshot = await PortfolioService.create_snapshot(db, portfolio)
        return snapshot
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create snapshot: {e}")
        raise HTTPException(status_code=500, detail="创建快照失败")


@router.get(
    "/{portfolio_id}/snapshots",
    response_model=list[SnapshotResponse],
    summary="查询投资组合快照历史",
    description="查询投资组合的历史快照记录，支持分页。需要 JWT 认证。",
    response_description="返回快照列表",
)
@limiter.limit("30/minute")
async def list_portfolio_snapshots(
    request: Request,
    portfolio_id: int,
    limit: int = Query(100, ge=1, le=1000, description="限制数量"),
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """查询投资组合快照历史"""
    try:
        from sqlalchemy import select

        from backend.services.trade_shared.portfolio.models import PortfolioSnapshot

        portfolio = await PortfolioService.get_portfolio(db, portfolio_id, user_id, tenant_id=tenant_id)
        if not portfolio:
            raise HTTPException(status_code=404, detail="投资组合不存在")

        stmt = (
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.portfolio_id == portfolio.id)
            .order_by(PortfolioSnapshot.snapshot_date.desc())
            .limit(limit)
        )

        result = await db.execute(stmt)
        snapshots = result.scalars().all()

        return snapshots
    except HTTPException:
        raise
@router.post(
    "/{portfolio_id}/settlement",
    response_model=SnapshotResponse,
    status_code=201,
    summary="强制执行每日结算快照",
    description="手动触发结算快照，用于补录或强制对账。会将 is_settlement 设为 True 并更新昨日收盘基准值。需要 JWT 认证。",
    response_description="返回创建的结算快照详情",
)
@limiter.limit("5/minute")
async def trigger_portfolio_settlement(
    request: Request,
    portfolio_id: int,
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """手动触发结算快照"""
    try:
        portfolio = await PortfolioService.get_portfolio(db, portfolio_id, user_id, tenant_id=tenant_id)
        if not portfolio:
            raise HTTPException(status_code=404, detail="投资组合不存在")

        # 1. 先计算最新指标，确保快照数据最新
        await PortfolioService.calculate_portfolio_metrics(db, portfolio)

        # 2. 触发结算快照 (is_settlement=True)
        # 注意：create_snapshot 内部会自动根据 is_settlement 更新 portfolio.yesterday_total_value
        snapshot = await PortfolioService.create_snapshot(db, portfolio, is_settlement=True)
        await db.commit()
        return snapshot
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to trigger manual settlement: {e}")
        raise HTTPException(status_code=500, detail="强制结算失败")


# ==================== 实盘交易管理端点 ====================


@router.post(
    "/{portfolio_id}/bind-strategy",
    response_model=RealTradingResponse,
    summary="绑定策略到组合",
    description="将指定策略绑定到投资组合，为实盘交易做准备。需要 JWT 认证。",
)
@limiter.limit("10/minute")
async def bind_strategy(
    request: Request,
    portfolio_id: int,
    data: BindStrategyRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    token: str = Depends(get_token),
    db: AsyncSession = Depends(get_db),
):
    """绑定策略到组合"""
    try:
        portfolio = await PortfolioService.bind_strategy(db, portfolio_id, data.strategy_id, tenant_id, user_id, token)
        return portfolio
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to bind strategy: {e}")
        raise HTTPException(status_code=500, detail="绑定策略失败")


@router.post(
    "/{portfolio_id}/start-trading",
    response_model=RealTradingResponse,
    summary="启动实盘交易",
    description="启动投资组合的实盘交易，需要先绑定策略。需要 JWT 认证。",
)
@limiter.limit("5/minute")
async def start_real_trading(
    request: Request,
    portfolio_id: int,
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    token: str = Depends(get_token),
    db: AsyncSession = Depends(get_db),
):
    """启动实盘交易"""
    try:
        portfolio = await PortfolioService.start_real_trading(db, portfolio_id, tenant_id, user_id, token)
        return portfolio
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to start real trading: {e}")
        raise HTTPException(status_code=500, detail="启动实盘失败")


@router.post(
    "/{portfolio_id}/stop-trading",
    response_model=RealTradingResponse,
    summary="停止实盘交易",
    description="停止投资组合的实盘交易。需要 JWT 认证。",
)
@limiter.limit("5/minute")
async def stop_real_trading(
    request: Request,
    portfolio_id: int,
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """停止实盘交易"""
    try:
        portfolio = await PortfolioService.stop_real_trading(db, portfolio_id, tenant_id, user_id)
        return portfolio
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to stop real trading: {e}")
        raise HTTPException(status_code=500, detail="停止实盘失败")


@router.post(
    "/{portfolio_id}/sync-status",
    response_model=RealTradingResponse,
    summary="同步实盘运行状态",
    description="从 Real Trading Service 同步实盘运行状态。需要 JWT 认证。",
)
@limiter.limit("30/minute")
async def sync_trading_status(
    request: Request,
    portfolio_id: int,
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """同步实盘运行状态"""
    try:
        portfolio = await PortfolioService.sync_status(db, portfolio_id, tenant_id, user_id)
        if not portfolio:
            raise HTTPException(status_code=404, detail="组合不存在")
        return portfolio
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to sync trading status: {e}")
        raise HTTPException(status_code=500, detail="同步状态失败")


@router.get("/{portfolio_id}/performance")
async def get_performance_metrics(
    portfolio_id: int = Path(..., description="组合ID"),
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """获取投资组合风险指标（夏普比率、波动率、最大回撤等）"""
    from sqlalchemy import select

    from backend.services.trade_shared.portfolio.models import PortfolioSnapshot
    from backend.services.trade_shared.portfolio.utils.risk_metrics import compute_risk_metrics

    portfolio = await PortfolioService.get_portfolio(db, portfolio_id, user_id, tenant_id=tenant_id)
    if not portfolio:
        raise HTTPException(status_code=404, detail="组合不存在")

    snap_stmt = (
        select(PortfolioSnapshot)
        .where(PortfolioSnapshot.portfolio_id == portfolio_id)
        .order_by(PortfolioSnapshot.snapshot_date.asc())
    )
    result = await db.execute(snap_stmt)
    snapshots = result.scalars().all()

    if len(snapshots) < 2:
        return {
            "portfolio_id": portfolio_id,
            "message": "快照数据不足，至少需要2个快照才能计算风险指标",
            "sharpe_ratio": None,
            "volatility": None,
            "max_drawdown": float(portfolio.max_drawdown or 0),
            "annualized_return": float(portfolio.total_return or 0),
        }

    total_values = [float(s.total_value) for s in snapshots]
    metrics = compute_risk_metrics(total_values)

    return {
        "portfolio_id": portfolio_id,
        "snapshot_count": len(snapshots),
        "sharpe_ratio": metrics.sharpe_ratio,
        "volatility": metrics.volatility,
        "max_drawdown": metrics.max_drawdown,
        "annualized_return": metrics.annualized_return,
        "calmar_ratio": metrics.calmar_ratio,
        "sortino_ratio": metrics.sortino_ratio,
    }


@router.get("/{portfolio_id}/attribution")
async def get_attribution(
    portfolio_id: int = Path(..., description="组合ID"),
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """获取投资组合绩效归因分析"""
    import dataclasses

    from backend.services.trade_shared.portfolio.services.attribution_service import (
        AttributionService,
    )

    portfolio = await PortfolioService.get_portfolio(db, portfolio_id, user_id, tenant_id=tenant_id)
    if not portfolio:
        raise HTTPException(status_code=404, detail="组合不存在")

    result = await AttributionService.analyze(db, portfolio)
    return dataclasses.asdict(result)
