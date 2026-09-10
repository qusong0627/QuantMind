"""Real-time order/trade (execution) event push over Redis.

Big QMT fires ``order_callback(ContextInfo, orderInfo)`` and
``deal_callback(ContextInfo, dealInfo)`` inside the strategy process. We normalize
the QMT order/deal object (ThinkTrader ``m_*`` fields) into a plain dict and
publish it to a Redis channel, so clients receive ``on_stock_order`` /
``on_stock_trade`` callbacks in real time (MiniQMT style) instead of polling.

Channels (also used as capped streams for short replay, xadd + publish):
- ``bigqmt:order_events:{account_id}``
- ``bigqmt:trade_events:{account_id}``

The normalized field names match ``BigQmtXtTrader._order_from_dict`` /
``_trade_from_dict`` so the client can shape them straight into MiniQMT objects.
"""

import json
import time


ORDER_CHANNEL_TEMPLATE = "bigqmt:order_events:{account_id}"
TRADE_CHANNEL_TEMPLATE = "bigqmt:trade_events:{account_id}"
ORDER_ERROR_CHANNEL_TEMPLATE = "bigqmt:order_error_events:{account_id}"
CANCEL_ERROR_CHANNEL_TEMPLATE = "bigqmt:cancel_error_events:{account_id}"
ORDER_IDENTITY_KEY_TEMPLATE = "bigqmt:order_identity:{account_id}:{user_order_id}"

EVENT_ORDER = "order"
EVENT_TRADE = "trade"
EVENT_ORDER_ERROR = "order_error"
EVENT_CANCEL_ERROR = "cancel_error"

# ThinkTrader enum_EEntrustBS (买卖方向, the m_nDirection field), universal across
# 股票/期货/期权. Ref: https://dict.thinktrader.net/innerApi/enum_constants.html
ENTRUST_BUY = 48         # 买入 / 多
ENTRUST_SELL = 49        # 卖出 / 空
ENTRUST_PLEDGE_IN = 81   # 质押入库
ENTRUST_PLEDGE_OUT = 66  # 质押出库

# enum_EEntrustBS (买卖方向, the m_nDirection field), per QMT enum docs.
# 48=买, 49=卖.  Universal across 股票/期货/期权.
#
# Real-world findings from live COrderDetail/CDealDetail callbacks
# (diagnosed via exec_events_debug_raw_fields=True, 2026-07-29):
#   QMT returns m_nDirection=48 **unconditionally** — even for sell orders.
#   m_nOffsetFlag correctly reflects direction (48=买, 49=卖 for stocks).
#   m_nOpType correctly reflects direction (23=买, 24=卖) on orders.
#   query_orders uses m_nOffsetFlag and works correctly in production.
#
# Therefore _extract_direction uses an arbitration chain:
#   Preferred: m_nOffsetFlag (most reliable in live callbacks, matches query_orders)
#   Fallback:  m_nDirection (traditional EEntrustBS; can be stuck at 48 in calls)
#   Arbiter:   when direction≠offset (futures: sell+open=49+48),
#              consult m_nOpType (23/24) to resolve the conflict; for trades
#              (no m_nOpType) trust m_nOffsetFlag (QMT docs confirm stock
#              direction=offset).
#   Last:      order_type (MiniQMT STOCK_BUY=23 / STOCK_SELL=24) and plain text
# Unknown -> "" (the raw value is always preserved so callers can refine).
OFFSET_OPEN = 48
OFFSET_CLOSE = 49
OFFSET_CLOSE_TODAY = 51
OFFSET_CLOSE_YESTERDAY = 52

_BUY_DIRECTIONS = {ENTRUST_BUY, str(ENTRUST_BUY), OFFSET_OPEN, str(OFFSET_OPEN), 23, "23", "BUY", "buy", "B"}
_SELL_DIRECTIONS = {ENTRUST_SELL, str(ENTRUST_SELL), OFFSET_CLOSE, str(OFFSET_CLOSE), OFFSET_CLOSE_TODAY, str(OFFSET_CLOSE_TODAY), OFFSET_CLOSE_YESTERDAY, str(OFFSET_CLOSE_YESTERDAY), 24, "24", "SELL", "sell", "S"}

