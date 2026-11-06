"""Read-only performance review for an authenticated inference run."""

import asyncio
import math
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from backend.shared.stock_utils import StockCodeUtil

_TZ = ZoneInfo("Asia/Shanghai")


def _price(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and n > 0 else None
    except (ValueError, TypeError):
        return None


def _historical(detail, top_k, exclude_st, benchmark):
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    hub = QuantDBDataHub.get_instance()
    run = detail["summary"]
    asof = date.fromisoformat(str(run["data_trade_date"])[:10])
    start = date.fromisoformat(str(run["prediction_trade_date"])[:10])
    today = datetime.now(_TZ).date()
    st_map = {}
    st_date = asof.strftime("%Y%m%d")
    rel = "6_ml_datasets/features_daily"
    if exclude_st and "is_st" in hub.partition_columns(rel, st_date):
        data = hub._read_partitioned(rel, [st_date], cols="symbol, is_st")
        for row in data.to_dict("records"):
            value = str(row.get("is_st")).lower().strip()
            if value in ("0", "0.0", "false", "1", "1.0", "true"):
                st_map[StockCodeUtil.to_suffix(str(row["symbol"]))] = value in (
                    "1",
                    "1.0",
                    "true",
                )
    stocks, seen, unknown = [], set(), 0
    for row in detail["items"]:
        symbol = StockCodeUtil.to_suffix(str(row.get("symbol") or ""))
        if not symbol or not StockCodeUtil.exchange(symbol) or symbol in seen:
            continue
        seen.add(symbol)
        is_st = st_map.get(symbol)
        if exclude_st and is_st is None:
            unknown += 1
            continue
        if exclude_st and is_st:
            continue
        stocks.append(
            {
                "code": symbol,
                "name": row.get("stock_name") or symbol,
                "industry": row.get("industry") or "—",
                "score": row.get("fusion_score"),
                "rank": row.get("score_rank") or len(stocks) + 1,
            }
        )
        if len(stocks) >= top_k:
            break
    history, index = {}, {}
    if stocks and start <= today:
        frame = hub.fetch_daily_kline_batch(
            [s["code"] for s in stocks], start, today, adjust="qfq"
        )
        for row in frame.to_dict("records"):
            sym = StockCodeUtil.to_suffix(str(row.get("symbol") or ""))
            history.setdefault(sym, {})[str(row.get("trade_date"))[:10]] = {
                "open": _price(row.get("open")),
                "close": _price(row.get("close")),
            }
        for row in hub.fetch_index_kline(benchmark, start, today).to_dict("records"):
            index[str(row.get("trade_date"))[:10]] = {
                "open": _price(row.get("open")),
                "close": _price(row.get("close")),
            }
    return stocks, history, index, unknown


def _previous_closes(symbols, previous_date, adjust):
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    if not symbols or not previous_date:
        return {}
    day = date.fromisoformat(previous_date)
    frame = QuantDBDataHub.get_instance().fetch_daily_kline_batch(
        symbols, day, day, adjust=adjust
    )
    return {
        StockCodeUtil.to_suffix(str(row.get("symbol") or "")): _price(row.get("close"))
        for row in frame.to_dict("records")
        if str(row.get("trade_date"))[:10] == previous_date
    }


async def _series_quotes(source, symbols, now):
    """批量取当日时序最新价；收盘后只接受当日尾盘记录。"""
    from backend.services.simulation.services.redis_series_quote import (
        parse_series_member,
    )

    if not symbols:
        return {}
    try:
        import exchange_calendars as xcals

        if not xcals.get_calendar("XSHG").is_session(now.date()):
            return {}
        client = source._get_client()
        candidates = [
            (symbol, key)
            for symbol in symbols
            for key in StockCodeUtil.series_keys(symbol)
        ]
        async with client.pipeline(transaction=False) as pipe:
            for _, key in candidates:
                pipe.zrevrange(key, 0, 0, withscores=True)
            results = await pipe.execute()
    except Exception:
        return {}
    result = {}
    for (symbol, _), rows in zip(candidates, results, strict=True):
        if symbol in result or not rows:
            continue
        member, score = rows[0]
        # 当日尾盘数据最多保留至当天结束；其他时段要求 300 秒内。
        closed = now.time() >= time(15, 0)
        try:
            tick = parse_series_member(
                member, score, now.timestamp(), 86400 if closed else 300
            )
        except (AttributeError, TypeError, ValueError):
            continue
        if not tick or not _price(tick.get("price")):
            continue
        stamp = datetime.fromtimestamp(tick["timestamp"], _TZ)
        if stamp.date() != now.date():
            continue
        if closed and not time(14, 55) <= stamp.time() <= time(15, 10):
            continue
        result[symbol] = {
            "symbol": symbol,
            "timestamp": stamp,
            "current_price": tick["price"],
            "open_price": tick.get("open"),
            "close_price": None,
            "is_stale": False,
            "estimated": True,
            "kind": "尾盘估算" if closed else "盘中估算",
        }
    return result


async def calculate_review(detail, top_k, exclude_st, benchmark):
    stocks, history, index, unknown = await asyncio.to_thread(
        _historical, detail, top_k, exclude_st, benchmark
    )
    target = str(detail["summary"]["prediction_trade_date"])[:10]
    now = datetime.now(_TZ)
    today = now.date().isoformat()
    dates = sorted({d for series in history.values() for d in series})
    starts = {
        s["code"]: history.get(s["code"], {}).get(target, {}).get("open")
        for s in stocks
    }
    from backend.services.stream.market_app.services.remote_redis_source import (
        RemoteRedisDataSource,
    )

    source = RemoteRedisDataSource()
    try:
        quotes = (
            await source.fetch_quotes([s["code"] for s in stocks]) if stocks else []
        )
        snapshot_symbols = {
            StockCodeUtil.to_suffix(q["symbol"])
            for q in quotes
            if not q["is_stale"]
            and q["timestamp"].astimezone(_TZ).date().isoformat() == today
        }
        fallback = await _series_quotes(
            source,
            [s["code"] for s in stocks if s["code"] not in snapshot_symbols],
            now,
        )
        quotes.extend(fallback.values())
    finally:
        if source._client is not None:
            await source._client.aclose()
    live = {
        StockCodeUtil.to_suffix(q["symbol"]): q
        for q in quotes
        if not q["is_stale"]
        and q["timestamp"].astimezone(_TZ).date().isoformat() == today
    }
    rows = []
    index_start = index.get(target, {}).get("open")
    for d in dates:
        values = []
        for s in stocks:
            start = starts[s["code"]]
            close = history.get(s["code"], {}).get(d, {}).get("close")
            values.append((close / start - 1) * 100 if start and close else None)
        close = index.get(d, {}).get("close")
        rows.append(
            {
                "date": d,
                "cumulative": sum(values) / len(values)
                if values and all(v is not None for v in values)
                else None,
                "benchmark": (close / index_start - 1) * 100
                if close and index_start
                else None,
                "coverage": sum(v is not None for v in values),
                "source": "日线",
            }
        )
    realtime_values = {}
    previous_date = None
    try:
        import exchange_calendars as xcals

        previous_date = str(
            xcals.get_calendar("XSHG").previous_session(now.date()).date()
        )
    except Exception:
        pass  # 无法确认上一交易日时，不拼接历史日线和实时行情。
    missing_previous = [
        code for code, q in live.items() if not _price(q.get("close_price"))
    ]
    raw_closes = (
        await asyncio.to_thread(
            _previous_closes, missing_previous, previous_date, "none"
        )
        if missing_previous
        else {}
    )
    for code in missing_previous:
        live[code]["close_price"] = raw_closes.get(code)
    adjusted_closes = (
        await asyncio.to_thread(_previous_closes, list(live), previous_date, "qfq")
        if live
        else {}
    )
    for code, q in live.items():
        reference = _price(q.get("close_price"))
        adjusted_previous = _price(adjusted_closes.get(code))
        factor = (
            adjusted_previous / reference if adjusted_previous and reference else None
        )
        q["adjusted_previous"] = adjusted_previous
        q["adjusted_price"] = (
            _price(q.get("current_price")) * factor
            if factor and _price(q.get("current_price"))
            else None
        )
        q["adjusted_open"] = (
            _price(q.get("open_price")) * factor
            if factor and _price(q.get("open_price"))
            else None
        )
    # 实时开盘价也须换算为前复权基准，缺少基准时不计算。
    if target == today:
        for stock in stocks:
            code = stock["code"]
            if not starts[code] and code in live:
                starts[code] = live[code].get("adjusted_open")
    if today >= target and (not dates or dates[-1] < today):
        for s in stocks:
            code = s["code"]
            q = live.get(code)
            start = starts[code]
            current = _price(q.get("adjusted_price")) if q else None
            previous = _price(q.get("adjusted_previous")) if q else None
            last = (
                history.get(code, {}).get(dates[-1], {}).get("close") if dates else None
            )
            if start and current and target == today:
                realtime_values[code] = (current / start - 1) * 100
            elif start and current and previous and last and dates[-1] == previous_date:
                realtime_values[code] = (last * current / previous / start - 1) * 100
        if realtime_values:
            rows.append(
                {
                    "date": today,
                    "cumulative": sum(realtime_values.values()) / len(stocks)
                    if len(realtime_values) == len(stocks)
                    else None,
                    "benchmark": None,
                    "coverage": len(realtime_values),
                    "source": "尾盘估算"
                    if any(q.get("kind") == "尾盘估算" for q in live.values())
                    else "盘中估算"
                    if any(q.get("estimated") for q in live.values())
                    else "实时",
                }
            )
    for s in stocks:
        code = s["code"]
        start = starts[code]
        hist = history.get(code, {}).get(dates[-1], {}).get("close") if dates else None
        # 日涨跌只需当日行情和昨收，与能否计算组合累计收益无关。
        q = (
            live.get(code)
            if today >= target and (not dates or dates[-1] < today)
            else None
        )
        value = (
            realtime_values.get(code)
            if q
            else ((hist / start - 1) * 100 if hist and start else None)
        )
        s.update(
            {
                "start": start,
                "latest": _price(q.get("adjusted_price")) if q else hist,
                "cumulative": value,
                "daily": (
                    (
                        _price(q.get("adjusted_price"))
                        / _price(q.get("adjusted_previous"))
                        - 1
                    )
                    * 100
                    if q
                    and _price(q.get("adjusted_price"))
                    and _price(q.get("adjusted_previous"))
                    else None
                ),
                "source": q.get("kind", "实时") if q else "日线" if hist else "缺失",
            }
        )
    return {
        "stocks": stocks,
        "days": rows,
        "unknown_st": unknown,
        "requested": top_k,
        "benchmark_start": index_start,
        "quote_coverage": len(live),
        "quote_time": min((q["timestamp"] for q in live.values()), default=None),
        "message": "备注：固定等权组合，历史行情采用前复权；当日涨跌以昨收为基准，累计收益以目标日开盘价为基准。结果为事后观察，未计费用和成交限制。当日行情依上一交易日前复权价与昨收换算，时序数据缺少交易所昨收时使用上日原始收盘价估算，除权除息日可能失真；行情缺失时不展示组合收益。",
    }
