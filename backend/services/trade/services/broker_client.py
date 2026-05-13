"""
Broker Client - 抽象 Broker 接口，支持模拟和真实交易

提供统一的下单接口，隔离 trading_engine 与具体 Broker 实现。
"""

import abc
import logging
import os
import random
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from backend.services.trade.services.simulation_manager import (
        SimulationAccountManager,
    )

from sqlalchemy import text

from backend.shared.auth import get_internal_call_secret
from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)


class BrokerResult:
    """Broker 执行结果"""

    def __init__(
        self,
        success: bool,
        filled_price: float = 0.0,
        filled_quantity: float = 0.0,
        commission: float = 0.0,
        exchange_order_id: str = "",
        message: str = "",
    ):
        self.success = success
        self.filled_price = filled_price
        self.filled_quantity = filled_quantity
        self.commission = commission
        self.exchange_order_id = exchange_order_id
        self.message = message




@dataclass
class MarketQuoteSnapshot:
    price: float
    limit_up: bool = False
    limit_down: bool = False
    suspended: bool = False

class BaseBroker(abc.ABC):
    """Broker 抽象基类"""

    @abc.abstractmethod
    async def place_order(
        self,
        user_id: int,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str,
        price: float | None = None,
        tenant_id: str = "default",
    ) -> BrokerResult:
        """下单"""
        ...

    @abc.abstractmethod
    async def query_account(
        self, user_id: str, tenant_id: str = "default"
    ) -> dict[str, Any]:
        """查询账户信息"""
        ...

    @abc.abstractmethod
    async def cancel_order(self, exchange_order_id: str, **kwargs) -> bool:
        """撤单"""
        ...

    @abc.abstractmethod
    async def query_quote(self, symbol: str) -> dict[str, Any]:
        """查询行情"""
        ...


