"""QMT 上游渠道适配（与 TDX / THS 并列的第四个源）。

数据来自**同机**伴生桥 ``http://127.0.0.1:17720``：伴生桥经命名管道问 QMT 策略，
策略本身零网络、只读（下单/撤单方法在伴生桥侧被拒）。桥这边只做两件事：

1. 转发 ``/api/v1/qmt/call``（鉴权后透传，方法白名单在伴生桥）；
2. 把 QMT 的资产/持仓/委托/成交归一成**桥内既有口径**，让调用方（quantmind、
   网页交易台）不用区分上游是通达信还是 QMT。

纯函数、无 IO：Linux 侧可直接单测（见 ``tests/test_qmt_source.py``）。
"""
from __future__ import annotations

#: 桥允许经 ``source=qmt`` 透传的方法（只读）。写操作（order/cancel/passorder）
#: 一律 403——QMT 的下单闸门在伴生桥与策略侧，桥这边也不留后门。
QMT_READONLY_METHODS = frozenset({
    # 行情
    "snapshot", "get_market_snapshot", "get_full_tick",
    "get_market_data", "get_market_data_ex", "get_trading_dates",
    "quote_tap_status",
    # 账户 / 持仓 / 委托 / 成交
    "ping", "get_asset", "get_positions", "account_snapshot",
    "query_orders", "query_trades", "get_instrument_detail",
    # 只读诊断（资产行全字段 / 字段名，不返回订单值）
    "query_account_infos", "describe_trade_detail_fields",
})

#: QMT 委托状态码 → 桥内 OrderStatus 字符串（与 quantmind qmt_exec 同口径）
QMT_STATUS_MAP: dict[int, str] = {
    48: "pending",            # 未申报
    49: "submitted",          # 等待申报
    50: "submitted",          # 已申报
    51: "submitted",          # 报撤中（未确认，可能仍成交）
    52: "partially_filled",   # 部成待撤
    53: "partial_cancelled",  # 部撤
    54: "cancelled",          # 已撤
    55: "partially_filled",   # 部分成交
    56: "filled",             # 全部成交
    57: "rejected",           # 废单
    255: "submitted",         # 未知（不误判为终态）
}

#: 委托类型 23=买 24=卖；direction 48=买 49=卖（xtconstant）
_BUY_CODES = {23}
_SELL_CODES = {24}
_BUY_DIRECTION = {48}
_SELL_DIRECTION = {49}


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value):
    num = _f(value)
    return int(num) if num is not None else 0


def _side(order: dict) -> str:
    """QMT 委托方向 → ``buy`` / ``sell``；认不出返回空串（不猜）。

    kit 的 OrderSnapshot/TradeSnapshot 用 ``action``（BUY/SELL）；
    旧版/裸字典可能给 ``side`` 或 ``order_type``/``direction`` 数字码。
    """
    raw = str(order.get("side") or order.get("action") or "").upper()
    if raw in ("BUY", "SELL"):
        return raw.lower()
    code = _i(order.get("order_type"))
    if code in _BUY_CODES:
        return "buy"
    if code in _SELL_CODES:
        return "sell"
    direction = _i(order.get("direction"))
    if direction in _BUY_DIRECTION:
        return "buy"
    if direction in _SELL_DIRECTION:
        return "sell"
    return ""


#: 英文状态串（kit 的 ccxt 风格）→ 桥内状态
_STATUS_TEXT = {
    "pending": "pending",
    "submitted": "submitted",
    "open": "submitted",
    "partially_filled": "partially_filled",
    "partial": "partially_filled",
    "filled": "filled",
    "closed": "filled",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "partial_cancelled": "partial_cancelled",
    "rejected": "rejected",
}


def _status(order: dict) -> str:
    """状态归一：优先数字状态码（kit 的 ``status`` 也是数字串），其次英文串。"""
    code = _i(order.get("status_code"))
    if code:
        return QMT_STATUS_MAP.get(code, "submitted")
    raw = order.get("status")
    code = _i(raw)
    if code:
        return QMT_STATUS_MAP.get(code, "submitted")
    text = str(raw or "").strip().lower()
    return _STATUS_TEXT.get(text, text or "submitted")


def _clock(value) -> str:
    """QMT 的 ``order_time`` 是当日秒数；统一成 HH:MM:SS（认不出就原样转字符串）。"""
    raw = str(value or "")
    if not raw:
        return ""
    if raw.isdigit():
        seconds = int(raw)
        if 0 <= seconds < 86400:
            return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return raw


