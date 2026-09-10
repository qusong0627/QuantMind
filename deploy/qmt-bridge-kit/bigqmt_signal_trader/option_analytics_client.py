"""Client-side option metadata, market-price, and chain analytics orchestration.

The numerical Black-Scholes-Merton implementation lives in
``option_analytics``.  This module owns the higher-level ``BigQmtXtData``
workflow so the already-large compatibility layer only exposes thin public
delegates.  It is client-only: QMT strategy/runtime modules do not import it.
"""

import datetime as _dt
import math

from .logging_setup import get_logger
from .option_analytics import calculate_option_analytics, normalize_option_type


log = get_logger("option_analytics_client")


def _latest_close(market_data, stock_code):
    """Extract the newest finite positive close from normalized market data."""
    if not isinstance(market_data, dict):
        return None
    frame = market_data.get(stock_code)
    if frame is None:
        frame = market_data.get(str(stock_code).upper())
    if frame is None:
        return None

    values = None
    if hasattr(frame, "columns") and "close" in getattr(frame, "columns", ()):
        values = frame["close"]
    elif isinstance(frame, dict):
        values = frame.get("close")
    elif isinstance(frame, (list, tuple)):
        for row in reversed(frame):
            if isinstance(row, dict) and "close" in row:
                values = [row.get("close")]
                break

    if values is None:
        return None
    if hasattr(values, "dropna"):
        values = values.dropna()
    try:
        value = values.iloc[-1]
    except Exception:
        if isinstance(values, dict):
            if not values:
                return None
            value = values[sorted(values.keys())[-1]]
        elif isinstance(values, (list, tuple)):
            value = values[-1] if values else None
        else:
            value = values
    return _positive_number(value)


def _latest_field_value(market_data, stock_code, field_name):
    """Return the newest raw field value from dict/list/DataFrame data."""
    if not isinstance(market_data, dict):
        return None
    frame = market_data.get(stock_code)
    if frame is None:
        frame = market_data.get(str(stock_code).upper())
    if frame is None:
        return None

    if hasattr(frame, "columns") and field_name in getattr(frame, "columns", ()):
        values = frame[field_name]
        if hasattr(values, "dropna"):
            values = values.dropna()
        try:
            return values.iloc[-1]
        except Exception:
            return None

    if isinstance(frame, dict):
        values = frame.get(field_name)
        if isinstance(values, dict):
            if not values:
                return None
            return values[sorted(values.keys())[-1]]
        if isinstance(values, (list, tuple)):
            if not values:
                return None
            if field_name in ("bidPrice", "askPrice"):
                return values[-1] if isinstance(values[-1], (list, tuple)) else values
            return values[-1]
        return values

    if isinstance(frame, (list, tuple)):
        for row in reversed(frame):
            if isinstance(row, dict) and field_name in row:
                return row.get(field_name)
    return None


def _positive_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _best_book_level(value):
    if isinstance(value, (list, tuple)):
        for item in value:
            number = _positive_number(item)
            if number is not None:
                return number
        return None
    return _positive_number(value)


def _latest_tick_price(market_data, stock_code):
    """Return ``(price, source)`` from a tick snapshot/history result."""
    last_price = _positive_number(
        _latest_field_value(market_data, stock_code, "lastPrice")
    )
    if last_price is not None:
        return last_price, "tick_last"

    bid = _best_book_level(
        _latest_field_value(market_data, stock_code, "bidPrice")
    )
    ask = _best_book_level(
        _latest_field_value(market_data, stock_code, "askPrice")
    )
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0, "tick_mid"
    if bid is not None:
        return bid, "tick_bid"
    if ask is not None:
        return ask, "tick_ask"
    return None, None


def _detail_value(detail, *names):
    if not isinstance(detail, dict):
        return None
    for name in names:
        if name in detail and detail[name] not in (None, ""):
            return detail[name]
    lowered = {str(key).lower(): value for key, value in detail.items()}
    for name in names:
        value = lowered.get(str(name).lower())
        if value not in (None, ""):
            return value
    return None