# Futures (0-15) / ETF option (50-55) opTypes never appear in _BUY/_SELL_DIRECTIONS
# (those hold the stock opTypes 23/24 and the direction/offset enums 48/49/51/52).
# A futures sell-to-open row arrives as direction=49 + offset=48(开仓) + op_type=3(开空):
# without these sets the arbiter cannot decide and falls back to offset=48, which
# reads as BUY. Sides mirror adapters.order_bigqmt._FUTURE_BUY_SIDE and friends
# (kept local here because order_bigqmt imports from this module).
_FUTURES_OP_BUY = frozenset({0, 4, 5, 8, 9, 12, 13, 14})
_FUTURES_OP_SELL = frozenset({1, 2, 3, 6, 7, 10, 11, 15})
_ETF_OPTION_OP_BUY = frozenset({50, 53, 55})
_ETF_OPTION_OP_SELL = frozenset({51, 52, 54})


def order_channel(account_id):
    return ORDER_CHANNEL_TEMPLATE.format(account_id=str(account_id or ""))


def trade_channel(account_id):
    return TRADE_CHANNEL_TEMPLATE.format(account_id=str(account_id or ""))


def order_error_channel(account_id):
    return ORDER_ERROR_CHANNEL_TEMPLATE.format(account_id=str(account_id or ""))


def cancel_error_channel(account_id):
    return CANCEL_ERROR_CHANNEL_TEMPLATE.format(account_id=str(account_id or ""))


def order_identity_key(account_id, user_order_id):
    return ORDER_IDENTITY_KEY_TEMPLATE.format(
        account_id=str(account_id or ""),
        user_order_id=str(user_order_id or ""),
    )


def _attr(obj, names, default=None):
    for name in names:
        if isinstance(obj, dict):
            if name in obj and obj[name] is not None:
                return obj[name]
        else:
            value = getattr(obj, name, None)
            if value is not None:
                return value
    return default


# 报单来源. passorder's 8th argument (strategyName) comes back on the callback
# under this name, not m_strStrategyName -- which is why #174 read "the row
# does not carry the strategy name" off a dump that contained it. #154 measured
# it on a live terminal: blank on the 13 hand-placed rows, the strategy name on
# the 3 the bridge sent, while m_strStrategyName was blank on all 16. Last, so
# a terminal that does populate a real strategy-name field still wins.
_STRATEGY_NAME_FIELDS = (
    "strategyName", "m_strStrategyName", "strategy_name", "m_strSource",
)


def _strategy_name_of(obj):
    """First NON-EMPTY strategy-name candidate, or "".

    Deliberately not _attr: that returns the first non-None, and "" is not
    None. A terminal carrying m_strStrategyName as an empty string would stop
    there and never reach m_strSource -- answering "unnamed" while the name is
    on the object. An empty source is a real answer (a hand-placed order), so
    it has to fall through rather than short-circuit.
    """
    for name in _STRATEGY_NAME_FIELDS:
        value = _attr(obj, [name], "")
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _with_exchange_suffix(raw_code, obj):
    """Append the exchange suffix to a bare instrument code.

    Live order/deal callbacks carry ``m_strInstrumentID`` as the bare code
    ('600000') while the exchange sits in ``m_strExchangeID`` — the same shape
    ``get_trade_detail_data`` rows use. Native MiniQMT callback objects carry
    the full '600000.SH' form, so events must match it. Codes that already
    carry a suffix (or objects without exchange info) pass through unchanged.
    """
    text = str(raw_code or "")
    if not text or "." in text:
        return text
    exchange = str(
        _attr(obj, ["m_strExchangeID", "m_strMarketID", "exchange_id", "market"], "") or ""
    ).strip().upper()
    if exchange:
        return "%s.%s" % (text, exchange)
    return text


def _action_from_direction(direction):
    if direction in _BUY_DIRECTIONS:
        return "BUY"
    if direction in _SELL_DIRECTIONS:
        return "SELL"
    return ""