class PaperTradingBroker(BaseBroker):
    """
    Paper Trading Broker with internal state management via Redis.
    Fetches real market prices for execution.
    """

    COMMISSION_RATE = 0.0003  # 0.03% commission

    def __init__(
        self,
        simulation_manager: "SimulationAccountManager",
        market_url: str = "http://stream-gateway:8003",
    ):
        self.simulation_manager = simulation_manager
        self.market_url = market_url
        self._client = None

    async def _get_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=5.0)
        return self._client

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

    async def _get_market_snapshot(self, symbol: str) -> MarketQuoteSnapshot:
        # Level 1: 实时行情
        try:
            client = await self._get_client()
            headers = {"X-Internal-Call": get_internal_call_secret()}
            resp = await client.get(f"{self.market_url}/api/v1/quotes/{symbol}", headers=headers)
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

                    return MarketQuoteSnapshot(
                        price=px,
                        limit_up=limit_up,
                        limit_down=limit_down,
                        suspended=suspended,
                    )
        except Exception as e:
            logger.warning(f"Failed to fetch real-time price for {symbol}: {e}")

        # Level 2: 数据库兜底 (L2 Fallback)
        try:
            async with get_session(read_only=True) as session:
                query_with_limits = text("""
                    SELECT close, adj_factor, limit_up_today, limit_down_today, volume
                    FROM stock_daily_latest
                    WHERE symbol = :symbol
                    ORDER BY trade_date DESC LIMIT 1
                """)
                try:
                    result = await session.execute(query_with_limits, {"symbol": symbol})
                    row = result.fetchone()
                    if row:
                        hfq_close = float(row[0])
                        adj_factor = float(row[1] or 1.0)
                        price = hfq_close / adj_factor if adj_factor > 0 else hfq_close
                        logger.info("[PaperTrading] Fallback to DB nominal price for %s: %s", symbol, price)
                        return MarketQuoteSnapshot(
                            price=price,
                            limit_up=self._is_price_near(price, self._as_float(row[2])),
                            limit_down=self._is_price_near(price, self._as_float(row[3])),
                            suspended=(self._as_float(row[4]) or 0.0) <= 0.0,
                        )
                except Exception:
                    query_legacy = text("""
                        SELECT close, adj_factor
                        FROM stock_daily_latest
                        WHERE symbol = :symbol
                        ORDER BY trade_date DESC LIMIT 1
                    """)
                    result = await session.execute(query_legacy, {"symbol": symbol})
                    row = result.fetchone()
                    if row:
                        hfq_close = float(row[0])
                        adj_factor = float(row[1] or 1.0)
                        price = hfq_close / adj_factor if adj_factor > 0 else hfq_close
                        logger.info("[PaperTrading] Fallback to DB legacy nominal price for %s: %s", symbol, price)
                        return MarketQuoteSnapshot(price=price)
        except Exception as e:
            logger.error(f"[PaperTrading] Database fallback failed for {symbol}: {e}")

        # Level 3: 最终保底
        return MarketQuoteSnapshot(price=100.0 + random.uniform(-1, 1))

    async def _get_market_price(self, symbol: str) -> float:
        """Fetch real-time price from Market Data Service with L2 DB fallback"""
        return (await self._get_market_snapshot(symbol)).price

    async def place_order(
        self,
        user_id: int,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str,
        price: float | None = None,
        tenant_id: str = "default",
    ) -> BrokerResult:
        snapshot = await self._get_market_snapshot(symbol)
        market_price = snapshot.price
        exec_price = 0.0
        slippage = random.uniform(-0.0005, 0.0005)

        normalized_side = str(side or "").strip().lower()
        if snapshot.suspended:
            return BrokerResult(success=False, message="Security is suspended, cannot trade")
        if normalized_side == "buy" and snapshot.limit_up:
            return BrokerResult(success=False, message="Limit-up locked, buy order cannot be filled")
        if normalized_side == "sell" and snapshot.limit_down:
            return BrokerResult(success=False, message="Limit-down locked, sell order cannot be filled")

        if order_type == "market":
            exec_price = market_price * (1 + slippage)
        elif order_type == "limit":
            if not price:
                return BrokerResult(success=False, message="Limit price required")
            if side == "buy":
                if price >= market_price:
                    exec_price = market_price
                else:
                    return BrokerResult(
                        success=False, message="Limit price not reached"
                    )
            else:
                if price <= market_price:
                    exec_price = market_price
                else:
                    return BrokerResult(
                        success=False, message="Limit price not reached"
                    )
        else:
            return BrokerResult(
                success=False, message=f"Unsupported order type: {order_type}"
            )

        exec_price = round(exec_price, 4)
        commission = round(quantity * exec_price * self.COMMISSION_RATE, 2)
        cost_or_proceeds = quantity * exec_price

        if side == "buy":
            delta_cash = -(cost_or_proceeds + commission)
            delta_volume = quantity
        else:
            delta_cash = cost_or_proceeds - commission
            delta_volume = -quantity

        # Update State
        update_result = await self.simulation_manager.update_balance(
            user_id=user_id,
            symbol=symbol,
            delta_cash=delta_cash,
            delta_volume=delta_volume,
            price=exec_price,
            tenant_id=tenant_id,
        )

        if not update_result.get("success"):
            reason = update_result.get("reason", "BALANCE_UPDATE_FAILED")
            if reason == "INSUFFICIENT_CASH":
                message = "Insufficient cash for buy order"
            elif reason == "INSUFFICIENT_HOLDINGS":
                message = "Insufficient holdings for sell order"
            else:
                message = f"Balance update failed: {reason}"
            return BrokerResult(success=False, message=message)

        exchange_id = f"SIM-{datetime.now().strftime('%Y%m%d%H%M%S')}-{random.randint(1000, 9999)}"
        logger.info(
            f"[PaperTrading] User {user_id} filled {side} {quantity} {symbol} @ {exec_price}"
        )

        return BrokerResult(
            success=True,
            filled_price=exec_price,
            filled_quantity=quantity,
            commission=commission,
            exchange_order_id=exchange_id,
            message="Paper Trading Fill",
        )

    async def query_account(
        self, user_id: str, tenant_id: str = "default"
    ) -> dict[str, Any]:
        """Query account state from Redis"""
        account = await self.simulation_manager.get_account(
            int(user_id), tenant_id=tenant_id
        )
        if not account:
            return {}
        return account

    async def cancel_order(self, exchange_order_id: str) -> bool:
        return True

    async def query_quote(self, symbol: str) -> dict[str, Any]:
        """Query quote from market service"""
        price = await self._get_market_price(symbol)
        return {
            "symbol": symbol,
            "last_price": price,
            "timestamp": datetime.now().isoformat(),
        }


