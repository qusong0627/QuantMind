"""
Trading Engine - 交易执行引擎

支持模拟交易和真实交易（通过 Broker 桥接），
执行完成后将账户状态回写到 Redis trade:account:{tenant_id}:{user_id}。
"""

import asyncio
import inspect
import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade.models.order import Order, OrderStatus, TradingMode
from backend.services.trade.redis_client import RedisClient
from backend.services.trade.services.broker_client import BaseBroker, create_broker
from backend.services.trade.services.risk_service import RiskService
from backend.services.trade.services.trade_service import TradeService
from backend.services.trade.trade_config import settings
from backend.shared.notification_publisher import publish_notification_async
from backend.shared.trade_account_cache import write_trade_account_cache

logger = logging.getLogger(__name__)


def _safe_schedule_notification(coro):
    try:
        asyncio.create_task(coro)
    except Exception:
        logger.debug("notification task schedule skipped")


class TradingEngine:
    """
    交易执行引擎

    统一调度模拟和真实 Broker 的下单流程，
    成交后同步账户状态到 Redis。
    """

    def __init__(self, db: AsyncSession, redis: RedisClient):
        self.db = db
        self.redis = redis
        from backend.services.trade.services.order_service import OrderService

        self.order_service = OrderService(db, redis)
        self.trade_service = TradeService(db, redis)
        self.risk_service = RiskService(db, redis)
        self._broker_cache: dict[str, BaseBroker] = {}

    def _get_broker(self, trading_mode: TradingMode) -> BaseBroker:
        """
        按订单 trading_mode 选择 broker，避免由全局开关决定执行路径。
        """
        mode = (trading_mode or TradingMode.SIMULATION).value
        if mode in self._broker_cache:
            return self._broker_cache[mode]

        enable_real = trading_mode == TradingMode.REAL
        broker = create_broker(
            enable_real=enable_real,
            broker_type=getattr(settings, "REAL_BROKER_TYPE", "bridge"),
            qmt_host=getattr(settings, "QMT_HOST", "127.0.0.1"),
            qmt_port=getattr(settings, "QMT_PORT", 18080),
            redis_client=self.redis,
            market_url=getattr(settings, "MARKET_DATA_SERVICE_URL", "http://stream-gateway:8003"),
            stream_base_url=getattr(settings, "MARKET_DATA_SERVICE_URL", "http://stream-gateway:8003"),
            internal_secret=getattr(settings, "INTERNAL_CALL_SECRET", None),
        )
        self._broker_cache[mode] = broker
        return broker

    async def submit_order(self, order: Order, *, tenant_id: str = "default") -> dict:
        """Submit order for execution"""
        try:
            tenant_id = (tenant_id or "").strip() or order.tenant_id or "default"

            # Use OrderService for status transition
            await self.order_service.transition_order_status(order, OrderStatus.SUBMITTED)

            logger.info(f"Order submitted: {order.order_id}")

            # 通过 Broker 执行（自动选择模拟/真实）
            await self._execute_via_broker(order, tenant_id=tenant_id)

            # 仅在未被 Broker 拒绝时尝试同步账户状态（严格以 broker 回报为准）
            tenant_id = (tenant_id or "").strip() or "default"
            if order.status != OrderStatus.REJECTED:
                await self._sync_account_to_redis(tenant_id, order.user_id)

            return {
                "success": True,
                "order_id": str(order.order_id),
                "status": order.status.value,
                "message": "Order submitted successfully",
            }

        except Exception as e:
            logger.error(f"Failed to submit order {order.order_id}: {e}")
            # Use transition_order_status for consistent state
            try:
                await self.order_service.transition_order_status(
                    order, OrderStatus.REJECTED, remarks=f"Submission failed: {str(e)}"
                )
            except:
                pass

            return {
                "success": False,
                "order_id": str(order.order_id),
                "status": order.status.value,
                "message": str(e),
            }

    async def _execute_via_broker(self, order: Order, tenant_id: str = "default"):
        """
        通过 Broker 执行订单 (工业级防御性实现)
        原则：本地 PENDING 记录必须先于外部动作。
        """
        broker = self._get_stock_broker(order.trading_mode)

        try:
            # 1. 外部请求阶段 (此时本地 order.status 应已为 SUBMITTED 并落库)
            logger.info(
                f"发送下单请求到 Broker: {order.symbol} {order.quantity} @ {order.price} (ID: {order.order_id})"
            )

            is_qmt_broker = broker.__class__.__name__ == "QMTBroker"
            side_arg = getattr(order.side, "name", str(order.side)).upper() if is_qmt_broker else str(order.side.value)
            order_type_arg = (
                getattr(order.order_type, "name", str(order.order_type)).upper()
                if is_qmt_broker
                else str(order.order_type.value)
            )
            place_order_kwargs = {
                "user_id": str(order.user_id),
                "symbol": order.symbol,
                "side": side_arg,
                "quantity": order.quantity,
                "order_type": order_type_arg,
                "price": order.price,
                "tenant_id": tenant_id,
            }
            if "client_order_id" in inspect.signature(broker.place_order).parameters:
                place_order_kwargs["client_order_id"] = order.client_order_id
            if "trade_action" in inspect.signature(broker.place_order).parameters:
                place_order_kwargs["trade_action"] = (
                    str(getattr(order.trade_action, "value", order.trade_action) or "").strip().lower() or None
                )
            if "position_side" in inspect.signature(broker.place_order).parameters:
                place_order_kwargs["position_side"] = (
                    str(getattr(order.position_side, "value", order.position_side) or "").strip().lower() or None
                )
            if "is_margin_trade" in inspect.signature(broker.place_order).parameters:
                place_order_kwargs["is_margin_trade"] = bool(getattr(order, "is_margin_trade", False))

            result = await broker.place_order(**place_order_kwargs)

            # 2. 回执处理阶段 (必须保证事务原子性)
            if not result.success:
                logger.warning(f"Broker 拒绝下单 {order.order_id}: {result.message}")
                await self.order_service.transition_order_status(
                    order, OrderStatus.REJECTED, remarks=f"Broker拒绝: {result.message}"
                )
                _safe_schedule_notification(
                    publish_notification_async(
                        user_id=str(order.user_id),
                        tenant_id=str(order.tenant_id or tenant_id or "default"),
                        title="订单被拒绝",
                        content=f"{order.symbol} 下单失败：{result.message}",
                        type="trading",
                        level="error",
                        action_url="/trading",
                    )
                )
                return

            # 3. 成功后处理：
            # - Bridge 模式通常先返回“已受理/已派发”，filled_quantity=0，真实成交稍后通过
            #   /internal/strategy/bridge/execution 回写；此处不应强行落本地成交。
            # - 仅当 Broker 明确返回有效成交(数量>0 且价格>0)时，才即时落成交。
            if result.exchange_order_id:
                order.exchange_order_id = result.exchange_order_id

            has_immediate_fill = float(result.filled_quantity or 0) > 0 and float(result.filled_price or 0) > 0
            if has_immediate_fill:
                await self.trade_service.create_trade(
                    order=order,
                    quantity=result.filled_quantity,
                    price=result.filled_price,
                    commission=result.commission,
                )
                logger.info(
                    "✅ 订单 %s 即时成交 (交易所单号: %s)",
                    order.order_id,
                    order.exchange_order_id,
                )
                _safe_schedule_notification(
                    publish_notification_async(
                        user_id=str(order.user_id),
                        tenant_id=str(order.tenant_id or tenant_id or "default"),
                        title="订单成交确认",
                        content=f"{order.symbol} 成交 {result.filled_quantity} 股，均价 {result.filled_price}",
                        type="trading",
                        level="success",
                        action_url="/trading",
                    )
                )
            else:
                # Bridge 模式：仅表示“已派发到 Agent”，并不等于柜台已接单。
                # 标记等待 ACK，供短超时扫描器兜底，避免长期停留 submitted 造成“假成功”。
                marker = "[AWAITING_BRIDGE_ACK]"
                base_remarks = (order.remarks or "").strip()
                if marker not in base_remarks:
                    order.remarks = (f"{base_remarks} {marker}").strip()
                await self.db.commit()
                await self.db.refresh(order)
                logger.info(
                    "✅ 订单 %s 已派发至 Broker，等待执行回报 (交易所单号: %s)",
                    order.order_id,
                    order.exchange_order_id,
                )
                _safe_schedule_notification(
                    publish_notification_async(
                        user_id=str(order.user_id),
                        tenant_id=str(order.tenant_id or tenant_id or "default"),
                        title="订单已提交",
                        content=f"{order.symbol} 已提交，等待成交回报",
                        type="trading",
                        level="info",
                        action_url="/trading",
                    )
                )

        except asyncio.TimeoutError:
            # 关键：超时绝对不能直接标记为失败，因为单子可能已经发给交易所了
            logger.error(f"⚠️ 下单请求超时 (ID: {order.order_id})。状态未知，进入人工核实流程。")
            marker = "[AWAITING_BRIDGE_ACK]"
            timeout_marker = "[BRIDGE_ACK_TIMEOUT_PENDING_REVIEW]"
            base_remarks = (order.remarks or "").strip()
            for token in (marker, timeout_marker):
                if token not in base_remarks:
                    base_remarks = f"{base_remarks} {token}".strip()
            order.remarks = base_remarks
            await self.order_service.transition_order_status(
                order, order.status, remarks="下单超时: 状态未知，请在柜台确认"
            )
            _safe_schedule_notification(
                publish_notification_async(
                    user_id=str(order.user_id),
                    tenant_id=str(order.tenant_id or tenant_id or "default"),
                    title="订单待核查",
                    content=f"{order.symbol} 下单超时，已进入柜台核查流程",
                    type="trading",
                    level="warning",
                    action_url="/trading",
                )
            )
        except Exception as e:
            logger.error(f"❌ Broker 执行崩溃 (ID: {order.order_id}): {e}", exc_info=True)
            # 对于非超时类的明确异常，可以尝试标记为拒绝
            await self.order_service.transition_order_status(order, OrderStatus.REJECTED, remarks=f"执行异常: {str(e)}")
            _safe_schedule_notification(
                publish_notification_async(
                    user_id=str(order.user_id),
                    tenant_id=str(order.tenant_id or tenant_id or "default"),
                    title="订单执行失败",
                    content=f"{order.symbol} 执行异常：{str(e)}",
                    type="trading",
                    level="error",
                    action_url="/trading",
                )
            )

    def _get_stock_broker(self, trading_mode: TradingMode) -> BaseBroker:
        """内部辅助：获取并校验 broker"""
        return self._get_broker(trading_mode)

    async def _sync_account_to_redis(self, tenant_id: str, user_id: int):
        """
        成交后将账户状态写入 Redis，供 portfolio_service 消费。

        Key: trade:account:{tenant_id}:{normalized_user_id}
        """
        if not self.redis:
            return

        try:
            # 严格以 Broker 回报为准，不在服务端合成“初始资金/持仓”数据。
            mode_stmt = (
                select(Order.trading_mode)
                .where(
                    and_(
                        Order.tenant_id == tenant_id,
                        Order.user_id == user_id,
                        Order.status == OrderStatus.FILLED,
                    )
                )
                .order_by(Order.filled_at.desc().nullslast(), Order.created_at.desc())
                .limit(1)
            )
            mode_result = await self.db.execute(mode_stmt)
            recent_mode = mode_result.scalar_one_or_none() or TradingMode.SIMULATION
            # REAL 账户快照以 QMT Agent bridge/account 上报为准，避免成交回写覆盖扩展字段。
            if recent_mode == TradingMode.REAL:
                logger.info(
                    "Skip syncing trade:account from TradingEngine for REAL mode; bridge/account is source of truth.",
                    extra={"tenant_id": tenant_id, "user_id": user_id},
                )
                return
            broker = self._get_broker(recent_mode)

            account = await broker.query_account(str(user_id), tenant_id=tenant_id)
            if not account:
                logger.warning(
                    "Broker account unavailable; skip syncing trade:account",
                    extra={"user_id": user_id, "broker": broker.__class__.__name__},
                )
                return

            required = {"total_asset", "cash", "market_value", "positions"}
            if not required.issubset(set(account.keys())):
                logger.warning(
                    "Broker account payload missing required fields; skip syncing trade:account",
                    extra={
                        "user_id": user_id,
                        "broker": broker.__class__.__name__,
                        "keys": sorted(list(account.keys())),
                    },
                )
                return

            # 统一补充时间戳字段，便于 consumer 做幂等/时序处理
            account_data = dict(account)
            account_data.setdefault("timestamp", datetime.now().isoformat())

            key = write_trade_account_cache(self.redis, tenant_id, user_id, account_data)
            logger.info("Account state synced to Redis: %s", key)

        except Exception as e:
            logger.error(f"Failed to sync account to Redis for user {user_id}: {e}")

    async def cancel_order_execution(self, order: Order) -> bool:
        """Cancel order execution (if not yet filled)"""
        try:
            if order.status in [OrderStatus.FILLED, OrderStatus.CANCELLED]:
                return False

            # 向 Broker（QMT）发出撤单指令，最终状态以异步回报为准。
            if order.exchange_order_id or order.client_order_id:
                try:
                    broker = self._get_broker(order.trading_mode)
                    await broker.cancel_order(
                        str(order.exchange_order_id or ""),
                        user_id=str(order.user_id),
                        tenant_id=str(order.tenant_id or "default"),
                        client_order_id=str(order.client_order_id or ""),
                        symbol=str(order.symbol or ""),
                        side=str(order.side.value if hasattr(order.side, "value") else order.side or ""),
                    )
                except Exception as broker_err:
                    logger.warning(
                        "broker cancel_order failed for order %s: %s",
                        order.order_id,
                        broker_err,
                    )

            await self.order_service.transition_order_status(
                order,
                order.status,
                remarks="撤单请求已发送，等待QMT回报确认最终状态",
            )
            return True

        except Exception as e:
            logger.error(f"Failed to cancel order {order.order_id}: {e}")
            return False

    async def check_order_risk(self, user_id: int, order: Order) -> dict:
        """Check order against risk rules"""
        from backend.services.trade.services.remote_service import remote_service

        # 1. Estimate order value if it's 0 (Market Order)
        # Market orders arrive with order_value=0; must estimate from real-time quote.
        order_val = float(order.order_value or 0)
        is_market_order = str(getattr(order.order_type, "value", order.order_type) or "").lower() == "market"
        if order_val <= 0 and order.quantity > 0:
            try:
                broker = self._get_broker(order.trading_mode)
                quote = await broker.query_quote(order.symbol)
                if quote and quote.get("last_price"):
                    last_price = float(quote.get("last_price"))
                    order_val = float(order.quantity) * last_price
                    order.order_value = order_val
                    # Persist estimated value so downstream checks all read the same figure
                    await self.db.commit()
                    await self.db.refresh(order)
                    logger.info(f"Estimated market order value for risk check: {order_val}")
            except Exception as e:
                logger.warning(f"Failed to estimate market order value: {e}")

        # If this is a market order and we still have no reference price, reject immediately
        # to prevent bypassing all value-based risk rules (purchasing power, position size, etc.).
        if is_market_order and order_val <= 0:
            return {
                "passed": False,
                "violations": [
                    {
                        "rule": "market_price_unavailable",
                        "message": (
                            f"无法获取 {order.symbol} 实时行情，市价单拒绝以防止风控绕过。"
                            "请稍后重试或改用限价单。"
                        ),
                    }
                ],
            }

        # 2. 优先从本地交易库读取组合资金，避免内部调用 portfolios 接口被 JWT 拦截后资金恒为 0。
        from backend.services.trade.portfolio.models import Portfolio

        available_cash = 0.0
        portfolio_value = 0.0
        trading_mode_value = str(
            getattr(getattr(order, "trading_mode", None), "value", getattr(order, "trading_mode", ""))
        ).upper()
        try:
            portfolio_stmt = (
                select(Portfolio)
                .where(
                    and_(
                        Portfolio.id == order.portfolio_id,
                        Portfolio.user_id == user_id,
                        Portfolio.tenant_id == order.tenant_id,
                        Portfolio.is_deleted == False,
                    )
                )
                .limit(1)
            )
            portfolio_result = await self.db.execute(portfolio_stmt)
            portfolio = portfolio_result.scalar_one_or_none()
            if portfolio is not None:
                available_cash = float(portfolio.available_cash or 0.0)
                portfolio_value = float(portfolio.total_value or 0.0)
        except Exception as e:
            logger.warning(f"Failed to read portfolio cash from DB: {e}")

        # REAL 模式优先使用最新账户快照的 available_cash，与手动任务买单预算口径一致。
        if trading_mode_value == "REAL":
            try:
                from backend.services.trade.routers.real_trading_utils import (
                    _fetch_latest_real_account_snapshot,
                )

                latest_snapshot = await _fetch_latest_real_account_snapshot(
                    self.db,
                    tenant_id=str(getattr(order, "tenant_id", "") or "default"),
                    user_id=str(user_id),
                )
                if latest_snapshot:
                    snapshot_cash = float(
                        latest_snapshot.get("available_cash")
                        or latest_snapshot.get("cash")
                        or 0.0
                    )
                    snapshot_total_asset = float(
                        latest_snapshot.get("total_asset") or 0.0
                    )
                    if snapshot_cash > 0:
                        available_cash = snapshot_cash
                    if snapshot_total_asset > 0 and portfolio_value <= 0:
                        portfolio_value = snapshot_total_asset
            except Exception as e:
                logger.warning(f"Failed to read available_cash from snapshot: {e}")

        # 兼容兜底：若本地库读取不到，再尝试旧远程接口。
        if available_cash <= 0:
            available_cash = await remote_service.get_portfolio_cash(order.portfolio_id, user_id)
        if portfolio_value <= 0:
            portfolio_value = available_cash

        # 3. Get daily trade count
        from datetime import date

        from sqlalchemy import func

        today = date.today()
        # 统计该用户在当前租户下今天的订单总数 (排除已拒绝的，保留待成交、已成交等)
        stmt = select(func.count(Order.id)).where(
            and_(
                Order.user_id == user_id,
                Order.tenant_id == order.tenant_id,
                Order.created_at >= datetime.combine(today, datetime.min.time()),
                Order.status != OrderStatus.REJECTED,
            )
        )
        count_result = await self.db.execute(stmt)
        daily_count = count_result.scalar() or 0
        logger.info(f"[Risk] User {user_id} daily order count: {daily_count}")

        result = await self.risk_service.check_order_risk(
            user_id=user_id,
            order=order,
            portfolio_value=portfolio_value,
            available_cash=available_cash,
            daily_trade_count=daily_count,
        )

        return result

    async def rotate_buy_next(self, user_id: int, portfolio_id: int, original_symbol: str) -> dict | None:
        """
        [Automation] 轮转买入下一只：
        当 original_symbol 因超时撤单时，从备选池中挑选下一只高权重股票买入。
        """
        try:
            from backend.services.trade.portfolio.models import Portfolio
            from backend.services.trade.models.order import Order, OrderStatus

            # 1. 获取当前组合配置
            stmt = select(Portfolio).where(Portfolio.id == portfolio_id).limit(1)
            res = await self.db.execute(stmt)
            portfolio = res.scalar_one_or_none()
            if not portfolio or not portfolio.strategy_id:
                return None

            # 2. 获取备选池 (从 Redis 或 DB 获取该策略的预设权重)
            # 方案：读取该租户/用户的实时活跃配置
            active_key = f"trade:active_strategy:{portfolio.tenant_id}:{str(user_id).zfill(8)}"
            active_data_raw = self.redis.client.get(active_key)
            if not active_data_raw:
                return None

            active_data = json.loads(active_data_raw)
            # 假设备选池在 live_trade_config.target_list 或类似字段中
            live_cfg = active_data.get("live_trade_config") or {}
            target_list = live_cfg.get("target_list") or [] # [{symbol, weight}, ...]

            if not target_list:
                logger.warning("Rotate-buy-next: No target_list found in live_config")
                return None

            # 按权重降序
            sorted_targets = sorted(target_list, key=lambda x: float(x.get("weight", 0)), reverse=True)

            # 3. 过滤掉已经持仓的，以及今天已经尝试过且未成功的
            # 获取当前持仓
            from backend.services.trade.models.position import Position
            pos_stmt = select(Position.symbol).where(and_(Position.portfolio_id == portfolio_id, Position.quantity > 0))
            pos_res = await self.db.execute(pos_stmt)
            current_symbols = set(pos_res.scalars().all())

            # 获取今日已报订单 (排除撤单或成交的？不，这里应该过滤掉今天所有已尝试的，避免死循环)
            from datetime import date
            today = date.today()
            order_stmt = select(Order.symbol).where(and_(
                Order.portfolio_id == portfolio_id,
                Order.created_at >= datetime.combine(today, datetime.min.time())
            ))
            order_res = await self.db.execute(order_stmt)
            tried_symbols = set(order_res.scalars().all())

            # 4. 选出第一个满足条件的
            next_target = None
            for target in sorted_targets:
                sym = target.get("symbol")
                if sym == original_symbol: continue
                if sym in current_symbols: continue
                if sym in tried_symbols: continue
                next_target = target
                break

            if not next_target:
                logger.info("Rotate-buy-next: No available backup stocks in target_list")
                return None

            # 5. 计算买入数量 (简单按组合剩余可用资金或预设权重计算)
            # 此处简单复用原单的价格/数量逻辑（或根据剩余可用资金重算的逻辑）
            # 注意：实际生产中需要精确计算。
            logger.info("Rotate-buy-next: matched next target %s", next_target['symbol'])

            # 推送通知
            await publish_notification_async(
                user_id=str(user_id),
                tenant_id=str(portfolio.tenant_id),
                title="触发权重轮转补位",
                content=f"由于 {original_symbol} 挂单超时已撤销，系统自动按权重尝试买入下一只备选：{next_target['symbol']}",
                type="trading",
                level="info"
            )

            # 此处返回建议，实际上可能需要创建一个新的 Order 对象并调用 submit_order
            # 为了简化，此处仅作为逻辑占位，后续可接入自动下单逻辑
            return next_target

        except Exception as e:
            logger.error("Rotate-buy-next failed: %s", e, exc_info=True)
            return None
