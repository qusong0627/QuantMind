"""Read-only performance review for an authenticated inference run."""

import asyncio
import math
from datetime import date, datetime
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
    """读取最后一笔时序行情；保留其实际时间，直到出现更新。"""
    from backend.services.simulation.services.redis_series_quote import (
        parse_series_member,
    )

    if not symbols:
        return {}
    try:
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
        try:
            tick = parse_series_member(
                member, score, now.timestamp(), int(now.timestamp())
            )
        except (AttributeError, TypeError, ValueError):
            continue
        if not tick or not _price(tick.get("price")):
            continue
        stamp = datetime.fromtimestamp(tick["timestamp"], _TZ)
        result[symbol] = {
            "symbol": symbol,
            "timestamp": stamp,
            "current_price": tick["price"],
            "open_price": tick.get("open"),
            "close_price": None,
            "is_stale": False,
            "estimated": True,
            "kind": "最近行情估算",
        }
    return result


async def calculate_review(detail, top_k, exclude_st, benchmark):
    stocks, history, index, unknown = await asyncio.to_thread(
        _historical, detail, top_k, exclude_st, benchmark
    )
    target = str(detail["summary"]["prediction_trade_date"])[:10]
    now = datetime.now(_TZ)
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
            await source.fetch_quotes([s["code"] for s in stocks], max_age_sec=None)
            if stocks
            else []
        )
        fallback = await _series_quotes(source, [s["code"] for s in stocks], now)
        quotes.extend(fallback.values())
    finally:
        if source._client is not None:
            await source._client.aclose()
    live = {}
    for q in quotes:
        code = StockCodeUtil.to_suffix(q["symbol"])
        if q["timestamp"] <= now and (
            code not in live or q["timestamp"] > live[code]["timestamp"]
        ):
            if q.get("is_stale"):
                q["kind"] = "最近行情"
            live[code] = q
    quote_dates = [
        q["timestamp"].astimezone(_TZ).date().isoformat() for q in live.values()
    ]
    latest_date = max(dates + quote_dates, default=None)
    live = {
        code: q
        for code, q in live.items()
        if q["timestamp"].astimezone(_TZ).date().isoformat() == latest_date
        and latest_date >= target
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
            xcals.get_calendar("XSHG")
            .previous_session(date.fromisoformat(latest_date))
            .date()
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
    if target == latest_date:
        for stock in stocks:
            code = stock["code"]
            if not starts[code] and code in live:
                starts[code] = live[code].get("adjusted_open")
    # 每只股票优先使用该行情日的 QuantDB 日线，缺失时使用 Redis。
    # 前复权换算必须有上一交易日的原始昨收和前复权收盘价。
    if latest_date and latest_date >= target:
        for stock in stocks:
            code = stock["code"]
            if history.get(code, {}).get(latest_date, {}).get("close"):
                continue
            q = live.get(code)
            start = starts[code]
            current = _price(q.get("adjusted_price")) if q else None
            previous = _price(q.get("adjusted_previous")) if q else None
            last = history.get(code, {}).get(previous_date, {}).get("close")
            if start and current and target == latest_date:
                realtime_values[code] = (current / start - 1) * 100
            elif start and current and previous and last:
                realtime_values[code] = (last * current / previous / start - 1) * 100
        if realtime_values:
            values = []
            daily_count = 0
            for stock in stocks:
                code = stock["code"]
                start = starts[code]
                close = history.get(code, {}).get(latest_date, {}).get("close")
                if start and close:
                    values.append((close / start - 1) * 100)
                    daily_count += 1
                else:
                    values.append(realtime_values.get(code))
            has_estimate = any(live[code].get("estimated") for code in realtime_values)
            has_old = any(live[code].get("is_stale") for code in realtime_values)
            live_source = (
                "最近行情估算" if has_estimate else "最近行情" if has_old else "实时"
            )
            index_close = index.get(latest_date, {}).get("close")
            latest_row = {
                "date": latest_date,
                "cumulative": sum(values) / len(values)
                if values and all(value is not None for value in values)
                else None,
                "benchmark": (index_close / index_start - 1) * 100
                if index_close and index_start
                else None,
                "coverage": sum(value is not None for value in values),
                "source": f"日线+{live_source}" if daily_count else live_source,
            }
            if rows and rows[-1]["date"] == latest_date:
                rows[-1] = latest_row
            else:
                rows.append(latest_row)
    # 历史日线的日涨跌必须使用同一复权口径的上一交易日收盘价。
    historical_previous = {}
    if latest_date:
        try:
            import exchange_calendars as xcals

            historical_previous_date = str(
                xcals.get_calendar("XSHG")
                .previous_session(date.fromisoformat(latest_date))
                .date()
            )
        except Exception:
            historical_previous_date = None
        if historical_previous_date:
            historical_previous = {
                s["code"]: history.get(s["code"], {})
                .get(historical_previous_date, {})
                .get("close")
                for s in stocks
            }
            missing = [
                s["code"]
                for s in stocks
                if history.get(s["code"], {}).get(latest_date, {}).get("close")
                and not _price(historical_previous.get(s["code"]))
            ]
            if missing:
                historical_previous.update(
                    await asyncio.to_thread(
                        _previous_closes, missing, historical_previous_date, "qfq"
                    )
                )
    for s in stocks:
        code = s["code"]
        start = starts[code]
        hist = (
            history.get(code, {}).get(latest_date, {}).get("close")
            if latest_date
            else None
        )
        # 日涨跌只需当日行情和昨收，与能否计算组合累计收益无关。
        q = (
            live.get(code)
            if latest_date
            and latest_date >= target
            and not history.get(code, {}).get(latest_date, {}).get("close")
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
                    else (hist / historical_previous[code] - 1) * 100
                    if not q and hist and _price(historical_previous.get(code))
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
