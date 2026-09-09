from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade_shared.models.enums import OrderSide, OrderStatus, OrderType, PositionSide, TradeAction, TradingMode
from backend.services.trade_shared.models.order import Order
from backend.services.trade_shared.portfolio.models import Portfolio
from backend.services.trade_shared.redis_client import RedisClient
from backend.services.trade_shared.schemas.order import OrderCreate
from backend.services.trade_shared.services.order_service import OrderService
from backend.services.trade_shared.simulation_manager import SimulationAccountManager
from backend.services.live_trading.services.trading_engine import TradingEngine
from backend.services.live_trading.routers.real_trading_utils import _fetch_active_portfolio_snapshot
from backend.services.live_trading.services.real_mirror_service import (
    mirror_virtual_fill,
)

logger = logging.getLogger(__name__)

_TRADE_ACTION_ALIAS = {
    "open": "buy_to_open",
    "buy_open": "buy_to_open",
    "buy_to_open": "buy_to_open",
    "close": "sell_to_close",
    "sell_close": "sell_to_close",
    "sell_to_close": "sell_to_close",
    "short": "sell_to_open",
    "sell_open": "sell_to_open",
    "sell_to_open": "sell_to_open",
    "cover": "buy_to_close",
    "buy_close": "buy_to_close",
    "buy_to_close": "buy_to_close",
}


def _normalize_trade_action(raw: Any) -> str | None:
    value = str(getattr(raw, "value", raw) or "").strip().lower()
    return _TRADE_ACTION_ALIAS.get(value, value) or None