def date_time_seconds(raw_date, raw_time):
    """Combine a QMT date + time pair into Unix seconds; 0 when unavailable.

    Official docs (dict.thinktrader.net, data_structure) leave the format of
    m_strTradeDate/m_strTradeTime/m_strInsertDate/m_strInsertTime unspecified,
    so tolerate the shapes seen in practice: date '20260819' or '2026-08-19',
    time '093015', '09:30:15(.123)', or a full 'YYYY-MM-DD HH:MM:SS' carried
    in the time field alone. Numeric timestamps pass through (ms normalized).
    """
    if isinstance(raw_time, (int, float)) and not isinstance(raw_time, bool):
        value = float(raw_time)
        if value > 1e11:  # ms epoch
            value /= 1000.0
        if value > 1e8:   # looks like an epoch, not HHMMSS
            return int(value)

    date_digits = "".join(ch for ch in str(raw_date or "") if ch.isdigit())
    time_digits = "".join(ch for ch in str(raw_time or "") if ch.isdigit())
    # Time field carrying its own date ('YYYYMMDDHHMMSS' or the dashed form).
    if len(time_digits) >= 14:
        date_digits, time_digits = time_digits[:8], time_digits[8:14]
    if not date_digits or len(date_digits) < 8:
        return 0
    time_digits = (time_digits + "000000")[:6]  # pad to HHMMSS, drop ms
    try:
        parsed = time.strptime(date_digits[:8] + time_digits, "%Y%m%d%H%M%S")
        return int(time.mktime(parsed))
    except ValueError:
        return 0


def _is_buy(val):
    v = int(val)
    return v in _BUY_DIRECTIONS


def _is_sell(val):
    v = int(val)
    return v in _SELL_DIRECTIONS


def _conflict_resolve(d_val, o_val, obj):
    """When m_nDirection and m_nOffsetFlag disagree, arbitrate via m_nOpType.

    Live diagnosis confirms:
      - Stock sell: direction=48(buy), offset=49(sell), op_type=24(sell) → sell
      - Futures sell+open: direction=49(sell), offset=48(open), op_type=24(sell) → sell
      - Futures buy+close: direction=48(buy), offset=49(close), op_type=23(buy) → buy

    Some terminals report futures callbacks with the stock opType domain
    (23/24, as above); others report the futures/ETF-option opType table
    (futures 0-15, ETF options 50-55), which _BUY/_SELL_DIRECTIONS do not
    hold. Those opTypes arbitrate through _FUTURES_OP_BUY/_SELL and
    _ETF_OPTION_OP_BUY/_SELL instead -- without them the arbiter falls back
    to offset, and offset is open/close on those accounts, so a futures
    sell-to-open (offset=48) resolved as BUY.

    Returns a resolved value, or None if no arbiter can decide.
    """
    op = _attr(obj, ["m_nOpType", "op_type", "order_type"])
    if op is not None:
        try:
            op_int = int(op)
            if op_int in _BUY_DIRECTIONS:
                return d_val if _is_buy(d_val) else o_val if _is_buy(o_val) else op
            if op_int in _SELL_DIRECTIONS:
                return d_val if _is_sell(d_val) else o_val if _is_sell(o_val) else op
            # Futures (0-15) / ETF option (50-55) opTypes: m_nDirection reliably
            # reflects the side for futures (48=买, 49=卖), so prefer d_val.
            if op_int in _FUTURES_OP_BUY or op_int in _ETF_OPTION_OP_BUY:
                return d_val if _is_buy(d_val) else o_val if _is_buy(o_val) else op
            if op_int in _FUTURES_OP_SELL or op_int in _ETF_OPTION_OP_SELL:
                return d_val if _is_sell(d_val) else o_val if _is_sell(o_val) else op
        except (TypeError, ValueError):
            if op in _BUY_DIRECTIONS:
                return d_val if _is_buy(d_val) else o_val if _is_buy(o_val) else op
            if op in _SELL_DIRECTIONS:
                return d_val if _is_sell(d_val) else o_val if _is_sell(o_val) else op
    # no arbiter — trust offset (QMT docs confirm stock direction=offset)
    return o_val