def _option_underlying_code(detail):
    code = str(_detail_value(
        detail, "OptUndlCode", "underlying_code", "underlyingCode"
    ) or "").strip().upper()
    market = str(_detail_value(
        detail, "OptUndlMarket", "underlying_market", "underlyingMarket"
    ) or "").strip().upper()
    if not code:
        raise ValueError("option detail has no underlying code")
    if "." not in code and market:
        code = "%s.%s" % (code, market)
    return code


def _option_as_of(value):
    if value is None:
        return _dt.datetime.now()
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime.combine(value, _dt.time())
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 100000000000:
            timestamp /= 1000.0
        return _dt.datetime.fromtimestamp(timestamp)
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S", "%Y-%m-%d", "%Y%m%d"):
        try:
            return _dt.datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise ValueError("as_of must be datetime, timestamp, YYYYMMDD or ISO local time")


def _infer_chain_dividend_yield(
        details, prices, underlying_price, as_of=None, risk_free_rate=None):
    """Infer a robust chain-level continuous yield from put-call parity."""
    spot = _positive_number(underlying_price)
    if spot is None:
        return None, 0, {}
    valued_at = _option_as_of(as_of)
    pairs = {}
    for raw_code, detail in (details or {}).items():
        code = str(raw_code or "").strip().upper()
        price = _positive_number((prices or {}).get(code))
        strike = _positive_number(_detail_value(
            detail, "OptExercisePrice", "exercise_price", "strike_price"
        ))
        expiry_value = _detail_value(
            detail, "ExpireDate", "EndDelivDate", "expire_date", "expiry_date"
        )
        if price is None or strike is None or expiry_value is None:
            continue
        try:
            kind = normalize_option_type(
                _detail_value(detail, "optType", "OptType", "option_type")
            )
            expiry_text = str(expiry_value).strip().replace("-", "")[:8]
            expiry_at = _dt.datetime.strptime(expiry_text, "%Y%m%d").replace(hour=15)
        except (TypeError, ValueError):
            continue
        years = (expiry_at - valued_at).total_seconds() / (365.0 * 86400.0)
        if years <= 0:
            continue
        rate = risk_free_rate
        if rate is None:
            rate = _detail_value(
                detail, "OptUndlRiskFreeRate", "risk_free_rate", "riskFreeRate"
            )
        try:
            rate = float(rate or 0.0)
        except (TypeError, ValueError):
            continue
        key = (expiry_text, strike)
        pair = pairs.setdefault(key, {"years": years, "rate": rate})
        pair[kind] = price

    implied = []
    implied_by_strike = {}
    for key, pair in pairs.items():
        if "C" not in pair or "P" not in pair:
            continue
        strike = key[1]
        years = pair["years"]
        rate = pair["rate"]
        discounted_spot = pair["C"] - pair["P"] + strike * math.exp(-rate * years)
        if discounted_spot <= 0:
            continue
        try:
            value = -math.log(discounted_spot / spot) / years
        except (ValueError, ZeroDivisionError):
            continue
        if math.isfinite(value) and -0.10 <= value <= 0.50:
            implied.append(value)
            implied_by_strike[key] = value
    if not implied:
        return None, 0, {}
    implied.sort()
    middle = len(implied) // 2
    if len(implied) % 2:
        median = implied[middle]
    else:
        median = (implied[middle - 1] + implied[middle]) / 2.0
    return median, len(implied), implied_by_strike


