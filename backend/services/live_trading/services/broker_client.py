"""
Broker Client - 抽象 Broker 接口，支持模拟和真实交易

提供统一的下单接口，隔离 trading_engine 与具体 Broker 实现。
"""

import abc
import logging
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from backend.services.trade_shared.simulation_manager import (
        SimulationAccountManager,
    )

from sqlalchemy import text

from backend.services.trade_shared.trade_config import settings
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

        # Level 3: 无法获取行情 —— 不伪造随机价格，交由 place_order 拒单，
        # 避免以虚假价格成交污染模拟盘资产/持仓。
        return MarketQuoteSnapshot(price=0.0)

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
        if market_price <= 0:
            return BrokerResult(
                success=False,
                message=f"无法获取 {symbol} 实时行情，模拟单拒绝成交",
            )
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
        # 佣金：与 SimulationExecutionEngine 口径一致 —— 按费率计算并设最低佣金（默认 5 元）
        brute_commission = round(
            quantity * exec_price * self.COMMISSION_RATE, 2
        )
        commission = max(brute_commission, float(settings.SIMULATION_COMMISSION_MIN))
        # 证券交易印花税：A 股卖出单边收取
        try:
            from backend.services.simulation.services.market_rules import (
                infer_market,
            )

            cn_market = str(infer_market(symbol).value).upper() == "CN"
        except Exception:  # noqa: BLE001
            cn_market = True
        stamp_duty = (
            round(quantity * exec_price * float(settings.SIMULATION_STAMP_DUTY_RATE), 2)
            if cn_market and str(side).lower() == "sell"
            else 0.0
        )
        total_fee = round(commission + stamp_duty, 2)
        cost_or_proceeds = quantity * exec_price

        if side == "buy":
            delta_cash = -(cost_or_proceeds + total_fee)
            delta_volume = quantity
        else:
            delta_cash = cost_or_proceeds - total_fee
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
            commission=total_fee,
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


