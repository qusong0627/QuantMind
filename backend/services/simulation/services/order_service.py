"""
Simulation order service.
"""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import String, and_, cast, false, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.simulation.models.order import OrderStatus, SimOrder
from backend.services.simulation.schemas.order import SimOrderCreate


def build_order_projection(order, *, trigger_source: str):
    """Build a SimulationOrderV2 row mirroring a SimOrder (caller adds+flushes).

    Single source of truth for hosted/risk/manual projection inserts so every
    execution path lands in the v2 ledger. `quantity` is ALWAYS the original
    ordered quantity (same semantics as SimOrder.quantity) — fill progress is
    tracked in SimOrder.filled_quantity + SimulationFill rows and must never
    be written back into this column (see execution_engine.apply_filled).
    """
    from backend.services.simulation.models.order_v2 import SimulationOrderV2

    status = getattr(order, "status", None)
    status_str = getattr(status, "value", status)
    tenant = str(getattr(order, "tenant_id", "default"))
    user = str(getattr(order, "user_id", ""))
    strategy_id = getattr(order, "strategy_id", None)
    side = getattr(order, "side", "")
    order_type = getattr(order, "order_type", "")
    position_side = getattr(order, "position_side", "long")
    return SimulationOrderV2(
        order_id=getattr(order, "order_id", None),
        tenant_id=tenant,
        user_id=user,
        strategy_id=str(strategy_id) if strategy_id is not None else None,
        account_id=f"sim:{tenant}:{user}",
        portfolio_id=int(getattr(order, "portfolio_id", 0) or 0),
        legacy_order_id=getattr(order, "id", None),
        symbol=str(getattr(order, "symbol", "")),
        side=str(getattr(side, "value", side)),
        position_side=str(getattr(position_side, "value", position_side) or "long"),
        trade_action=getattr(order, "trade_action", None),
        order_type=str(getattr(order_type, "value", order_type)),
        time_in_force="DAY",
        quantity=float(getattr(order, "quantity", 0) or 0.0),
        price=getattr(order, "price", None),
        trigger_source=trigger_source,
        status=str(status_str or "pending"),
    )