def _analytics_from_detail(
        opt_code, detail, option_price, underlying_price, as_of=None,
        risk_free_rate=None, dividend_yield=0.0, price_period="1m",
        option_price_source="argument", underlying_price_source="argument",
        dividend_yield_source="argument"):
    """Build one contract result from already-fetched metadata and prices."""
    option_type = _detail_value(detail, "optType", "OptType", "option_type")
    strike_price = _detail_value(
        detail, "OptExercisePrice", "exercise_price", "strike_price"
    )
    expiry_value = _detail_value(
        detail, "ExpireDate", "EndDelivDate", "expire_date", "expiry_date"
    )
    if option_type is None:
        raise ValueError("option detail has no option type")
    if strike_price is None:
        raise ValueError("option detail has no exercise price")
    if expiry_value is None:
        raise ValueError("option detail has no expiry date")

    expiry_text = str(expiry_value).strip().replace("-", "")[:8]
    try:
        expiry_date = _dt.datetime.strptime(expiry_text, "%Y%m%d")
    except ValueError:
        raise ValueError("unsupported option expiry date %r" % expiry_value)
    expiry_at = expiry_date.replace(hour=15)
    valued_at = _option_as_of(as_of)
    days_to_expiry = (expiry_at - valued_at).total_seconds() / 86400.0
    if days_to_expiry <= 0:
        raise ValueError(
            "option expired at %s" % expiry_at.strftime("%Y-%m-%d %H:%M:%S")
        )

    if risk_free_rate is None:
        risk_free_rate = _detail_value(
            detail, "OptUndlRiskFreeRate", "risk_free_rate", "riskFreeRate"
        )
    if risk_free_rate in (None, ""):
        risk_free_rate = 0.0

    result = calculate_option_analytics(
        option_type=option_type,
        underlying_price=underlying_price,
        strike_price=strike_price,
        option_price=option_price,
        days_to_expiry=days_to_expiry,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
    )
    result.update({
        "option_code": str(opt_code).upper(),
        "underlying_code": _option_underlying_code(detail),
        "expiry_date": expiry_at.strftime("%Y%m%d"),
        "as_of": valued_at.strftime("%Y-%m-%d %H:%M:%S"),
        "price_period": str(price_period),
        "option_price_source": option_price_source,
        "underlying_price_source": underlying_price_source,
        "dividend_yield_source": dividend_yield_source,
        "greek_units": {
            "vega": "price per 1.00 volatility",
            "vega_1pct": "price per 1 volatility point",
            "theta_per_year": "price per calendar year",
            "theta_per_day": "price per calendar day",
            "rho": "price per 1.00 rate",
            "rho_1pct": "price per 1 rate point",
        },
    })
    return result


def _resolve_market_prices(data_client, stock_codes, price_period="1m"):
    """Resolve prices in at most three batches and retain source labels."""
    codes = []
    seen = set()
    for raw_code in stock_codes or []:
        code = str(raw_code or "").strip().upper()
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    prices = {}
    sources = {}
    if not codes:
        return prices, sources

    period = str(price_period or "1m")

    def _read_close(target_codes, requested_period):
        if not target_codes:
            return
        try:
            data = data_client.get_market_data_ex(
                field_list=["close"],
                stock_list=target_codes,
                period=requested_period,
                count=1,
                fill_data=False,
            ) or {}
        except Exception as exc:
            log.warning(
                "option price %s close batch failed for %d code(s): %s",
                requested_period, len(target_codes), exc,
            )
            return
        for code in target_codes:
            value = _latest_close(data, code)
            if value is not None:
                prices[code] = value
                sources[code] = "%s_close" % requested_period

    def _read_tick(target_codes):
        if not target_codes:
            return
        try:
            data = data_client.get_market_data_ex(
                field_list=["lastPrice", "bidPrice", "askPrice"],
                stock_list=target_codes,
                period="tick",
                count=1,
                fill_data=False,
            ) or {}
        except Exception as exc:
            log.warning(
                "option price tick batch failed for %d code(s): %s",
                len(target_codes), exc,
            )
            return
        for code in target_codes:
            value, source = _latest_tick_price(data, code)
            if value is not None:
                prices[code] = value
                sources[code] = source

    if period.lower() == "tick":
        _read_tick(codes)
    else:
        _read_close(codes, period)
        _read_tick([code for code in codes if code not in prices])

    unresolved = [code for code in codes if code not in prices]
    if unresolved and period.lower() != "1d":
        _read_close(unresolved, "1d")
    return prices, sources