class TdxBroker(BaseBroker):
    """
    通达信 (TDX) 交易桥 Broker

    通过 Windows 桥 (bridge-windows, 监听 :8550) 与通达信客户端交互。
    通达信实盘下单需要用户在客户端手动确认 (Value=1)，本类遵循 Bridge 模式:
    下单成功返回 Wtbh 作为 exchange_order_id，成交回报由用户确认后由桥回写。

    配置:
      TDX_BRIDGE_URL  - 桥地址, 如 http://192.168.31.31:8550
      TDX_BRIDGE_TOKEN - 桥鉴权 token (与 Linux 侧 BRIDGE_AUTH_TOKEN 一致)
      TDX_ACCOUNT     - 通达信资金账号 (可选, 为空则用默认账号 account_id=0)
      TDX_ACCOUNT_TYPE - 账号类型, 默认 "stock"
    """

    def __init__(
        self,
        bridge_url: str = "",
        bridge_token: str = "",
        account: str = "",
        account_type: str = "stock",
        timeout: float = 10.0,
    ):
        self.bridge_url = str(bridge_url or os.getenv("TDX_BRIDGE_URL", "")).rstrip("/")
        self.bridge_token = str(
            bridge_token or os.getenv("TDX_BRIDGE_TOKEN", "")
        ).strip()
        self.account = str(account or os.getenv("TDX_ACCOUNT", "")).strip()
        self.account_type = str(
            account_type or os.getenv("TDX_ACCOUNT_TYPE", "stock")
        ).strip()
        self.timeout = timeout
        self._client = None
        if not self.bridge_url:
            logger.warning("[TdxBroker] TDX_BRIDGE_URL 未配置")

    async def _get_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {self.bridge_token}",
        }

    def _std_symbol(self, symbol: str) -> str:
        """补齐标准代码: 600519 -> 600519.SH, 000001 -> 000001.SZ"""
        s = str(symbol or "").strip()
        if not s:
            return s
        if "." in s:
            return s.upper()
        if s.startswith(("6", "9")):
            return f"{s}.SH"
        return f"{s}.SZ"

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
        if not self.bridge_url:
            return BrokerResult(success=False, message="TDX_BRIDGE_URL 未配置")
        if not self.bridge_token:
            return BrokerResult(success=False, message="TDX_BRIDGE_TOKEN 未配置")

        std_symbol = self._std_symbol(symbol)
        is_sell = str(side or "").strip().upper() in ("SELL", "S")
        price_type = 1  # 市价
        price_value = 0.0
        if str(order_type or "").strip().upper() in ("LIMIT", "L"):
            price_type = 0  # 限价
            price_value = float(price or 0)
            if price_value <= 0:
                return BrokerResult(
                    success=False, message="限价单必须提供价格 (price)"
                )

        plan_id = str(client_order_id or f"qm_{int(time.time())}_{os.getpid()}")
        payload = {
            "plan_id": plan_id,
            "account": self.account,
            "account_type": self.account_type,
            "source": "quantmind",
            "orders": [
                {
                    "stock_code": std_symbol,
                    "side": "sell" if is_sell else "buy",
                    "volume": int(float(quantity or 0)),
                    "order_type": "limit" if price_type == 0 else "market",
                    "price_type": price_type,
                    "price": price_value if price_type == 0 else None,
                }
            ],
        }
        if int(payload["orders"][0]["volume"]) <= 0:
            return BrokerResult(success=False, message="quantity must be > 0")

        try:
            client = await self._get_client()
            resp = await client.post(
                f"{self.bridge_url}/api/v1/plans/execute",
                json=payload,
                headers=self._headers(),
            )
            if resp.status_code != 200:
                return BrokerResult(
                    success=False,
                    message=f"桥返回 HTTP {resp.status_code}: {resp.text}",
                )
            data = resp.json()
            if data.get("status") == "duplicate":
                return BrokerResult(
                    success=False, message=f"重复计划: {data.get('message')}"
                )
            orders = data.get("orders") or []
            first = orders[0] if orders else {}
            status = first.get("status", data.get("status", "unknown"))
            order_id = first.get("order_id", "")

            if status in ("rejected", "error"):
                return BrokerResult(
                    success=False,
                    message=first.get("message") or data.get("message") or "下单被拒",
                )

            # Bridge 模式: 已提交/待确认, 返回 Wtbh 作为 exchange_order_id
            return BrokerResult(
                success=True,
                exchange_order_id=order_id,
                message=f"TDX 已受理: {first.get('message') or status}",
            )
        except Exception as e:
            logger.error("[TdxBroker] place_order failed: %s", e)
            return BrokerResult(success=False, message=str(e))

    async def query_account(
        self, user_id: str, tenant_id: str = "default"
    ) -> dict[str, Any]:
        if not self.bridge_url:
            return {}
        try:
            client = await self._get_client()
            resp = await client.post(
                f"{self.bridge_url}/api/v1/account/query",
                json={"account": self.account, "account_type": self.account_type},
                headers=self._headers(),
            )
            if resp.status_code != 200:
                logger.warning(
                    "[TdxBroker] query_account HTTP %s: %s",
                    resp.status_code,
                    resp.text,
                )
                return {}
            data = resp.json() or {}
            asset = data.get("asset") or {}
            positions = data.get("positions") or []
            return {
                "broker": "tdx",
                "available_cash": asset.get("cash", 0),
                "balance": asset.get("balance", 0),
                "total_asset": asset.get("asset", 0),
                "market_value": asset.get("market_value", 0),
                "positions": positions,
            }
        except Exception as e:
            logger.error("[TdxBroker] query_account failed: %s", e)
            return {}

    async def cancel_order(self, exchange_order_id: str, **kwargs) -> bool:
        if not self.bridge_url:
            return False
        symbol = self._std_symbol(kwargs.get("symbol", ""))
        try:
            client = await self._get_client()
            resp = await client.post(
                f"{self.bridge_url}/api/v1/orders/cancel",
                json={
                    "account": self.account,
                    "account_type": self.account_type,
                    "stock_code": symbol,
                    "order_id": str(exchange_order_id),
                },
                headers=self._headers(),
            )
            if resp.status_code != 200:
                logger.warning(
                    "[TdxBroker] cancel_order HTTP %s: %s", resp.status_code, resp.text
                )
                return False
            data = resp.json() or {}
            return bool(data.get("success"))
        except Exception as e:
            logger.error("[TdxBroker] cancel_order failed: %s", e)
            return False

    async def query_quote(self, symbol: str) -> dict[str, Any]:
        """用通达信快照接口取最新价."""
        if not self.bridge_url:
            return {}
        try:
            client = await self._get_client()
            resp = await client.get(
                f"{self.bridge_url}/api/v1/health", headers=self._headers()
            )
            if resp.status_code != 200:
                return {}
            # 行情走共享/桥的 get_market_snapshot; 简单返回空, 让上层走 stream 行情
            return {}
        except Exception as e:
            logger.error("[TdxBroker] query_quote failed for %s: %s", symbol, e)
            return {}