class QMTBroker(BaseBroker):
    """
    QMT Broker 桥接

    通过 HTTP 调用本地 QMT 柜台客户端进行实盘下单。
    QMT 客户端通常在本地运行，提供 REST 接口或 Socket 接口。
    """

    def __init__(self, qmt_host: str = "127.0.0.1", qmt_port: int = 18080):
        self.base_url = f"http://{qmt_host}:{qmt_port}"
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import httpx

            self._session = httpx.AsyncClient(timeout=10.0)
        return self._session

    async def place_order(
        self,
        user_id: int,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str,
        price: float | None = None,
        tenant_id: str = "default",
    ) -> BrokerResult:
        try:
            client = await self._get_session()
            payload = {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "order_type": order_type,
                "price": price,
            }
            resp = await client.post(f"{self.base_url}/api/order", json=payload)

            if resp.status_code == 200:
                data = resp.json()
                return BrokerResult(
                    success=data.get("success", False),
                    filled_price=data.get("filled_price", 0.0),
                    filled_quantity=data.get("filled_quantity", 0.0),
                    commission=data.get("commission", 0.0),
                    exchange_order_id=data.get("order_id", ""),
                    message=data.get("message", ""),
                )
            else:
                return BrokerResult(
                    success=False,
                    message=f"QMT HTTP {resp.status_code}: {resp.text}",
                )
        except Exception as e:
            logger.error(f"[QMTBroker] place_order failed: {e}")
            return BrokerResult(success=False, message=str(e))

    async def query_account(
        self, user_id: str, tenant_id: str = "default"
    ) -> dict[str, Any]:
        try:
            client = await self._get_session()
            resp = await client.get(f"{self.base_url}/api/account")
            if resp.status_code == 200:
                data = resp.json()
                normalized = self._normalize_account_payload(data)
                return normalized
        except Exception as e:
            logger.error(f"[QMTBroker] query_account failed: {e}")
        return {}

    async def cancel_order(self, exchange_order_id: str) -> bool:
        try:
            client = await self._get_session()
            resp = await client.post(
                f"{self.base_url}/api/cancel",
                json={"order_id": exchange_order_id},
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"[QMTBroker] cancel_order failed: {e}")
            return False

    async def query_quote(self, symbol: str) -> dict[str, Any]:
        try:
            client = await self._get_session()
            resp = await client.get(f"{self.base_url}/api/quote/{symbol}")
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.error(f"[QMTBroker] query_quote failed: {e}")
        return {}

    @staticmethod
    def _normalize_account_payload(data: Any) -> dict[str, Any]:
        """
        将 QMT Bridge 的 /api/account 输出规范化为 trading_service 统一结构。

        统一结构（必需字段）：
        - total_asset: number
        - cash: number
        - market_value: number
        - positions: object，key 为 symbol，value 至少包含 volume/market_value/price
        """
        if not isinstance(data, dict):
            return {}

        # 兼容部分实现把核心字段放在 data 字段中
        if "data" in data and isinstance(data.get("data"), dict):
            data = data["data"]

        required = {"total_asset", "cash", "market_value", "positions"}
        if not required.issubset(set(data.keys())):
            return {}

        positions = data.get("positions")
        if isinstance(positions, list):
            # 兼容 positions 为列表：[{symbol, volume, market_value, price}, ...]
            pos_map: dict[str, Any] = {}
            for item in positions:
                if not isinstance(item, dict):
                    continue
                sym = item.get("symbol") or item.get("ts_code") or item.get("code")
                if not sym:
                    continue
                pos_map[str(sym)] = {
                    "volume": item.get("volume", 0),
                    "market_value": item.get("market_value", 0),
                    "price": item.get("price", 0),
                }
            positions = pos_map

        if not isinstance(positions, dict):
            return {}

        # 位置字段最小归一（避免下游 consumer 解析失败）
        cleaned_positions: dict[str, Any] = {}
        for sym, p in positions.items():
            if not isinstance(p, dict):
                continue
            cleaned_positions[str(sym)] = {
                "volume": p.get("volume", 0),
                "market_value": p.get("market_value", 0),
                "price": p.get("price", 0),
            }

        return {
            "total_asset": data.get("total_asset"),
            "cash": data.get("cash"),
            "market_value": data.get("market_value"),
            "today_pnl": data.get("today_pnl"),
            "positions": cleaned_positions,
        }


