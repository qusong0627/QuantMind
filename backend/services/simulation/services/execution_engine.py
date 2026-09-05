"""
Synthetic execution engine for simulation orders.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.simulation.models.order import (
    OrderStatus,
    OrderType,
    SimOrder,
)
from backend.services.simulation.models.trade import SimTrade
from backend.services.simulation.services.simulation_manager import (
    SimulationAccountManager,
)
from backend.services.trade_shared.trade_config import settings
from backend.shared.auth import get_internal_call_secret
from backend.shared.trade_account_cache import (
    write_json_cache,
    write_trade_account_cache,
)

logger = logging.getLogger(__name__)


class ExecutionResult:
    def __init__(
        self,
        *,
        success: bool,
        price: float = 0.0,
        quantity: float = 0.0,
        commission: float = 0.0,
        stamp_duty: float = 0.0,
        market: str = "CN",
        account_snapshot: dict | None = None,
        price_source: str | None = None,
        message: str = "",
    ):
        self.success = success
        self.price = price
        self.quantity = quantity
        self.commission = commission
        self.stamp_duty = stamp_duty
        self.market = market
        self.account_snapshot = account_snapshot
        self.price_source = price_source
        self.message = message


@dataclass
class MarketSnapshot:
    price: float
    price_source: str
    limit_up: bool = False
    limit_down: bool = False
    suspended: bool = False


class SimulationExecutionEngine:
    def __init__(self, db: AsyncSession, manager: SimulationAccountManager):
        self.db = db
        self.manager = manager
        self._http: httpx.AsyncClient | None = None

    async def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=5.0)
        return self._http

    @staticmethod
    def _as_float(value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            if value is None:
                return None
            return int(float(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if not text:
            return False
        return text in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _is_price_near(price: float, limit_price: float | None, tolerance: float = 0.0015) -> bool:
        if limit_price is None or limit_price <= 0 or price <= 0:
            return False
        return abs(price - limit_price) / max(limit_price, 1e-6) <= tolerance

    async def _latest_price(
        self,
        symbol: str,
        *,
        user_id: int | None = None,
        tenant_id: str | None = None,
    ) -> MarketSnapshot:
        market_url = settings.MARKET_DATA_SERVICE_URL.rstrip("/")
        endpoint = f"{market_url}/api/v1/quotes/{symbol}"

        # Level 1: 实时行情服务
        try:
            client = await self._http_client()
            headers = {"X-Internal-Call": get_internal_call_secret()}
            if user_id is not None:
                headers["X-User-Id"] = str(user_id)
                headers["X-Tenant-Id"] = str(tenant_id or "default")
            resp = await client.get(endpoint, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                px = self._as_float(data.get("current_price") or data.get("last_price"))
                if px and px > 0:
                    limit_up = self._as_bool(data.get("is_limit_up"))
                    limit_down = self._as_bool(data.get("is_limit_down"))
                    suspended = self._as_bool(data.get("suspended") or data.get("is_suspended"))
                    limit_up_price = self._as_float(data.get("limit_up_today"))
                    limit_down_price = self._as_float(data.get("limit_down_today"))
                    if not limit_up and self._is_price_near(px, limit_up_price):
                        limit_up = True
                    if not limit_down and self._is_price_near(px, limit_down_price):
                        limit_down = True

                    pre_close = self._as_float(data.get("pre_close") or data.get("close_price"))
                    ask1_volume = self._as_int(data.get("ask1_volume"))
                    bid1_volume = self._as_int(data.get("bid1_volume"))
                    if pre_close and pre_close > 0:
                        change_ratio = (px - pre_close) / pre_close
                        if not limit_up and ask1_volume is not None and ask1_volume <= 0 and change_ratio >= 0.095:
                            limit_up = True
                        if not limit_down and bid1_volume is not None and bid1_volume <= 0 and change_ratio <= -0.095:
                            limit_down = True

                    return MarketSnapshot(
                        price=px,
                        price_source="market_data_service",
                        limit_up=limit_up,
                        limit_down=limit_down,
                        suspended=suspended,
                    )
        except Exception as e:
            logger.warning("Failed to fetch market quote for %s: %s", symbol, e)

        # Level 2: 数据库兜底 (L2 Fallback) — stock_daily_latest
        try:
            from sqlalchemy import text
            from backend.shared.stock_utils import StockCodeUtil

            # stock_daily_latest 存 prefix 格式（SH600519），下单可能传 suffix（600519.SH）
            db_symbol = StockCodeUtil.to_prefix(symbol) or symbol

            query_with_limits = text(
                """
                SELECT close, adj_factor
                FROM stock_daily_latest
                WHERE symbol = :symbol
                ORDER BY trade_date DESC LIMIT 1
                """
            )
            try:
                result = await self.db.execute(query_with_limits, {"symbol": db_symbol})
                row = result.fetchone()
                if row:
                    hfq_close = float(row[0])
                    adj_factor = float(row[1] or 1.0)
                    price = hfq_close / adj_factor if adj_factor > 0 else hfq_close
                    logger.info("Fallback to DB nominal price for %s: %s", symbol, price)
                    return MarketSnapshot(
                        price=price,
                        price_source="db_fallback",
                    )
            except Exception:
                # 首次查询失败（如事务被污染），rollback 恢复后再用更简单的查询重试
                try:
                    await self.db.rollback()
                except Exception:
                    pass
                query_legacy = text(
                    """
                    SELECT close, adj_factor
                    FROM stock_daily_latest
                    WHERE symbol = :symbol
                    ORDER BY trade_date DESC LIMIT 1
                    """
                )
                legacy_result = await self.db.execute(query_legacy, {"symbol": db_symbol})
                legacy_row = legacy_result.fetchone()
                if legacy_row:
                    hfq_close = float(legacy_row[0])
                    adj_factor = float(legacy_row[1] or 1.0)
                    price = hfq_close / adj_factor if adj_factor > 0 else hfq_close
                    logger.info("Fallback to DB legacy nominal price for %s: %s", symbol, price)
                    return MarketSnapshot(price=price, price_source="db_fallback")
        except Exception as e:
            logger.error("Database fallback failed for %s: %s", symbol, e)

        # Level 2.5: 本地日线兜底（QuantDB parquet）— Redis 不可用时以开盘价撮合
        # 模拟盘核心兜底：直读本地不复权日线，用开盘价作为撮合价，不依赖实时流
        def _local_daily_snapshot() -> MarketSnapshot | None:
            from backend.services.simulation.services.local_market_data import get_local_market_data
            from backend.services.simulation.services.market_rules import infer_market
            from datetime import date as _date

            mkt = infer_market(symbol).value if symbol else "CN"
            lmd = get_local_market_data(market=mkt)
            # 优先当日，其次最近交易日
            for d in [ _date.today(), lmd.latest_trade_date() ]:
                if d is None:
                    continue
                bar = lmd.get_bar(symbol, d)
                if bar and bar.open > 0:
                    logger.info("Fallback to LocalMarketData open for %s %s: open=%s", symbol, d, bar.open)
                    return MarketSnapshot(price=float(bar.open), price_source="local_daily_open")
                if bar and bar.close > 0:
                    logger.info("Fallback to LocalMarketData close for %s %s: close=%s", symbol, d, bar.close)
                    return MarketSnapshot(price=float(bar.close), price_source="local_daily_close")
            return None

        try:
            # 直读分区文件是同步磁盘 IO，放线程里跑，避免阻塞事件循环
            local_snapshot = await asyncio.to_thread(_local_daily_snapshot)
            if local_snapshot is not None:
                return local_snapshot
        except Exception as e:
            logger.warning("LocalMarketData fallback failed for %s: %s", symbol, e)

        # Level 3: 无法获取行情 —— 不伪造随机价格，交由 execute_order 拒单，
        # 避免以虚假价格成交污染模拟盘资产/持仓。
        return MarketSnapshot(price=0.0, price_source="unavailable")

    async def execute_order(self, order: SimOrder, market: str | None = None) -> ExecutionResult:
        snapshot = await self._latest_price(
            order.symbol,
            user_id=order.user_id,
            tenant_id=order.tenant_id,
        )
        base_price = snapshot.price
        fetched_source = snapshot.price_source

        # 行情不可用（实时行情与 DB 兜底都失败时 price=0 / unavailable）：
        # 市价与限价单都无从定价，直接拒单，避免随机价格或空价格成交污染账户。
        if base_price <= 0 or fetched_source == "unavailable":
            return ExecutionResult(
                success=False,
                message=f"无法获取 {order.symbol} 实时行情，模拟单拒绝成交",
            )

        slippage = settings.SIMULATION_SLIPPAGE_BPS / 10000

        # 市场规则：由标的代码推断（信号/订单来自同一市场），佣金、
        # 印花税、T+1 语义均按市场区分。
        from backend.services.simulation.services.market_rules import (
            infer_market,
            rules_for,
        )

        rules = rules_for(market or infer_market(order.symbol))
        market_str = rules.market.value

        # 记录更新前的账户快照：供 apply_filled 落库失败时补偿恢复，
        # 保证 Redis 余额/持仓与 sim_orders/sim_trades 的数据一致性（T+1 语义下无法用反向增减安全回退）。
        account_snapshot = await self.manager.get_account(
            order.user_id, tenant_id=order.tenant_id, market=market_str
        )

        side = str(order.side.value).lower()
        if snapshot.suspended:
            return ExecutionResult(success=False, message="Security is suspended, cannot trade")
        if side == "buy" and snapshot.limit_up:
            return ExecutionResult(success=False, message="Limit-up locked, buy order cannot be filled")
        if side == "sell" and snapshot.limit_down:
            return ExecutionResult(success=False, message="Limit-down locked, sell order cannot be filled")

        if order.order_type == OrderType.MARKET:
            direction = 1 if side == "buy" else -1
            exec_price = round(base_price * (1 + direction * slippage), 4)
            price_source = fetched_source
        elif order.order_type == OrderType.LIMIT:
            if order.price is None or order.price <= 0:
                return ExecutionResult(success=False, message="Limit price required")
            # 限价单需校验当前市价可成交性，并以更优市价成交（与 PaperTradingBroker 口径一致）：
            # 买单：委托价 >= 市价才成交，成交价=市价；卖单：委托价 <= 市价才成交，成交价=市价。
            if side == "buy":
                if order.price < base_price:
                    return ExecutionResult(
                        success=False,
                        message=f"买单委托价 {order.price} 低于市价 {base_price}，限价单未成交",
                    )
            else:
                if order.price > base_price:
                    return ExecutionResult(
                        success=False,
                        message=f"卖单委托价 {order.price} 高于市价 {base_price}，限价单未成交",
                    )
            exec_price = round(float(base_price), 4)
            price_source = "market_price"
        else:
            return ExecutionResult(success=False, message=f"Unsupported order type: {order.order_type}")

        gross = order.quantity * exec_price
        if rules.market.value == "CN":
            # A 股保持既有全局费率口径（可由 env 覆盖），并设最低佣金（默认 5 元）
            commission = round(
                order.quantity * exec_price * settings.SIMULATION_COMMISSION_RATE, 2
            )
            commission = max(commission, float(settings.SIMULATION_COMMISSION_MIN))
            # 证券交易印花税：A 股卖出单边收取（买入不收取）
            stamp_duty = (
                round(gross * float(settings.SIMULATION_STAMP_DUTY_RATE), 2)
                if order.side.value == "sell"
                else 0.0
            )
        else:
            commission = rules.compute_commission(order.quantity, exec_price, side)
            stamp_duty = 0.0
        if order.side.value == "buy":
            delta_cash = -(gross + commission)
            delta_volume = order.quantity
        else:
            delta_cash = gross - commission - stamp_duty
            delta_volume = -order.quantity

        update = await self.manager.update_balance(
            user_id=order.user_id,
            symbol=order.symbol,
            delta_cash=delta_cash,
            delta_volume=delta_volume,
            price=exec_price,
            tenant_id=order.tenant_id,
            market=rules.market.value,
            t_plus_1=rules.t_plus_1,
        )
        if not update.get("success"):
            reason = update.get("reason", "BALANCE_UPDATE_FAILED")
            if reason == "INSUFFICIENT_CASH":
                return ExecutionResult(success=False, message="Insufficient cash for buy order")
            if reason == "INSUFFICIENT_HOLDINGS":
                return ExecutionResult(success=False, message="Insufficient holdings for sell order")
            return ExecutionResult(success=False, message=f"Balance update failed: {reason}")

        return ExecutionResult(
            success=True,
            price=exec_price,
            quantity=order.quantity,
            commission=commission,
            stamp_duty=stamp_duty,
            market=market_str,
            account_snapshot=account_snapshot,
            price_source=price_source,
        )

    async def apply_filled(self, order: SimOrder, result: ExecutionResult) -> SimTrade:
        trade_value = result.quantity * result.price
        total_fee = result.commission + result.stamp_duty
        trade = SimTrade(
            order_id=order.order_id,
            tenant_id=order.tenant_id,
            user_id=order.user_id,
            portfolio_id=order.portfolio_id,
            symbol=order.symbol,
            side=order.side,
            quantity=result.quantity,
            price=result.price,
            trade_value=trade_value,
            commission=result.commission,
            stamp_duty=result.stamp_duty,
            total_fee=total_fee,
            executed_at=datetime.now(),
            price_source=result.price_source,
        )
        self.db.add(trade)

        order.status = OrderStatus.FILLED
        order.submitted_at = order.submitted_at or datetime.now()
        order.filled_at = datetime.now()
        order.filled_quantity = result.quantity
        order.average_price = result.price
        order.filled_value = trade_value
        order.commission = result.commission
        order.total_fee = total_fee
        # 委托金额以实际成交金额为准（市价单无委托价，此前 quantity*(price or 0)=0 失真）
        order.order_value = trade_value
        order.execution_model = "synthetic_price"
        order.price_source = result.price_source

        try:
            await self.db.commit()
        except Exception:
            # #5 兜底：DB 落成交失败时，Redis 账户已在 execute_order 被扣款/加仓，
            # 此处回滚 DB 并把 Redis 账户恢复到执行前快照，避免资金与订单不一致。
            await self.db.rollback()
            if self.manager.redis and self.manager.redis.client:
                try:
                    key = self.manager._get_key(
                        order.user_id, order.tenant_id, result.market
                    )
                    if result.account_snapshot is not None:
                        write_json_cache(
                            self.manager.redis, key, result.account_snapshot
                        )
                    else:
                        self.manager.redis.client.delete(key)
                except Exception as restore_err:  # noqa: BLE001
                    logger.error(
                        "Failed to restore sim account after commit failure: %s",
                        restore_err,
                        exc_info=True,
                    )
            logger.error(
                "Sim order %s apply_filled commit failed; DB rolled back and account restored",
                order.order_id,
                exc_info=True,
            )
            raise

        await self.db.refresh(order)
        await self.db.refresh(trade)
        await self._sync_trade_account(order.tenant_id, order.user_id)
        # 交易时即失效 Redis，下次 GET 立即回源 DB 并回填缓存，实现秒级可见
        try:
            if self.manager.redis and self.manager.redis.client:
                self.manager.redis.delete_pattern(f"sim_trade:list:{order.tenant_id}:{order.user_id}:*")
                self.manager.redis.delete_pattern(f"sim_trade:stats:{order.tenant_id}:{order.user_id}:*")
        except Exception:
            pass
        return trade

    async def mark_rejected(self, order: SimOrder, message: str):
        order.status = OrderStatus.REJECTED
        order.submitted_at = order.submitted_at or datetime.now()
        order.remarks = f"Execution rejected: {message}"
        await self.db.commit()
        await self.db.refresh(order)

    async def _sync_trade_account(self, tenant_id: str, user_id: int):
        if not self.manager.redis.client:
            return
        account = await self.manager.get_account(user_id, tenant_id=tenant_id)
        if not account:
            return
        payload = dict(account)
        payload.setdefault("timestamp", datetime.now().isoformat())
        write_trade_account_cache(self.manager.redis, tenant_id, user_id, payload)
