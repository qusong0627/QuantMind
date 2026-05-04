"""
Position Service - 持仓业务逻辑
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade.models.enums import PositionSide, TradeAction
from backend.services.trade.portfolio.config import settings
from backend.services.trade.portfolio.models import Portfolio, Position, PositionHistory
from backend.services.trade.portfolio.schemas import (
    PositionAdjust,
    PositionCreate,
    TradeSync,
)
from backend.services.trade.portfolio.utils import cache, get_cache_key

logger = logging.getLogger(__name__)


class PositionService:
    """持仓服务"""

    @staticmethod
    async def create_position(db: AsyncSession, portfolio_id: int, data: PositionCreate) -> Position:
        """创建持仓（开仓）"""
        # 检查组合是否存在
        stmt = select(Portfolio).where(Portfolio.id == portfolio_id)
        result = await db.execute(stmt)
        portfolio = result.scalar_one_or_none()
        if not portfolio:
            raise ValueError("投资组合不存在")

        # 检查是否已有该持仓
        stmt = select(Position).where(
            and_(
                Position.portfolio_id == portfolio_id,
                Position.symbol == data.symbol,
                Position.status == "holding",
            )
        )
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing:
            raise ValueError("该证券已有持仓，请使用调整接口")

        # 检查持仓数量限制
        stmt = select(func.count(Position.id)).where(
            and_(Position.portfolio_id == portfolio_id, Position.status == "holding")
        )
        result = await db.execute(stmt)
        position_count = result.scalar()

        if position_count >= settings.MAX_POSITIONS_PER_PORTFOLIO:
            raise ValueError(f"超过最大持仓数量限制: {settings.MAX_POSITIONS_PER_PORTFOLIO}")

        # 计算成本
        total_cost = data.quantity * data.price

        # 检查可用资金
        if portfolio.available_cash < total_cost:
            raise ValueError("可用资金不足")

        # 创建持仓
        position = Position(
            portfolio_id=portfolio_id,
            symbol=data.symbol,
            symbol_name=data.symbol_name,
            exchange=data.exchange,
            quantity=data.quantity,
            available_quantity=data.quantity,
            avg_cost=data.price,
            total_cost=total_cost,
            current_price=data.price,
            market_value=total_cost,
            status="holding",
        )

        db.add(position)
        await db.flush()
        await db.refresh(position)

        # 更新组合资金
        portfolio.available_cash -= total_cost
        portfolio.current_capital = portfolio.available_cash + total_cost

        # 创建持仓历史
        history = PositionHistory(
            position_id=position.id,
            action="open",
            quantity_change=data.quantity,
            price=data.price,
            amount=total_cost,
            quantity_after=data.quantity,
            avg_cost_after=data.price,
            note="开仓",
        )
        db.add(history)

        await db.flush()

        logger.info(f"Created position {position.id} for portfolio {portfolio_id}")
        return position

    @staticmethod
    async def get_position(
        db: AsyncSession,
        position_id: int,
        user_id: int | None = None,
        tenant_id: str | None = None,
    ) -> Position | None:
        """查询持仓，支持按用户作用域过滤"""
        use_cache = user_id is None and tenant_id is None
        cache_key = get_cache_key("position", position_id)
        if use_cache:
            cached = await cache.get(cache_key)
            if cached:
                # Reconstruction from cache dict
                return Position(**{k: v for k, v in cached.items() if k != "_sa_instance_state"})

        stmt = select(Position).where(Position.id == position_id)
        if user_id is not None or tenant_id is not None:
            stmt = stmt.join(Portfolio)
        if tenant_id is not None:
            stmt = stmt.where(Portfolio.tenant_id == tenant_id)
        if user_id is not None:
            stmt = stmt.where(Portfolio.user_id == user_id)
        result = await db.execute(stmt)
        position = result.scalar_one_or_none()

        if position and use_cache:
            p_dict = {k: v for k, v in position.__dict__.items() if k != "_sa_instance_state"}
            await cache.set(cache_key, p_dict, settings.CACHE_TTL_POSITION)

        return position

    @staticmethod
    async def list_positions(db: AsyncSession, portfolio_id: int, status: str | None = None) -> list[Position]:
        """查询持仓列表"""
        stmt = select(Position).where(Position.portfolio_id == portfolio_id)

        if status:
            stmt = stmt.where(Position.status == status)
        else:
            stmt = stmt.where(Position.status == "holding")

        stmt = stmt.order_by(Position.opened_at.desc())

        result = await db.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def update_position_price(
        db: AsyncSession,
        position_id: int,
        current_price: Decimal,
        user_id: int | None = None,
        tenant_id: str | None = None,
    ) -> Position:
        """更新持仓价格（用于行情更新）"""
        position = await PositionService.get_position(db, position_id, user_id=user_id, tenant_id=tenant_id)
        if not position:
            raise ValueError("持仓不存在")

        # 更新价格和市值
        position.current_price = current_price
        position.market_value = position.quantity * current_price

        # 计算浮动盈亏
        position.unrealized_pnl = position.market_value - position.total_cost
        if position.total_cost > 0:
            position.unrealized_pnl_rate = position.unrealized_pnl / position.total_cost

        position.updated_at = datetime.now()
        await db.flush()

        # 清除缓存
        cache_key = get_cache_key("position", position_id)
        await cache.delete(cache_key)

        return position

    @staticmethod
    async def adjust_position(
        db: AsyncSession,
        position_id: int,
        data: PositionAdjust,
        user_id: int | None = None,
        tenant_id: str | None = None,
    ) -> Position:
        """调整持仓（加仓/减仓）"""
        position = await PositionService.get_position(db, position_id, user_id=user_id, tenant_id=tenant_id)
        if not position:
            raise ValueError("持仓不存在")

        if position.status != "holding":
            raise ValueError("持仓状态不允许调整")

        # 获取组合
        stmt = select(Portfolio).where(Portfolio.id == position.portfolio_id)
        result = await db.execute(stmt)
        portfolio = result.scalar_one_or_none()
        if not portfolio:
            raise ValueError("投资组合不存在")

        amount = data.quantity * data.price

        if data.action == "add":
            # 加仓
            if portfolio.available_cash < amount:
                raise ValueError("可用资金不足")

            # 更新持仓
            position.total_cost
            position.quantity

            position.quantity += data.quantity
            position.available_quantity += data.quantity
            position.total_cost += amount
            position.avg_cost = position.total_cost / position.quantity
            position.market_value = position.quantity * position.current_price

            # 更新组合资金
            portfolio.available_cash -= amount

            # 创建历史
            history = PositionHistory(
                position_id=position.id,
                action="add",
                quantity_change=data.quantity,
                price=data.price,
                amount=amount,
                quantity_after=position.quantity,
                avg_cost_after=position.avg_cost,
                note=data.note or "加仓",
            )
            db.add(history)

        elif data.action == "reduce":
            # 减仓
            if position.available_quantity < data.quantity:
                raise ValueError("可用数量不足")

            # 计算已实现盈亏
            sell_cost = position.avg_cost * data.quantity
            realized_pnl = amount - sell_cost

            # 更新持仓
            position.quantity -= data.quantity
            position.available_quantity -= data.quantity
            position.total_cost -= sell_cost
            position.realized_pnl += realized_pnl

            if position.quantity > 0:
                position.market_value = position.quantity * position.current_price
                position.unrealized_pnl = position.market_value - position.total_cost
            else:
                position.market_value = Decimal("0")
                position.unrealized_pnl = Decimal("0")
                position.status = "closed"
                position.closed_at = datetime.now()

            # 更新组合资金
            portfolio.available_cash += amount

            # 创建历史
            history = PositionHistory(
                position_id=position.id,
                action="reduce",
                quantity_change=-data.quantity,
                price=data.price,
                amount=amount,
                quantity_after=position.quantity,
                avg_cost_after=(position.avg_cost if position.quantity > 0 else Decimal("0")),
                note=data.note or f"减仓，已实现盈亏: {realized_pnl}",
            )
            db.add(history)

        else:
            raise ValueError(f"不支持的操作: {data.action}")

        position.updated_at = datetime.now()
        await db.flush()
        await db.refresh(position)

        # 清除缓存
        cache_key = get_cache_key("position", position_id)
        await cache.delete(cache_key)

        logger.info(f"Adjusted position {position_id}: {data.action} {data.quantity}")
        return position

    @staticmethod
    async def close_position(
        db: AsyncSession,
        position_id: int,
        price: Decimal,
        note: str | None = None,
        user_id: int | None = None,
        tenant_id: str | None = None,
    ) -> Position:
        """平仓"""
        position = await PositionService.get_position(db, position_id, user_id=user_id, tenant_id=tenant_id)
        if not position:
            raise ValueError("持仓不存在")

        if position.status != "holding":
            raise ValueError("持仓状态不允许平仓")

        if position.quantity <= 0:
            raise ValueError("持仓数量为0")

        # 平仓等同于全部减仓
        adjust_data = PositionAdjust(
            action="reduce",
            quantity=position.quantity,
            price=price,
            note=note or "平仓",
        )

        return await PositionService.adjust_position(db, position_id, adjust_data, user_id=user_id, tenant_id=tenant_id)

    @staticmethod
    async def get_position_history(
        db: AsyncSession,
        position_id: int,
        limit: int = 100,
        user_id: int | None = None,
        tenant_id: str | None = None,
    ) -> list[PositionHistory]:
        """查询持仓历史"""
        if user_id is not None:
            position = await PositionService.get_position(db, position_id, user_id=user_id, tenant_id=tenant_id)
            if not position:
                return []

        stmt = (
            select(PositionHistory)
            .where(PositionHistory.position_id == position_id)
            .order_by(PositionHistory.created_at.desc())
            .limit(limit)
        )

        result = await db.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def sync_trade_update(db: AsyncSession, data: TradeSync) -> Position:
        """根据成交信息同步持仓（供 trade service 内部调用）"""
        # 使用 with_for_update 锁定组合，确保资金更新原子性
        stmt = select(Portfolio).where(Portfolio.id == data.portfolio_id).with_for_update()
        result = await db.execute(stmt)
        portfolio = result.scalar_one_or_none()
        if not portfolio:
            raise ValueError("投资组合不存在")

        symbol = data.symbol.upper()
        price = Decimal(str(data.price))
        quantity = int(data.quantity)

        # 细化费用核算：优先使用总费用字段，若无则累加各项
        if hasattr(data, 'total_fee') and data.total_fee is not None:
            total_fees = Decimal(str(data.total_fee))
        else:
            commission = Decimal(str(data.commission or 0))
            stamp_duty = Decimal(str(getattr(data, 'stamp_duty', 0) or 0))
            transfer_fee = Decimal(str(getattr(data, 'transfer_fee', 0) or 0))
            total_fees = commission + stamp_duty + transfer_fee

        side = (data.side or "").lower()
        if side not in ("buy", "sell"):
            raise ValueError("side 必须是 buy 或 sell")

        position_side = data.position_side or PositionSide.LONG
        trade_action = data.trade_action

        # 使用 with_for_update 锁定持仓
        pos_stmt = select(Position).where(
            and_(
                Position.portfolio_id == data.portfolio_id,
                Position.symbol == symbol,
                Position.side == (position_side.value if isinstance(position_side, PositionSide) else position_side),
                Position.status == "holding",
            )
        ).with_for_update()
        pos_result = await db.execute(pos_stmt)
        position = pos_result.scalar_one_or_none()

        if trade_action == TradeAction.BUY_TO_OPEN:
            gross = price * quantity
            total_amount = gross + total_fees
            if Decimal(str(portfolio.available_cash)) < total_amount:
                raise ValueError("可用资金不足")

            if position is None:
                position = Position(
                    portfolio_id=data.portfolio_id,
                    symbol=symbol,
                    symbol_name=symbol,
                    exchange=None,
                    side=PositionSide.LONG,
                    quantity=quantity,
                    available_quantity=quantity,
                    avg_cost=price,
                    total_cost=total_amount,
                    current_price=price,
                    market_value=gross,
                    unrealized_pnl=-commission,
                    unrealized_pnl_rate=((-commission / total_amount) if total_amount > 0 else Decimal("0")),
                    realized_pnl=Decimal("0"),
                    status="holding",
                    opened_at=datetime.now(),
                )
                db.add(position)
                await db.flush()
            else:
                old_qty = int(position.quantity)
                new_qty = old_qty + quantity
                old_cost = Decimal(str(position.total_cost))
                new_cost = old_cost + total_amount
                position.quantity = new_qty
                position.available_quantity = int(position.available_quantity) + quantity
                position.total_cost = new_cost
                position.avg_cost = (new_cost / Decimal(str(new_qty))) if new_qty > 0 else Decimal("0")
                position.current_price = price
                position.market_value = Decimal(str(new_qty)) * price
                position.unrealized_pnl = position.market_value - new_cost
                position.unrealized_pnl_rate = (position.unrealized_pnl / new_cost) if new_cost > 0 else Decimal("0")
                position.updated_at = datetime.now()

            portfolio.available_cash = Decimal(str(portfolio.available_cash)) - total_amount

            history = PositionHistory(
                position_id=position.id,
                action="sync_buy",
                quantity_change=quantity,
                price=price,
                amount=total_amount,
                quantity_after=position.quantity,
                avg_cost_after=position.avg_cost,
                note=f"成交同步买入 trade_id={data.trade_id or ''}",
            )
            db.add(history)
        elif trade_action == TradeAction.SELL_TO_CLOSE:
            if position is None or int(position.available_quantity) < quantity:
                raise ValueError("持仓可卖数量不足")

            gross = price * quantity
            net_amount = gross - commission
            avg_cost = Decimal(str(position.avg_cost))
            cost_reduced = avg_cost * quantity
            realized = net_amount - cost_reduced

            position.quantity = int(position.quantity) - quantity
            position.available_quantity = int(position.available_quantity) - quantity
            position.total_cost = Decimal(str(position.total_cost)) - cost_reduced
            position.realized_pnl = Decimal(str(position.realized_pnl)) + realized
            position.current_price = price
            if int(position.quantity) > 0:
                position.market_value = Decimal(str(position.quantity)) * price
                position.unrealized_pnl = position.market_value - Decimal(str(position.total_cost))
                position.unrealized_pnl_rate = (
                    (position.unrealized_pnl / Decimal(str(position.total_cost)))
                    if Decimal(str(position.total_cost)) > 0
                    else Decimal("0")
                )
            else:
                position.market_value = Decimal("0")
                position.unrealized_pnl = Decimal("0")
                position.unrealized_pnl_rate = Decimal("0")
                position.status = "closed"
                position.closed_at = datetime.now()
            position.updated_at = datetime.now()

            portfolio.available_cash = Decimal(str(portfolio.available_cash)) + net_amount

            history = PositionHistory(
                position_id=position.id,
                action="sync_sell",
                quantity_change=-quantity,
                price=price,
                amount=net_amount,
                quantity_after=position.quantity,
                avg_cost_after=position.avg_cost,
                note=f"成交同步卖出 trade_id={data.trade_id or ''}",
            )
            db.add(history)
        elif trade_action == TradeAction.SELL_TO_OPEN:
            gross = price * quantity
            margin_required = gross * Decimal("0.5")
            borrow_fee = gross * Decimal("0.08") / Decimal("252")
            total_required = margin_required + borrow_fee + commission
            if Decimal(str(portfolio.available_cash)) < total_required:
                raise ValueError("可用保证金不足")

            if position is None:
                position = Position(
                    portfolio_id=data.portfolio_id,
                    symbol=symbol,
                    symbol_name=symbol,
                    exchange=None,
                    side=PositionSide.SHORT,
                    quantity=quantity,
                    available_quantity=quantity,
                    avg_cost=price,
                    total_cost=gross,
                    current_price=price,
                    market_value=gross,
                    unrealized_pnl=-(borrow_fee + commission),
                    unrealized_pnl_rate=((-(borrow_fee + commission) / gross) if gross > 0 else Decimal("0")),
                    realized_pnl=Decimal("0"),
                    borrow_fee=borrow_fee,
                    financing_fee=Decimal("0"),
                    margin_occupied=margin_required,
                    maintenance_margin_ratio=Decimal("1"),
                    status="holding",
                    opened_at=datetime.now(),
                )
                db.add(position)
                await db.flush()
            else:
                old_qty = int(position.quantity)
                new_qty = old_qty + quantity
                old_cost = Decimal(str(position.total_cost))
                new_cost = old_cost + gross
                position.quantity = new_qty
                position.available_quantity = int(position.available_quantity) + quantity
                position.total_cost = new_cost
                position.avg_cost = (new_cost / Decimal(str(new_qty))) if new_qty > 0 else Decimal("0")
                position.current_price = price
                position.market_value = Decimal(str(new_qty)) * price
                position.borrow_fee = Decimal(str(position.borrow_fee)) + borrow_fee
                position.margin_occupied = Decimal(str(position.margin_occupied)) + margin_required
                position.unrealized_pnl = (
                    (Decimal(str(position.avg_cost)) - price) * Decimal(str(new_qty))
                    - Decimal(str(position.borrow_fee))
                    - Decimal(str(commission))
                )
                position.unrealized_pnl_rate = (position.unrealized_pnl / new_cost) if new_cost > 0 else Decimal("0")
                position.updated_at = datetime.now()

            portfolio.available_cash = Decimal(str(portfolio.available_cash)) - total_required
            portfolio.liabilities = Decimal(str(portfolio.liabilities)) + gross
            portfolio.short_market_value = Decimal(str(portfolio.short_market_value)) + gross

            history = PositionHistory(
                position_id=position.id,
                action="sync_sell_to_open",
                quantity_change=quantity,
                price=price,
                amount=gross,
                quantity_after=position.quantity,
                avg_cost_after=position.avg_cost,
                note=f"成交同步卖空开仓 trade_id={data.trade_id or ''}",
            )
            db.add(history)
        elif trade_action == TradeAction.BUY_TO_CLOSE:
            if position is None or int(position.available_quantity) < quantity:
                raise ValueError("空头可平数量不足")

            gross = price * quantity
            borrow_fee = gross * Decimal("0.08") / Decimal("252")
            avg_cost = Decimal(str(position.avg_cost))
            realized = (avg_cost - price) * quantity - commission - borrow_fee
            margin_release = min(
                Decimal(str(position.margin_occupied)),
                avg_cost * quantity * Decimal("0.5"),
            )
            position.quantity = int(position.quantity) - quantity
            position.available_quantity = int(position.available_quantity) - quantity
            position.total_cost = Decimal(str(position.total_cost)) - (avg_cost * quantity)
            position.realized_pnl = Decimal(str(position.realized_pnl)) + realized
            position.borrow_fee = Decimal(str(position.borrow_fee)) + borrow_fee
            position.margin_occupied = Decimal(str(position.margin_occupied)) - margin_release
            position.current_price = price
            if int(position.quantity) > 0:
                position.market_value = Decimal(str(position.quantity)) * price
                position.unrealized_pnl = (Decimal(str(position.avg_cost)) - price) * Decimal(
                    str(position.quantity)
                ) - Decimal(str(position.borrow_fee))
                position.unrealized_pnl_rate = (
                    (position.unrealized_pnl / Decimal(str(position.total_cost)))
                    if Decimal(str(position.total_cost)) > 0
                    else Decimal("0")
                )
            else:
                position.market_value = Decimal("0")
                position.unrealized_pnl = Decimal("0")
                position.unrealized_pnl_rate = Decimal("0")
                position.status = "closed"
                position.closed_at = datetime.now()
            position.updated_at = datetime.now()

            portfolio.available_cash = (
                Decimal(str(portfolio.available_cash)) - gross - commission - borrow_fee + margin_release
            )
            portfolio.liabilities = max(
                Decimal("0"),
                Decimal(str(portfolio.liabilities)) - (avg_cost * quantity),
            )
            portfolio.short_market_value = max(
                Decimal("0"),
                Decimal(str(portfolio.short_market_value)) - (avg_cost * quantity),
            )

            history = PositionHistory(
                position_id=position.id,
                action="sync_buy_to_close",
                quantity_change=-quantity,
                price=price,
                amount=gross,
                quantity_after=position.quantity,
                avg_cost_after=position.avg_cost,
                note=f"成交同步买入平空 trade_id={data.trade_id or ''}",
            )
            db.add(history)
        else:
            raise ValueError(f"不支持的 trade_action: {trade_action}")

        # 轻量更新组合资金快照
        port_pos_stmt = select(Position).where(
            and_(Position.portfolio_id == data.portfolio_id, Position.status == "holding")
        )
        port_positions = (await db.execute(port_pos_stmt)).scalars().all()
        total_market_value = sum(
            (Decimal(str(p.market_value)) if p.side == PositionSide.LONG else -Decimal(str(p.market_value)))
            for p in port_positions
        )
        portfolio.current_capital = Decimal(str(portfolio.available_cash)) + total_market_value
        portfolio.total_value = portfolio.current_capital
        if Decimal(str(portfolio.liabilities or 0)) > 0:
            portfolio.maintenance_margin_ratio = portfolio.current_capital / Decimal(str(portfolio.liabilities))
            if portfolio.maintenance_margin_ratio <= Decimal("1.1"):
                portfolio.warning_level = "closeout"
            elif portfolio.maintenance_margin_ratio <= Decimal("1.3"):
                portfolio.warning_level = "warning"
            else:
                portfolio.warning_level = "normal"
        portfolio.updated_at = datetime.now()

        await db.flush()
        await db.refresh(position)

        await cache.delete(get_cache_key("position", position.id))
        await cache.delete(get_cache_key("portfolio", data.portfolio_id))

        logger.info(
            "Synced trade to position: portfolio=%s symbol=%s side=%s qty=%s",
            data.portfolio_id,
            symbol,
            side,
            quantity,
        )
        return position