# 价格保护豁免来源：报价由专用链路决定
#   sltp-  止损执行器（保护价=跌停价，天然贴边）
#   flat-/flatten-  人工/脚本全量平仓
#   mir-   镜像单（已过 2% 偏离闸门 + 强平豁免）
_PRICE_PROTECTION_EXEMPT_CID_PREFIXES = ("sltp-", "flat-", "flatten-", "mir-")
# 涨跌停带容差：报价允许比带边界再外扩 2%（覆盖滑点/复权误差），超出即拒
_PRICE_PROTECTION_BAND_TOLERANCE = 0.02


def check_price_protection_band(
    *,
    price: float,
    side: str,
    detail: dict[str, Any] | None,
    client_order_id: str = "",
    tolerance: float = _PRICE_PROTECTION_BAND_TOLERANCE,
) -> str | None:
    """报价是否越出涨跌停带（返回人类可读原因；``None`` = 放行，纯函数）。

    实测柜台对超范围价**不拒单**且按盘口成交（卖 1.80 低于跌停 1.97 → 成交 2.40），
    所以本地必须自己设闸：防程序 bug 把离谱价格当市价单打出去。
    取不到 ``UpStopPrice/DownStopPrice`` 时放行（不因行情缺失阻断交易）。
    """
    _ = side
    cid = str(client_order_id or "")
    if any(cid.startswith(prefix) for prefix in _PRICE_PROTECTION_EXEMPT_CID_PREFIXES):
        return None
    value = float(price or 0)
    if value <= 0:
        return None
    band = detail or {}
    try:
        up = float(band.get("UpStopPrice") or 0)
        down = float(band.get("DownStopPrice") or 0)
    except (TypeError, ValueError):
        return None
    if up <= 0 or down <= 0:
        return None
    lower = down * (1 - tolerance)
    upper = up * (1 + tolerance)
    if lower <= value <= upper:
        return None
    return (
        f"限价 {value:.2f} 越出涨跌停带 [{lower:.2f}, {upper:.2f}]"
        f"（跌停 {down:.2f} / 涨停 {up:.2f}），疑似价格错误"
    )


