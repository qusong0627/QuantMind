from datetime import datetime
from uuid import UUID

import logging
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade_shared.deps import (
    AuthContext,
    get_auth_context,
    get_db,
    get_redis,
)
from backend.services.trade_shared.redis_client import RedisClient
from backend.services.simulation.models.order import OrderStatus, TradingMode
from backend.services.simulation.schemas.order import (
    SimOrderCancelRequest,
    SimOrderCreate,
    SimOrderResponse,
)
from backend.services.simulation.services.execution_engine import (
    SimulationExecutionEngine,
)
from backend.services.simulation.services.order_service import (
    DuplicateSimOrderError,
    SimOrderService,
)
from backend.services.simulation.services.simulation_manager import (
    SimulationAccountManager,
    require_sim_user_id,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _require_user_id(raw_user_id: str, tenant_id: str = "default") -> int:
    """兼容别名，统一走 require_sim_user_id（OSS admin 归 10000001）。"""
    return require_sim_user_id(raw_user_id, tenant_id=tenant_id)


@router.post(
    "/orders", response_model=SimOrderResponse, status_code=status.HTTP_201_CREATED
)
async def create_order(
    data: SimOrderCreate,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    if data.trading_mode != TradingMode.SIMULATION:
        raise HTTPException(
            status_code=400,
            detail="Simulation service only accepts trading_mode=simulation",
        )

    order_service = SimOrderService(db)
    manager = SimulationAccountManager(redis)
    engine = SimulationExecutionEngine(db, manager)

    user_id = _require_user_id(auth.user_id, auth.tenant_id)
    # P0-1：同用户撮合临界区串行化，防止并发下单双花/快照恢复覆盖。
    # 拿不到锁直接429由前端重试，不静默放行。
    try:
        async with SimulationAccountManager.locked_execution(user_id, auth.tenant_id):
            try:
                order = await order_service.create_order(auth.tenant_id, user_id, data)
            except DuplicateSimOrderError as exc:
                # T-P2-08：同幂等键重放——返回已有台账单（客户端重试语义，不报价新单不 500）
                existing = exc.existing
                await db.refresh(existing)
                return existing
            # 会话门：非交易时段即时单转挂单顺延（与 pending worker 同语义，唯一评估实现）
            session_decision = await engine.assess_execution_window(order)
            if not session_decision.can_execute:
                await order_service.queue_order(
                    order,
                    session_decision.message,
                    trading_session_date=session_decision.target_trade_date,
                )
                await db.refresh(order)
                return order
            order.status = OrderStatus.SUBMITTED
            await db.commit()
            await db.refresh(order)

            # 风控卡点（T-RC-02）：**申报前闸**，与挂单路径（pending_order_worker 的
            # P0-2 派发复检）逐字对齐。
            #
            # 此前本端点手搓了「建单 → 会话判定 → 撮合 → 落账」，唯独漏了风控这一跳，
            # 而它不在 OrderRouter（唯一入口）之内、中间件也按端点前缀拦不到。后果：
            # 同一笔人工委托，**开市与否决定它过不过风控**——
            #   收市提交 → 转挂单 → 次日 worker 派发时复检 ✅
            #   开市提交 → 直接成交，全程无闸 ❌
            #
            # 闸放在「即将执行」这一刻而非提交时，是为了与挂单路径同口径：挂单路径注释
            # 写明「入队时刻的时段/时效约束已按语义降级为告警，此处才是真正的'申报前闸'
            # ——行情按当前 fresh 口径判定」。若提到会话判定之前，一笔本可顺延次日成交
            # 的单会在提交瞬间被拒，两条路行为分叉。
            #
            # fail-closed：闸门自身抛异常时**拒单留痕**，不静默放行。挂单路径可以
            # 「推迟本轮」（单子还在，下轮重扫），本端点没有重试容器——静默放行等于
            # 一笔未经风控的成交直接落账且无人知晓。
            #
            # 入参照挂单路径的 SimpleNamespace 形状显式构造（而非直接传 ORM 行）：
            # build_context 是 getattr 容错的鸭子类型，缺字段会静默取缺省值，
            # 显式给出才能保证两条路喂给闸门的是同一份上下文。
            try:
                from types import SimpleNamespace as _SNS

                from backend.services.trade.services.risk_gate_service import (
                    check_order as _risk_check,
                )

                _side = str(getattr(order.side, "value", order.side) or "").lower()
                _otype = str(
                    getattr(order.order_type, "value", order.order_type) or "market"
                ).lower()
                _risk = await _risk_check(
                    _SNS(
                        tenant_id=str(getattr(order, "tenant_id", "default")),
                        user_id=int(getattr(order, "user_id", 0) or 0),
                        symbol=str(getattr(order, "symbol", "")),
                        # 枚举须取 .value：`_CaseInsensitiveEnum` 没覆盖 `__str__`，
                        # 3.10 下 `str(OrderSide.BUY)` 是 `'OrderSide.BUY'` 不是 `'buy'`。
                        side=_side,
                        quantity=float(getattr(order, "quantity", 0) or 0.0),
                        price=(
                            float(order.price) if getattr(order, "price", None) else None
                        ),
                        order_type=_otype,
                        trading_mode="SIMULATION",
                        source=str(getattr(order, "source", "") or "manual"),
                        remarks=getattr(order, "remarks", None),
                        client_order_id=str(
                            getattr(order, "client_order_id", "") or ""
                        ),
                        strategy_id=str(getattr(order, "strategy_id", "") or ""),
                    ),
                    db=db,
                    redis=redis,
                )
                risk_passed = bool(_risk.passed)
                risk_message = (
                    f"风控拒单[{_risk.rule_id or 'risk'}]（申报前）: {_risk.reason}"
                )
            except Exception as exc:  # noqa: BLE001 - fail-closed，绝不静默放行
                logger.warning(
                    "simulation order risk gate unavailable (fail-closed): %s", exc
                )
                risk_passed = False
                risk_message = f"风控不可用（fail-closed），拒单: {exc}"

            if not risk_passed:
                await engine.mark_rejected(order, risk_message[:500])
                await db.refresh(order)
                return order

            result = await engine.execute_order(order)
            if not result.success:
                await engine.mark_rejected(order, result.message)
                await db.refresh(order)
                return order

            await engine.apply_filled(order, result)
            await db.refresh(order)
            return order
    except RuntimeError as exc:
        if str(exc) == "SIM_EXEC_LOCK_BUSY":
            raise HTTPException(
                status_code=429, detail="账户撮合繁忙，请稍后重试"
            ) from exc
        raise


@router.get("/orders", response_model=list[SimOrderResponse])
async def list_orders(
    portfolio_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    start_date: datetime | None = Query(default=None),
    end_date: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    user_id = _require_user_id(auth.user_id, auth.tenant_id)
    service = SimOrderService(db)
    orders = await service.list_orders(
        auth.tenant_id,
        user_id,
        portfolio_id=portfolio_id,
        status=status,
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )
    # 批量 enrich 名称，避免前端 N+1，外观与 真实订单/持仓 口径一致
    try:
        from backend.services.trade_shared.utils.stock_lookup import lookup_symbol_name

        enriched = []
        for o in orders:
            try:
                name = (
                    lookup_symbol_name(o.symbol) if getattr(o, "symbol", None) else None
                )
            except Exception:
                name = None
            # Pydantic from_attributes 读不到 DB 列时，用运行时属性补齐
            try:
                o.symbol_name = name  # type: ignore[attr-defined]
            except Exception:
                pass
            enriched.append(o)
        return enriched
    except Exception:
        return orders


@router.get("/orders/{order_id}", response_model=SimOrderResponse)
async def get_order(
    order_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    user_id = _require_user_id(auth.user_id, auth.tenant_id)
    service = SimOrderService(db)
    order = await service.get_order(auth.tenant_id, user_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Simulation order not found")
    return order


@router.post("/orders/{order_id}/cancel", response_model=SimOrderResponse)
async def cancel_order(
    order_id: UUID,
    request: SimOrderCancelRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    user_id = _require_user_id(auth.user_id, auth.tenant_id)
    service = SimOrderService(db)
    order = await service.get_order(auth.tenant_id, user_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Simulation order not found")

    try:
        return await service.cancel_order(order, request.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
