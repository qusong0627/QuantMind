"""
跨市场 K 线 API
==================

GET /api/v1/market/kline?symbol=600519.SH&market=A&period=daily&start=2024-01-01&end=2024-12-31
GET /api/v1/market/kline/{symbol}?market=A&days=120

返回：
{
  "success": true,
  "data": {
    "market": "A",
    "symbol": "600519.SH",
    "period": "daily",
    "source_used": "baostock",
    "items": [{"date":"YYYY-MM-DD","open":...,"high":...,"low":...,"close":...,"volume":..., "amount":...}],
    "fallbacks_tried": ["..."],
    "cleaning_report": {...}
  }
}

策略：
1. 优先走 A 股 stock_daily_latest（命中即返回，时延最低）
2. 否则调用 FieldAggregator.fetch(market, field='daily_kline', symbol=symbol)
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.services.api.user_app.middleware.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/market", tags=["Market"])

# K 线内存缓存：历史行情静态不变，按 (market, symbol, period, days 窗口) 缓存。
# 命中即跳过 QuantDB parquet / DuckDB 读取，冷读 2s+ → 缓存命中 <5ms。
_KLINE_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_KLINE_CACHE_TTL = int(__import__("os").getenv("KLINE_CACHE_TTL_SECONDS", "300"))
_KLINE_CACHE_MAX = 2048


def _kline_cache_key(market: str, symbol: str, start: str, end: str, adjust: str) -> str:
    return f"{market}:{symbol}:{adjust}:{start}:{end}"


def _kline_cache_get(key: str) -> list[dict[str, Any]] | None:
    entry = _KLINE_CACHE.get(key)
    if not entry:
        return None
    ts, items = entry
    if time.time() - ts > _KLINE_CACHE_TTL:
        _KLINE_CACHE.pop(key, None)
        return None
    return items


def _kline_cache_set(key: str, items: list[dict[str, Any]]) -> None:
    _KLINE_CACHE[key] = (time.time(), items)
    if len(_KLINE_CACHE) > _KLINE_CACHE_MAX:
        oldest = min(_KLINE_CACHE, key=lambda k: _KLINE_CACHE[k][0])
        _KLINE_CACHE.pop(oldest, None)


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid date: {s}")


async def _try_quantdb_parquet(symbol: str, start: Optional[date], end: Optional[date], days: int, adjust: str = "qfq"):
    """A 股最快路径：从 QuantDB 本地 parquet 读取（DuckDB）。"""
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
    except Exception:
        return None

    hub = QuantDBDataHub()
    if not hub.available:
        return None

    # QuantDB parquet 内 symbol 为后缀式（600519.SH），入参可能是前缀式（SH600519），
    # 不转换会永远查不到行而静默跌入聚合器兜底（聚合器仅前复权口径）
    from backend.shared.stock_utils import StockCodeUtil

    qdb_symbol = StockCodeUtil.to_suffix(symbol)

    try:
        import asyncio
        if not start or not end:
            end = end or date.today()
            start = start or (end - timedelta(days=days * 2))

        def _read():
            df = hub.fetch_daily_kline(qdb_symbol, start, end, adjust=adjust)
            if df is None or df.empty:
                return None
            items = []
            for _, r in df.iterrows():
                items.append({
                    "date": str(r.get("trade_date", ""))[:10],
                    "open": _safe_float(r.get("open")),
                    "high": _safe_float(r.get("high")),
                    "low": _safe_float(r.get("low")),
                    "close": _safe_float(r.get("close")),
                    "volume": _safe_float(r.get("volume")),
                    "amount": _safe_float(r.get("amount"), default=None),
                })
            return items

        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warning("quantdb_parquet fast-path failed: %s", exc)
        return None


async def _try_stock_daily_latest(symbol: str, start: Optional[date], end: Optional[date], days: int):
    """A 股快路径：从 stock_daily_latest 直接拉。

    该表由 quantdb_daily_sync 从 qdb_daily_forward 写入，存储的是前复权价（adj_factor 恒为 1.0），
    因此仅对 adjust=qfq 有效，直接原价返回；不做任何因子换算，
    避免未来写入真实复权因子时产生口径漂移。
    """
    try:
        from sqlalchemy import text
        from backend.shared.database_manager_v2 import get_session
    except Exception:
        return None

    async with get_session(read_only=True) as session:
        if start and end:
            res = await session.execute(
                text(
                    "SELECT trade_date, open, high, low, close, volume "
                    "FROM stock_daily_latest "
                    "WHERE symbol = :s AND trade_date BETWEEN :a AND :b "
                    "ORDER BY trade_date ASC"
                ),
                {"s": symbol, "a": start, "b": end},
            )
        else:
            res = await session.execute(
                text(
                    "SELECT trade_date, open, high, low, close, volume "
                    "FROM stock_daily_latest "
                    "WHERE symbol = :s ORDER BY trade_date DESC LIMIT :l"
                ),
                {"s": symbol, "l": days},
            )
        rows = list(res)
        if not rows:
            return None
        items = []
        for r in rows:
            items.append({
                "date": str(r[0]),
                "open": _safe_float(r[1]),
                "high": _safe_float(r[2]),
                "low": _safe_float(r[3]),
                "close": _safe_float(r[4]),
                "volume": float(r[5]) if r[5] is not None else 0.0,
            })
        if not (start and end):
            items.reverse()
        return items


_AGG_CACHE = None


def _get_aggregator():
    global _AGG_CACHE
    if _AGG_CACHE is None:
        from backend.services.engine.data_platform.adapters import register_all
        from backend.services.engine.data_platform.aggregator import (
            FieldAggregator, FieldRoutingTable,
        )
        from backend.services.engine.data_platform.cleaner import DataCleaner
        from backend.services.engine.data_platform.monitor import get_monitor
        from backend.services.engine.data_platform.registry import get_registry

        register_all()
        _AGG_CACHE = FieldAggregator(
            registry=get_registry(),
            routing=FieldRoutingTable(),
            monitor=get_monitor(),
            cleaner=DataCleaner(),
        )
    return _AGG_CACHE


def _safe_float(v, default=0.0):
    """Convert a value to float, handling pd.NA/None/NaN."""
    if v is None:
        return default
    try:
        import math
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return default
    except (TypeError, ValueError):
        pass
    try:
        import pandas as pd
        if pd.isna(v):
            return default
    except (TypeError, ValueError):
        pass
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _direct_yahoo_fetch(symbol: str, start: Optional[date], end: Optional[date]):
    """HK/US 快路径：直接调用 yahoo_finance adapter，跳过 aggregator 管线。"""
    from backend.services.engine.data_platform.registry import get_registry
    reg = get_registry()
    adapter = reg.get("yahoo_finance")
    if adapter is None:
        return None
    df = adapter.fetch_daily(symbol, start=start, end=end)
    if df is None or len(df) == 0:
        return None
    items: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        items.append({
            "date": str(r.get("trade_date")),
            "open": _safe_float(r.get("open")),
            "high": _safe_float(r.get("high")),
            "low": _safe_float(r.get("low")),
            "close": _safe_float(r.get("close")),
            "volume": _safe_float(r.get("volume")),
            "amount": _safe_float(r.get("amount"), default=None),
        })
    return {
        "items": items,
        "source_used": "yahoo_finance",
        "fallbacks_tried": [],
        "cleaning_report": {},
    }


def _aggregator_fetch(market: str, symbol: str, start: Optional[date], end: Optional[date]):
    """通过 FieldAggregator 调多源拉日 K。"""
    agg = _get_aggregator()  # cached; register_all() runs only in main thread
    res = agg.fetch(
        market=market, field="daily_kline", symbol=symbol,
        start=start, end=end,
    )
    df = res.data
    items: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        # 必须走 _safe_float：`float(x or 0)` 对 NaN 是放行的（NaN 为 truthy），
        # 会把 NaN 带进响应体，FastAPI 序列化时抛 "Out of range float values" → 500。
        # 港股/美股走 aggregator 兜底路径时实测 amount 整列为 NaN，正是这个 500 的来源。
        items.append({
            "date": str(r.get("trade_date")),
            "open": _safe_float(r.get("open")),
            "high": _safe_float(r.get("high")),
            "low": _safe_float(r.get("low")),
            "close": _safe_float(r.get("close")),
            "volume": _safe_float(r.get("volume")),
            "amount": _safe_float(r.get("amount"), default=None),
        })
    return {
        "items": items,
        "source_used": res.source_used,
        "fallbacks_tried": res.fallbacks_tried,
        "cleaning_report": res.cleaning_report,
    }


@router.get("/kline")
async def get_kline(
    symbol: str = Query(..., description="600519.SH / 00700.HK / AAPL"),
    market: str = Query("A", description="A / HK / US"),
    period: str = Query("daily", description="daily 仅支持 daily"),
    start: Optional[str] = Query(None, description="YYYY-MM-DD"),
    end: Optional[str] = Query(None, description="YYYY-MM-DD"),
    days: int = Query(120, ge=5, le=4000),
    adjust: str = Query("qfq", description="复权方式：qfq=前复权（默认）/ hfq=后复权 / none=不复权，仅 A 股生效"),
    current_user: dict = Depends(get_current_user),
):
    if period != "daily":
        raise HTTPException(status_code=400, detail=f"period {period} 暂未支持")
    adj = adjust.lower()
    if adj not in ("qfq", "hfq", "none"):
        raise HTTPException(status_code=400, detail=f"adjust {adjust} 非法，可选 qfq / hfq / none")

    m = market.upper()
    sym = symbol.upper()
    sd = _parse_date(start)
    ed = _parse_date(end)
    if not (sd and ed):
        ed = ed or date.today()
        sd = sd or (ed - timedelta(days=days * 2))

    # 历史行情静态不变：命中内存缓存直接返回（冷读 2s+ → 缓存命中 <5ms）
    # 缓存键含复权方式，不同口径互不污染；A 股外市场忽略 adjust（yahoo 等源不提供复权）
    cache_key = _kline_cache_key(m, sym, sd.isoformat(), ed.isoformat(), adj if m == "A" else "raw")
    cached = _kline_cache_get(cache_key)
    if cached is not None:
        return {
            "success": True,
            "data": {
                "market": m, "symbol": sym, "period": period,
                "source_used": "kline_cache",
                "items": cached, "fallbacks_tried": [], "cleaning_report": {},
            },
        }

    # A 股优先走 QuantDB 本地 parquet（最快路径，无 DB 依赖）
    if m == "A":
        try:
            items = await _try_quantdb_parquet(sym, sd, ed, days, adjust=adj)
            if items:
                _kline_cache_set(cache_key, items)
                return {
                    "success": True,
                    "data": {
                        "market": m, "symbol": sym, "period": period,
                        "adjust": adj,
                        "source_used": "quantdb_parquet",
                        "items": items, "fallbacks_tried": [], "cleaning_report": {},
                    },
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("quantdb_parquet fast-path failed: %s", exc)

    # A 股其次走 latest 表（表内为前复权口径，仅 qfq 可用）
    if m == "A" and adj == "qfq":
        try:
            items = await _try_stock_daily_latest(sym, sd, ed, days)
            if items:
                _kline_cache_set(cache_key, items)
                return {
                    "success": True,
                    "data": {
                        "market": m, "symbol": sym, "period": period,
                        "adjust": adj,
                        "source_used": "stock_daily_latest",
                        "items": items, "fallbacks_tried": [], "cleaning_report": {},
                    },
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("stock_daily_latest fast-path failed: %s", exc)

    # HK/US 优先走 yahoo_finance 直连（跳过 aggregator 管线，避免超时）
    if m in ("HK", "US"):
        try:
            import asyncio
            payload = await asyncio.to_thread(_direct_yahoo_fetch, sym, sd, ed)
            if payload is not None:
                return {
                    "success": True,
                    "data": {
                        "market": m, "symbol": sym, "period": period,
                        **payload,
                    },
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("yahoo_finance direct fast-path failed: %s", exc)

    try:
        import asyncio
        payload = await asyncio.to_thread(_aggregator_fetch, m, sym, sd, ed)
    except Exception as exc:
        logger.error("aggregator fetch failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=503, detail=f"no data: {exc}")

    return {
        "success": True,
        "data": {
            "market": m, "symbol": sym, "period": period,
            **payload,
        },
    }


@router.get("/kline/{symbol}")
async def get_kline_by_path(
    symbol: str,
    market: str = Query("A"),
    days: int = Query(120, ge=5, le=4000),
    adjust: str = Query("qfq", description="复权方式：qfq / hfq / none，仅 A 股生效"),
    current_user: dict = Depends(get_current_user),
):
    """路径参数风格，方便前端写 /api/v1/market/kline/600519.SH?market=A&days=120"""
    return await get_kline(
        symbol=symbol, market=market, period="daily",
        start=None, end=None, days=days, adjust=adjust, current_user=current_user,
    )


@router.get("/index-kline")
async def get_index_kline(
    symbol: str = Query("000001.SH", description="指数代码，缺省上证指数"),
    days: int = Query(120, ge=20, le=500),
    current_user: dict = Depends(get_current_user),
):
    """上证指数(000001.SH) 日线 + MA20，用于 K 线图大盘趋势叠加。

    返回 {dates, close, ma20, below_ma20(最新日是否<MA20)}。
    数据源：QuantDB index_daily。
    """
    _ = current_user
    try:
        from datetime import date, timedelta
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub()
        end = date.today()
        start = end - timedelta(days=int(days * 1.6))
        df = hub.fetch_index_kline(symbol, start, end)
        if df is None or df.empty:
            return {"success": True, "data": {"dates": [], "close": [], "ma20": [], "below_ma20": None, "source_used": "none"}}
        df = df.sort_values("trade_date").tail(days).reset_index(drop=True)
        closes = df["close"].astype(float).tolist()
        dates = [str(x)[:10] for x in df["trade_date"].tolist()]
        # MA20
        ma20_list: list[float | None] = []
        for i in range(len(closes)):
            if i < 19:
                ma20_list.append(None)
            else:
                ma20_list.append(round(sum(closes[i - 19:i + 1]) / 20.0, 2))
        latest = closes[-1] if closes else None
        ma20_latest = ma20_list[-1] if ma20_list else None
        below_ma20 = bool(latest is not None and ma20_latest is not None and latest < ma20_latest)
        return {
            "success": True,
            "data": {
                "symbol": symbol,
                "dates": dates,
                "close": [round(c, 2) for c in closes],
                "ma20": ma20_list,
                "below_ma20": below_ma20,
                "latest_close": round(latest, 2) if latest is not None else None,
                "latest_ma20": ma20_latest,
                "source_used": "quantdb_index_daily",
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("index kline failed: %s", exc)
        return {"success": True, "data": {"dates": [], "close": [], "ma20": [], "below_ma20": None, "source_used": "none", "error": str(exc)}}


@router.get("/index-ma")
async def get_index_ma(
    symbol: str = Query("000001.SH", description="指数代码，缺省上证指数"),
    asof: str | None = Query(None, description="基准日 YYYY-MM-DD，缺省最新交易日"),
    current_user: dict = Depends(get_current_user),
):
    """大盘均线过滤：上证指数 MA5/10/20/30/60 相对位置 + 可持仓判断。

    返回 {symbol, name, trade_date, close, ma5/ma10/ma20/ma30/ma60,
          above_ma20(收盘>MA20 可持仓), status(描述文案)}。
    数据源：QuantDB index_daily。
    """
    _ = current_user
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub()
        end = date.fromisoformat(asof) if asof else date.today()
        start = end - timedelta(days=160)
        df = hub.fetch_index_kline(symbol, start, end)
        if df is None or df.empty:
            return {"success": True, "data": None, "error": "无指数数据"}
        df = df.sort_values("trade_date").reset_index(drop=True)
        closes = df["close"].astype(float).tolist()
        dates = [str(x)[:10] for x in df["trade_date"].tolist()]

        def _ma(n: int) -> float | None:
            if len(closes) < n:
                return None
            return round(sum(closes[-n:]) / n, 2)

        ma5, ma10, ma20 = _ma(5), _ma(10), _ma(20)
        ma30, ma60 = _ma(30), _ma(60)
        close = round(closes[-1], 2) if closes else None
        above = bool(close is not None and ma20 is not None and close > ma20)
        status = "指数在 MA20 上方，可正常按信号操作" if above else "指数在 MA20 下方，建议观望或减仓"
        return {
            "success": True,
            "data": {
                "symbol": symbol,
                "name": "上证指数" if symbol == "000001.SH" else symbol,
                "trade_date": dates[-1],
                "close": close,
                "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma30": ma30, "ma60": ma60,
                "above_ma20": above,
                "status": status,
                "source_used": "quantdb_index_daily",
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("index ma failed: %s", exc)
        return {"success": True, "data": None, "error": str(exc)}


# 指数快照缓存：hub 的 DuckDB 连接是 thread-local，新请求线程需重新挂载视图（~2.6s）。
# 指数行情为日频本地数据，短 TTL 快照缓存即可让后续请求毫秒级返回。
_QUOTES_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_QUOTES_TTL = 60.0


@router.get("/quotes")
async def get_index_quotes(
    market: str = Query("CN", description="CN / HK / US / CRYPTO / FUTURES"),
    asof: str | None = Query(None, description="历史快照日 YYYY-MM-DD，取该日及之前最近行情（缺省=最新）"),
    current_user: dict = Depends(get_current_user),
):
    """多市场指数/品种行情快照（轻量版 /overview，用于页面指数条）。

    一次返回 {quotes: [{symbol, name, price, change, change_percent, trade_date}]}，
    各市场本地 parquet 读取，带 60s 快照缓存（asof 指定历史日时不走缓存）。
    """
    _ = current_user
    market_upper = str(market or "CN").upper()
    metas = _MARKET_INDEX_META.get(market_upper, _MARKET_INDEX_META["CN"])

    cached = None if asof else _QUOTES_CACHE.get(market_upper)
    if cached is not None and time.monotonic() - cached[0] < _QUOTES_TTL:
        quotes = cached[1]
    else:
        quotes = []
        for meta in metas:
            q = _hub_latest_quote(market_upper, meta["symbol"], asof=asof)
            if q is not None:
                quotes.append(q)
        if not asof:
            _QUOTES_CACHE[market_upper] = (time.monotonic(), quotes)

    if not quotes:
        return {"success": False, "data": {"market": market_upper, "quotes": []}, "error": f"market {market_upper} 无可用行情数据"}
    return {"success": True, "data": {"market": market_upper, "quotes": quotes}}


# ── 多市场指数概览 ─────────────────────────────────────────────────────────────
# 各市场要展示的指数品种（symbol -> 名称）。优先用本地 parquet 真实行情。
_MARKET_INDEX_META: dict[str, list[dict[str, str]]] = {
    "CN": [
        {"symbol": "000001.SH", "name": "上证指数"},
        {"symbol": "399001.SZ", "name": "深证成指"},
        {"symbol": "000300.SH", "name": "沪深300"},
        {"symbol": "000905.SH", "name": "中证500"},
        {"symbol": "399006.SZ", "name": "创业板指"},
        {"symbol": "000688.SH", "name": "科创50"},
        {"symbol": "000016.SH", "name": "上证50"},
        {"symbol": "899050.BJ", "name": "北证50"},
    ],
    "HK": [
        {"symbol": "HSI.HK", "name": "恒生指数"},
        {"symbol": "HSCEI.HK", "name": "恒生国企"},
        {"symbol": "HSTECH.HK", "name": "恒生科技"},
        {"symbol": "HSCCI.HK", "name": "恒生红筹"},
    ],
    "US": [
        {"symbol": "DJI.US", "name": "道琼斯"},
        {"symbol": "IXIC.US", "name": "纳斯达克"},
        {"symbol": "SPX.US", "name": "标普500"},
        {"symbol": "NDX.US", "name": "纳斯达克100"},
        {"symbol": "SOX.US", "name": "费城半导体"},
    ],
    "CRYPTO": [
        {"symbol": "BTCUSDT", "name": "比特币"},
        {"symbol": "ETHUSDT", "name": "以太坊"},
        {"symbol": "BNBUSDT", "name": "BNB"},
        {"symbol": "SOLUSDT", "name": "Solana"},
        {"symbol": "XRPUSDT", "name": "瑞波币"},
        {"symbol": "DOGEUSDT", "name": "狗狗币"},
    ],
    "FUTURES": [
        {"symbol": "CL.FUT", "name": "WTI原油"},
        {"symbol": "GC.FUT", "name": "COMEX黄金"},
        {"symbol": "SI.FUT", "name": "COMEX白银"},
        {"symbol": "HG.FUT", "name": "COMEX铜"},
        {"symbol": "RB0.CN", "name": "螺纹钢主力"},
        {"symbol": "CU0.CN", "name": "沪铜主力"},
        {"symbol": "AU0.CN", "name": "沪金主力"},
        {"symbol": "I0.CN", "name": "铁矿石主力"},
    ],
}

# market -> (hub 模块路径, hub 类名, 读取方法)
_MARKET_HUB_CFG: dict[str, tuple[str, str, str]] = {
    "CN": ("backend.services.engine.data_platform.quantdb_hub", "QuantDBDataHub", "fetch_index_kline"),
    "HK": ("backend.services.engine.data_platform.quanthk_hub", "QuantHKDataHub", "fetch_index_kline"),
    "US": ("backend.services.engine.data_platform.quantus_hub", "QuantUSDataHub", "fetch_index_kline"),
    "CRYPTO": ("backend.services.engine.data_platform.quantbc_hub", "QuantBCDataHub", "fetch_daily_kline"),
    "FUTURES": ("backend.services.engine.data_platform.quantfutures_hub", "QuantFuturesDataHub", "fetch_daily_kline"),
}


def _hub_latest_quote(market: str, symbol: str, asof: str | None = None) -> dict[str, Any] | None:
    """从市场 hub 读取指定标的最近 2 个交易日的日K，计算行情快照。

    asof=YYYY-MM-DD 时取该日及之前最近行情（历史日联动，指数条随日历变）。
    返回 {symbol, name, price, change, change_percent, open, high, low,
          pre_close, volume, amount, trade_date}，无数据返回 None。
    """
    import importlib

    entry = _MARKET_HUB_CFG.get(market)
    if entry is None:
        return None
    try:
        mod = importlib.import_module(entry[0])
        cls = getattr(mod, entry[1])
        hub = cls.get_instance()
        method = getattr(hub, entry[2])
        if asof:
            try:
                end = date.fromisoformat(asof)
            except ValueError:
                end = date.today()
        else:
            end = date.today()
        start = end - timedelta(days=14)  # 足够覆盖周末/假期
        df = method(symbol, start, end)
        if df is None or df.empty:
            return None
        df = df.sort_values("trade_date").reset_index(drop=True)
        if asof:
            # asof 早于数据起点（如 2016 年）时取第一行作展示（不回落最新，避免误导）
            asof_ts = pd.Timestamp(asof)
            df = df[df["trade_date"] <= asof_ts] if (df["trade_date"] <= asof_ts).any() else df.iloc[:1]
        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else None

        price = float(latest.get("close", 0))
        if price <= 0:
            return None
        prev_close = float(prev["close"]) if prev is not None else float(latest.get("open", price))
        if prev_close <= 0:
            prev_close = price
        change = price - prev_close
        change_percent = (change / prev_close * 100) if prev_close else 0.0

        def _f(v, default=0.0) -> float:
            try:
                x = float(v)
                return x if x == x else default  # NaN check
            except Exception:
                return default

        return {
            "symbol": symbol,
            "name": next((m["name"] for m in _MARKET_INDEX_META.get(market, []) if m["symbol"] == symbol), symbol),
            "price": round(price, 4),
            "change": round(change, 4),
            "change_percent": round(change_percent, 4),
            "open": round(_f(latest.get("open")), 4),
            "high": round(_f(latest.get("high")), 4),
            "low": round(_f(latest.get("low")), 4),
            "pre_close": round(prev_close, 4),
            "volume": _f(latest.get("volume")),
            "amount": _f(latest.get("amount")),
            "trade_date": str(df["trade_date"].iloc[-1])[:10] if "trade_date" in df.columns else "",
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("market %s symbol %s quote failed: %s", market, symbol, exc)
        return None


@router.get("/overview")
async def get_market_overview(
    market: str = Query("CN", description="CN / HK / US / CRYPTO / FUTURES"),
    current_user: dict = Depends(get_current_user),
):
    """多市场指数/品种最新行情概览。

    从各市场本地 parquet（QuantDB/QuantHK/QuantUS/QuantBC/QuantFutures）
    读取真实最近交易日行情，替代前端模拟数据。

    返回 {success, data: {market, indices: [...], last_update, source_used}}。
    """
    _ = current_user
    market_upper = str(market or "CN").upper()
    metas = _MARKET_INDEX_META.get(market_upper, _MARKET_INDEX_META["CN"])

    indices: list[dict[str, Any]] = []
    for meta in metas:
        q = _hub_latest_quote(market_upper, meta["symbol"])
        if q is not None:
            indices.append(q)

    if not indices:
        return {
            "success": False,
            "data": {"market": market_upper, "indices": [], "last_update": "", "source_used": "none"},
            "error": f"market {market_upper} 无可用行情数据",
        }

    # 统计涨跌家数
    up = sum(1 for x in indices if x["change_percent"] > 0)
    down = sum(1 for x in indices if x["change_percent"] < 0)
    flat = len(indices) - up - down
    return {
        "success": True,
        "data": {
            "market": market_upper,
            "indices": indices,
            "last_update": max(x["trade_date"] for x in indices if x["trade_date"]),
            "stats": {"up": up, "down": down, "flat": flat, "total": len(indices)},
            "source_used": "local_parquet",
        },
    }
