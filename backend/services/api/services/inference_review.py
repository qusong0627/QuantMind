"""Read-only performance review for an authenticated inference run."""

import asyncio
import math
import re
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


def _previous_session(day):
    """返回 day 的前一交易日；日历不可用时返回 None（不猜）。"""
    try:
        import exchange_calendars as xcals

        calendar = xcals.get_calendar("XSHG")
        try:
            session = calendar.previous_session(date.fromisoformat(day))
        except Exception:
            # 非交易日（节假日等）先归一到最近的前一交易日，再取它的上一交易日。
            session = calendar.previous_session(
                calendar.date_to_session(day, direction="previous")
            )
        return str(session.date())
    except Exception:
        return None


def _breadth(values):
    """涨跌家数统计：None 记为缺失，不参与计数。"""
    rising = sum(1 for v in values if v is not None and v > 0)
    falling = sum(1 for v in values if v is not None and v < 0)
    flat = sum(1 for v in values if v == 0)
    counted = rising + falling + flat
    return {
        "rising": rising,
        "falling": falling,
        "flat": flat,
        "coverage": counted,
        "missing": len(values) - counted,
    }


_INDEX_TENCENT = {
    "000300.SH": "sh000300",
    "000905.SH": "sh000905",
    "000001.SH": "sh000001",
}
_benchmark_cache = {}


async def _fetch_benchmark_quote(symbol, now):
    """读取公开指数最新行情，服务异常时保留本进程最后一笔有效数据。"""
    code = _INDEX_TENCENT.get(symbol)
    if not code:
        return None
    try:
        import aiohttp

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=4)
        ) as session:
            async with session.get(f"https://qt.gtimg.cn/q={code}") as response:
                response.raise_for_status()
                text = (await response.read()).decode("gb18030")
        match = re.search(r"v_" + re.escape(code) + r'="([^"]+)"', text)
        fields = match.group(1).split("~") if match else []
        if len(fields) > 30:
            stamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=_TZ)
            price, opening = _price(fields[3]), _price(fields[5])
            if price and opening and stamp <= now:
                quote = {"timestamp": stamp, "price": price, "open": opening}
                cached = _benchmark_cache.get(symbol)
                if not cached or stamp >= cached["timestamp"]:
                    _benchmark_cache[symbol] = quote
    except (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        UnicodeError,
        ValueError,
        IndexError,
    ):
        pass
    return _benchmark_cache.get(symbol)


def _historical(detail, top_k, exclude_st, benchmark, buy_close=False):
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
        # 尾盘买入时基准是推理日(asof)收盘，需把取数起点前移到 asof。
        frame_start = min(asof, start) if buy_close else start
        frame = hub.fetch_daily_kline_batch(
            [s["code"] for s in stocks], frame_start, today, adjust="qfq"
        )
        for row in frame.to_dict("records"):
            sym = StockCodeUtil.to_suffix(str(row.get("symbol") or ""))
            history.setdefault(sym, {})[str(row.get("trade_date"))[:10]] = {
                "open": _price(row.get("open")),
                "close": _price(row.get("close")),
            }
        for row in hub.fetch_index_kline(benchmark, frame_start, today).to_dict(
            "records"
        ):
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


