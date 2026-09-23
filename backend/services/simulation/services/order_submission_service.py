"""
Unified simulation/shadow order submission pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.simulation.models.fill import SimulationFill
from backend.services.simulation.models.order_v2 import SimulationOrderV2
from backend.services.simulation.models.order import (
    OrderSide,
    OrderStatus,
    OrderType,
)
from backend.services.simulation.schemas.order import SimOrderCreate
from backend.services.simulation.services.execution_engine import (
    SimulationExecutionEngine,
)
from backend.services.simulation.services.order_service import (
    DuplicateSimOrderError,
    SimOrderService,
)
from backend.services.simulation.services.simulation_manager import (
    SimulationAccountManager,
)
from backend.shared.order_contract import normalize_agent


@dataclass
class SimulationSubmissionOutcome:
    success: bool
    order_id: str | None = None
    trade_id: str | None = None
    client_order_id: str | None = None
    fill_price: float = 0.0
    filled_quantity: float = 0.0
    commission: float = 0.0
    price_source: str | None = None
    message: str = ""


class SimulationOrderSubmissionService:
    def __init__(self, db: AsyncSession, manager: SimulationAccountManager):
        self.db = db
        self.manager = manager
        self.order_service = SimOrderService(db)
        self.engine = SimulationExecutionEngine(db, manager)

    async def submit_and_fill(
        self,
        *,
        tenant_id: str,
        user_id: int,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str,
        price: float | None = None,
        portfolio_id: int = 0,
        strategy_id: int | None = None,
        trade_action: str | None = None,
        position_side: str = "long",
        is_margin_trade: bool = False,
        remarks: str | None = None,
        client_order_id: str | None = None,
        trigger_source: str = "manual",
        time_in_force: str = "DAY",
        expires_at: datetime | None = None,
        strict_market: bool = True,
        bar: Any = None,
        agent: str = "",
    ) -> SimulationSubmissionOutcome:
        # P0-1/P0-4：同用户临界区串行化（幂等查+建单+撮合+落库），防并发双花与
        # 融券读-改-写丢更新。锁忙直接失败由调用方重试，不静默放行。
        try:
            _lock_cm = SimulationAccountManager.locked_execution(
                int(user_id), tenant_id
            )
        except Exception:
            _lock_cm = None
        if _lock_cm is None:
            return SimulationSubmissionOutcome(
                success=False,
                client_order_id=str(client_order_id or "").strip() or None,
                message="账户撮合繁忙，请稍后重试",
            )
        try:
            async with _lock_cm:
                return await self._submit_and_fill_locked(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    order_type=order_type,
                    price=price,
                    portfolio_id=portfolio_id,
                    strategy_id=strategy_id,
                    trade_action=trade_action,
                    position_side=position_side,
                    is_margin_trade=is_margin_trade,
                    remarks=remarks,
                    client_order_id=client_order_id,
                    trigger_source=trigger_source,
                    time_in_force=time_in_force,
                    expires_at=expires_at,
                    strict_market=strict_market,
                    bar=bar,
                    agent=agent,
                )
        except RuntimeError:
            return SimulationSubmissionOutcome(
                success=False,
                client_order_id=str(client_order_id or "").strip() or None,
                message="账户撮合繁忙，请稍后重试",
            )

    async def _submit_and_fill_locked(
        self,
        *,
        tenant_id: str,
        user_id: int,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str,
        price: float | None = None,
        portfolio_id: int = 0,
        strategy_id: int | None = None,
        trade_action: str | None = None,
        position_side: str = "long",
        is_margin_trade: bool = False,
        remarks: str | None = None,
        client_order_id: str | None = None,
        trigger_source: str = "manual",
        time_in_force: str = "DAY",
        expires_at: datetime | None = None,
        strict_market: bool = True,
        bar: Any = None,
        agent: str = "",
    ) -> SimulationSubmissionOutcome:
        normalized_client_order_id = str(client_order_id or "").strip() or None
        if normalized_client_order_id:
            # T-P2-08：先查台账本体（权威）——投影可能为空，单查投影是幂等断链的历史根因；
            # 命中后尽量用投影富化成交信息，投影缺失则给最小 duplicate 结果（促发者只需 duplicate 语义）
            sim_existing = await self.order_service.get_sim_order_by_client_order_id(
                tenant_id=tenant_id,
                user_id=user_id,
                client_order_id=normalized_client_order_id,
            )
            if sim_existing is not None:
                projection = (
                    await self.order_service.get_projection_order_by_client_order_id(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        client_order_id=normalized_client_order_id,
                    )
                )
                if projection is not None:
                    return await self._build_duplicate_outcome(projection)
                return SimulationSubmissionOutcome(
                    success=True,
                    order_id=str(sim_existing.order_id),
                    client_order_id=normalized_client_order_id,
                    message="duplicate client_order_id skipped",
                )
            existing_order = (
                await self.order_service.get_projection_order_by_client_order_id(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    client_order_id=normalized_client_order_id,
                )
            )
            if existing_order is not None:
                return await self._build_duplicate_outcome(existing_order)

        try:
            order = await self.order_service.create_order(
                tenant_id,
                user_id,
                SimOrderCreate(
                    portfolio_id=max(0, int(portfolio_id or 0)),
                    strategy_id=strategy_id,
                    client_order_id=normalized_client_order_id,
                    time_in_force=str(time_in_force or "DAY").strip().upper() or "DAY",
                    expires_at=expires_at,
                    symbol=symbol,
                    side=OrderSide(str(side or "").strip().lower()),
                    order_type=OrderType(str(order_type or "").strip().lower()),
                    quantity=float(quantity),
                    price=float(price) if price and float(price) > 0 else None,
                    remarks=remarks,
                    trade_action=trade_action,
                    position_side=str(position_side or "long").strip().lower(),
                    is_margin_trade=bool(is_margin_trade),
                    # P2.7 分账归属：即时链是 DecisionRouter 的唯一落地路径，落台账后
                    # 镜像真单从这一列继承。此处按列宽归一**而不是**把裸名交给 schema：
                    # 超宽名会被 ``max_length`` 抛校验错 → 整笔单发不出去（模型让卖、
                    # 系统没卖），而所有权都是「这个名太长了」这种非交易原因。
                    agent=normalize_agent(agent),
                ),
                trigger_source=trigger_source,
            )
        except DuplicateSimOrderError:
            # T-P2-08：唯一索引竞态兜底（并发/重放命中同一幂等键）——转既有 duplicate 语义
            return SimulationSubmissionOutcome(
                success=True,
                client_order_id=normalized_client_order_id,
                message="duplicate client_order_id skipped",
            )
        expires_at_value = self.engine._normalize_runtime_datetime(
            getattr(order, "expires_at", None)
        )
        if expires_at_value is not None and expires_at_value <= datetime.now():
            await self.engine.mark_expired(order, "Order expired before execution")
            return SimulationSubmissionOutcome(
                success=False,
                order_id=str(order.order_id),
                client_order_id=normalized_client_order_id,
                message="Order expired before execution",
            )

        session_decision = await self.engine.assess_execution_window(order)
        if session_decision.target_trade_date is not None:
            order.trading_session_date = session_decision.target_trade_date
        if not session_decision.can_execute:
            if session_decision.final_state == "expired":
                await self.engine.mark_expired(order, session_decision.message)
                await self.order_service.sync_order_projection(
                    order, rejected_reason=str(session_decision.message or "")[:500]
                )
                return SimulationSubmissionOutcome(
                    success=False,
                    order_id=str(order.order_id),
                    client_order_id=normalized_client_order_id,
                    message=session_decision.message,
                )
            if session_decision.retryable:
                await self.order_service.queue_order(
                    order,
                    session_decision.message,
                    trading_session_date=session_decision.target_trade_date,
                )
                return SimulationSubmissionOutcome(
                    success=True,
                    order_id=str(order.order_id),
                    client_order_id=normalized_client_order_id,
                    message="queued_pending_session",
                )
            await self.engine.mark_rejected(order, session_decision.message)
            await self.order_service.sync_order_projection(
                order, rejected_reason=str(session_decision.message or "")[:500]
            )
            return SimulationSubmissionOutcome(
                success=False,
                order_id=str(order.order_id),
                client_order_id=normalized_client_order_id,
                message=session_decision.message,
            )

        order.status = OrderStatus.SUBMITTED
        order.submitted_at = order.submitted_at or datetime.now(timezone.utc)
        await self.order_service.sync_order_projection(order)
        await self.db.commit()

        execution_result = await self.engine.execute_order(
            order, strict_market=strict_market, bar=bar
        )
        if not execution_result.success:
            if str(execution_result.message or "") == "Order expired before execution":
                await self.engine.mark_expired(order, execution_result.message)
            else:
                await self.engine.mark_rejected(order, execution_result.message)
            # 终态镜像 V2 投影（否则投影滞留 submitted，拒因丢失）
            await self.order_service.sync_order_projection(
                order, rejected_reason=str(execution_result.message or "")[:500] or None
            )
            return SimulationSubmissionOutcome(
                success=False,
                order_id=str(order.order_id),
                client_order_id=normalized_client_order_id,
                message=str(execution_result.message or ""),
            )

        trade = await self.engine.apply_filled(order, execution_result)
        await self.order_service.sync_order_projection(order)
        return SimulationSubmissionOutcome(
            success=True,
            order_id=str(order.order_id),
            trade_id=str(trade.trade_id),
            client_order_id=normalized_client_order_id,
            fill_price=round(float(execution_result.price or 0.0), 4),
            filled_quantity=float(execution_result.quantity or 0.0),
            commission=float(execution_result.commission or 0.0),
            price_source=execution_result.price_source,
            message="filled",
        )

    async def _build_duplicate_outcome(
        self,
        order: SimulationOrderV2,
    ) -> SimulationSubmissionOutcome:
        fills = list(
            (
                await self.db.execute(
                    select(SimulationFill)
                    .where(SimulationFill.order_id == order.order_id)
                    .order_by(
                        SimulationFill.executed_at.desc(), SimulationFill.id.desc()
                    )
                )
            )
            .scalars()
            .all()
        )
        latest_fill = fills[0] if fills else None
        status = str(getattr(order.status, "value", order.status) or "").lower()
        if status == OrderStatus.FILLED.value and latest_fill is not None:
            return SimulationSubmissionOutcome(
                success=True,
                order_id=str(order.order_id),
                trade_id=str(latest_fill.fill_id),
                client_order_id=order.client_order_id,
                fill_price=round(float(latest_fill.fill_price or 0.0), 4),
                filled_quantity=float(latest_fill.fill_quantity or 0.0),
                commission=float(latest_fill.commission or 0.0),
                price_source=latest_fill.price_source,
                message="duplicate client_order_id skipped",
            )
        return SimulationSubmissionOutcome(
            success=status
            not in {
                OrderStatus.REJECTED.value,
                OrderStatus.CANCELLED.value,
                "expired",
            },
            order_id=str(order.order_id),
            trade_id=str(latest_fill.fill_id) if latest_fill is not None else None,
            client_order_id=order.client_order_id,
            fill_price=round(float(latest_fill.fill_price or 0.0), 4)
            if latest_fill is not None
            else 0.0,
            filled_quantity=float(latest_fill.fill_quantity or 0.0)
            if latest_fill is not None
            else 0.0,
            commission=float(latest_fill.commission or 0.0)
            if latest_fill is not None
            else 0.0,
            price_source=latest_fill.price_source if latest_fill is not None else None,
            message=(
                "duplicate client_order_id skipped"
                if status
                in {
                    OrderStatus.PENDING.value,
                    OrderStatus.SUBMITTED.value,
                    OrderStatus.FILLED.value,
                }
                else str(
                    order.rejected_reason
                    or (
                        "duplicate client_order_id expired"
                        if status == "expired"
                        else "duplicate client_order_id rejected"
                    )
                )
            ),
        )