def _option_detail(data_client, stock_code):
    """Prefer FormulaServer metadata and fall back to the option RPC."""
    try:
        instrument = data_client.get_instrument_detail(stock_code)
        if isinstance(instrument, dict) and instrument:
            detail = dict(instrument)
            for key in ("ExtendInfo", "extendInfo", "extend_info"):
                extended = instrument.get(key)
                if isinstance(extended, dict):
                    for name, value in extended.items():
                        detail.setdefault(name, value)
            required = (
                _detail_value(detail, "optType", "OptType", "option_type"),
                _detail_value(
                    detail, "OptExercisePrice", "exercise_price", "strike_price"
                ),
                _detail_value(
                    detail, "ExpireDate", "EndDelivDate", "expire_date", "expiry_date"
                ),
            )
            if all(value not in (None, "") for value in required):
                _option_underlying_code(detail)
                return detail
    except Exception:
        pass
    return data_client.get_option_detail_data(stock_code)


def get_option_analytics(
        data_client, opt_code, option_price=None, underlying_price=None,
        as_of=None, risk_free_rate=None, dividend_yield=None,
        price_period="1m", include_native_iv=False):
    """Return local IV and standard Greeks for one option contract."""
    code = str(opt_code or "").strip().upper()
    if not code:
        raise ValueError("opt_code is required")
    detail = _option_detail(data_client, code)
    if not isinstance(detail, dict) or not detail:
        raise ValueError("no option detail for %s" % code)
    underlying_code = _option_underlying_code(detail)

    needed_codes = []
    if option_price is None:
        needed_codes.append(code)
    if underlying_price is None:
        needed_codes.append(underlying_code)
    prices, sources = _resolve_market_prices(
        data_client, needed_codes, price_period=price_period
    )
    if option_price is None:
        option_price = prices.get(code)
        if option_price is None:
            raise ValueError(
                "no usable market price for %s (tried %s, tick, 1d)"
                % (code, price_period)
            )
        option_source = sources.get(code, "unknown")
    else:
        option_source = "argument"
    if underlying_price is None:
        underlying_price = prices.get(underlying_code)
        if underlying_price is None:
            raise ValueError(
                "no usable market price for %s (tried %s, tick, 1d)"
                % (underlying_code, price_period)
            )
        underlying_source = sources.get(underlying_code, "unknown")
    else:
        underlying_source = "argument"
    if dividend_yield is None:
        dividend_yield = 0.0
        dividend_source = "default_zero"
    else:
        dividend_source = "argument"

    result = _analytics_from_detail(
        code, detail, option_price, underlying_price, as_of=as_of,
        risk_free_rate=risk_free_rate, dividend_yield=dividend_yield,
        price_period=price_period, option_price_source=option_source,
        underlying_price_source=underlying_source,
        dividend_yield_source=dividend_source,
    )
    if include_native_iv:
        try:
            result["native_iv"] = data_client.get_option_iv(code)
        except Exception as exc:
            result["native_iv"] = None
            result["native_iv_error"] = str(exc)
    return result