class SimOrderService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_order(
        self, tenant_id: str, user_id: str, data: SimOrderCreate, **kwargs
    ) -> SimOrder:
        order_value = data.quantity * (data.price or 0)
        try:
            from backend.shared.stock_utils import StockCodeUtil

            symbol = StockCodeUtil.normalize(data.symbol) or data.symbol.upper()
        except Exception:
            symbol = data.symbol.upper()
        order = SimOrder(
            tenant_id=tenant_id,
            user_id=int(user_id) if str(user_id).isdigit() else user_id,
            portfolio_id=data.portfolio_id or 0,
            strategy_id=data.strategy_id,
            symbol=symbol,
            side=data.side,
            order_type=data.order_type,
            quantity=data.quantity,
            price=data.price,
            order_value=order_value,
            remarks=data.remarks,
            status=OrderStatus.PENDING,
        )
        # client_order_id 不落 sim_orders 表，只写入 simulation_orders 投影。
        client_order_id = str(data.client_order_id or "").strip() or None
        self.db.add(order)
        await self.db.commit()
        await self.db.refresh(order)
        # V2链路会同步写simulation_orders投影；旧链路不需要，忽略trigger等kwargs
        try:
            projection = await self.sync_order_projection(
                order, client_order_id=client_order_id
            )
            if projection is not None:
                projection.time_in_force = str(
                    data.time_in_force or "DAY"
                ).upper()
                projection.expires_at = self._utc_naive(data.expires_at)
                projection.trade_action = data.trade_action
                projection.position_side = data.position_side or "long"
                await self.db.commit()
        except Exception:
            pass
        return order

    @staticmethod
    def _utc_naive(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    async def get_order(
        self, tenant_id: str, user_id: str, order_id: UUID
    ) -> SimOrder | None:
        result = await self.db.execute(
            select(SimOrder).where(
                and_(
                    SimOrder.tenant_id == tenant_id,
                    cast(SimOrder.user_id, String) == str(user_id),
                    SimOrder.order_id == order_id,
                )
            )
        )
        return result.scalar_one_or_none()

    async def list_orders(
        self,
        tenant_id: str,
        user_id: str,
        *,
        portfolio_id: int | None = None,
        status: str | None = None,
        symbol: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SimOrder]:
        conditions = [
            SimOrder.tenant_id == tenant_id,
            cast(SimOrder.user_id, String) == str(user_id),
        ]
        if portfolio_id is not None:
            conditions.append(SimOrder.portfolio_id == portfolio_id)
        if status:
            # 前端 getOrders 统一 toUpperCase 传大写（如 FILLED），PG 枚举标签为小写；
            # 此处归一化，与实盘 trading_orders 的 normalized_status 同口径。
            # 非法值匹配空（而非让 PG 非法枚举标签抛 500）。
            status_norm = str(status).strip().lower()
            try:
                conditions.append(SimOrder.status == OrderStatus(status_norm))
            except ValueError:
                conditions.append(false())
        if symbol:
            # 落库已收敛为后缀正典；老 prefix 行读兼容（suffix 优先、prefix 兜底）。
            from backend.shared.stock_utils import StockCodeUtil as _SCU

            _sfx = _SCU.normalize(symbol)
            _pfx = _SCU.to_prefix(symbol)
            if _sfx and _pfx and _sfx != _pfx:
                conditions.append(SimOrder.symbol.in_([_sfx, _pfx]))
            else:
                conditions.append(SimOrder.symbol == (_sfx or symbol.upper()))
        if start_date:
            conditions.append(SimOrder.created_at >= start_date)
        if end_date:
            conditions.append(SimOrder.created_at <= end_date)

        stmt = (
            select(SimOrder)
            .where(and_(*conditions))
            .order_by(SimOrder.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def cancel_order(
        self, order: SimOrder, reason: str | None = None
    ) -> SimOrder:
        if order.status in [
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        ]:
            raise ValueError(f"Cannot cancel order in status: {order.status.value}")
        order.status = OrderStatus.CANCELLED
        order.cancelled_at = datetime.now(timezone.utc)
        if reason:
            order.remarks = f"{order.remarks or ''} [Cancelled: {reason}]"
        await self.db.commit()
        await self.db.refresh(order)
        return order

    # ── V2投影链路兼容（margin/强平与pending worker调用） ──
    async def get_projection_order_by_client_order_id(
        self, *, tenant_id: str, user_id: int, client_order_id: str
    ):
        """按client_order_id幂等查单；无表/无记录返回None，不抛错。"""
        try:
            from backend.services.simulation.models.order_v2 import SimulationOrderV2

            result = await self.db.execute(
                select(SimulationOrderV2)
                .where(
                    SimulationOrderV2.tenant_id == str(tenant_id),
                    SimulationOrderV2.user_id == str(user_id),
                    SimulationOrderV2.client_order_id == str(client_order_id),
                )
                .order_by(SimulationOrderV2.id.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()
        except Exception:
            return None

    async def sync_order_projection(
        self,
        order,
        rejected_reason: str | None = None,
        client_order_id: str | None = None,
    ):
        """把旧SimOrder同步到simulation_orders投影；失败只记日志，保证主链路可用。"""
        try:
            import logging

            from backend.services.simulation.models.order_v2 import SimulationOrderV2

            status = getattr(order, "status", None)
            status_str = getattr(status, "value", status)
            resolved_client_order_id = (
                str(
                    client_order_id
                    or getattr(order, "client_order_id", None)
                    or ""
                ).strip()
                or None
            )
            stmt = (
                select(SimulationOrderV2)
                .where(SimulationOrderV2.order_id == getattr(order, "order_id", None))
                .limit(1)
                if getattr(order, "order_id", None) is not None
                else None
            )
            existing = None
            if stmt is not None:
                existing = (await self.db.execute(stmt)).scalar_one_or_none()
            if existing is None:
                try:
                    proj = SimulationOrderV2(
                        order_id=getattr(order, "order_id", None),
                        tenant_id=str(getattr(order, "tenant_id", "default")),
                        user_id=str(getattr(order, "user_id", "")),
                        account_id=f"sim:{getattr(order, 'tenant_id', 'default')}:{getattr(order, 'user_id', '')}",
                        symbol=str(getattr(order, "symbol", "")),
                        side=str(
                            getattr(
                                getattr(order, "side", ""),
                                "value",
                                getattr(order, "side", ""),
                            )
                        ),
                        order_type=str(
                            getattr(
                                getattr(order, "order_type", ""),
                                "value",
                                getattr(order, "order_type", ""),
                            )
                        ),
                        quantity=float(getattr(order, "quantity", 0) or 0),
                        price=getattr(order, "price", None),
                        status=str(status_str or "pending"),
                        rejected_reason=rejected_reason,
                        client_order_id=resolved_client_order_id,
                    )
                    self.db.add(proj)
                    await self.db.commit()
                    return proj
                except Exception as exc:
                    logging.getLogger(__name__).debug(
                        "sync_order_projection insert skipped: %s", exc
                    )
                    try:
                        await self.db.rollback()
                    except Exception:
                        pass
            else:
                existing.status = str(status_str or existing.status)
                if rejected_reason is not None:
                    existing.rejected_reason = rejected_reason
                if resolved_client_order_id and not existing.client_order_id:
                    existing.client_order_id = resolved_client_order_id
                await self.db.commit()
                return existing
        except Exception:
            return None
        return None

    async def queue_order(self, order, message: str = "", trading_session_date=None):
        """挂单排队兼容：更新投影状态为pending，不阻塞主流程。"""
        try:
            await self.sync_order_projection(
                order, rejected_reason=str(message or "")[:500]
            )
            if getattr(order, "order_id", None) is not None:
                from backend.services.simulation.models.order_v2 import (
                    SimulationOrderV2,
                )

                projection = (
                    await self.db.execute(
                        select(SimulationOrderV2)
                        .where(SimulationOrderV2.order_id == order.order_id)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if projection is not None:
                    projection.status = OrderStatus.PENDING.value
                    projection.trading_session_date = trading_session_date
                    if (
                        projection.expires_at is None
                        and trading_session_date is not None
                        and str(projection.time_in_force or "DAY").upper() == "DAY"
                    ):
                        from zoneinfo import ZoneInfo

                        from backend.services.simulation.services.market_rules import (
                            infer_market,
                        )

                        market = infer_market(str(projection.symbol or "")).value
                        timezone_name = {
                            "CN": "Asia/Shanghai",
                            "HK": "Asia/Hong_Kong",
                            "US": "America/New_York",
                        }.get(market, "Asia/Shanghai")
                        close_hour = 16 if market in {"HK", "US"} else 15
                        local_deadline = datetime.combine(
                            trading_session_date,
                            datetime.min.time(),
                            tzinfo=ZoneInfo(timezone_name),
                        ).replace(hour=close_hour)
                        projection.expires_at = local_deadline.astimezone(
                            timezone.utc
                        ).replace(tzinfo=None)
                    await self.db.commit()
        except Exception:
            pass

    def _build_runtime_order(self, projection_order, remarks: str | None = None):
        """V2投影转旧SimOrder运行时对象（内存态，供worker复用execute_order）。"""
        order = SimOrder(
            tenant_id=getattr(projection_order, "tenant_id", "default"),
            user_id=int(getattr(projection_order, "user_id", 0) or 0),
            portfolio_id=int(getattr(projection_order, "portfolio_id", 0) or 0),
            symbol=str(getattr(projection_order, "symbol", "")),
            side=getattr(projection_order, "side", "buy"),
            order_type=getattr(projection_order, "order_type", "market"),
            quantity=float(getattr(projection_order, "quantity", 0) or 0),
            price=getattr(projection_order, "price", None),
            remarks=remarks,
            status=OrderStatus.PENDING,
        )
        try:
            order.order_id = getattr(projection_order, "order_id", order.order_id)
        except Exception:
            pass
        return order