def _extract_direction(obj):
    """Extract buy/sell direction, matching query_orders' reliable logic.

    Priority chain (documented with live-diagnosis justification):
      1. m_nOffsetFlag         — most reliable in live callbacks (matches query_orders)
      2. m_nDirection           — traditional EEntrustBS (can be stuck at 48)
      3. Arbitration: when direction≠offset, consult m_nOpType (orders: 23/24)
         to resolve correctly for both stocks AND futures.
      4. m_nOpType / order_type — last resort fallback.

    The raw value is always returned (even pledge=81) so callers can inspect it;
    _action_from_direction maps only known buy/sell values, leaving others "".

    References
    ----------
    - Live diagnosis 2026-07-29 (COrderDetail/CDealDetail):
      m_nDirection=48 unconditionally, m_nOffsetFlag=48(buy)/49(sell) correct,
      m_nOpType=23(buy)/24(sell) correct (orders only).
    - QMT enum docs: enum_EEntrustBS (48=买,49=卖), enum_EOffset_Flag_Type
      (48=开仓,49=平仓). For stocks direction=offset; for futures they differ.
    - query_orders uses m_nOffsetFlag and works correctly in production.
    """
    offset = _attr(obj, ["m_nOffsetFlag", "offset_flag"])
    direction = _attr(obj, ["m_nDirection", "direction"])

    # 1. offset alone — use it directly (matches query_orders)
    if offset is not None and direction is None:
        try:
            o = int(offset)
            if o in _BUY_DIRECTIONS or o in _SELL_DIRECTIONS:
                return offset
        except (TypeError, ValueError):
            if offset in _BUY_DIRECTIONS or offset in _SELL_DIRECTIONS:
                return offset

    # 2. direction alone — use it
    if direction is not None and offset is None:
        try:
            d = int(direction)
            if d in _BUY_DIRECTIONS or d in _SELL_DIRECTIONS:
                return direction
            if d != 0:
                return direction
        except (TypeError, ValueError):
            if direction in _BUY_DIRECTIONS or direction in _SELL_DIRECTIONS:
                return direction
            return direction

    # 3. both present
    if direction is not None and offset is not None:
        try:
            d = int(direction)
            o = int(offset)
            d_valid = (d in _BUY_DIRECTIONS or d in _SELL_DIRECTIONS)
            o_valid = (o in _BUY_DIRECTIONS or o in _SELL_DIRECTIONS)

            if d_valid and o_valid:
                if d == o:
                    return direction  # agree → use either
                # disagree → arbitrate via m_nOpType
                return _conflict_resolve(d, o, obj)

            if d_valid and not o_valid:
                return direction
            if o_valid and not d_valid:
                return offset
            # neither valid — fall through
        except (TypeError, ValueError):
            pass

    # 4. last resort: m_nOpType / order_type
    return _attr(obj, ["m_nOpType", "op_type", "order_type"])


# Fields we care about when diagnosing a direction misread. Anything starting
# with "m_" is captured automatically; these are the MiniQMT-style names that
# do not match that prefix.
_RAW_SNAPSHOT_EXTRA_FIELDS = (
    "stock_code",
    "order_type",
    "op_type",
    "direction",
    "offset_flag",
    "order_status",
    "order_volume",
    "traded_volume",
    "price",
    "order_id",
    "order_sysid",
    "order_sys_id",
    "trade_id",
    "traded_price",
    "traded_id",
    "strategy_name",
    "strategyName",
    "user_order_id",
    "order_remark",
    "remark",
)


def raw_field_snapshot(obj, max_repr=120):
    """Capture every readable field of a live QMT callback object.

    Direction extraction relies on understanding what ``m_nDirection``,
    ``m_nOffsetFlag`` and ``m_nOpType`` carry in live callbacks. This dumps
    every readable field so one live order settles the question.

    Returns ``{name: "<type> <value>"}``. Never raises: a callback that dies
    while being diagnosed would be worse than no diagnosis.
    """
    snapshot = {}
    try:
        if isinstance(obj, dict):
            names = list(obj.keys())
        else:
            names = [name for name in dir(obj) if name.startswith("m_")]
            names.extend(_RAW_SNAPSHOT_EXTRA_FIELDS)
    except Exception:
        return {"__error__": "dir() failed"}
    seen = set()
    for name in names:
        key = str(name)
        if key in seen or key.startswith("__"):
            continue
        seen.add(key)
        try:
            if isinstance(obj, dict):
                if key not in obj:
                    continue
                value = obj[key]
            else:
                if not hasattr(obj, key):
                    continue
                value = getattr(obj, key)
            if callable(value):
                continue
            text = repr(value)
            if len(text) > max_repr:
                text = text[:max_repr] + "..."
            snapshot[key] = "%s %s" % (type(value).__name__, text)
        except Exception as exc:  # noqa: BLE001 - diagnostics must not break callbacks
            snapshot[key] = "<unreadable: %s>" % exc.__class__.__name__
    return snapshot