class QmtExecBroker(BaseBroker):
    """
    大 QMT 执行端 Broker（big-convert RPC 直连）。

    与 TdxBroker 的差异：不经 HTTP 桥，而是通过 QMT 内置 Python 里常驻的
    big-convert RPC 服务端（``qmt_exec_client``）下单/查询；下单只返回委托编号，
    成交由 ``qmt_exec_poller`` 轮询回收后经共享内核落库。

    配置（Redis ``broker:config:qmt_exec`` 优先，回退环境变量）:
      QMT_EXEC_ENABLED / QMT_EXEC_ACCOUNT_ID / QMT_EXEC_ACCOUNT_TYPE
      QMT_EXEC_STRATEGY_NAME / QMT_EXEC_TIMEOUT
    """

    def __init__(
        self,
        account_id: str = "",
        account_type: str = "STOCK",
        strategy_name: str = "",
        timeout: float = 0.0,
        client: Any = None,
    ):
        from backend.services.live_trading.services.qmt_exec_client import (
            QmtExecClient,
            get_qmt_exec_client,
        )

        if client is not None:
            self._client = client
        elif account_id or strategy_name or timeout:
            env_client = get_qmt_exec_client()
            self._client = QmtExecClient(
                enabled=True,
                account_id=account_id or env_client.account_id,
                account_type=account_type or env_client.account_type,
                timeout=float(timeout or env_client.timeout),
                strategy_name=strategy_name or env_client.strategy_name,
                bridge_redis=env_client.bridge_redis,
            )
        else:
            self._client = get_qmt_exec_client()
        if not self._client.configured:
            logger.warning(
                "[QmtExecBroker] QMT 执行端未启用/未配置（QMT_EXEC_ENABLED、QMT_EXEC_ACCOUNT_ID）"
            )

    @property
    def client(self):
        return self._client

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
        _ = (user_id, tenant_id, trade_action, position_side, is_margin_trade)
        from backend.services.live_trading.services.qmt_exec_client import QmtExecError

        side_raw = str(side or "").strip().upper()
        if side_raw not in ("BUY", "SELL"):
            return BrokerResult(success=False, message=f"非法方向: {side}")
        order_type_raw = str(order_type or "LIMIT").strip().upper()
        if order_type_raw not in ("LIMIT", "MARKET"):
            return BrokerResult(success=False, message=f"不支持的下单类型: {order_type}")
        if order_type_raw == "LIMIT" and float(price or 0) <= 0:
            return BrokerResult(success=False, message="限价单必须提供价格")

        # 价格保护（Phase 4.1）：报价必须落在涨跌停带内（含容差），否则拒单。
        # 实测柜台会"照收"超范围价并按盘口成交（卖 1.80 低于跌停 1.97 → 成交 2.40），
        # 所以这一层是防程序 bug 打出离谱价格的最后一道闸。
        block_reason = await self._check_price_protection(
            symbol=symbol,
            order_type=order_type_raw,
            price=float(price or 0),
            side=side_raw,
            client_order_id=str(client_order_id or ""),
        )
        if block_reason:
            logger.warning(
                "[QmtExecBroker] 价格保护拒绝 symbol=%s side=%s price=%s cid=%s: %s",
                symbol,
                side_raw,
                price,
                client_order_id,
                block_reason,
            )
            return BrokerResult(success=False, message=f"[PRICE_PROTECTION] {block_reason}")

        try:
            result = await self._client.submit_order(
                symbol=symbol,
                side=side_raw,
                quantity=quantity,
                order_type=order_type_raw,
                price=price,
                client_order_id=str(client_order_id or ""),
            )
        except QmtExecError as exc:
            logger.error(
                "[QmtExecBroker] 下单失败 symbol=%s side=%s qty=%s code=%s: %s",
                symbol,
                side_raw,
                quantity,
                exc.code,
                exc,
            )
            return BrokerResult(success=False, message=f"[{exc.code}] {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.error("[QmtExecBroker] 下单异常: %s", exc, exc_info=True)
            return BrokerResult(success=False, message=str(exc))

        exchange_order_id = str(
            result.get("order_id") or result.get("order_sysid") or ""
        )
        return BrokerResult(
            success=True,
            exchange_order_id=exchange_order_id,
            message=f"QMT 已受理: order_id={exchange_order_id} remark={result.get('remark', '')}",
        )

    async def query_account(
        self, user_id: str, tenant_id: str = "default"
    ) -> dict[str, Any]:
        _ = (user_id, tenant_id)
        from backend.services.live_trading.services.qmt_exec_client import QmtExecError

        try:
            asset = await self._client.get_asset()
            positions = await self._client.get_positions()
        except QmtExecError as exc:
            logger.warning("[QmtExecBroker] query_account 失败 code=%s: %s", exc.code, exc)
            return {}
        except Exception as exc:  # noqa: BLE001
            logger.error("[QmtExecBroker] query_account 异常: %s", exc)
            return {}
        return {
            "broker": "qmt_exec",
            "available_cash": asset.get("cash", 0),
            "balance": asset.get("total_asset", 0),
            "total_asset": asset.get("total_asset", 0),
            "market_value": asset.get("market_value", 0),
            "frozen_cash": asset.get("frozen_cash", 0),
            "positions": [
                {
                    "symbol": item.get("symbol"),
                    "stock_code": item.get("stock_code"),
                    "volume": item.get("volume"),
                    "available_volume": item.get("can_use_volume"),
                    "cost_price": item.get("avg_price"),
                    "market_value": item.get("market_value"),
                }
                for item in positions
            ],
        }

    async def cancel_order(self, exchange_order_id: str, **kwargs) -> bool:
        ok, _reason = await self.cancel_order_verbose(exchange_order_id, **kwargs)
        return ok

    async def cancel_order_verbose(
        self, exchange_order_id: str, **kwargs
    ) -> tuple[bool, str]:
        """撤单（如实上报）：返回 ``(是否受理, 原因码)``。

        原因码：``submitted`` 已发往柜台；``counter_rejected`` 柜台拒绝
        （多为已成交/已撤销）；``timeout`` 结果未知需先查询委托；其余为 RPC 错误码。
        """
        from backend.services.live_trading.services.qmt_exec_client import QmtExecError

        symbol = str(kwargs.get("symbol") or "")
        try:
            await self._client.cancel_order(
                order_id=str(exchange_order_id), symbol=symbol
            )
            return True, "submitted"
        except QmtExecError as exc:
            code = str(getattr(exc, "code", "") or "")
            reason = {
                "CANCEL_REJECTED": "counter_rejected",
                "TIMEOUT": "timeout",
            }.get(code, code.lower() or "rejected")
            logger.warning(
                "[QmtExecBroker] 撤单失败 order_id=%s code=%s reason=%s: %s",
                exchange_order_id,
                exc.code,
                reason,
                exc,
            )
            return False, reason
        except Exception as exc:  # noqa: BLE001
            logger.error("[QmtExecBroker] 撤单异常: %s", exc)
            return False, "error"

    async def _check_price_protection(
        self,
        *,
        symbol: str,
        order_type: str,
        price: float,
        side: str,
        client_order_id: str,
    ) -> str | None:
        """限价单报价必须落在涨跌停带内（强平/镜像来源豁免）。取不到带则放行。"""
        if str(order_type or "").upper() != "LIMIT":
            return None
        if float(price or 0) <= 0:
            return None
        if str(client_order_id or "").startswith(_PRICE_PROTECTION_EXEMPT_CID_PREFIXES):
            return None
        try:
            detail = await self._client.get_instrument_detail(symbol)
        except Exception as exc:  # noqa: BLE001 - 行情取不到不拦交易
            logger.warning(
                "[QmtExecBroker] 价格保护取合约详情失败（放行） symbol=%s: %s", symbol, exc
            )
            return None
        return check_price_protection_band(price=price, side=side, detail=detail)

    async def query_quote(self, symbol: str) -> dict[str, Any]:
        """行情走 stream / QuantDB，不依赖 QMT 行情权限。"""
        _ = symbol
        return {}


def create_broker(enable_real: bool, **kwargs) -> BaseBroker:
    """
    工厂方法：根据配置创建 Broker 实例。

    broker_type (str, kwargs):
      "bridge" → QMTBridgeBroker（通过 stream /internal/bridge/order 下发到 WS bridge agent）
      "redis"  → RedisBroker（通过 Trade Redis Stream 向终端代理下发指令）
      "qmt"    → QMTBroker（HTTP 调用本地 QMT Bridge，旧模式）
      "tdx"    → TdxBroker（Windows TDX 桥 :8550）
      "qmt_exec" → QmtExecBroker（大 QMT 内置 Python 的 big-convert RPC 直连）
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
        if broker_type == "tdx":
            return TdxBroker(
                bridge_url=kwargs.get("tdx_bridge_url")
                or os.getenv("TDX_BRIDGE_URL", ""),
                bridge_token=kwargs.get("tdx_bridge_token")
                or os.getenv("TDX_BRIDGE_TOKEN", ""),
                account=kwargs.get("tdx_account") or os.getenv("TDX_ACCOUNT", ""),
                account_type=kwargs.get("tdx_account_type")
                or os.getenv("TDX_ACCOUNT_TYPE", "stock"),
            )
        if broker_type == "qmt_exec":
            # 大 QMT 执行端（big-convert RPC）：账户/桥参数由 qmt_exec_client 统一解析
            return QmtExecBroker(
                account_id=kwargs.get("qmt_exec_account_id") or "",
                account_type=kwargs.get("qmt_exec_account_type") or "STOCK",
                strategy_name=kwargs.get("qmt_exec_strategy_name") or "",
                timeout=float(kwargs.get("qmt_exec_timeout") or 0),
            )
        if broker_type in ("tiger", "futu", "ib"):
            # 海外券商（港/美/期货实盘）：SDK 懒加载，密钥见 overseas_brokers 模块注释
            from backend.services.trade.services.overseas_brokers import (
                get_overseas_broker,
            )

            return get_overseas_broker(broker_type)
        raise ValueError(
            f"[create_broker] 未知 broker_type='{broker_type}'，"
            "有效值: 'bridge'（默认）, 'redis', 'qmt', 'tdx', 'qmt_exec', 'tiger', 'futu', 'ib'。"
            "请检查 REAL_BROKER_TYPE 环境变量配置。"
        )

    # Inject Simulation Manager
    from backend.services.trade_shared.simulation_manager import (
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
