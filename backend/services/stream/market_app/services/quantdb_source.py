"""
QuantDB 数据源 — A 股行情统一从本地 QuantDB parquet 读取。

设计目标：
- 所有 A 股实时行情（quote）与 K 线统一走 QuantDB 日线，不再依赖
  opentdx / tencent / sina 等外部行情源。
- 优先读取最近交易日日线（LocalMarketData 已含涨跌停/ST/停牌标记），
  timestamp 使用日线真实交易日期，而非当前墙钟时间。
- 未来接入分钟/tick/l2 数据后，在此适配器内优先读实时、日线兜底，
  market:series:* 时序机制保持不变。
"""

import logging
from datetime import datetime, timezone
from typing import Any

from .data_source import DataSourceAdapter

logger = logging.getLogger(__name__)


class QuantDBDataSource(DataSourceAdapter):
    """从本地 QuantDB parquet（DuckDB 视图）读取 A 股日线行情。"""

    def __init__(self):
        self._market_data = None

    def _get_market_data(self):
        """懒初始化 LocalMarketData（进程内共享 QuantDBDataHub 实例）。"""
        if self._market_data is None:
            from backend.services.simulation.services.local_market_data import (
                get_local_market_data,
            )

            self._market_data = get_local_market_data()
        return self._market_data

    # ------------------------------------------------------------------ #
    #  实时行情
    # ------------------------------------------------------------------ #

    async def fetch_quote(self, symbol: str) -> dict[str, Any] | None:
        """获取单只标的最近交易日日线行情。"""
        quotes = await self.fetch_quotes([symbol])
        return quotes[0] if quotes else None

    async def fetch_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        """批量获取最近交易日日线行情。

        QuantDB 的读取是同步 DuckDB 扫描，在线程池中执行避免阻塞事件循环。
        """
        if not symbols:
            return []

        import asyncio

        def _load() -> list[dict[str, Any]]:
            market_data = self._get_market_data()
            latest_date = market_data.latest_trade_date()
            if latest_date is None:
                logger.warning("[quantdb] QuantDB 无可用日线数据")
                return []
            bars = market_data.load_date(latest_date, symbols=symbols)
            results: list[dict[str, Any]] = []
            for symbol, bar in bars.items():
                trade_date = bar.trade_date
                trade_ts = datetime(
                    trade_date.year, trade_date.month, trade_date.day, tzinfo=timezone.utc
                )
                results.append(
                    {
                        "symbol": symbol,
                        "timestamp": trade_ts,
                        "current_price": bar.close,
                        "open_price": bar.open,
                        "high_price": bar.high,
                        "low_price": bar.low,
                        "pre_close": bar.pre_close if bar.pre_close > 0 else None,
                        "volume": int(bar.volume),
                        "amount": bar.amount,
                        "vwap": bar.vwap,
                        "limit_up": bar.limit_up,
                        "limit_down": bar.limit_down,
                        "is_st": bar.is_st,
                        "is_suspended": bar.suspended,
                        "is_stale": False,
                        "data_source": "quantdb",
                    }
                )
            logger.info(
                "[quantdb] 最近交易日 %s 取回行情 %d/%d",
                latest_date.isoformat(),
                len(results),
                len(symbols),
            )
            return results

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, _load)
        except Exception as exc:
            logger.error(f"[quantdb] fetch_quotes failed: {exc}")
            return []

    # ------------------------------------------------------------------ #
    #  K 线
    # ------------------------------------------------------------------ #

    async def fetch_kline(
        self,
        symbol: str,
        interval: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """从 QuantDB 读取日线 K 线（interval 仅支持日级及以下，分钟级暂返回空）。"""
        if not symbol:
            return []
        period = (interval or "1d").lower()
        if period not in {"1d", "1w", "1mo", "1mon", "day", "week", "month"}:
            # 分钟级数据暂未接入，返回空由上层兜底
            return []

        import asyncio
        from datetime import date, timedelta

        def _load() -> list[dict[str, Any]]:
            from backend.services.engine.data_platform.quantdb_hub import (
                QuantDBDataHub,
            )
            from backend.shared.stock_utils import StockCodeUtil

            hub = QuantDBDataHub.get_instance()
            market_data = self._get_market_data()
            latest_date = market_data.latest_trade_date()
            if latest_date is None:
                return []
            end_date = end_time.date() if end_time else latest_date
            start_date = (
                start_time.date()
                if start_time
                else end_date - timedelta(days=int(limit * 2) + 10)
            )
            # QuantDB parquet 使用后缀格式（600036.SH），统一转换后再查询
            suffix_symbol = StockCodeUtil.to_suffix(symbol)
            df = hub.fetch_daily_kline(suffix_symbol, start_date, end_date, adjust="none")
            if df.empty:
                return []

            output: list[dict[str, Any]] = []
            prev_close: float | None = None
            for row in df.itertuples(index=False):
                close_price = float(getattr(row, "close", 0.0) or 0.0)
                ts = datetime.combine(row.trade_date, datetime.min.time())
                change = (close_price - prev_close) if prev_close else None
                change_pct = (
                    (change / prev_close * 100.0) if prev_close and change is not None else None
                )
                output.append(
                    {
                        "symbol": symbol,
                        "interval": period,
                        "timestamp": ts,
                        "open_price": float(getattr(row, "open", 0.0) or 0.0),
                        "high_price": float(getattr(row, "high", 0.0) or 0.0),
                        "low_price": float(getattr(row, "low", 0.0) or 0.0),
                        "close_price": close_price,
                        "volume": int(getattr(row, "volume", 0) or 0),
                        "amount": float(getattr(row, "amount", 0.0) or 0.0),
                        "change": round(change, 4) if change is not None else None,
                        "change_percent": round(change_pct, 4)
                        if change_pct is not None
                        else None,
                        "turnover_rate": None,
                        "data_source": "quantdb",
                    }
                )
                prev_close = close_price
            return output[-limit:]

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, _load)
        except Exception as exc:
            logger.error(f"[quantdb] fetch_kline failed for {symbol} {interval}: {exc}")
            return []

    # ------------------------------------------------------------------ #
    #  标的列表
    # ------------------------------------------------------------------ #

    async def fetch_symbols(self, market: str | None = None) -> list[dict[str, Any]]:
        """从 QuantDB instrument_detail 读取 A 股标的列表。"""
        import asyncio

        def _load() -> list[dict[str, Any]]:
            from backend.services.engine.data_platform.quantdb_hub import (
                QuantDBDataHub,
            )

            hub = QuantDBDataHub.get_instance()
            detail_dir = hub.data_dir / "2_base_sector" / "instrument_detail"
            file_path = detail_dir / "instrument_list.parquet"
            if not file_path.exists():
                file_path = detail_dir / "instrument_detail.parquet"
            if not file_path.exists():
                logger.warning(f"[quantdb] instrument_detail 缺失: {file_path}")
                return []

            import pandas as pd

            try:
                df = pd.read_parquet(file_path, columns=["Symbol", "Name"])
            except Exception as exc:
                logger.warning(f"[quantdb] instrument_detail 读取失败: {exc}")
                return []

            if df.empty:
                return []

            market_upper = (market or "").strip().upper()
            result: list[dict[str, Any]] = []
            for row in df.itertuples(index=False):
                symbol = str(getattr(row, "Symbol", "") or "").strip()
                if not symbol:
                    continue
                if market_upper and not symbol.endswith(f".{market_upper}"):
                    continue
                result.append(
                    {
                        "symbol": symbol,
                        "code": symbol.partition(".")[0],
                        "market": symbol.partition(".")[2] or "",
                        "name": str(getattr(row, "Name", "") or "").strip(),
                    }
                )
            return result

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, _load)
        except Exception as exc:
            logger.error(f"[quantdb] fetch_symbols failed: {exc}")
            return []