def format_raw_snapshot(kind, obj):
    """One-line, GBK-safe rendering of :func:`raw_field_snapshot` for the QMT panel."""
    snapshot = raw_field_snapshot(obj)
    parts = ["%s=%s" % (name, snapshot[name]) for name in sorted(snapshot)]
    return "[bigqmt_exec_raw] %s type=%s %s" % (
        kind,
        type(obj).__name__,
        " | ".join(parts) or "<no fields>",
    )


def normalize_order_event(order, account_id=""):
    """Build a JSON-able order event dict from a Big QMT orderInfo object."""
    direction = _extract_direction(order)
    return {
        "event_type": EVENT_ORDER,
        "account_id": str(_attr(order, ["m_strAccountID", "account_id"], account_id) or account_id or ""),
        "stock_code": _with_exchange_suffix(
            _attr(order, ["m_strInstrumentID", "stock_code", "m_strInstrument"], ""), order
        ),
        "order_sys_id": str(_attr(order, ["m_strOrderSysID", "order_sys_id", "order_sysid", "order_id"], "") or ""),
        # MiniQMT XtOrder.order_volume is the ORIGINAL ordered volume; the
        # remaining volume (m_nVolumeTotal) drops to 0 on the filled push.
        "order_volume": _attr(
            order, ["m_nVolumeTotalOriginal", "m_nVolumeTotal", "order_volume", "volume"]
        ),
        "traded_volume": _attr(order, ["m_nVolumeTraded", "traded_volume"]),
        "price": _attr(order, ["m_dLimitPrice", "price", "limit_price"]),
        "traded_price": _attr(
            order, ["m_dTradedPrice", "traded_price", "avg_traded_price"]
        ),
        # 成交金额。查询路径 (query_orders) 和推送路径要给出同一个
        # 字段，否则走回调的调用方拿不到 cost (issue #173)。
        "trade_amount": _attr(
            order, ["m_dTradeAmount", "trade_amount"]
        ),
        "status": _attr(order, ["m_nOrderStatus", "order_status", "status"]),
        "direction": direction,
        "action": _action_from_direction(direction),
        "offset_flag": _attr(order, ["m_nOffsetFlag", "offset_flag"]),
        # 报单来源 (m_strSource) is where passorder's strategyName lands, so an
        # order this bridge sent names itself here -- no identity store, no
        # remark matching, and it still works for one submitted before this
        # process started (issue #174). The publisher enriches from the journal
        # / redis only when this comes back empty.
        "strategy_name": _strategy_name_of(order),
        "instrument_name": str(
            _attr(order, ["m_strInstrumentName", "instrument_name"], "") or ""),
        "remark": str(_attr(order, ["m_strRemark", "order_remark", "remark", "user_order_id"], "") or ""),
        "user_order_id": str(_attr(order, ["m_strRemark", "user_order_id", "order_remark", "remark"], "") or ""),
        "opt_name": str(_attr(order, ["m_strOptName", "opt_name"], "") or ""),
        # 委托状态描述。官方字段表: m_strCancelInfo=废单原因, m_strErrorMsg=状态信息。
        # 柜台的拒单理由 ("[COUNTER] 资金可用余额不足，尚需[...]") 只在这里,
        # 此前完全没有透传, 客户端看到的是一个没有原因的失败 (issue #60)。
        "status_msg": str(
            _attr(order, ["m_strCancelInfo", "m_strErrorMsg", "m_strStatusMsg",
                          "status_msg", "error_msg"], "") or ""
        ),
        # 官方 Order 字段 m_strInsertDate+m_strInsertTime -> 真实报单 Unix 秒。
        # 0 = 回调对象未携带 (老版本), 客户端会退回 created_at_ts。
        "order_time": date_time_seconds(
            _attr(order, ["m_strInsertDate", "m_strOrderDate", "insert_date", "order_date"]),
            _attr(order, ["m_strInsertTime", "m_strOrderTime", "insert_time", "order_time"]),
        ),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_at_ts": time.time(),
    }