class QMTBridgeBroker(BaseBroker):
    """
    QMT Agent Bridge Broker

    REAL 下单通过 quantmind-stream 内部派发接口推送到 bridge_session 连接，
    执行回报由 Agent 回写 /internal/strategy/bridge/execution。
    """

    def __init__(
        self,
        stream_base_url: str,
        internal_secret: str = "",
        redis_client: Any = None,
    ):
        self.stream_base_url = str(stream_base_url or "").rstrip("/")
        self.internal_secret = (
            str(internal_secret or "").strip() or get_internal_call_secret()
        )
        self.redis_client = redis_client
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import httpx

            self._session = httpx.AsyncClient(timeout=10.0)
        return self._session

    async def place_order(
        self,
        user_id: int,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str,
        price: float | None = None,
        tenant_id: str = "default",
        client_order_id: str | None = None,
        trade_action: str | None = None,
        position_side: str | None = None,
        is_margin_trade: bool | None = None,
    ) -> BrokerResult:
        client_oid = str(client_order_id or "").strip()
        if not client_oid:
            return BrokerResult(
                success=False, message="client_order_id is required in bridge mode"
            )
        if not self.stream_base_url:
            return BrokerResult(success=False, message="stream_base_url is empty")

        payload = {
            "tenant_id": str(tenant_id or "").strip() or "default",
            "user_id": str(user_id),
            "payload": {
                "client_order_id": client_oid,
                "symbol": str(symbol or "").strip(),
                "side": str(side or "").strip().upper(),
                "quantity": int(float(quantity or 0)),
                "order_type": str(order_type or "").strip().upper(),
                "price": float(price or 0.0),
                "trade_action": str(trade_action or "").strip().lower() or None,
                "position_side": str(position_side or "").strip().lower() or None,
                "is_margin_trade": bool(is_margin_trade)
                if is_margin_trade is not None
                else None,
                "dispatch_mode": "async",  # 使用异步下单，避免 QMT SDK 同步调用挂起 WS 接收线程
            },
        }
        if payload["payload"]["quantity"] <= 0:
            return BrokerResult(success=False, message="quantity must be > 0")

        try:
            client = await self._get_session()
            resp = await client.post(
                f"{self.stream_base_url}/api/v1/internal/bridge/order",
                json=payload,
                headers={"X-Internal-Call": self.internal_secret},
            )
            if resp.status_code != 200:
                return BrokerResult(
                    success=False,
                    message=f"bridge dispatch HTTP {resp.status_code}: {resp.text}",
                )

            data = resp.json()
            if not data.get("ok"):
                return BrokerResult(
                    success=False,
                    message=str(data.get("reason") or "bridge dispatch failed"),
                )

            return BrokerResult(
                success=True,
                exchange_order_id="",
                message=(
                    f"bridge dispatched to {data.get('dispatched', 0)} connection(s); "
                    "awaiting qmt exchange_order_id callback"
                ),
            )
        except Exception as e:
            logger.error("[QMTBridgeBroker] place_order failed: %s", e)
            return BrokerResult(success=False, message=str(e))

    async def query_account(
        self, user_id: str, tenant_id: str = "default"
    ) -> dict[str, Any]:
        try:
            async with get_session(read_only=True) as session:
                row = (
                    (
                        await session.execute(
                            text(
                                """
                            SELECT *
                            FROM real_account_snapshot_overview_v
                            WHERE tenant_id = :tenant_id
                              AND user_id IN (:user_id, LPAD(CAST(:user_id AS TEXT), 8, '0'))
                            ORDER BY snapshot_at DESC, id DESC
                            LIMIT 1
                            """
                            ),
                            {"tenant_id": tenant_id, "user_id": str(user_id).strip()},
                        )
                    )
                    .mappings()
                    .first()
                )
            if row:
                data = dict(row)
                payload = data.get("payload_json") or {}
                if isinstance(payload, dict):
                    data.setdefault("positions", payload.get("positions") or [])
                    for key in (
                        "broker",
                        "available_cash",
                        "frozen_cash",
                        "yesterday_balance",
                        "short_proceeds",
                        "liabilities",
                        "short_market_value",
                        "credit_limit",
                        "maintenance_margin_ratio",
                        "credit_enabled",
                        "shortable_symbols_count",
                        "last_short_check_at",
                        "compacts",
                        "credit_subjects",
                        "debug_version",
                        "metrics",
                        "metrics_meta",
                    ):
                        if key in payload and payload[key] is not None:
                            data[key] = payload[key]
                return data
        except Exception as e:
            logger.error("[QMTBridgeBroker] query_account failed: %s", e)
        return {}

    async def cancel_order(self, exchange_order_id: str, **kwargs) -> bool:
        user_id = str(kwargs.get("user_id") or "").strip()
        tenant_id = str(kwargs.get("tenant_id") or "default").strip() or "default"
        account_id = str(kwargs.get("account_id") or "").strip() or None
        client_order_id = str(kwargs.get("client_order_id") or "").strip() or None
        symbol = str(kwargs.get("symbol") or "").strip()
        side = str(kwargs.get("side") or "").strip()

        if not user_id:
            logger.warning(
                "[QMTBridgeBroker] cancel_order missing user_id, skipping bridge dispatch"
            )
            return False
        if not self.stream_base_url:
            return False

        cancel_payload: dict = {}
        if str(exchange_order_id or "").strip():
            cancel_payload["exchange_order_id"] = str(exchange_order_id).strip()
        if client_order_id:
            cancel_payload["client_order_id"] = client_order_id
        if symbol:
            cancel_payload["symbol"] = symbol
        if side:
            cancel_payload["side"] = side

        try:
            client = await self._get_session()
            resp = await client.post(
                f"{self.stream_base_url}/api/v1/internal/bridge/cancel",
                json={
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "account_id": account_id,
                    "payload": cancel_payload,
                },
                headers={"X-Internal-Call": self.internal_secret},
            )
            if resp.status_code != 200:
                logger.warning(
                    "[QMTBridgeBroker] cancel_order HTTP %s: %s",
                    resp.status_code,
                    resp.text,
                )
                return False
            data = resp.json()
            if not data.get("ok"):
                logger.warning(
                    "[QMTBridgeBroker] cancel_order bridge dispatch failed: %s",
                    data.get("reason"),
                )
                return False
            return True
        except Exception as e:
            logger.error("[QMTBridgeBroker] cancel_order failed: %s", e)
            return False

    async def query_quote(self, symbol: str) -> dict[str, Any]:
        if not self.stream_base_url:
            return {}

        try:
            client = await self._get_session()
            resp = await client.get(
                f"{self.stream_base_url}/api/v1/quotes/{symbol}",
                headers={"X-Internal-Call": self.internal_secret},
            )
            if resp.status_code != 200:
                logger.warning(
                    "[QMTBridgeBroker] query_quote HTTP %s for %s: %s",
                    resp.status_code,
                    symbol,
                    resp.text,
                )
                return {}
            data = resp.json() or {}
            last_price = data.get("current_price") or data.get("last_price")
            if last_price is None:
                return {}
            price = float(last_price)
            if price <= 0:
                return {}
            return {
                "symbol": symbol,
                "last_price": price,
                "timestamp": data.get("timestamp") or datetime.now().isoformat(),
            }
        except Exception as e:
            logger.error("[QMTBridgeBroker] query_quote failed for %s: %s", symbol, e)
            return {}