def normalize_asset(raw: dict) -> dict:
    """QMT 资产 → 桥 ``/account/query`` 的 asset 口径（TDX 同名字段）。

    ``balance``/``asset`` 在 TDX 里是「总资产」的两个别名；QMT 只有
    ``total_asset``，这里同时映射过去，调用方不用改。
    """
    raw = raw or {}
    total = _f(raw.get("total_asset"))
    return {
        "currency": raw.get("currency") or "CNY",
        "balance": total,
        "cash": _f(raw.get("cash")),
        "asset": total,
        "market_value": _f(raw.get("market_value")),
        # 以下为 QMT 独有，保留给页面展示
        "frozen_cash": _f(raw.get("frozen_cash")),
        "total_asset": total,
    }


def normalize_positions(raw, quotes: dict | None = None) -> list[dict]:
    """QMT 持仓 → ``[{stock_code, cost_price, total_volume, available_volume, ...}]``。

    ``raw`` 兼容 dict（伴生桥按代码索引）与 list 两种形态。
    ``quotes`` 是伴生桥合并进来的最新价（``{code: {last_price, market_value, pnl}}``），
    没有就保持 None——不拿成本价冒充现价。
    """
    quotes = quotes or {}
    if isinstance(raw, dict):
        rows = [dict(v or {}, stock_code=(v or {}).get("stock_code") or k)
                for k, v in raw.items()]
    else:
        rows = [dict(r) for r in (raw or []) if isinstance(r, dict)]
    out = []
    for row in rows:
        code = str(row.get("stock_code") or "")
        volume = _i(row.get("volume", row.get("total_volume")))
        if not code or volume <= 0:
            continue
        quote = quotes.get(code) if isinstance(quotes, dict) else None
        last_price = _f(row.get("last_price"))
        if last_price is None:
            # 实时 RPC 回落的持仓带的是 price（kit 命名），不是 last_price
            last_price = _f(row.get("price"))
        if last_price is None and isinstance(quote, dict):
            last_price = _f(quote.get("last_price"))
        market_value = _f(row.get("market_value"))
        if market_value is None and isinstance(quote, dict):
            market_value = _f(quote.get("market_value"))
        cost = _f(row.get("cost", row.get("open_price", row.get("cost_price"))))
        pnl = _f(row.get("pnl"))
        if pnl is None and isinstance(quote, dict):
            pnl = _f(quote.get("pnl"))
        if pnl is None and last_price is not None and cost is not None:
            pnl = round((last_price - cost) * volume, 2)
        out.append({
            "stock_code": code,
            "cost_price": cost,
            "total_volume": volume,
            "available_volume": _i(row.get("available", row.get("can_use_volume"))),
            "stock_name": row.get("stock_name") or row.get("instrument_name") or "",
            "last_price": last_price,
            "market_value": market_value,
            "pnl": pnl,
        })
    return out


def normalize_orders(raw) -> list[dict]:
    """QMT 委托 → 桥内 TDX 同名字段（order_id/stock_code/side/status/...）。"""
    out = []
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        out.append({
            "order_id": str(row.get("order_id") or row.get("order_sys_id")
                            or row.get("order_sysid") or ""),
            "stock_code": str(row.get("stock_code") or ""),
            "stock_name": row.get("instrument_name") or "",
            "time": _clock(row.get("order_time") or row.get("time")),
            "side": _side(row),
            "status": _status(row),
            "order_price": _f(row.get("price")),
            "filled_price": _f(row.get("traded_price")),
            "filled_volume": _i(row.get("traded_volume")),
            "total_volume": _i(row.get("order_volume", row.get("volume"))),
            "order_remark": (row.get("order_remark") or row.get("remark")
                             or row.get("user_order_id") or ""),
            "status_msg": row.get("status_msg") or "",
            "status_code": _i(row.get("status_code")),
        })
    return out


def normalize_trades(raw) -> list[dict]:
    """QMT 成交 → 桥内成交口径。"""
    out = []
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        volume = _i(row.get("traded_volume", row.get("volume")))
        price = _f(row.get("traded_price", row.get("price")))
        amount = _f(row.get("traded_amount"))
        if amount is None and price is not None:
            amount = round(price * volume, 2)
        out.append({
            "trade_id": str(row.get("trade_id") or row.get("traded_id")
                            or row.get("order_sys_id") or row.get("order_sysid") or ""),
            "order_id": str(row.get("order_id") or row.get("order_sys_id")
                            or row.get("order_sysid") or ""),
            "stock_code": str(row.get("stock_code") or ""),
            "stock_name": row.get("instrument_name") or "",
            "side": _side(row),
            "traded_volume": volume,
            "traded_price": price,
            "traded_amount": amount,
            "traded_at": str(row.get("traded_at") or row.get("traded_time") or ""),
            "order_remark": (row.get("order_remark") or row.get("remark")
                             or row.get("user_order_id") or ""),
        })
    return out