def remember_order_identity(redis_client, account_id, user_order_id, strategy_name="", stock_code="", ttl_seconds=86400):
    user_order_id = str(user_order_id or "").strip()
    if not user_order_id or redis_client is None:
        return None
    payload = {
        "account_id": str(account_id or ""),
        "user_order_id": user_order_id,
        "strategy_name": str(strategy_name or ""),
        "stock_code": str(stock_code or ""),
        "created_at_ts": time.time(),
    }
    try:
        redis_client.setex(
            order_identity_key(account_id, user_order_id),
            int(ttl_seconds or 86400),
            json.dumps(payload, ensure_ascii=False, default=str),
        )
    except Exception:
        pass
    return payload


def order_identity_map(redis_client, account_id, user_order_ids, limit=500):
    """user_order_id -> remembered identity, for a whole result set at once.

    Neither ORDER nor DEAL rows have m_strStrategyName (verified by listing
    every attribute on a live terminal: 120 and 47 of them respectively, and it
    is in neither).

    That was read as "QMT will not tell you the name", which was wrong -- it
    tells you under another name, 报单来源 / m_strSource, and
    _strategy_name_of reads it now (issue #174). This map still earns its keep
    for the case the row cannot cover: an order placed through some OTHER
    channel that the bridge nonetheless remembered, and rows from a terminal
    that blanks the source.

    For orders this bridge submitted, it is remembered here at submit time
    (remember_order_identity), keyed by the user_order_id that goes out as the
    order remark. So a query can put it back. Orders placed by hand in the
    terminal have no remark and stay unattributed -- there is nothing to
    recover.

    One mget rather than N gets: this runs on the main strategy thread, where
    every round trip is charged to the whole bridge.
    """
    wanted = []
    seen = set()
    for user_order_id in user_order_ids:
        text = str(user_order_id or "").strip()
        if text and text not in seen:
            seen.add(text)
            wanted.append(text)
        if len(wanted) >= limit:
            break
    if not wanted or redis_client is None:
        return {}

    keys = [order_identity_key(account_id, text) for text in wanted]
    raws = None
    mget = getattr(redis_client, "mget", None)
    if mget is not None:
        try:
            raws = mget(keys)
        except Exception:
            raws = None
    if raws is None:
        raws = []
        for key in keys:
            try:
                raws.append(redis_client.get(key))
            except Exception:
                raws.append(None)

    found = {}
    for text, raw in zip(wanted, raws):
        if not raw:
            continue
        try:
            found[text] = json.loads(
                raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw))
        except Exception:
            continue
    return found


def enrich_order_identity(redis_client, account_id, event):
    if redis_client is None or not isinstance(event, dict):
        return event
    user_order_id = str(event.get("user_order_id") or event.get("remark") or "").strip()
    if not user_order_id:
        return event
    try:
        raw = redis_client.get(order_identity_key(account_id, user_order_id))
    except Exception:
        raw = None
    if not raw:
        return event
    try:
        identity = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw))
    except Exception:
        return event
    if identity.get("strategy_name"):
        # The row's strategy-name field carries the QMT-side strategy's
        # registered name (the bridge process, e.g. BIGQMT_REDIS_DRYRUN), not
        # the strategy_name the caller passed at order time (#216: an order
        # placed with strategy_name='DaBanStrategy' reported back
        # '大QMT桥接器'). The identity was written at submit time from the
        # caller's own value, so it wins whenever it has a name. Rows with no
        # identity record (hand orders, other processes) keep the row's value.
        event["strategy_name"] = str(identity.get("strategy_name") or "")
    if not event.get("stock_code") and identity.get("stock_code"):
        event["stock_code"] = str(identity.get("stock_code") or "")
    return event