class RedisBroker(BaseBroker):
    """
    Redis Stream Broker — 通过 Trade Redis Stream 向 QMT Agent 下发交易指令。

    流程:
      1. place_order → XADD quantmind:trade:cmds:{user_id}（持久化指令 + HMAC 签名）
      2. 终端代理用 XREADGROUP 消费，验签后执行，处理完成后 XACK
      3. 执行后 XADD qm:exec:stream:{tenant_id}，ExecutionStreamConsumer 消费回报
      4. Agent 离线期间指令留在 Stream，重连后自动补发（不丢失）

    账户查询优先读取 PostgreSQL 视图，不再依赖 Redis 账户快照键。
    """

    CMD_CONSUMER_GROUP = "qmt-agent"

    def __init__(
        self,
        redis_host: str,
        redis_port: int,
        redis_password: str,
        hmac_secret: str = "",
        cmd_stream_prefix: str = "quantmind:trade:cmds",
        cmd_stream_maxlen: int = 10000,
    ):
        import redis as _redis

        self._redis = _redis.StrictRedis(
            host=redis_host,
            port=redis_port,
            password=redis_password,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        self._hmac_secret = hmac_secret
        self._cmd_stream_prefix = cmd_stream_prefix
        self._cmd_stream_maxlen = cmd_stream_maxlen

    def _sign_cmd(self, payload: dict) -> str:
        """对指令 payload 生成 HMAC-SHA256 签名（不含 hmac 字段本身）。
        签名基于类型化字段（int quantity, float price），与 Agent 侧保持一致。"""
        import hashlib
        import hmac as _hmac
        import json as _json

        canonical = _json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return _hmac.new(
            self._hmac_secret.encode(),
            canonical.encode(),
            hashlib.sha256,
        ).hexdigest()

    def _cmd_stream_key(self, user_id) -> str:
        return f"{self._cmd_stream_prefix}:{user_id}"

    async def place_order(
        self,
        user_id: int,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str,
        price: float | None = None,
        tenant_id: str = "default",
        client_order_id: str = "",
    ) -> BrokerResult:
        import uuid as _uuid

        # client_order_id 必须由上层传入，确保重试幂等性。
        # 若未传入则生成随机值并记录警告，避免重试时重复下单。
        if not client_order_id:
            client_order_id = str(_uuid.uuid4())
            logger.warning(
                "[RedisBroker] client_order_id 未传入，本次使用随机值 %s；"
                "若存在重试逻辑，请确保传入稳定的 client_order_id。",
                client_order_id,
            )

        # 构建类型化指令 payload（用于 HMAC 签名，类型须与 Agent 解析一致）
        typed_cmd = {
            "order_id": client_order_id,
            "symbol": symbol,
            "side": side.upper(),
            "quantity": int(quantity),
            "price": float(price or 0),
            "order_type": order_type.upper(),
        }
        if self._hmac_secret:
            typed_cmd["hmac"] = self._sign_cmd(typed_cmd)
        else:
            logger.warning(
                "[RedisBroker] QMT_CMD_HMAC_SECRET 未配置，指令将以明文发送（不安全）"
            )

        # Stream 字段必须全部为字符串
        stream_fields = {k: str(v) for k, v in typed_cmd.items()}
        stream_key = self._cmd_stream_key(user_id)
        try:
            msg_id = self._redis.xadd(
                stream_key,
                stream_fields,
                maxlen=self._cmd_stream_maxlen,
                approximate=True,
            )
            logger.info(
                "[RedisBroker] 指令已写入Stream: key=%s msg_id=%s %s %s qty=%s price=%s",
                stream_key,
                msg_id,
                side,
                symbol,
                quantity,
                price,
            )
            return BrokerResult(
                success=True,
                exchange_order_id=typed_cmd["order_id"],
                message=f"enqueued to {stream_key} msg_id={msg_id}",
            )
        except Exception as e:
            logger.error("[RedisBroker] XADD failed: %s", e)
            return BrokerResult(success=False, message=str(e))

    async def query_account(
        self, user_id: str, tenant_id: str = "default"
    ) -> dict[str, Any]:
        try:
            async with get_session(read_only=True) as session:
                row = (
                    (
                        await session.execute(
                            text(
                                """
                            SELECT *
                            FROM real_account_snapshot_overview_v
                            WHERE tenant_id = :tenant_id
                              AND user_id IN (:user_id, LPAD(CAST(:user_id AS TEXT), 8, '0'))
                            ORDER BY snapshot_at DESC, id DESC
                            LIMIT 1
                            """
                            ),
                            {"tenant_id": tenant_id, "user_id": str(user_id).strip()},
                        )
                    )
                    .mappings()
                    .first()
                )
            if row:
                data = dict(row)
                payload = data.get("payload_json") or {}
                if isinstance(payload, dict):
                    data.setdefault("positions", payload.get("positions") or [])
                    for key in (
                        "broker",
                        "available_cash",
                        "frozen_cash",
                        "yesterday_balance",
                        "short_proceeds",
                        "liabilities",
                        "short_market_value",
                        "credit_limit",
                        "maintenance_margin_ratio",
                        "credit_enabled",
                        "shortable_symbols_count",
                        "last_short_check_at",
                        "compacts",
                        "credit_subjects",
                        "debug_version",
                        "metrics",
                        "metrics_meta",
                    ):
                        if key in payload and payload[key] is not None:
                            data[key] = payload[key]
                return data
        except Exception as e:
            logger.error("[RedisBroker] query_account failed: %s", e)
        return {}

    async def cancel_order(self, exchange_order_id: str) -> bool:
        # 撤单指令可扩展为向 quantmind:trade:cancel:{user_id} publish
        logger.warning(
            "[RedisBroker] cancel_order not implemented yet: %s", exchange_order_id
        )
        return False

    async def query_quote(self, symbol: str) -> dict[str, Any]:
        # 行情由 Stream 服务提供，不走终端代理
        return {}


def create_broker(enable_real: bool, **kwargs) -> BaseBroker:
    """
    工厂方法：根据配置创建 Broker 实例。

    broker_type (str, kwargs):
      "bridge" → QMTBridgeBroker（通过 stream /internal/bridge/order 下发到 WS bridge agent）
      "redis"  → RedisBroker（通过 Trade Redis Stream 向终端代理下发指令）
      "qmt"    → QMTBroker（HTTP 调用本地 QMT Bridge，旧模式）
      未设置   → QMTBridgeBroker（REAL）或 PaperTradingBroker（SIM）
    """
    broker_type = str(kwargs.get("broker_type", "bridge")).lower()

    if enable_real:
        if broker_type == "redis":
            redis_password = kwargs.get("redis_trade_password") or os.getenv(
                "REDIS_PASSWORD", ""
            )
            hmac_secret = kwargs.get("hmac_secret") or os.getenv(
                "QMT_CMD_HMAC_SECRET", ""
            )
            if not hmac_secret:
                logger.warning(
                    "[create_broker] QMT_CMD_HMAC_SECRET 未设置，RedisBroker 将以不签名模式运行"
                )
            return RedisBroker(
                redis_host=kwargs.get("redis_trade_host")
                or os.getenv("REDIS_HOST", "localhost"),
                redis_port=int(
                    kwargs.get("redis_trade_port") or os.getenv("REDIS_PORT", "6379")
                ),
                redis_password=redis_password,
                hmac_secret=hmac_secret,
                cmd_stream_prefix=os.getenv(
                    "TRADE_CMD_STREAM_PREFIX", "quantmind:trade:cmds"
                ),
                cmd_stream_maxlen=int(os.getenv("TRADE_CMD_STREAM_MAXLEN", "10000")),
            )
        if broker_type == "qmt":
            return QMTBroker(
                qmt_host=kwargs.get("qmt_host", "127.0.0.1"),
                qmt_port=kwargs.get("qmt_port", 18080),
            )
        if broker_type == "bridge":
            return QMTBridgeBroker(
                stream_base_url=kwargs.get("stream_base_url")
                or kwargs.get("market_url")
                or os.getenv("MARKET_DATA_SERVICE_URL", "http://stream-gateway:8003"),
                internal_secret=kwargs.get("internal_secret")
                or get_internal_call_secret(),
                redis_client=kwargs.get("redis_client"),
            )
        raise ValueError(
            f"[create_broker] 未知 broker_type='{broker_type}'，"
            "有效值: 'bridge'（默认）, 'redis', 'qmt'。请检查 REAL_BROKER_TYPE 环境变量配置。"
        )

    # Inject Simulation Manager
    from backend.services.trade.services.simulation_manager import (
        SimulationAccountManager,
    )

    # We need redis client here. But factory is usually called without redis...
    # Strategy: Pass redis_client in kwargs or let Broker init it?
    # Better: TradingEngine passes redis_client to create_broker
    redis_client = kwargs.get("redis_client")
    market_url = kwargs.get("market_url", "http://stream-gateway:8003")

    if not redis_client:
        # Fallback or Error?
        # For safety in existing tests that might call this without redis, we might need a workaround.
        # But this is a major feature change.
        raise ValueError("Redis Client required for Paper Trading Broker")

    sim_manager = SimulationAccountManager(redis_client)
    return PaperTradingBroker(sim_manager, market_url)
