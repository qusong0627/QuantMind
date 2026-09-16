"""
Synthetic execution engine for simulation orders.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
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
    def _is_price_near(
        price: float, limit_price: float | None, tolerance: float = 0.0015
    ) -> bool:
        if limit_price is None or limit_price <= 0 or price <= 0:
            return False
        return abs(price - limit_price) / max(limit_price, 1e-6) <= tolerance

    @staticmethod
    def _board_limit_threshold(symbol: str) -> float:
        """按板块返回涨跌停启发式判定阈值（ask1/bid1缺失时的兜底）。

        主板 10% -> 0.095；创业板/科创 20% -> 0.195；北交所 30% -> 0.295。
        取整-0.005容差，避免恰好压线时漏判。
        """
        try:
            from backend.services.simulation.services.local_market_data import (
                _board_pct,
            )

            pct = float(_board_pct(symbol))
            if pct >= 0.29:
                return 0.295
            if pct >= 0.19:
                return 0.195
            return 0.095
        except Exception:
            return 0.095

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
                limit_up_price and price > 0 and price >= limit_up_price * (1 - 0.0015)
            )
            limit_down = bool(
                limit_down_price
                and price > 0
                and price <= limit_down_price * (1 + 0.0015)
            )
            return limit_up, limit_down, False, limit_up_price, limit_down_price
        except Exception:
            return False, False, False, None, None

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
                fetch_series_tick,
            )

            tick = await fetch_series_tick(symbol)
            if tick:
                logger.info(
                    "Redis series price for %s: %.4f (age=%.0fs)",
                    symbol,
                    tick["price"],
                    tick["age_s"],
                )
                px = float(tick["price"])
                # L0 原先直接返回裸价导致涨跌停/停牌拦截失效，这里补齐风控字段
                lu, ld, susp, lu_px, ld_px = self._enrich_cn_limits(symbol, px)
                return MarketSnapshot(
                    price=px,
                    price_source="redis_series",
                    limit_up=lu,
                    limit_down=ld,
                    suspended=susp,
                    limit_up_price=lu_px,
                    limit_down_price=ld_px,
                )
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
                        limit_up=limit_up,
                        limit_down=limit_down,
                        suspended=suspended,
                        limit_up_price=limit_up_price,
                        limit_down_price=limit_down_price,
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
                        bool(px >= float(bar.limit_up or 0) * (1 - 0.0015))
                        if (bar.limit_up and bar.limit_up != float("inf"))
                        else False
                    )
                    ld = (
                        bool(px <= float(bar.limit_down or 0) * (1 + 0.0015))
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
                        bool(px >= float(bar.limit_up or 0) * (1 - 0.0015))
                        if (bar.limit_up and bar.limit_up != float("inf"))
                        else False
                    )
                    ld = (
                        bool(px <= float(bar.limit_down or 0) * (1 + 0.0015))
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
    ) -> ResolvedFill:
        """**取价契约唯一实现**：两执行路径共用（守卫/降级/标注单实现）。

        规则：
        1. L0/L1 新鲜实时价 → 直接使用（source 如实）；
        2. strict（手动即时市价单，保持 P0-5 语义）遇非实时 → 拒单；
        3. 否则降级：优先 bar（自带 trade_date，如实标注 today_bar_close / prev_close_bar，
           绝不静默按昨收成交——`[RULE:PRICE-STALE]` WARNING 点名），bar 无则用陈旧快照价；
        4. 全无 → 拒单。
        """
        snapshot = await self._latest_price(
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
        self, order: SimOrder, market: str | None = None, *, strict_market: bool = True
    ) -> ExecutionResult:
        # T-P2-03：价格与陈旧价守卫收敛到 _resolve_fill_price（唯一实现）；
        # strict_market=True（手动即时单，默认）保持 P0-5 语义——非实时市价单拒绝成交；
        # 自动化路径（沙箱/TDX/由 Router 传入 False）允许如实标注的降级。
        resolved = await self._resolve_fill_price(order, None, strict_market=strict_market)
        if not resolved.ok:
            return ExecutionResult(
                success=False, message=resolved.message, price_source=resolved.source or None
            )
        base_price = resolved.price
        fetched_source = resolved.source
        snapshot = resolved.snapshot  # 下游涨跌停钳制需要 limit_up/down_price

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

        # A股申报数量校验（T-P2-02：唯一实现；科创板 200 股起、1 股递增，其余 100 整数倍）
        if rules.market.value == "CN" and side == "buy":
            from backend.services.simulation.services.market_rules import (
                normalize_order_quantity,
            )

            qty = float(order.quantity or 0)
            normalized = normalize_order_quantity(qty, order.symbol, "CN")
            if (
                abs(qty - round(qty)) > 1e-6
                or normalized <= 0
                or normalized != int(round(qty))
            ):
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

        gross = order.quantity * exec_price
        if rules.market.value == "CN":
            # T-P2-02：费用分项唯一实现（market_rules.compute_fee_breakdown）；
            # settings 仅作显式覆盖（env/前端可配置语义保持不变）
            commission, stamp_duty, transfer_fee = rules.compute_fee_breakdown(
                order.quantity,
                exec_price,
                order.side.value,
                commission_rate=float(settings.SIMULATION_COMMISSION_RATE),
                commission_min=float(settings.SIMULATION_COMMISSION_MIN),
                stamp_duty_rate=float(settings.SIMULATION_STAMP_DUTY_RATE),
            )
        else:
            # 非 CN：同一实现（印花税独立分项——现金合计不变、sim_trades 分项更正确）
            commission, stamp_duty, transfer_fee = rules.compute_fee_breakdown(
                order.quantity, exec_price, side
            )
        if order.side.value == "buy":
            delta_cash = -(gross + commission + transfer_fee)
            delta_volume = order.quantity
        else:
            delta_cash = gross - commission - stamp_duty - transfer_fee
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
            quantity=order.quantity,
            commission=commission,
            stamp_duty=stamp_duty,
            transfer_fee=transfer_fee,
            market=market_str,
            account_snapshot=account_snapshot,
            price_source=price_source,
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
            # 时区BUG修复：timestamptz 列必须用 aware UTC，naive 值会被会话
            # 时区重解释（曾导致成交时间 -8h）。
            executed_at=datetime.now(timezone.utc),
            price_source=result.price_source,
        )
        self.db.add(trade)

        order.status = OrderStatus.FILLED
        order.submitted_at = order.submitted_at or datetime.now(timezone.utc)
        order.filled_at = datetime.now(timezone.utc)
        order.filled_quantity = result.quantity
        order.average_price = result.price
        order.filled_value = trade_value
        order.commission = result.commission
        order.total_fee = total_fee
        # 委托金额以实际成交金额为准（市价单无委托价，此前 quantity*(price or 0)=0 失真）
        order.order_value = trade_value
        order.execution_model = "synthetic_price"
        order.price_source = result.price_source

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

    async def mark_rejected(self, order: SimOrder, message: str):
        order.status = OrderStatus.REJECTED
        order.submitted_at = order.submitted_at or datetime.now(timezone.utc)
        order.remarks = f"Execution rejected: {message}"
        await self.db.commit()
        await self.db.refresh(order)

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

    async def assess_execution_window(self, order):
        """V2挂单链路兼容：模拟盘无盘前盘后会话限制，默认可执行。"""
        from types import SimpleNamespace

        return SimpleNamespace(
            can_execute=True,
            target_trade_date=getattr(order, "trading_session_date", None),
            final_state=None,
            retryable=False,
            message="ok",
        )

    async def mark_expired(self, order, message: str):
        """V2挂单过期兼容：旧SimOrder无EXPIRED枚举，降级为REJECTED；V2投影用字符串expired。"""
        try:
            status = getattr(order, "status", None)
            # SimulationOrderV2.status 是纯字符串
            if isinstance(status, str):
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