def normalize_trade_event(trade, account_id=""):
    """Build a JSON-able trade (成交) event dict from a Big QMT dealInfo object."""
    direction = _extract_direction(trade)
    return {
        "event_type": EVENT_TRADE,
        "account_id": str(_attr(trade, ["m_strAccountID", "account_id"], account_id) or account_id or ""),
        "stock_code": _with_exchange_suffix(
            _attr(trade, ["m_strInstrumentID", "stock_code"], ""), trade
        ),
        "instrument_name": str(
            _attr(trade, ["m_strInstrumentName", "instrument_name"], "") or ""),
        "order_sys_id": str(_attr(trade, ["m_strOrderSysID", "order_sys_id", "order_sysid", "order_id"], "") or ""),
        "trade_id": str(_attr(trade, ["m_strTradeID", "trade_id"], "") or ""),
        "volume": _attr(trade, ["m_nVolume", "volume", "traded_volume"]),
        "price": _attr(trade, ["m_dPrice", "price", "traded_price"]),
        "amount": _attr(trade, ["m_dTradeAmount", "amount"]),
        "commission": _attr(trade, ["m_dComssion", "m_dCommission", "commission"]),
        "direction": direction,
        "action": _action_from_direction(direction),
        "offset_flag": _attr(trade, ["m_nOffsetFlag", "offset_flag"]),
        "traded_at": str(_attr(trade, ["m_strTradeTime", "traded_at", "trade_time"], "") or ""),
        # 和委托事件一致带上 remark。客户端要在拿到 order_sys_id 之前就把成交
        # 关联到某笔异步委托 (issue #51)，而那时唯一已知的标识就是 remark。
        # QMT 的成交行不一定有这个字段，取不到则为空，客户端退回按
        # order_sys_id 关联。
        "remark": str(_attr(trade, ["m_strRemark", "order_remark", "remark", "user_order_id"], "") or ""),
        "user_order_id": str(_attr(trade, ["m_strRemark", "user_order_id", "order_remark", "remark"], "") or ""),
        # 成交事件此前**根本没有这个键** (issue #174) -- 不是空字符串, 是不存在,
        # 所以客户端 item.get("strategy_name") or "" 只可能答 ""。委托事件一直
        # 有, 成交没有, 于是 on_stock_trade 拿不到策略名。
        #
        # m_strStrategyName 确实两边都没有 (实盘列全部属性: ORDER 120 个、
        # DEAL 47 个)。但当时据此下的结论 ——「大 QMT 不给策略名」—— 是错的:
        # 名字在 m_strSource (报单来源) 里, 就是 passorder 的第 8 个参数
        # strategyName 原样回来。#174 报告人的 dump 里委托和成交都带着它。
        # 所以桥接器自己下的单在这里就能自报家门, 补全只是兜底。
        "strategy_name": _strategy_name_of(trade),
        # 官方 Deal 字段 m_strTradeDate+m_strTradeTime -> 真实成交 Unix 秒。
        # 0 = 未携带, 客户端会退回 created_at_ts。
        "traded_time": date_time_seconds(
            _attr(trade, ["m_strTradeDate", "trade_date", "m_strDealDate"]),
            _attr(trade, ["m_strTradeTime", "trade_time", "traded_at"]),
        ),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_at_ts": time.time(),
    }


# Push-channel topics, used when exec events travel over the quote push channel
# instead of Redis pub/sub. They mirror the Redis channel names minus the
# account (a zmq PUB socket is already per-account).
EXEC_TOPICS = {
    EVENT_ORDER: "exec:order",
    EVENT_TRADE: "exec:trade",
    "order_error": "exec:order_error",
    "cancel_error": "exec:cancel_error",
}


def exec_topic(event_type):
    return EXEC_TOPICS.get(str(event_type or ""), "exec:order")


def publish_exec_event(sink, account_id, event):
    """Publish one exec event through whichever sink the deployment has.

    ``sink`` is either a Redis client or a QuotePushChannel. Redis stays on the
    original per-account channels (streams + pub/sub, so short replay keeps
    working); a push channel gets one topic per event type.

    Exec events used to be Redis-only, which meant a zmq deployment silently
    received no order/trade callbacks at all (issue #76) -- the publish path
    just returned when no Redis client could be built.
    """
    event_type = str((event or {}).get("event_type") or EVENT_ORDER)
    if hasattr(sink, "publish") and not hasattr(sink, "xadd"):
        # QuotePushChannel: publish(topic, data).
        sink.publish(exec_topic(event_type), event)
        return event
    if event_type == EVENT_TRADE:
        return publish_trade_event(sink, account_id, event)
    if event_type == "order_error":
        return publish_order_error_event(sink, account_id, event)
    if event_type == "cancel_error":
        return publish_cancel_error_event(sink, account_id, event)
    return publish_order_event(sink, account_id, event)