def get_option_chain_analytics(
        data_client, undl_code, dedate, opttype="", isavailavle=False,
        underlying_price=None, as_of=None, risk_free_rate=None,
        dividend_yield=None, price_period="1m"):
    """Calculate a whole expiry's IV/Greeks with batched market reads."""
    codes = list(data_client.get_option_list(
        undl_code, dedate, opttype=opttype, isavailavle=isavailavle
    ) or [])
    details = {}
    detail_errors = {}
    for raw_code in codes:
        code = str(raw_code).strip().upper()
        try:
            detail = _option_detail(data_client, code)
            if not isinstance(detail, dict) or not detail:
                raise ValueError("empty option detail")
            details[code] = detail
        except Exception as exc:
            detail_errors[code] = str(exc)

    underlying_code = str(undl_code or "").strip().upper()
    if details:
        underlying_code = _option_underlying_code(next(iter(details.values())))
    price_codes = list(details.keys())
    if underlying_price is None and underlying_code:
        price_codes.append(underlying_code)
    prices, sources = _resolve_market_prices(
        data_client, price_codes, price_period=price_period
    )
    if underlying_price is None:
        underlying_price = prices.get(underlying_code)
        underlying_source = sources.get(underlying_code, "unknown")
    else:
        underlying_source = "argument"

    parity_pair_count = 0
    parity_yields_by_strike = {}
    if dividend_yield is None:
        dividend_yield, parity_pair_count, parity_yields_by_strike = (
            _infer_chain_dividend_yield(
                details, prices, underlying_price,
                as_of=as_of, risk_free_rate=risk_free_rate,
            )
        )
        if dividend_yield is None:
            dividend_yield = 0.0
            dividend_source = "default_zero"
        else:
            dividend_source = "put_call_parity_median"
    else:
        dividend_source = "argument"

    contracts = []
    for raw_code in codes:
        code = str(raw_code).strip().upper()
        if code in detail_errors:
            contracts.append({
                "option_code": code,
                "analytics_error": detail_errors[code],
            })
            continue
        detail = details[code]
        option_price = prices.get(code)
        option_source = sources.get(code, "unknown")
        base = {
            "option_code": code,
            "underlying_code": underlying_code,
            "option_price": option_price,
            "option_price_source": option_source,
            "underlying_price_source": underlying_source,
            "dividend_yield": dividend_yield,
            "dividend_yield_source": dividend_source,
            "option_type": _detail_value(detail, "optType", "OptType", "option_type"),
            "strike_price": _detail_value(
                detail, "OptExercisePrice", "exercise_price", "strike_price"
            ),
            "expiry_date": str(_detail_value(
                detail, "ExpireDate", "EndDelivDate", "expire_date", "expiry_date"
            ) or ""),
        }
        item = None
        try:
            if underlying_price is None:
                raise ValueError(
                    "no usable market price for %s (tried %s, tick, 1d)"
                    % (underlying_code, price_period)
                )
            if option_price is None:
                raise ValueError(
                    "no usable market price for %s (tried %s, tick, 1d)"
                    % (code, price_period)
                )
            item = _analytics_from_detail(
                code, detail, option_price, underlying_price, as_of=as_of,
                risk_free_rate=risk_free_rate, dividend_yield=dividend_yield,
                price_period=price_period, option_price_source=option_source,
                underlying_price_source=underlying_source,
                dividend_yield_source=dividend_source,
            )
        except Exception as exc:
            if (dividend_source == "put_call_parity_median"
                    and "no-arbitrage bounds" in str(exc)):
                expiry_text = str(_detail_value(
                    detail, "ExpireDate", "EndDelivDate",
                    "expire_date", "expiry_date"
                ) or "").strip().replace("-", "")[:8]
                strike = _positive_number(_detail_value(
                    detail, "OptExercisePrice", "exercise_price", "strike_price"
                ))
                strike_yield = parity_yields_by_strike.get((expiry_text, strike))
                if strike_yield is not None:
                    try:
                        item = _analytics_from_detail(
                            code, detail, option_price, underlying_price,
                            as_of=as_of, risk_free_rate=risk_free_rate,
                            dividend_yield=strike_yield, price_period=price_period,
                            option_price_source=option_source,
                            underlying_price_source=underlying_source,
                            dividend_yield_source="put_call_parity_strike",
                        )
                    except Exception as retry_exc:
                        exc = retry_exc
            if item is None:
                base["analytics_error"] = str(exc)
                item = base
        contracts.append(item)

    valid_count = sum(1 for item in contracts if "analytics_error" not in item)
    return {
        "underlying_code": underlying_code,
        "underlying_price": underlying_price,
        "underlying_price_source": underlying_source,
        "dividend_yield": dividend_yield,
        "dividend_yield_source": dividend_source,
        "dividend_yield_pair_count": parity_pair_count,
        "dedate": str(dedate),
        "as_of": _option_as_of(as_of).strftime("%Y-%m-%d %H:%M:%S"),
        "price_period": str(price_period),
        "count": len(contracts),
        "valid_count": valid_count,
        "error_count": len(contracts) - valid_count,
        "contracts": contracts,
    }


__all__ = ["get_option_analytics", "get_option_chain_analytics"]