async def dispatch_internal_strategy_order(
    *,
    order_data: dict[str, Any],
    user_id: str,
    tenant_id: str,
    redis: RedisClient,
    db: AsyncSession,
) -> dict[str, Any]:
    """复用内部策略下单逻辑：实盘走真实风控/柜台，影子/模拟走虚拟成交。"""
    # user_id 口径与模拟盘接口（simulation.py _require_user_id）对齐：
    # 非数字 JWT sub 统一映射为 0，保证命中同一模拟账户 Redis 键。
    _uid_raw = str(user_id or "").strip()
    uid = int(_uid_raw) if _uid_raw.isdigit() else 0
    tenant = (tenant_id or "").strip() or "default"
    trading_mode_raw = str(order_data.get("trading_mode", "REAL")).upper()
    try:
        trading_mode = TradingMode(trading_mode_raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid trading_mode: {trading_mode_raw}")

    symbol = str(order_data.get("symbol") or "").strip().upper()
    side_raw = str(order_data.get("side") or "").strip().upper()
    quantity = float(order_data.get("quantity") or 0)
    price = float(order_data.get("price") or 0)
    order_type_raw = str(order_data.get("order_type") or "LIMIT").strip().upper()
    trade_action_raw = _normalize_trade_action(order_data.get("trade_action"))
    position_side_raw = str(order_data.get("position_side") or "long").strip().lower()
    is_margin_trade = bool(order_data.get("is_margin_trade", False))
    client_order_id = str(order_data.get("client_order_id") or "").strip() or None
    remarks = order_data.get("remarks")

    if client_order_id is None:
        client_order_id = f"auto-{uuid.uuid4().hex}"

    if not symbol:
        raise HTTPException(status_code=400, detail="missing symbol")
    if side_raw not in {"BUY", "SELL"}:
        raise HTTPException(status_code=400, detail=f"invalid side: {side_raw}")
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be > 0")

    logger.info(
        "[Order] 收到信号 | 租户=%s 模式=%s | %s %s @ %s | trade_action=%s position_side=%s margin=%s",
        tenant,
        trading_mode.value,
        side_raw,
        symbol,
        price,
        trade_action_raw,
        position_side_raw,
        is_margin_trade,
    )

    if trading_mode in {TradingMode.SHADOW, TradingMode.SIMULATION}:
        sim_manager = SimulationAccountManager(redis)
        try:
            from datetime import datetime, timezone

            from backend.services.simulation.models.order import (
                OrderSide as SimOrderSide,
                OrderStatus as SimOrderStatus,
                OrderType as SimOrderType,
                SimOrder,
            )
            from backend.services.simulation.models.trade import SimTrade

            # 幂等：同一 client_order_id 已落账则跳过，防止任务重试导致重复扣款/加仓。
            # sim_orders 无 client_order_id 列，以 remarks 标记作为幂等键。
            dup_marker = f"client_order_id={client_order_id}"
            dup_stmt = (
                select(SimOrder.order_id)
                .where(
                    and_(
                        SimOrder.tenant_id == tenant,
                        SimOrder.user_id == uid,
                        SimOrder.remarks == dup_marker,
                    )
                )
                .limit(1)
            )
            if (await db.execute(dup_stmt)).scalar_one_or_none() is not None:
                return {
                    "status": "success",
                    "execution": "duplicate_skipped",
                    "result": {
                        "success": True,
                        "message": f"duplicate client_order_id skipped: {client_order_id}",
                    },
                }

            side = 1 if side_raw == "BUY" else -1
            gross = price * quantity
            # A 股虚拟成交费用：佣金（双向、最低 5 元）+ 印花税（卖出单边），
            # 与 PaperTradingBroker / SimulationExecutionEngine 口径一致，
            # 否则手动/托管任务的虚拟成交不扣费，账户现金与真实券商口径背离。
            commission = 0.0
            stamp_duty = 0.0
            total_fee = 0.0
            if gross > 0:
                try:
                    from backend.services.trade_shared.trade_config import settings

                    commission = max(
                        round(gross * float(settings.SIMULATION_COMMISSION_RATE), 2),
                        float(settings.SIMULATION_COMMISSION_MIN),
                    )
                    try:
                        from backend.services.simulation.services.market_rules import (
                            infer_market,
                        )

                        cn_market = str(infer_market(symbol).value).upper() == "CN"
                    except Exception:  # noqa: BLE001
                        cn_market = True
                    stamp_duty = (
                        round(gross * float(settings.SIMULATION_STAMP_DUTY_RATE), 2)
                        if cn_market and side < 0
                        else 0.0
                    )
                except Exception:  # noqa: BLE001
                    commission = max(round(gross * 0.0003, 2), 5.0)
                    stamp_duty = round(gross * 0.0005, 2) if side < 0 else 0.0
                total_fee = round(commission + stamp_duty, 2)
            delta_cash = -(gross + total_fee) if side > 0 else gross - total_fee
            result = await sim_manager.update_balance(
                user_id=uid,
                symbol=symbol,
                delta_cash=delta_cash,
                delta_volume=quantity if side > 0 else -quantity,
                price=price,
                tenant_id=tenant,
                trade_action=trade_action_raw,
                position_side=position_side_raw,
                is_margin_trade=is_margin_trade,
            )
            if not result.get("success"):
                logger.warning(
                    "[Shadow/Sim] 虚拟成交被账户拒绝: %s %s reason=%s",
                    symbol,
                    side_raw,
                    result.get("reason"),
                )
                return {"status": "failed", "execution": "virtual", "detail": result}

            # 补写模拟订单/成交台账（sim_orders + sim_trades），让仪表盘交易记录、
            # 成交统计等读取侧能查到手动/托管任务的虚拟成交。
            # DB 落账失败不回滚 Redis 账户（账户资金为准），仅记录错误。
            now = datetime.now(timezone.utc)
            strategy_id_raw = str(order_data.get("strategy_id") or "").strip()
            sim_side = SimOrderSide.BUY if side > 0 else SimOrderSide.SELL
            sim_order = SimOrder(
                tenant_id=tenant,
                user_id=uid,
                portfolio_id=0,
                strategy_id=int(strategy_id_raw) if strategy_id_raw.isdigit() else None,
                symbol=symbol,
                side=sim_side,
                order_type=SimOrderType.MARKET if order_type_raw == "MARKET" else SimOrderType.LIMIT,
                status=SimOrderStatus.FILLED,
                quantity=quantity,
                filled_quantity=quantity,
                price=price if price > 0 else None,
                average_price=price,
                order_value=gross,
                filled_value=gross,
                commission=commission,
                total_fee=total_fee,
                submitted_at=now,
                filled_at=now,
                execution_model="virtual_fill",
                price_source="internal_dispatcher",
                remarks=dup_marker,
            )
            db.add(sim_order)
            await db.flush()
            db.add(
                SimTrade(
                    order_id=sim_order.order_id,
                    tenant_id=tenant,
                    user_id=uid,
                    portfolio_id=0,
                    symbol=symbol,
                    side=sim_side,
                    quantity=quantity,
                    price=price,
                    trade_value=gross,
                    commission=commission,
                    stamp_duty=stamp_duty,
                    total_fee=total_fee,
                    executed_at=datetime.utcnow(),
                    price_source="internal_dispatcher",
                )
            )
            try:
                await db.commit()
            except Exception:  # noqa: BLE001
                await db.rollback()
                logger.error(
                    "[Shadow/Sim] 成交台账落库失败（账户已生效）: %s %s",
                    symbol,
                    side_raw,
                    exc_info=True,
                )
            logger.info(
                "[Shadow/Sim] 虚拟成交完成: %s %s qty=%s fee=%.2f",
                symbol,
                side_raw,
                quantity,
                total_fee,
            )
            # 双轨镜像：虚拟成交已生效，按开关/白名单/限额向大 QMT 补一笔真单。
            # mirror_virtual_fill 自身吞掉全部异常，不影响上面的虚拟账本。
            await mirror_virtual_fill(
                db=db,
                redis=redis,
                tenant_id=tenant,
                user_id=str(uid),
                symbol=symbol,
                side=side_raw,
                quantity=quantity,
                price=price,
                client_order_id=client_order_id or "",
                strategy_id=strategy_id_raw,
                source=f"internal_dispatcher:{trading_mode.value}",
            )
            return {
                "status": "success",
                "execution": "virtual",
                "order_id": str(sim_order.order_id),
                "detail": result,
            }
        except Exception as exc:
            logger.error("[Shadow/Sim] 虚拟成交失败: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc))

    try:
        strategy_id = order_data.get("strategy_id")
        strategy_id_str = str(strategy_id or "").strip()
        strategy_id_val = int(strategy_id_str) if strategy_id_str.isdigit() else None
        portfolio_id = int(order_data.get("portfolio_id") or 0)
        if portfolio_id <= 0:
            snapshot = await _fetch_active_portfolio_snapshot(
                db,
                tenant_id=tenant,
                user_id=str(uid),
                strategy_id=str(strategy_id or ""),
            )
            if snapshot:
                portfolio_id = int(snapshot.get("portfolio_id") or 0)

        if portfolio_id <= 0:
            stmt = (
                select(Portfolio.id)
                .where(
                    and_(
                        Portfolio.tenant_id == tenant,
                        Portfolio.user_id == uid,
                        Portfolio.status == "active",
                    )
                )
                .order_by(Portfolio.updated_at.desc())
                .limit(1)
            )
            result = await db.execute(stmt)
            portfolio_id = int(result.scalar_one_or_none() or 0)

        if portfolio_id <= 0:
            raise HTTPException(status_code=400, detail="no active portfolio available")

        try:
            order_type = OrderType(order_type_raw)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid order_type: {order_type_raw}")
        try:
            position_side = PositionSide(position_side_raw)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid position_side: {position_side_raw}")
        trade_action = None
        if trade_action_raw:
            try:
                trade_action = TradeAction(trade_action_raw)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"invalid trade_action: {trade_action_raw}")

        order_service = OrderService(db, redis)
        engine = TradingEngine(db, redis)

        if client_order_id:
            existed_stmt = (
                select(Order)
                .where(
                    and_(
                        Order.tenant_id == tenant,
                        Order.user_id == str(uid),
                        Order.client_order_id == client_order_id,
                    )
                )
                .limit(1)
            )
            existed_result = await db.execute(existed_stmt)
            existed_order = existed_result.scalar_one_or_none()
            if existed_order is not None:
                return {
                    "status": "success",
                    "execution": "duplicate_skipped",
                    "order_id": str(existed_order.order_id),
                    "result": {
                        "success": True,
                        "message": "duplicate client_order_id skipped",
                        "client_order_id": client_order_id,
                    },
                }

        order = await order_service.create_order(
            user_id=uid,
            tenant_id=tenant,
            order_data=OrderCreate(
                portfolio_id=portfolio_id,
                strategy_id=strategy_id_val,
                symbol=symbol,
                symbol_name=order_data.get("symbol_name"),
                side=OrderSide(side_raw),
                order_type=order_type,
                quantity=quantity,
                price=price if price > 0 else None,
                trade_action=trade_action,
                position_side=position_side,
                is_margin_trade=is_margin_trade,
                trading_mode=trading_mode,
                client_order_id=client_order_id,
                remarks=remarks,
            ),
        )
    except IntegrityError:
        if not client_order_id:
            raise
        dup_stmt = (
            select(Order)
            .where(
                and_(
                    Order.tenant_id == tenant,
                    Order.user_id == str(uid),
                    Order.client_order_id == client_order_id,
                )
            )
            .limit(1)
        )
        dup_result = await db.execute(dup_stmt)
        dup_order = dup_result.scalar_one_or_none()
        if dup_order is None:
            raise
        return {
            "status": "success",
            "execution": "duplicate_skipped",
            "order_id": str(dup_order.order_id),
            "result": {
                "success": True,
                "message": "duplicate client_order_id skipped",
                "client_order_id": client_order_id,
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Internal order dispatch failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    risk_result = await engine.check_order_risk(uid, order)
    if not risk_result.get("passed"):
        await order_service.transition_order_status(
            order,
            OrderStatus.REJECTED,
            remarks=f"Risk check failed: {risk_result.get('violations')}",
        )
        return {
            "status": "rejected",
            "execution": "risk_blocked",
            "order_id": str(order.order_id),
            "violations": risk_result.get("violations", []),
        }

    submit_result = await engine.submit_order(order, tenant_id=tenant)
    return {
        "status": "success" if submit_result.get("success") else "failed",
        "execution": "direct",
        "order_id": str(order.order_id),
        "result": submit_result,
    }
