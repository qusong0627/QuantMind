"""
Simulation order service.
"""

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import String, and_, cast, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.simulation.models.order import OrderStatus, SimOrder
from backend.services.simulation.schemas.order import SimOrderCreate

logger = logging.getLogger(__name__)


class DuplicateSimOrderError(Exception):
    """幂等键命中已有台账单（T-P2-08）：唯一索引竞态兜底。

    各调用方应转既有 duplicate 语义（success+duplicate / duplicate_skipped），
    不得向用户暴露为 500。
    """

    def __init__(self, client_order_id: str, existing: SimOrder):
        super().__init__(f"duplicate client_order_id skipped: {client_order_id}")
        self.client_order_id = client_order_id
        self.existing = existing


class SimOrderService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_sim_order_by_client_order_id(
        self, tenant_id: str, user_id: str, client_order_id: str
    ) -> SimOrder | None:
        """按幂等键查**台账本体**（sim_orders）——投影可能为空，本体才是权威（T-P2-08）。

        user_id 形与 create_order 同规则（数字转 int），保证与写入行可比。
        """
        cid = str(client_order_id or "").strip()
        if not cid:
            return None
        uid = int(user_id) if str(user_id).isdigit() else user_id
        try:
            result = await self.db.execute(
                select(SimOrder)
                .where(
                    and_(
                        SimOrder.tenant_id == tenant_id,
                        SimOrder.user_id == uid,
                        SimOrder.client_order_id == cid,
                    )
                )
                .order_by(SimOrder.id.desc())
                .limit(1)
            )
            return result.scalars().first()
        except Exception as exc:  # noqa: BLE001 - 探测失败不阻断（走旧语义）
            logger.warning("sim_orders 幂等键反查失败 cid=%s: %s", cid, exc)
            return None

    async def create_order(
        self, tenant_id: str, user_id: str, data: SimOrderCreate, **kwargs
    ) -> SimOrder:
        order_value = data.quantity * (data.price or 0)
        try:
            from backend.shared.stock_utils import StockCodeUtil

            symbol = StockCodeUtil.to_prefix(data.symbol) or data.symbol.upper()
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
        # T-P1-03：client_order_id 同时落 sim_orders 台账（投影之外的第二份幂等凭据；
        # 此前注释自述"只写投影"，投影表为空时幂等实际断链）；source 取 trigger_source
        from backend.shared.order_contract import (
            SOURCE_MANUAL,
            ensure_order_contract_columns_async,
            normalize_agent,
        )

        client_order_id = str(data.client_order_id or "").strip() or None
        trigger_source = str(kwargs.get("trigger_source") or "").strip()
        order.client_order_id = client_order_id
        order.source = (trigger_source or SOURCE_MANUAL)[:32]
        # P2.7 分账归属：模拟台账是意图的源头，镜像真单从这一列继承。非 LLM 腿为空。
        order.agent = normalize_agent(data.agent) or None
        await ensure_order_contract_columns_async()
        # T-P2-08：幂等键唯一索引（部分索引，cid 非空行）；未启用（存量重复/失败）则旧语义
        from backend.shared.order_contract import ensure_sim_order_unique_index_async

        await ensure_sim_order_unique_index_async()
        self.db.add(order)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            # 唯一索引竞态兜底：同 (tenant,user,cid) 并发双提交/重放 → 转 duplicate 语义
            existing = None
            if client_order_id:
                existing = await self.get_sim_order_by_client_order_id(
                    tenant_id, str(user_id), client_order_id
                )
            if existing is not None:
                logger.info(
                    "sim_orders 幂等键命中（T-P2-08）：cid=%s 已有单 %s，返回 duplicate 语义",
                    client_order_id,
                    getattr(existing, "order_id", "?"),
                )
                raise DuplicateSimOrderError(client_order_id, existing) from exc
            raise
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
            conditions.append(SimOrder.status == status)
        if symbol:
            conditions.append(SimOrder.symbol == symbol.upper())
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
        # V2 投影镜像：否则 worker 扫描仍视该单为 pending，下个会话重放已撤单
        await self.sync_order_projection(order)
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
            from backend.services.simulation.services.market_rules import (
                infer_market,
            )
            from backend.shared.simulation_account_keys import ledger_account_id

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
                        # 市场化账户：账户 id 经唯一实现（市场按标的推断；此前为第三种手写格式）
                        account_id=ledger_account_id(
                            getattr(order, "tenant_id", "default"),
                            getattr(order, "user_id", ""),
                            infer_market(str(getattr(order, "symbol", ""))),
                        ),
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
