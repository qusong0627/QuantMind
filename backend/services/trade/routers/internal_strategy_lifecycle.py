from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from .internal_strategy_utils import *
from backend.services.trade.services.internal_strategy_dispatcher import dispatch_internal_strategy_order
from backend.services.trade.services.manual_execution_service import manual_execution_service

router = APIRouter(tags=["Internal Strategy Gateway"])
logger = logging.getLogger(__name__)


class HostedExecutionCreateRequest(BaseModel):
    task_id: str | None = None
    strategy_id: str
    run_id: str | None = None
    trading_mode: str = "REAL"
    execution_config: dict[str, Any] | None = None
    live_trade_config: dict[str, Any] | None = None
    signals: list[dict[str, Any]] = Field(default_factory=list)
    trigger_context: dict[str, Any] | None = None
    parent_runtime_id: str | None = None
    note: str | None = None

@router.post("/heartbeat", dependencies=[Depends(verify_internal_call)])
async def strategy_heartbeat(
    payload: dict[str, Any],
    x_user_id: str = Header(...),
    x_tenant_id: str | None = Header(None),
    redis: RedisClient = Depends(get_redis),
):
    """
    接收策略 Pod 的心跳与内部状态上报
    """
    user_id = x_user_id
    strategy_id = payload.get("strategy_id", "default")

    # 构造展示给 UI 的状态数据
    status_data = {
        "last_seen": time.time(),
        "status": "running",
        "metrics": payload.get("metrics", {}),
        "strategy_nav": payload.get("strategy_nav", 1.0),
        "pod_name": payload.get("pod_name", "unknown"),
    }

    # 统一写入 Redis 命名空间：quantmind:strategy:status:{user_id}
    tenant_id = (x_tenant_id or "").strip() or "default"
    key = f"quantmind:strategy:status:{tenant_id}:{user_id}:{strategy_id}"
    # RedisClient.set 会自动 json.dumps 并支持 ttl
    redis.set(key, status_data, ttl=60)

    return {"status": "ok"}


@router.get("/sync-account", dependencies=[Depends(verify_internal_call)])
async def sync_account_state(
    x_user_id: str = Header(...), x_tenant_id: str | None = Header(None), db=Depends(get_db)
):
    """
    供策略 Pod 启动时初始化：获取真实的资金和持仓
    """
    try:
        user_id = int(x_user_id)
        tenant_id = (x_tenant_id or "").strip() or "default"
        p_stmt = (
            select(Portfolio)
            .where(
                and_(
                    Portfolio.tenant_id == tenant_id,
                    Portfolio.user_id == user_id,
                    Portfolio.status == "active",
                    Portfolio.is_deleted == False,
                )
            )
            .order_by(Portfolio.updated_at.desc())
            .limit(1)
        )
        p_result = await db.execute(p_stmt)
        portfolio = p_result.scalar_one_or_none()
        if portfolio is None:
            return {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "portfolio_id": None,
                "cash": 0.0,
                "market_value": 0.0,
                "total_asset": 0.0,
                "positions": {},
            }

        pos_stmt = select(Position).where(
            and_(
                Position.portfolio_id == portfolio.id,
                Position.status == "holding",
            )
        )
        pos_result = await db.execute(pos_stmt)
        rows = pos_result.scalars().all()
        positions = {
            str(p.symbol): {
                "volume": int(p.quantity or 0),
                "market_value": float(p.market_value or 0),
                "price": float(p.current_price or 0),
            }
            for p in rows
        }

        return {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "portfolio_id": portfolio.id,
            "cash": float(portfolio.available_cash or 0),
            "market_value": float(portfolio.total_value or 0) - float(portfolio.available_cash or 0),
            "total_asset": float(portfolio.total_value or 0),
            "positions": positions,
        }
    except Exception as e:
        logger.error(f"Failed to sync account: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/order", dependencies=[Depends(verify_internal_call)])
async def strategy_order(
    order_data: dict[str, Any],
    x_user_id: str = Header(...),
    x_tenant_id: str | None = Header(None),
    redis: RedisClient = Depends(get_redis),
    db=Depends(get_db),
):
    """接收策略生成的信号，支持实盘、影子与模拟模式。"""
    return await dispatch_internal_strategy_order(
        order_data=order_data,
        user_id=x_user_id,
        tenant_id=(x_tenant_id or "default"),
        redis=redis,
        db=db,
    )


@router.post("/hosted-executions", dependencies=[Depends(verify_internal_call)])
async def create_hosted_execution(
    payload: HostedExecutionCreateRequest,
    x_user_id: str = Header(...),
    x_tenant_id: str | None = Header(None),
):
    result = await manual_execution_service.create_hosted_task(
        tenant_id=(x_tenant_id or "default"),
        user_id=x_user_id,
        task_id=payload.task_id,
        run_id=payload.run_id,
        strategy_id=payload.strategy_id,
        trading_mode=payload.trading_mode,
        execution_config=payload.execution_config,
        live_trade_config=payload.live_trade_config,
        trigger_context=payload.trigger_context,
        parent_runtime_id=payload.parent_runtime_id,
        note=payload.note,
    )
    return {"status": "success", **result}
