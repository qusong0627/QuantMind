"""
Synthetic execution engine for simulation orders.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

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
from backend.shared.freshness import UNAVAILABLE, quote_policy
from backend.shared.utc_datetime import utc_now
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
        transfer_fee: float = 0.0,
        market: str = "CN",
        account_snapshot: dict | None = None,
        price_source: str | None = None,
        requested_quantity: float | None = None,
        quote_timestamp: int | None = None,
        quote_age_seconds: float | None = None,
        message: str = "",
    ):
        self.success = success
        self.price = price
        self.quantity = quantity
        self.commission = commission
        self.stamp_duty = stamp_duty
        self.transfer_fee = transfer_fee
        self.market = market
        self.account_snapshot = account_snapshot
        self.price_source = price_source
        self.requested_quantity = requested_quantity
        self.quote_timestamp = quote_timestamp
        self.quote_age_seconds = quote_age_seconds
        self.message = message


@dataclass
class ResolvedFill:
    """取价契约结果（T-P2-03）：ok=False 时 message 为拒单原因。

    snapshot 携带原始行情快照（下游涨跌停钳制需要 limit_up/down_price）。
    """

    ok: bool
    price: float = 0.0
    source: str = ""
    degraded: bool = False  # True=用了非实时价（bar/陈旧快照），已如实标注并告警
    message: str = ""
    snapshot: Any = None


_SH_TZ = ZoneInfo("Asia/Shanghai")


def _resolve_match_session(now: datetime | None = None, market: Any = "CN") -> str:
    """当前墙钟对应的撮合会话（T-P2-07：墙钟→会话的唯一推导点）。

    盘后固定价格（15:05–15:30 按收盘价）是 **A股专属制度**（2026-07-06 新规）——
    T-P3-07：仅 CN 市场启用；美股/港股等回落 regular 连续会话（本函数语义），
    不误用收盘价固定成交。仅墙钟驱动的模拟执行路径使用（托管/手动/挂单重试）；
    replay/回测按历史日线驱动，不按墙钟，不经本函数。
    """
    from backend.shared.market_sessions import normalize_market_key
    from backend.services.simulation.services.market_rules import session_for_time

    if normalize_market_key(market) != "CN":
        from backend.services.simulation.services.market_rules import (
            SESSION_CONTINUOUS,
        )

        return SESSION_CONTINUOUS
    return session_for_time(now or datetime.now(_SH_TZ))


@dataclass
class MarketSnapshot:
    price: float
    price_source: str
    quote_timestamp: int | None = None
    quote_age_seconds: float | None = None
    recent_volume: float | None = None
    limit_up: bool = False
    limit_down: bool = False
    suspended: bool = False
    limit_up_price: float | None = None
    limit_down_price: float | None = None


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
    def _quote_age_seconds(data: dict[str, Any]) -> float | None:
        raw = (
            data.get("timestamp")
            or data.get("quote_timestamp")
            or data.get("updated_at")
            or data.get("update_time")
        )
        if raw is None:
            return None
        try:
            if isinstance(raw, (int, float)) or str(raw).replace(".", "", 1).isdigit():
                ts = float(raw)
                if ts > 10_000_000_000:
                    ts /= 1000.0
                return max(0.0, datetime.now(timezone.utc).timestamp() - ts)
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(
                0.0,
                datetime.now(timezone.utc).timestamp() - parsed.timestamp(),
            )
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _is_price_near(price: float, limit_price: float | None) -> bool:
        """成交价是否贴上涨跌停价。

        容差**唯一事实源** = ``local_market_data.TOUCH_TOLERANCE``。原先容差是
        默认参数（``tolerance: float = 0.0015``），8 处同值硬编码之一 —— 已去掉该
        参数：默认参数在**定义期**求值，要接权威常量就得在导入期导入行情栈重模块
        （实测多花 3.5s 且会拉起 LiteLLM 远程价目表抓取），不划算；而留着默认值
        等于又开了一个「调用方可传旧口径」的口子。全库无调用方传过该参数。
        """
        from backend.services.simulation.services.local_market_data import (
            TOUCH_TOLERANCE,
        )

        if limit_price is None or limit_price <= 0 or price <= 0:
            return False
        return abs(price - limit_price) / max(limit_price, 1e-6) <= TOUCH_TOLERANCE

    @classmethod
    def _board_limit_threshold(cls, symbol: str) -> float:
        """按标的返回涨跌停判定阈值（ask1/bid1缺失时的兜底）。

        口径**唯一事实源** = ``local_market_data.limit_pct``：主板 10%、创业板/科创
        20%、北交所 30%、ST 主板 5%→10%（2026-07-06 切换）、创业板 2020-08-24
        注册制改革全在那里。旧实现自己复述板块常量，并且 ``except: return 0.095``
        对**任何**失败都退回主板 10% —— 创业板/北交所的真实 20%/30% 被压成 10%，
        于是一根 12% 的普通阳线被误判成涨停。

        ``is_st`` 默认 False：本路径拿不到逐日 ST 口径（同 ``cn_exchange`` 的
        已知缺口）。ST 主板 5% 保护期只到 2026-07-06，此后 ST 主板同为 10%，
        故默认值对当日及以后正确。
        """
        from datetime import date as _date

        # 阈值（板别 − 取整容差）—— 唯一事实源，本类不再持有容差副本，
        # 也不再自己挑容差：北交所截尾取整、容差翻倍，这件事只有事实源知道。
        from backend.services.simulation.services.local_market_data import (
            limit_threshold,
        )

        pct = 0.095  # fidelity: allow-limit-threshold — 兜底按最严主板线（10% − 0.5pp）
        try:
            pct = limit_threshold(
                symbol,
                # 只取「今天」，而今天已 ≥ 2026-07-06 —— 该日起 ST 主板同为
                # 10%，is_st 不再改变结果。判历史日期的路径不适用此豁免。
                is_st=False,  # fidelity: allow-limit-threshold — 只取今天
                trade_date=_date.today(),
            )
        except Exception:
            # 不静默：退回主板口径会让宽板的涨停判定失真（见 docstring）
            logger.warning(
                "涨跌停板规解析失败，按主板 10% 兜底 (symbol=%s)", symbol, exc_info=True
            )
        return pct

    @staticmethod
    def _enrich_cn_limits(
        symbol: str, price: float
    ) -> tuple[bool, bool, bool, float | None, float | None]:
        """用本地日线为L0/L2等缺风控字段的价格源补齐涨跌停/停牌信息。

        返回 (limit_up, limit_down, suspended, limit_up_price, limit_down_price)。
        任何失败返回全False，保证撮合不因 enrichment 崩。
        """
        try:
            from datetime import date as _date

            from backend.services.simulation.services.local_market_data import (
                TOUCH_TOLERANCE,
                get_local_market_data,
            )
            from backend.services.simulation.services.market_rules import infer_market

            mkt = infer_market(symbol).value if symbol else "CN"
            if mkt != "CN":
                return False, False, False, None, None
            lmd = get_local_market_data(market=mkt)
            bar = None
            for d in [_date.today(), lmd.latest_trade_date()]:
                if d is None:
                    continue
                try:
                    bar = lmd.get_bar(symbol, d)
                except Exception:
                    bar = None
                if bar is not None:
                    break
            if bar is None:
                return False, False, False, None, None
            if getattr(bar, "suspended", False):
                return False, False, True, None, None
            limit_up_price = float(getattr(bar, "limit_up", 0) or 0) or None
            limit_down_price = float(getattr(bar, "limit_down", 0) or 0) or None
            # 无穷大表示无限制（如新股首日），按无涨跌停处理
            if limit_up_price is not None and limit_up_price == float("inf"):
                limit_up_price = None
            if limit_down_price is not None and limit_down_price <= 0:
                limit_down_price = None
            limit_up = bool(
                limit_up_price
                and price > 0
                and price >= limit_up_price * (1 - TOUCH_TOLERANCE)
            )
            limit_down = bool(
                limit_down_price
                and price > 0
                and price <= limit_down_price * (1 + TOUCH_TOLERANCE)
            )
            return limit_up, limit_down, False, limit_up_price, limit_down_price
        except Exception:
            return False, False, False, None, None

    def market_snapshot_from_tick(
        self,
        symbol: str,
        tick: dict[str, Any],
    ) -> MarketSnapshot:
        """Build the shared execution snapshot used by manual and hosted orders."""
        price = float(tick.get("price") or 0.0)
        lu, ld, suspended, lu_price, ld_price = self._enrich_cn_limits(
            symbol, price
        )
        return MarketSnapshot(
            price=price,
            price_source="redis_series",
            quote_timestamp=self._as_int(tick.get("timestamp")),
            quote_age_seconds=self._as_float(tick.get("age_s")),
            recent_volume=self._as_float(tick.get("recent_volume")),
            limit_up=lu,
            limit_down=ld,
            suspended=suspended,
            limit_up_price=lu_price,
            limit_down_price=ld_price,
        )

    def _reserve_liquidity(
        self,
        *,
        symbol: str,
        requested: float,
        capacity: float,
        quote_timestamp: int | None,
    ) -> float:
        """Atomically reserve a symbol's participation capacity per quote window."""
        client = getattr(getattr(self.manager, "redis", None), "client", None)
        if client is None or capacity <= 0:
            return min(requested, capacity)
        import os

        try:
            window = max(1, int(os.getenv("SIM_LIQUIDITY_WINDOW_SEC", "60")))
        except ValueError:
            window = 60
        ts = int(quote_timestamp or datetime.now(timezone.utc).timestamp())
        bucket = ts // window
        key = f"simulation:liquidity:{symbol}:{bucket}"
        script = """
local key = KEYS[1]
local requested = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local used = tonumber(redis.call("GET", key) or "0")
local remaining = math.max(0, capacity - used)
local granted = math.min(requested, remaining)
redis.call("SET", key, used + granted, "EX", ttl)
return tostring(granted)
"""
        try:
            return max(
                0.0,
                float(
                    client.eval(
                        script,
                        1,
                        key,
                        str(requested),
                        str(capacity),
                        str(window * 2),
                    )
                ),
            )
        except Exception as exc:
            logger.warning("liquidity reservation failed for %s: %s", symbol, exc)
            return min(requested, capacity)

    async def _latest_price(
        self,
        symbol: str,
        *,
        user_id: int | None = None,
        tenant_id: str | None = None,
    ) -> MarketSnapshot:
        market_url = settings.MARKET_DATA_SERVICE_URL.rstrip("/")
        endpoint = f"{market_url}/api/v1/quotes/{symbol}"

        # Level 0: Redis 实时行情（market:series ZSET）— 模拟撮合第一价格源。
        # 盘中 tick 新鲜时直接按 Redis 现价成交；陈旧/缺失则走下方兜底链路。
        try:
            from backend.services.simulation.services.redis_series_quote import (
                fetch_series_ticks,
            )

            tick = (await fetch_series_ticks([symbol])).get(symbol)
            if tick:
                logger.info(
                    "Redis series price for %s: %.4f (age=%.0fs)",
                    symbol,
                    tick["price"],
                    tick["age_s"],
                )
                return self.market_snapshot_from_tick(symbol, tick)
        except Exception as e:
            logger.warning("Failed to fetch redis series quote for %s: %s", symbol, e)

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
                age_seconds = self._quote_age_seconds(data)
                # 新鲜度口径唯一走 shared.freshness（T-P6-05）；unavailable 视为无行情
                if quote_policy().classify(age_seconds) == UNAVAILABLE:
                    logger.warning(
                        "Market quote for %s missing/stale timestamp; rejected",
                        symbol,
                    )
                    data = {}
                px = self._as_float(data.get("current_price") or data.get("last_price"))
                if px and px > 0:
                    limit_up = self._as_bool(data.get("is_limit_up"))
                    limit_down = self._as_bool(data.get("is_limit_down"))
                    suspended = self._as_bool(
                        data.get("suspended") or data.get("is_suspended")
                    )
                    limit_up_price = self._as_float(data.get("limit_up_today"))
                    limit_down_price = self._as_float(data.get("limit_down_today"))
                    if not limit_up and self._is_price_near(px, limit_up_price):
                        limit_up = True
                    if not limit_down and self._is_price_near(px, limit_down_price):
                        limit_down = True

                    pre_close = self._as_float(
                        data.get("pre_close") or data.get("close_price")
                    )
                    ask1_volume = self._as_int(data.get("ask1_volume"))
                    bid1_volume = self._as_int(data.get("bid1_volume"))
                    if pre_close and pre_close > 0:
                        change_ratio = (px - pre_close) / pre_close
                        threshold = self._board_limit_threshold(symbol)
                        if (
                            not limit_up
                            and ask1_volume is not None
                            and ask1_volume <= 0
                            and change_ratio >= threshold
                        ):
                            limit_up = True
                        if (
                            not limit_down
                            and bid1_volume is not None
                            and bid1_volume <= 0
                            and change_ratio <= -threshold
                        ):
                            limit_down = True

                    return MarketSnapshot(
                        price=px,
                        price_source="market_data_service",
                        quote_age_seconds=age_seconds,
                        limit_up=limit_up,
                        limit_down=limit_down,
                        suspended=suspended,
                        limit_up_price=limit_up_price,
                        limit_down_price=limit_down_price,
                    )
        except Exception as e:
            logger.warning("Failed to fetch market quote for %s: %s", symbol, e)

        # Executable orders require a fresh realtime quote. Historical daily
        # data remains available to valuation code, but is never tradable.
        return MarketSnapshot(price=0.0, price_source="realtime_quote_unavailable")

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
                    logger.info(
                        "Fallback to DB nominal price for %s: %s", symbol, price
                    )
                    lu, ld, susp, lu_px, ld_px = self._enrich_cn_limits(symbol, price)
                    return MarketSnapshot(
                        price=price,
                        price_source="db_fallback",
                        limit_up=lu,
                        limit_down=ld,
                        suspended=susp,
                        limit_up_price=lu_px,
                        limit_down_price=ld_px,
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
                legacy_result = await self.db.execute(
                    query_legacy, {"symbol": db_symbol}
                )
                legacy_row = legacy_result.fetchone()
                if legacy_row:
                    hfq_close = float(legacy_row[0])
                    adj_factor = float(legacy_row[1] or 1.0)
                    price = hfq_close / adj_factor if adj_factor > 0 else hfq_close
                    logger.info(
                        "Fallback to DB legacy nominal price for %s: %s", symbol, price
                    )
                    lu, ld, susp, lu_px, ld_px = self._enrich_cn_limits(symbol, price)
                    return MarketSnapshot(
                        price=price,
                        price_source="db_fallback",
                        limit_up=lu,
                        limit_down=ld,
                        suspended=susp,
                        limit_up_price=lu_px,
                        limit_down_price=ld_px,
                    )
        except Exception as e:
            logger.error("Database fallback failed for %s: %s", symbol, e)

        # Level 2.5: 本地日线兜底（QuantDB parquet）— Redis 不可用时以开盘价撮合
        # 模拟盘核心兜底：直读本地不复权日线，用开盘价作为撮合价，不依赖实时流
        def _local_daily_snapshot() -> MarketSnapshot | None:
            from backend.services.simulation.services.local_market_data import (
                TOUCH_TOLERANCE,
                get_local_market_data,
            )
            from backend.services.simulation.services.market_rules import infer_market
            from datetime import date as _date

            mkt = infer_market(symbol).value if symbol else "CN"
            lmd = get_local_market_data(market=mkt)
            # 优先当日，其次最近交易日
            for d in [_date.today(), lmd.latest_trade_date()]:
                if d is None:
                    continue
                bar = lmd.get_bar(symbol, d)
                if bar and bar.open > 0:
                    logger.info(
                        "Fallback to LocalMarketData open for %s %s: open=%s",
                        symbol,
                        d,
                        bar.open,
                    )
                    px = float(bar.open)
                    lu = (
                        bool(px >= float(bar.limit_up or 0) * (1 - TOUCH_TOLERANCE))
                        if (bar.limit_up and bar.limit_up != float("inf"))
                        else False
                    )
                    ld = (
                        bool(px <= float(bar.limit_down or 0) * (1 + TOUCH_TOLERANCE))
                        if (bar.limit_down and bar.limit_down > 0)
                        else False
                    )
                    return MarketSnapshot(
                        price=px,
                        price_source="local_daily_open",
                        limit_up=lu,
                        limit_down=ld,
                        suspended=bool(getattr(bar, "suspended", False)),
                        limit_up_price=float(bar.limit_up)
                        if bar.limit_up != float("inf")
                        else None,
                        limit_down_price=float(bar.limit_down)
                        if bar.limit_down > 0
                        else None,
                    )
                if bar and bar.close > 0:
                    logger.info(
                        "Fallback to LocalMarketData close for %s %s: close=%s",
                        symbol,
                        d,
                        bar.close,
                    )
                    px = float(bar.close)
                    lu = (
                        bool(px >= float(bar.limit_up or 0) * (1 - TOUCH_TOLERANCE))
                        if (bar.limit_up and bar.limit_up != float("inf"))
                        else False
                    )
                    ld = (
                        bool(px <= float(bar.limit_down or 0) * (1 + TOUCH_TOLERANCE))
                        if (bar.limit_down and bar.limit_down > 0)
                        else False
                    )
                    return MarketSnapshot(
                        price=px,
                        price_source="local_daily_close",
                        limit_up=lu,
                        limit_down=ld,
                        suspended=bool(getattr(bar, "suspended", False)),
                        limit_up_price=float(bar.limit_up)
                        if bar.limit_up != float("inf")
                        else None,
                        limit_down_price=float(bar.limit_down)
                        if bar.limit_down > 0
                        else None,
                    )
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

    # 取价契约（T-P2-03）：新鲜实时源 vs 陈旧/兜底源
    _FRESH_PRICE_SOURCES = frozenset({"redis_series", "market_data_service"})

    async def _resolve_fill_price(
        self,
        order: SimOrder,
        bar: Any,
        *,
        strict_market: bool,
        snapshot: MarketSnapshot | None = None,
    ) -> ResolvedFill:
        """**取价契约唯一实现**：两执行路径共用（守卫/降级/标注单实现）。

        规则：
        1. L0/L1 新鲜实时价 → 直接使用（source 如实）；
        2. strict（手动即时市价单，保持 P0-5 语义）遇非实时 → 拒单；
        3. 否则降级：优先 bar（自带 trade_date，如实标注 today_bar_close / prev_close_bar，
           绝不静默按昨收成交——`[RULE:PRICE-STALE]` WARNING 点名），bar 无则用陈旧快照价；
        4. 全无 → 拒单。
        """
        snapshot = snapshot or await self._latest_price(
            order.symbol,
            user_id=order.user_id,
            tenant_id=order.tenant_id,
        )
        price = float(getattr(snapshot, "price", 0.0) or 0.0)
        source = str(getattr(snapshot, "price_source", "") or "unavailable")
        is_market = getattr(order, "order_type", None) == OrderType.MARKET

        if price > 0 and source in self._FRESH_PRICE_SOURCES:
            return ResolvedFill(ok=True, price=price, source=source, snapshot=snapshot)

        if strict_market and is_market:
            if price <= 0 or source == "unavailable":
                return ResolvedFill(
                    ok=False,
                    message=f"无法获取 {order.symbol} 实时行情，模拟单拒绝成交",
                )
            return ResolvedFill(
                ok=False,
                message=(
                    f"{order.symbol} 当前为非实时行情({source})，"
                    "市价单拒绝成交，请用限价单或盘中再试"
                ),
            )

        bar_price = 0.0
        if bar is not None:
            from backend.services.simulation.services.ashare_matcher import (
                _pick_price as _matcher_pick_price,
            )

            try:
                bar_price = float(_matcher_pick_price(bar, "close") or 0.0)
            except Exception:  # noqa: BLE001
                bar_price = 0.0
        if bar_price > 0:
            is_today_bar = getattr(bar, "trade_date", None) == datetime.now().date()
            bar_source = "today_bar_close" if is_today_bar else "prev_close_bar"
            from backend.shared.errfmt import locate

            logger.warning(
                locate(
                    "RULE:PRICE-STALE",
                    f"{order.symbol} 实时价不可用({source})，按 {bar_source}={bar_price:.4f} 成交",
                    ref=str(getattr(order, "order_id", "") or ""),
                    where="execution_engine.py:_resolve_fill_price",
                )
            )
            return ResolvedFill(
                ok=True, price=bar_price, source=bar_source, degraded=True, snapshot=snapshot
            )
        if price > 0:
            return ResolvedFill(
                ok=True, price=price, source=source, degraded=True, snapshot=snapshot
            )
        return ResolvedFill(
            ok=False, message=f"无法获取 {order.symbol} 行情，模拟单拒绝成交"
        )

    async def execute_from_bar(
        self,
        order: SimOrder,
        bar: Any,
        market: str | None = None,
    ) -> ExecutionResult:
        """按当日不复权日 K 走 ashare_matcher（托管/周期调仓与回放同口径）。

        T-P2-03：成交价先经 `_resolve_fill_price` 解析（实时链优先，bar 降级如实标注），
        解析价以 external_price 喂给撮合；滑点/涨跌停钳制不变。
        """
        from backend.services.simulation.services.ashare_matcher import (
            MatchConfig,
            match_order,
        )
        from backend.services.simulation.services.market_rules import (
            infer_market,
            lot_size_for_symbol,
            rules_for,
        )

        rules = rules_for(market or infer_market(order.symbol))
        market_str = rules.market.value
        account_snapshot = await self.manager.get_account(
            order.user_id, tenant_id=order.tenant_id, market=market_str
        )
        side = str(order.side.value).lower()
        available_volume = None
        if side == "sell" and isinstance(account_snapshot, dict):
            positions = account_snapshot.get("positions") or {}
            pos = positions.get(order.symbol)
            if pos is None:
                from backend.shared.stock_utils import StockCodeUtil

                pos = positions.get(StockCodeUtil.to_suffix(order.symbol)) or positions.get(
                    StockCodeUtil.to_prefix(order.symbol)
                )
            if isinstance(pos, dict):
                avail = pos.get("available_volume")
                available_volume = (
                    float(pos.get("volume", 0) or 0)
                    if avail is None
                    else float(avail)
                )

        # T-P2-03：取价契约——实时链优先，bar 降级如实标注（修复"盘中按昨收成交"）
        resolved = await self._resolve_fill_price(order, bar, strict_market=False)
        if not resolved.ok:
            return ExecutionResult(
                success=False, message=resolved.message, price_source=resolved.source or None
            )

        cfg = MatchConfig(
            price_mode="close",
            slippage_bps=float(settings.SIMULATION_SLIPPAGE_BPS),
            commission_rate=float(settings.SIMULATION_COMMISSION_RATE),
            commission_min=float(settings.SIMULATION_COMMISSION_MIN),
            stamp_duty_rate=float(settings.SIMULATION_STAMP_DUTY_RATE),
            lot_size=lot_size_for_symbol(order.symbol, rules.market),
            external_price=resolved.price,
            session=_resolve_match_session(market=rules.market.value),
        )
        mr = match_order(
            side=side,
            quantity=int(order.quantity or 0),
            bar=bar,
            cfg=cfg,
            available_volume=available_volume,
        )
        if not mr.success:
            return ExecutionResult(success=False, message=mr.reason)

        gross = mr.fill_quantity * mr.fill_price
        if side == "buy":
            delta_cash = -(gross + mr.total_fee)
            delta_volume = mr.fill_quantity
        else:
            delta_cash = gross - mr.total_fee
            delta_volume = -mr.fill_quantity

        update = await self.manager.update_balance(
            user_id=order.user_id,
            symbol=order.symbol,
            delta_cash=delta_cash,
            delta_volume=delta_volume,
            price=mr.fill_price,
            tenant_id=order.tenant_id,
            market=rules.market.value,
            t_plus_1=rules.t_plus_1,
        )
        if not update.get("success"):
            reason = update.get("reason", "BALANCE_UPDATE_FAILED")
            return ExecutionResult(
                success=False, message=f"Balance update failed: {reason}"
            )

        return ExecutionResult(
            success=True,
            price=mr.fill_price,
            quantity=mr.fill_quantity,
            commission=mr.commission,
            stamp_duty=mr.stamp_duty,
            transfer_fee=mr.transfer_fee,
            market=market_str,
            account_snapshot=account_snapshot,
            price_source=resolved.source,
        )

    async def execute_order(
        self,
        order: SimOrder,
        market: str | None = None,
        snapshot: MarketSnapshot | None = None,
        requested_quantity: float | None = None,
        *,
        strict_market: bool = True,
    ) -> ExecutionResult:
        # T-P2-03：价格与陈旧价守卫收敛到 _resolve_fill_price（唯一实现）；
        # strict_market=True（手动即时单，默认）保持 P0-5 语义——非实时市价单拒绝成交；
        # 自动化路径（沙箱/TDX/由 Router 传入 False）允许如实标注的降级。
        resolved = await self._resolve_fill_price(
            order, None, strict_market=strict_market, snapshot=snapshot
        )
        if not resolved.ok:
            return ExecutionResult(
                success=False,
                message=resolved.message,
                price_source=resolved.source or None,
            )
        base_price = resolved.price
        fetched_source = resolved.source
        snapshot = resolved.snapshot  # 下游涨跌停钳制/流动性约束需要 limit_up/down_price 与 recent_volume

        slippage = settings.SIMULATION_SLIPPAGE_BPS / 10000

        # 市场规则：由标的代码推断（信号/订单来自同一市场），佣金、
        # 印花税、T+1 语义均按市场区分。
        from backend.services.simulation.services.market_rules import (
            infer_market,
            rules_for,
        )

        rules = rules_for(market or infer_market(order.symbol))
        market_str = rules.market.value
        requested_qty = float(
            requested_quantity
            if requested_quantity is not None
            else order.quantity
        )
        if requested_qty <= 0:
            return ExecutionResult(success=False, message="quantity must be > 0")

        # 记录更新前的账户快照：供 apply_filled 落库失败时补偿恢复，
        # 保证 Redis 余额/持仓与 sim_orders/sim_trades 的数据一致性（T+1 语义下无法用反向增减安全回退）。
        account_snapshot = await self.manager.get_account(
            order.user_id, tenant_id=order.tenant_id, market=market_str
        )

        side = str(order.side.value).lower()
        if snapshot.suspended:
            return ExecutionResult(
                success=False, message="Security is suspended, cannot trade"
            )
        if side == "buy" and snapshot.limit_up:
            return ExecutionResult(
                success=False, message="Limit-up locked, buy order cannot be filled"
            )
        if side == "sell" and snapshot.limit_down:
            return ExecutionResult(
                success=False, message="Limit-down locked, sell order cannot be filled"
            )

        # T-P2-07：会话解析上移（申报上限依赖会话：盘后固定价格统一 100 万股上限）
        from backend.services.simulation.services.market_rules import (
            SESSION_AFTER_HOURS_FIXED,
        )

        after_hours_fixed = (
            _resolve_match_session(market=rules.market.value) == SESSION_AFTER_HOURS_FIXED
        )

        # A股申报数量校验（T-P2-02：唯一实现）——整数股全员；买入另需整手
        # （科创板≥200 可 1 股递增，其余 100 整数倍；卖出可零股）。
        if rules.market.value == "CN":
            from backend.services.simulation.services.market_rules import (
                normalize_order_quantity,
            )

            qty = requested_qty
            if abs(qty - round(qty)) > 1e-6:
                return ExecutionResult(
                    success=False,
                    message=f"CN委托数量须为整数股，当前{order.quantity}",
                )
            if side == "buy":
                normalized = normalize_order_quantity(qty, order.symbol, "CN")
                if normalized <= 0 or normalized != int(round(qty)):
                    return ExecutionResult(
                        success=False,
                        message=(
                            f"买入申报数量不合规（{order.symbol}: {order.quantity}；"
                            "科创板≥200可1股递增，其余100整数倍）"
                        ),
                    )

        # 单笔申报数量上限（2026-07-06 新规；唯一实现；买卖双侧）
        if rules.market.value == "CN":
            from backend.services.simulation.services.market_rules import (
                order_quantity_cap,
            )

            cap = order_quantity_cap(
                order.symbol,
                order_type=getattr(
                    getattr(order, "order_type", None), "value", None
                ),
                session=SESSION_AFTER_HOURS_FIXED if after_hours_fixed else None,
                market=rules.market,
            )
            if cap is not None and float(order.quantity or 0) > cap:
                return ExecutionResult(
                    success=False,
                    message=(
                        f"单笔申报数量超过上限（{order.symbol}: {order.quantity} > {cap} 股；"
                        "主板/创业板限价30万·市价15万，科创板限价10万·市价5万，盘后100万股）"
                    ),
                )

        # T-P2-07：盘后固定价格会话 —— 市价单成交价=收盘价（取价链结果）、无滑点；
        # 限价分支现语义（买<市价拒/卖>市价拒/按市价成交）即盘后申报价有效性规则本身，零改动。
        # （会话判定已上移至申报数量校验之前——申报上限依赖会话）
        if after_hours_fixed:
            logger.info(
                "[RULE:AFTER-HOURS] %s 盘后固定价格会话下单（基准价 %.4f）order_type=%s",
                order.symbol,
                base_price,
                getattr(order.order_type, "value", order.order_type),
            )

        if order.order_type == OrderType.MARKET:
            if after_hours_fixed:
                exec_price = round(base_price, 2)
            else:
                direction = 1 if side == "buy" else -1
                exec_price = round(base_price * (1 + direction * slippage), 2)
            # 市价滑点不得冲破涨跌停：钳制到日内限价内
            if snapshot.limit_up_price and exec_price > snapshot.limit_up_price:
                exec_price = round(float(snapshot.limit_up_price), 2)
            if snapshot.limit_down_price and exec_price < snapshot.limit_down_price:
                exec_price = round(float(snapshot.limit_down_price), 2)
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
            exec_price = round(float(base_price), 2)
            price_source = fetched_source
        else:
            return ExecutionResult(
                success=False, message=f"Unsupported order type: {order.order_type}"
            )

        # ── F2 快照级撮合（T-P6-17，exec_core=snapshot）：盘口新鲜且完整时以盘口深度为准；
        #    不可用（缺失/陈旧/封板排队）自动回退日频核（回退计数可见，绝不静默）。
        book_capacity: float | None = None
        try:
            from backend.services.simulation.services.exec_core import (
                MODE_DAILY,
                MODE_SNAPSHOT,
                resolve_exec_core,
                try_snapshot_fill,
            )

            if not after_hours_fixed and resolve_exec_core() == MODE_SNAPSHOT:
                from backend.services.simulation.services.market_rules import (
                    lot_size_for_symbol,
                )

                lot = int(lot_size_for_symbol(order.symbol, rules.market))
                fill_plan, _book = try_snapshot_fill(
                    symbol=order.symbol,
                    side=side,
                    quantity=requested_qty,
                    order_type=str(getattr(order.order_type, "value", order.order_type)).lower(),
                    limit_price=order.price,
                    lot_size=lot,
                )
                if fill_plan is not None:
                    exec_price = float(fill_plan.fill_price)
                    price_source = "snapshot"
                    book_capacity = float(fill_plan.fill_qty)
                    logger.info(
                        "[RULE:F2-SNAPSHOT] %s %s 盘口撮合 价 %.2f 量 %.0f/%.0f（穿 %d 档%s）",
                        order.symbol, side, exec_price, fill_plan.fill_qty, requested_qty,
                        fill_plan.levels_consumed,
                        ("；" + ",".join(fill_plan.notes)) if fill_plan.notes else "",
                    )
        except Exception as exc:  # noqa: BLE001 - 快照核失败一律回退日频核（不阻断交易主链）
            logger.warning("[RULE:F2-SNAPSHOT] 快照核异常，回退日频核: %s", exc)

        fill_quantity = requested_qty
        if book_capacity is not None:
            # 盘口深度=容量基数；_reserve_liquidity 仍作**跨单防重复消耗**账本（同窗口约束）
            fill_quantity = self._reserve_liquidity(
                symbol=order.symbol,
                requested=requested_qty,
                capacity=book_capacity,
                quote_timestamp=snapshot.quote_timestamp,
            )
            if fill_quantity <= 0:
                return ExecutionResult(
                    success=False,
                    message="insufficient_realtime_liquidity",
                )
        elif snapshot.recent_volume is not None:
            import os

            try:
                participation = min(
                    1.0,
                    max(
                        0.001,
                        float(os.getenv("SIMULATION_MAX_PARTICIPATION_RATE", "0.10")),
                    ),
                )
            except ValueError:
                participation = 0.10
            capacity = max(0.0, float(snapshot.recent_volume) * participation)
            if rules.market.value == "CN" and side == "buy":
                from backend.services.simulation.services.market_rules import (
                    lot_size_for_symbol,
                )

                lot_size = int(lot_size_for_symbol(order.symbol, rules.market))
                capacity = int(capacity // lot_size) * lot_size
            elif rules.market.value == "CN":
                capacity = int(capacity)
            fill_quantity = self._reserve_liquidity(
                symbol=order.symbol,
                requested=requested_qty,
                capacity=capacity,
                quote_timestamp=snapshot.quote_timestamp,
            )
            if fill_quantity <= 0:
                return ExecutionResult(
                    success=False,
                    message="insufficient_realtime_liquidity",
                )
        else:
            import os

            try:
                max_unverified_notional = float(
                    os.getenv("SIM_LIQUIDITY_UNVERIFIED_MAX_NOTIONAL", "100000")
                )
            except ValueError:
                max_unverified_notional = 100000.0
            if requested_qty * exec_price > max_unverified_notional:
                return ExecutionResult(
                    success=False,
                    message="realtime_liquidity_unavailable_for_large_order",
                )

        gross = fill_quantity * exec_price
        if rules.market.value == "CN":
            # T-P2-02：费用分项唯一实现（market_rules.compute_fee_breakdown）；
            # settings 仅作显式覆盖（env/前端可配置语义保持不变）
            commission, stamp_duty, transfer_fee = rules.compute_fee_breakdown(
                fill_quantity,
                exec_price,
                order.side.value,
                commission_rate=float(settings.SIMULATION_COMMISSION_RATE),
                commission_min=float(settings.SIMULATION_COMMISSION_MIN),
                stamp_duty_rate=float(settings.SIMULATION_STAMP_DUTY_RATE),
            )
        else:
            # 非 CN：同一实现（印花税独立分项——现金合计不变、sim_trades 分项更正确）
            commission, stamp_duty, transfer_fee = rules.compute_fee_breakdown(
                fill_quantity, exec_price, side
            )
        if order.side.value == "buy":
            delta_cash = -(gross + commission + transfer_fee)
            delta_volume = fill_quantity
        else:
            delta_cash = gross - commission - stamp_duty - transfer_fee
            delta_volume = -fill_quantity

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
                return ExecutionResult(
                    success=False, message="Insufficient cash for buy order"
                )
            if reason == "INSUFFICIENT_HOLDINGS":
                return ExecutionResult(
                    success=False, message="Insufficient holdings for sell order"
                )
            if reason == "INSUFFICIENT_AVAILABLE_VOLUME":
                return ExecutionResult(
                    success=False,
                    message=f"Insufficient available volume for sell order (T+1 locked): {update}",
                )
            return ExecutionResult(
                success=False, message=f"Balance update failed: {reason}"
            )

        return ExecutionResult(
            success=True,
            price=exec_price,
            quantity=fill_quantity,
            commission=commission,
            stamp_duty=stamp_duty,
            transfer_fee=transfer_fee,
            market=market_str,
            account_snapshot=account_snapshot,
            price_source=price_source,
            requested_quantity=requested_qty,
            quote_timestamp=snapshot.quote_timestamp,
            quote_age_seconds=snapshot.quote_age_seconds,
            message=(
                "partially_filled"
                if fill_quantity + 1e-6 < requested_qty
                else "filled"
            ),
        )

    async def apply_filled(self, order: SimOrder, result: ExecutionResult) -> SimTrade:
        trade_value = result.quantity * result.price
        transfer_fee = float(getattr(result, "transfer_fee", 0.0) or 0.0)
        total_fee = result.commission + result.stamp_duty + transfer_fee
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
            transfer_fee=transfer_fee,
            total_fee=total_fee,
            # sim_trades.executed_at 是 TIMESTAMPTZ，必须写 aware UTC。
            # naive UTC 会在旧库 timestamptz 上被 asyncpg 拒绝，整笔成交回滚。
            executed_at=utc_now(),
            price_source=result.price_source,
        )
        self.db.add(trade)
        # Both identifiers are database-generated and are referenced by the
        # ledger/fill projections below.
        await self.db.flush()

        requested_quantity = float(
            result.requested_quantity
            if result.requested_quantity is not None
            else order.quantity
        )
        partial = result.quantity + 1e-6 < requested_quantity
        # Existing PostgreSQL enum has no partially_filled value. A partial DAY
        # order remains pending with filled_quantity > 0 and a queued remainder.
        order.status = OrderStatus.PENDING if partial else OrderStatus.FILLED
        order.submitted_at = order.submitted_at or datetime.now(timezone.utc)
        order.filled_at = None if partial else datetime.now(timezone.utc)
        old_filled_quantity = float(order.filled_quantity or 0.0)
        old_filled_value = float(order.filled_value or 0.0)
        new_filled_quantity = old_filled_quantity + result.quantity
        new_filled_value = old_filled_value + trade_value
        order.filled_quantity = new_filled_quantity
        order.average_price = (
            new_filled_value / new_filled_quantity
            if new_filled_quantity > 0
            else result.price
        )
        order.filled_value = new_filled_value
        order.commission = float(order.commission or 0.0) + result.commission
        order.total_fee = float(order.total_fee or 0.0) + total_fee
        # 委托金额以实际成交金额为准（市价单无委托价，此前 quantity*(price or 0)=0 失真）
        order.order_value = trade_value
        order.execution_model = (
            "snapshot_core" if result.price_source == "snapshot" else "synthetic_price"
        )
        order.price_source = result.price_source
        audit = (
            f"quote_ts={result.quote_timestamp or 'unknown'} "
            f"quote_age_s={float(result.quote_age_seconds or 0.0):.1f}"
        )
        order.remarks = f"{order.remarks or ''} [{audit}]".strip()[:500]

        # live 成交同步写入 ledger 台账（持仓批次/资金流水/账户），与 SimTrade 同事务。
        # 此前 live 路径从不写台账，PG 侧 lots/accounts 全空，EOD/策略监控等读 PG
        # 处全部归零（"回到初始状态"）。失败随主事务回滚（已有 Redis 恢复兜底）。
        try:
            from backend.services.simulation.services.ledger_service import (
                SimulationLedgerService,
            )

            # T-P1-04：台账列自愈（market 维度）+ 成交落市场
            from backend.shared.ledger_contract import (
                ensure_ledger_contract_columns_async,
            )

            await ensure_ledger_contract_columns_async()
            ledger = SimulationLedgerService(self.db)
            before_snapshot = (
                dict(result.account_snapshot)
                if isinstance(result.account_snapshot, dict)
                else {}
            )
            await ledger.record_trade(
                order=order,
                trade=trade,
                account_snapshot=before_snapshot,
                market=str(getattr(result, "market", None) or "CN"),
            )
            from sqlalchemy import select

            from backend.services.simulation.models.fill import SimulationFill
            from backend.services.simulation.models.order_v2 import (
                SimulationOrderV2,
            )

            projection_order = (
                await self.db.execute(
                    select(SimulationOrderV2)
                    .where(SimulationOrderV2.order_id == order.order_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if projection_order is not None:
                if partial:
                    projection_order.quantity = max(
                        0.0, requested_quantity - result.quantity
                    )
                    projection_order.status = OrderStatus.PENDING.value
                    projection_order.rejected_reason = (
                        "partially_filled; remainder queued for current DAY session"
                    )
                else:
                    projection_order.quantity = 0.0
                    projection_order.status = OrderStatus.FILLED.value
                    projection_order.rejected_reason = None
                self.db.add(
                    SimulationFill(
                        fill_id=trade.trade_id,
                        order_id=order.order_id,
                        legacy_trade_id=trade.id,
                        tenant_id=str(order.tenant_id),
                        user_id=str(order.user_id),
                        account_id=projection_order.account_id,
                        strategy_id=projection_order.strategy_id,
                        portfolio_id=int(order.portfolio_id or 0),
                        symbol=order.symbol,
                        side=str(order.side.value),
                        position_side=str(
                            getattr(order, "position_side", "long") or "long"
                        ),
                        trade_action=getattr(order, "trade_action", None),
                        fill_price=result.price,
                        fill_quantity=result.quantity,
                        gross_amount=trade_value,
                        commission=result.commission,
                        stamp_duty=result.stamp_duty,
                        transfer_fee=transfer_fee,
                        executed_at=utc_now().replace(tzinfo=None),
                        price_source=result.price_source,
                    )
                )
        except Exception as ledger_exc:  # noqa: BLE001
            from backend.shared.errfmt import locate

            logger.error(
                locate(
                    "CONTRACT:LEDGER",
                    f"Sim ledger record failed: {ledger_exc}",
                    ref=str(order.order_id or ""),
                    where="execution_engine.py:apply_filled",
                ),
                exc_info=True,
            )
            raise

        try:
            await self.db.commit()
        except Exception:
            # #5 兜底：DB 落成交失败时，Redis 账户已在 execute_order 被扣款/加仓，
            # 此处回滚 DB 并把 Redis 账户恢复到执行前快照，避免资金与订单不一致。
            # 禁止删键：PG 为主、Redis 只是缓存，删键会丢持仓；无快照时从 PG 自愈。
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
                        healed = await self.manager.get_account(
                            order.user_id,
                            tenant_id=order.tenant_id,
                            market=result.market,
                        )
                        if not healed:
                            logger.error(
                                "Sim account missing and PG has no history, cannot restore: %s",
                                key,
                            )
                except Exception as restore_err:  # noqa: BLE001
                    logger.error(
                        "Failed to restore sim account after commit failure: %s",
                        restore_err,
                        exc_info=True,
                    )
            from backend.shared.errfmt import locate

            logger.error(
                locate(
                    "CONTRACT:LEDGER",
                    "Sim order apply_filled commit failed; DB rolled back and account restored",
                    ref=str(order.order_id or ""),
                    where="execution_engine.py:apply_filled(commit)",
                ),
                exc_info=True,
            )
            raise

        await self.db.refresh(order)
        await self.db.refresh(trade)
        await self._sync_trade_account(order.tenant_id, order.user_id)
        # 交易时即失效 Redis，下次 GET 立即回源 DB 并回填缓存，实现秒级可见
        try:
            if self.manager.redis and self.manager.redis.client:
                self.manager.redis.delete_pattern(
                    f"sim_trade:list:{order.tenant_id}:{order.user_id}:*"
                )
                self.manager.redis.delete_pattern(
                    f"sim_trade:stats:{order.tenant_id}:{order.user_id}:*"
                )
                try:
                    from backend.services.trade_shared.utils.redis_cache import (
                        invalidate_user_cache as _invalidate_user_cache,
                    )

                    _invalidate_user_cache(
                        order.tenant_id,
                        order.user_id,
                        func_names=["get_status", "get_orders"],
                    )
                except Exception:
                    pass
        except Exception:
            pass
        return trade

    async def _set_projection_status(
        self, order: SimOrder, status: str, reason: str | None = None
    ) -> None:
        from sqlalchemy import update

        from backend.services.simulation.models.order_v2 import SimulationOrderV2

        order_id = getattr(order, "order_id", None)
        if order_id is None:
            return
        await self.db.execute(
            update(SimulationOrderV2)
            .where(SimulationOrderV2.order_id == order_id)
            .values(status=status, rejected_reason=(reason or None))
        )

    async def mark_rejected(self, order: SimOrder, message: str):
        """V2挂单拒单兼容（同 mark_expired 口径）。

        - V2 投影（SimulationOrderV2）用字符串状态；旧 SimOrder 用枚举。
        - 瞬态对象（内存态运行时单）不容崩：commit 无物可落属预期，
          refresh 仅对持久对象有意义。此前无条件 refresh 致线上 worker 每轮
          InvalidRequestError("not persistent")——订单永久卡 submitted、拒因丢失。
        - 投影直写经 `_set_projection_status`（引擎内唯一出口，与 worker 侧镜像等价）。
        """
        try:
            from backend.services.simulation.models.order_v2 import (
                SimulationOrderV2,
            )

            if isinstance(order, SimulationOrderV2):
                order.status = "rejected"
            else:
                order.status = OrderStatus.REJECTED
            if getattr(order, "submitted_at", None) is None and hasattr(
                order, "submitted_at"
            ):
                order.submitted_at = datetime.now(timezone.utc)
            if hasattr(order, "rejected_reason"):
                order.rejected_reason = str(message or "")[:500]
            if hasattr(order, "remarks"):
                order.remarks = f"Execution rejected: {message}"
            await self._set_projection_status(
                order, OrderStatus.REJECTED.value, str(message or "")[:500]
            )
            await self.db.commit()
            try:
                await self.db.refresh(order)
            except Exception:
                pass
        except Exception:
            logger.error("mark_rejected failed", exc_info=True)
            raise

    @staticmethod
    def _normalize_runtime_datetime(value):
        """V2挂单链路兼容：透传datetime/None，避免AttributeError。"""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        try:
            from datetime import datetime as _dt

            return _dt.fromisoformat(str(value))
        except Exception:
            return None

    async def assess_execution_window(self, order, now: datetime | None = None):
        """Enforce the exchange session before any executable price is used."""
        from types import SimpleNamespace
        from zoneinfo import ZoneInfo

        from backend.services.simulation.services.market_rules import infer_market

        market = infer_market(str(getattr(order, "symbol", "") or ""))
        if market.value == "CRYPTO":
            return SimpleNamespace(
                can_execute=True,
                target_trade_date=(now or datetime.now(timezone.utc)).date(),
                final_state=None,
                retryable=False,
                message="ok",
            )

        tz_name = {
            "CN": "Asia/Shanghai",
            "HK": "Asia/Hong_Kong",
            "US": "America/New_York",
            "FUTURES": "Asia/Shanghai",
        }.get(market.value, "Asia/Shanghai")
        local_now = now or datetime.now(ZoneInfo(tz_name))
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=ZoneInfo(tz_name))
        else:
            local_now = local_now.astimezone(ZoneInfo(tz_name))
        today = local_now.date()

        is_session = local_now.weekday() < 5
        calendar = None
        try:
            import pandas as pd
            from exchange_calendars import get_calendar

            calendar_name = {
                "CN": "XSHG",
                "HK": "XHKG",
                "US": "XNYS",
            }.get(market.value)
            if calendar_name:
                calendar = get_calendar(calendar_name)
                is_session = bool(calendar.is_session(pd.Timestamp(today)))
        except Exception:
            calendar = None
        if market.value in {"CN", "HK"}:
            morning_close = (12, 0) if market.value == "HK" else (11, 30)
            afternoon_close = (16, 0) if market.value == "HK" else (15, 0)
            morning = (
                (9, 30) <= (local_now.hour, local_now.minute) < morning_close
            )
            afternoon = (
                (13, 0) <= (local_now.hour, local_now.minute) < afternoon_close
            )
            can_execute = is_session and (morning or afternoon)
        elif market.value == "US":
            can_execute = is_session and (
                (9, 30) <= (local_now.hour, local_now.minute) < (16, 0)
            )
        else:
            can_execute = is_session

        target_date = today
        market_close = (16, 0) if market.value == "HK" else (15, 0)
        if not is_session or (
            market.value in {"CN", "HK"}
            and (local_now.hour, local_now.minute) >= market_close
        ) or (
            market.value == "US" and (local_now.hour, local_now.minute) >= (16, 0)
        ):
            target_date = today
            while True:
                target_date += timedelta(days=1)
                if calendar is not None:
                    import pandas as pd

                    if calendar.is_session(pd.Timestamp(target_date)):
                        break
                elif target_date.weekday() < 5:
                    break

        if can_execute:
            return SimpleNamespace(
                can_execute=True,
                target_trade_date=today,
                final_state=None,
                retryable=False,
                message="ok",
            )

        return SimpleNamespace(
            can_execute=False,
            target_trade_date=target_date,
            final_state=None,
            retryable=True,
            message="queued for next valid session",
        )

    async def mark_expired(self, order, message: str):
        """V2挂单过期兼容：旧SimOrder无EXPIRED枚举，降级为REJECTED；V2投影用字符串expired。"""
        try:
            from backend.services.simulation.models.order_v2 import (
                SimulationOrderV2,
            )

            # 类型分派必须按模型类型判断——OrderStatus 是 str 子类，
            # isinstance(status, str) 恒真（旧写法会把 "expired" 写进 v1 原生枚举列，
            # 该行以后 ORM 读取时 LookupError）。
            if isinstance(order, SimulationOrderV2):
                order.status = "expired"
            else:
                order.status = OrderStatus.REJECTED
            if hasattr(order, "rejected_reason"):
                order.rejected_reason = str(message or "")[:500]
            if hasattr(order, "remarks"):
                order.remarks = f"Execution expired: {message}"
            if getattr(order, "submitted_at", None) is None and hasattr(
                order, "submitted_at"
            ):
                order.submitted_at = datetime.now(timezone.utc)
            await self._set_projection_status(
                order, "expired", str(message or "")[:500]
            )
            await self.db.commit()
            try:
                await self.db.refresh(order)
            except Exception:
                pass
        except Exception:
            logger.error("mark_expired failed", exc_info=True)
            raise

    async def _sync_trade_account(self, tenant_id: str, user_id: int):
        if not self.manager.redis.client:
            return
        account = await self.manager.get_account(user_id, tenant_id=tenant_id)
        if not account:
            return
        payload = dict(account)
        payload.setdefault("timestamp", datetime.now().isoformat())
        write_trade_account_cache(self.manager.redis, tenant_id, user_id, payload)