def _publish(redis_client, channel, event, maxlen=2000):
    from .adapters.redis_common import (
        note_stream_failure, streams_dead, touch_stream_ttl,
    )

    raw = json.dumps(event, ensure_ascii=False, default=str)
    if not streams_dead():
        try:
            redis_client.xadd(channel, {"payload": raw}, maxlen=maxlen, approximate=True)
            # maxlen 只挡单键膨胀，挡不住键永远不消失：换个账号、停用一个部署，
            # 键就一直留着。续期一次，停写的流自然过期（#213）。
            touch_stream_ttl(redis_client, channel)
        except Exception as exc:
            # redis < 5.0 has no streams: log once, then skip xadd for good.
            # Anything else stays silent and retried, as before (issue #163).
            note_stream_failure(exc)
    redis_client.publish(channel, raw)
    return event


def publish_order_event(redis_client, account_id, event):
    return _publish(redis_client, order_channel(account_id), event)


def publish_trade_event(redis_client, account_id, event):
    return _publish(redis_client, trade_channel(account_id), event)


def publish_order_error_event(redis_client, account_id, event):
    return _publish(redis_client, order_error_channel(account_id), event)


def publish_cancel_error_event(redis_client, account_id, event):
    return _publish(redis_client, cancel_error_channel(account_id), event)


def normalize_order_error_event(order_error, account_id=""):
    """Build a JSON-able order-error event dict (废单/拒单).

    QMT order callbacks carry the failed order via m_strOrderSysID / error info.
    MiniQMT's on_order_error receives an XtOrderError with error_id/error_msg.
    """
    return {
        "event_type": EVENT_ORDER_ERROR,
        "account_id": str(_attr(order_error, ["m_strAccountID", "account_id"], account_id) or account_id or ""),
        "stock_code": str(_attr(order_error, ["m_strInstrumentID", "stock_code"], "") or ""),
        "order_sys_id": str(_attr(order_error, ["m_strOrderSysID", "order_sys_id", "order_sysid", "order_id"], "") or ""),
        "error_id": _attr(order_error, ["m_nErrorID", "error_id", "m_nOrderStatus"]),
        # 废单状态和备注也要带上：客户端 on_order_error 需要 order_remark 关联
        # 回报（issue #64），柜台拒单理由已在 error_msg（m_strCancelInfo，#60）。
        "status": _attr(order_error, ["m_nOrderStatus", "status"]),
        "order_remark": str(_attr(order_error, ["m_strRemark", "order_remark", "remark", "user_order_id"], "") or ""),
        "user_order_id": str(_attr(order_error, ["m_strRemark", "user_order_id", "order_remark", "remark"], "") or ""),
        # m_strCancelInfo 排在最前: 官方字段表把它标为「废单原因」, 而柜台的
        # 拒单理由 ("[COUNTER] 资金可用余额不足，尚需[...]") 正是走这个字段。
        # 之前只读 m_strErrorMsg, 于是 error_msg 常常是空的 (issue #60)。
        "error_msg": str(
            _attr(order_error, ["m_strCancelInfo", "m_strErrorMsg", "error_msg",
                                "m_strMsg", "m_strStatusMsg", "status_msg"], "") or ""
        ),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_at_ts": time.time(),
    }


def normalize_cancel_error_event(cancel_error, account_id=""):
    """Build a JSON-able cancel-error event dict (撤单失败)."""
    return {
        "event_type": EVENT_CANCEL_ERROR,
        "account_id": str(_attr(cancel_error, ["m_strAccountID", "account_id"], account_id) or account_id or ""),
        "stock_code": str(_attr(cancel_error, ["m_strInstrumentID", "stock_code"], "") or ""),
        "order_sys_id": str(_attr(cancel_error, ["m_strOrderSysID", "order_sys_id", "order_sysid", "order_id"], "") or ""),
        "error_id": _attr(cancel_error, ["m_nErrorID", "error_id"]),
        "order_remark": str(_attr(cancel_error, ["m_strRemark", "order_remark", "remark", "user_order_id"], "") or ""),
        "user_order_id": str(_attr(cancel_error, ["m_strRemark", "user_order_id", "order_remark", "remark"], "") or ""),
        "error_msg": str(_attr(cancel_error, ["m_strErrorMsg", "error_msg", "m_strMsg"], "") or ""),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_at_ts": time.time(),
    }