async def calculate_review(detail, top_k, exclude_st, benchmark, buy_close=False):
    stocks, history, index, unknown = await asyncio.to_thread(
        _historical, detail, top_k, exclude_st, benchmark, buy_close
    )
    target = str(detail["summary"]["prediction_trade_date"])[:10]
    asof_str = str(detail["summary"].get("data_trade_date") or "")[:10]
    now = datetime.now(_TZ)
    dates = sorted({d for series in history.values() for d in series})
    if buy_close and asof_str:
        # 尾盘买入：唯一改动——起始价从目标日开盘改为推理日收盘（含隔夜跳空）。
        starts = {
            s["code"]: history.get(s["code"], {}).get(asof_str, {}).get("close")
            for s in stocks
        }
    else:
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
    if buy_close and asof_str:
        index_start = index.get(asof_str, {}).get("close")
    else:
        index_start = index.get(target, {}).get("open")
    benchmark_quote = None
    start_from_tencent = False
    if (
        latest_date
        and latest_date >= target
        and (not index_start or not index.get(latest_date, {}).get("close"))
    ):
        benchmark_quote = await _fetch_benchmark_quote(benchmark, now)
        if (
            not index_start
            and not buy_close
            and benchmark_quote
            and benchmark_quote["timestamp"].date().isoformat() == target
        ):
            index_start = benchmark_quote["open"]
            start_from_tencent = True
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
    # 无法确认上一交易日时，不拼接历史日线和实时行情。
    previous_date = _previous_session(latest_date)
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
    # 尾盘买入时起始价已是推理日收盘，不再用当日开盘回填。
    if target == latest_date and not buy_close:
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
    # 指数与组合必须属于同一个行情日，QuantDB 日线始终优先。
    benchmark_reason = None
    if rows:
        latest_row = rows[-1]
        if latest_row["benchmark"] is not None:
            latest_row["benchmark_source"] = (
                "QuantDB 日线，腾讯开盘价" if start_from_tencent else "QuantDB 日线"
            )
        else:
            quote = benchmark_quote
            quote_date = quote["timestamp"].date().isoformat() if quote else None
            base = index_start or (
                quote["open"]
                if quote and quote_date == target and not buy_close
                else None
            )
            if quote and quote_date == latest_row["date"] and base:
                latest_row["benchmark"] = (quote["price"] / base - 1) * 100
                latest_row["benchmark_source"] = "腾讯财经"
                latest_row["benchmark_time"] = quote["timestamp"].isoformat()
            elif not quote:
                benchmark_reason = "指数行情暂不可用"
            elif quote_date != latest_row["date"]:
                benchmark_reason = "指数与组合行情日期不一致"
            else:
                benchmark_reason = (
                    "推理日指数收盘价缺失" if buy_close else "目标日指数开盘价缺失"
                )
    else:
        benchmark_reason = "暂无组合行情"

    # 历史日线的日涨跌必须使用同一复权口径的上一交易日收盘价。
    historical_previous = {}
    if latest_date:
        historical_previous_date = _previous_session(latest_date)
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
    # 复盘口径的涨跌家数：严格历史口径——目标日收盘 vs 其前一交易日收盘，
    # 全部取 QuantDB 日线（前复权）。最新日横截面会随行情前移，历史批次每天
    # 打开结果都不同，不能作为复盘结论。
    target_previous = _previous_session(target) if target else None
    target_changes = {}
    if target:
        for s in stocks:
            code = s["code"]
            close = history.get(code, {}).get(target, {}).get("close")
            previous = (
                history.get(code, {}).get(target_previous, {}).get("close")
                if target_previous
                else None
            )
            if close and _price(previous):
                target_changes[code] = (close / previous - 1) * 100
        # 默认取数窗口从目标日开始，前一交易日收盘不在 history 内，需补拉。
        missing = [
            s["code"]
            for s in stocks
            if s["code"] not in target_changes
            and target_previous
            and history.get(s["code"], {}).get(target, {}).get("close")
        ]
        if missing and target_previous:
            filled = await asyncio.to_thread(
                _previous_closes, missing, target_previous, "qfq"
            )
            for code in missing:
                close = history.get(code, {}).get(target, {}).get("close")
                previous = _price(filled.get(code))
                if close and previous:
                    target_changes[code] = (close / previous - 1) * 100
    # 唯一例外：目标日就是今天、且当日日线还没落库时，用 Redis 在线行情
    # （前复权价 / 昨收）临时补。已落库的股票不走在线行情，历史日更不走。
    if target and target == now.date().isoformat():
        for s in stocks:
            code = s["code"]
            if code in target_changes:
                continue
            if history.get(code, {}).get(target, {}).get("close"):
                continue  # 有当日日线但缺昨收：记缺失，不拿实时价凑
            q = live.get(code)
            price = _price(q.get("adjusted_price")) if q else None
            previous = _price(q.get("adjusted_previous")) if q else None
            if price and previous:
                target_changes[code] = (price / previous - 1) * 100
    target_values = [target_changes.get(s["code"]) for s in stocks]
    return {
        "stocks": stocks,
        "days": rows,
        "unknown_st": unknown,
        "requested": top_k,
        "benchmark_start": index_start,
        "benchmark_unavailable_reason": benchmark_reason,
        "buy_close": buy_close,
        "entry_label": "推理日收盘起算" if buy_close else "目标日开盘起算",
        "quote_coverage": len(live),
        "quote_time": min((q["timestamp"] for q in live.values()), default=None),
        "target_date": target,
        "target_breadth": _breadth(target_values),
        "message": "备注：固定等权组合，历史行情采用前复权；上涨/下跌按目标日收盘对前一交易日收盘统计，当日涨跌以昨收为基准，累计收益以目标日开盘价为基准，尾盘买入时以推理日收盘价为基准。结果为事后观察，未计费用和成交限制。当日行情依上一交易日前复权价与昨收换算，时序数据缺少交易所昨收时使用上日原始收盘价估算，除权除息日可能失真；行情缺失时不展示组合收益。",
    }
