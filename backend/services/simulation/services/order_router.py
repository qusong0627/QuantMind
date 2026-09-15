"""OrderRouter（T-P2-01）：模拟盘**下单唯一入口**。

五条下单路径（托管引擎 / 手动·内部单 / 沙箱信号 / TDX 滚动 paper / 保证金平仓）
全部改经本入口；链内统一提供：参数校验 → 账户锁 → 幂等（client_order_id）→
建单（Order 契约列落 source/client_order_id）→ 撮合 → 落账（apply_filled→Ledger）→
（可选）真单镜像。

**架构决策（细案论证）**：组合而非重写——即时链委托给已被线上验证的
``SimulationOrderSubmissionService``（账户锁/幂等/会话窗/投影），本模块在其上补齐：
① 统一请求/结果契约；② from_bar 托管模式（周期调仓）；③ 镜像收口（修沙箱链路
"永不同步真单"）；④ strict_market 分级（手动=True 保 P0-5；自动化=False 允许
如实标注的降级，见取价契约 T-P2-03）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.shared.order_contract import SOURCE_MANUAL

logger = logging.getLogger(__name__)


@dataclass
class OrderRequest:
    tenant_id: str
    user_id: int  # 归一 int uid（调用方保证；0 视为无效）
    symbol: str
    side: str  # buy | sell
    quantity: float
    order_type: str = "market"  # market | limit
    price: float | None = None
    source: str = SOURCE_MANUAL
    client_order_id: str | None = None
    strategy_id: int | None = None
    portfolio_id: int = 0
    remarks: str | None = None
    trade_action: str | None = None
    position_side: str = "long"
    is_margin_trade: bool = False
    bar: Any = None  # 非空 → 托管 from_bar 撮合；空 → 即时撮合
    run_id: str = ""
    mirror: bool = False  # 成交后是否触发真单镜像
    mirror_source: str = ""
    strict_market: bool | None = None  # None=按模式自动

    def resolved_strict(self) -> bool:
        if self.strict_market is not None:
            return bool(self.strict_market)
        return self.bar is None  # 即时默认 strict（P0-5）；托管允许如实降级


@dataclass
class RouterOutcome:
    success: bool
    order_id: str | None = None
    trade_id: str | None = None
    client_order_id: str | None = None
    fill_price: float = 0.0
    filled_quantity: float = 0.0
    commission: float = 0.0
    price_source: str | None = None
    message: str = ""
    duplicate: bool = False
    mirror: dict | None = None


def is_duplicate_message(message: str | None) -> bool:
    """幂等命中判定（纯函数，与 SubmissionService 的话术契约一致）。"""
    return str(message or "").startswith("duplicate client_order_id")


async def submit_order(db, redis, req: OrderRequest) -> RouterOutcome:
    """唯一入口。db=AsyncSession；redis=交易 Redis 客户端（SimulationAccountManager 用）。"""
    from backend.services.trade_shared.simulation_manager import SimulationAccountManager

    if not str(req.symbol or "").strip():
        return RouterOutcome(success=False, message="参数无效：symbol 为空")
    if float(req.quantity or 0) <= 0:
        return RouterOutcome(success=False, message="参数无效：quantity<=0")
    uid = int(req.user_id or 0)
    if uid <= 0:
        return RouterOutcome(success=False, message="无效用户 ID")

    manager = SimulationAccountManager(redis)
    if req.bar is None:
        routed = await _submit_immediate(db, manager, req)
    else:
        routed = await _submit_from_bar(db, manager, req)

    if req.mirror and routed.success and not routed.duplicate:
        routed.mirror = await _mirror_fill(redis, req, routed)
    return routed


async def _submit_immediate(db, manager, req: OrderRequest) -> RouterOutcome:
    """即时路径：委托给统一提交链（锁/幂等/会话窗/投影/落账 全在其内）。"""
    from backend.services.simulation.services.order_submission_service import (
        SimulationOrderSubmissionService,
    )

    service = SimulationOrderSubmissionService(db, manager)
    outcome = await service.submit_and_fill(
        tenant_id=req.tenant_id,
        user_id=req.user_id,
        symbol=req.symbol,
        side=req.side,
        quantity=req.quantity,
        order_type=req.order_type,
        price=req.price,
        portfolio_id=req.portfolio_id,
        strategy_id=req.strategy_id,
        trade_action=req.trade_action,
        position_side=req.position_side,
        is_margin_trade=req.is_margin_trade,
        remarks=req.remarks,
        client_order_id=req.client_order_id,
        trigger_source=req.source,
        strict_market=req.resolved_strict(),
    )
    return RouterOutcome(
        success=bool(outcome.success),
        order_id=outcome.order_id,
        trade_id=outcome.trade_id,
        client_order_id=outcome.client_order_id,
        fill_price=float(outcome.fill_price or 0.0),
        filled_quantity=float(outcome.filled_quantity or 0.0),
        commission=float(outcome.commission or 0.0),
        price_source=outcome.price_source,
        message=str(outcome.message or ""),
        duplicate=is_duplicate_message(outcome.message),
    )


async def _submit_from_bar(db, manager, req: OrderRequest) -> RouterOutcome:
    """托管周期路径：锁→幂等→建单→execute_from_bar→落账（链内补齐）。"""
    from backend.services.simulation.models.order import (
        OrderSide,
        OrderStatus,
        OrderType,
    )
    from backend.services.simulation.schemas.order import SimOrderCreate
    from backend.services.simulation.services.execution_engine import (
        SimulationExecutionEngine,
    )
    from backend.services.simulation.services.order_service import SimOrderService
    from backend.shared.order_contract import ensure_order_contract_columns_async
    from backend.services.trade_shared.simulation_manager import (
        SimulationAccountManager,
    )

    order_service = SimOrderService(db)
    engine = SimulationExecutionEngine(db, manager)
    cid = str(req.client_order_id or "").strip() or None

    if cid:
        existing = await order_service.get_projection_order_by_client_order_id(
            tenant_id=req.tenant_id,
            user_id=req.user_id,
            client_order_id=cid,
        )
        if existing is not None:
            return RouterOutcome(
                success=True,
                order_id=str(existing.order_id),
                client_order_id=cid,
                duplicate=True,
                message="duplicate client_order_id skipped",
            )

    try:
        lock_cm = SimulationAccountManager.locked_execution(int(req.user_id), req.tenant_id)
    except Exception:
        lock_cm = None
    if lock_cm is None:
        return RouterOutcome(success=False, message="账户撮合繁忙，请稍后重试")

    async with lock_cm:
        await ensure_order_contract_columns_async()
        order = await order_service.create_order(
            req.tenant_id,
            str(req.user_id),
            SimOrderCreate(
                portfolio_id=max(0, int(req.portfolio_id or 0)),
                strategy_id=req.strategy_id,
                client_order_id=cid,
                symbol=req.symbol,
                side=OrderSide(str(req.side or "").strip().lower()),
                order_type=OrderType(str(req.order_type or "").strip().lower()),
                quantity=float(req.quantity),
                price=float(req.price) if req.price and float(req.price) > 0 else None,
                remarks=req.remarks,
                position_side=str(req.position_side or "long").strip().lower(),
                is_margin_trade=bool(req.is_margin_trade),
            ),
            trigger_source=req.source,
        )
        order.status = OrderStatus.SUBMITTED
        order.submitted_at = order.submitted_at or datetime.now(timezone.utc)
        await db.commit()

        result = await engine.execute_from_bar(order, req.bar)
        if not result.success:
            await engine.mark_rejected(order, result.message)
            await db.commit()
            return RouterOutcome(
                success=False,
                order_id=str(order.order_id),
                client_order_id=cid,
                price_source=result.price_source,
                message=str(result.message or ""),
            )
        trade = await engine.apply_filled(order, result)
        return RouterOutcome(
            success=True,
            order_id=str(order.order_id),
            trade_id=str(trade.trade_id),
            client_order_id=cid,
            fill_price=round(float(result.price or 0.0), 4),
            filled_quantity=float(result.quantity or 0.0),
            commission=float(result.commission or 0.0),
            price_source=result.price_source,
            message="filled",
        )


async def _mirror_fill(redis, req: OrderRequest, routed: RouterOutcome) -> dict | None:
    """成交后真单镜像（收口在 Router；mirror_virtual_fill 自身吞异常）。"""
    try:
        from backend.services.live_trading.services.real_mirror_service import (
            mirror_virtual_fill,
        )
        from backend.services.simulation.services.market_rules import infer_market

        market = infer_market(req.symbol).value
        return await mirror_virtual_fill(
            db=None,
            redis=redis,
            tenant_id=req.tenant_id,
            user_id=str(req.user_id),
            symbol=req.symbol,
            side=req.side,
            quantity=routed.filled_quantity or req.quantity,
            price=routed.fill_price,
            sim_order_id=str(routed.order_id or ""),
            run_id=req.run_id,
            strategy_id=str(req.strategy_id or ""),
            market=market,
            source=req.mirror_source or req.source,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[OrderRouter] 镜像调用异常（不影响虚拟成交）: %s", exc)
        return None
