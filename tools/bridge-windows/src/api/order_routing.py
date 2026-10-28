"""下单渠道路由（纯函数）。

桥对外的交易接口按渠道分开，字段口径不同；本模块只做**归一与转换**，
不发请求。路由端点用它把统一请求拆成通达信 TradePlan / QMT RPC 参数。
"""

from __future__ import annotations

import uuid

CHANNELS = ("tdx", "qmt")


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_order(raw: dict, default_channel: str = "tdx") -> dict:
    """把外部下单参数归一成桥内统一结构；非法直接 ValueError。"""
    raw = dict(raw or {})
    channel = str(raw.get("channel") or default_channel or "").strip().lower()
    if channel not in CHANNELS:
        raise ValueError("channel 必须是 tdx 或 qmt")
    symbol = str(raw.get("symbol") or raw.get("stock_code") or "").strip().upper()
    if not symbol:
        raise ValueError("symbol/stock_code is required")
    side = str(raw.get("side") or raw.get("action") or "").strip().lower()
    if side not in ("buy", "sell"):
        raise ValueError("side 必须是 buy 或 sell")
    volume = int(raw.get("volume") or raw.get("order_volume") or 0)
    if volume <= 0:
        raise ValueError("volume 必须大于 0")
    order_type = str(raw.get("order_type") or "limit").strip().lower()
    if order_type not in ("limit", "market"):
        raise ValueError("order_type 必须是 limit 或 market")
    price = _f(raw.get("price"))
    if order_type == "limit":
        if price is None or price <= 0:
            raise ValueError("限价单必须提供 price")
    else:
        price = None
    client_order_id = str(
        raw.get("client_order_id") or raw.get("remark") or ""
    ).strip()
    if channel == "qmt" and not client_order_id:
        raise ValueError("qmt 下单必须提供 client_order_id（幂等键）")
    return {
        "channel": channel,
        "symbol": symbol,
        "side": side,
        "volume": volume,
        "order_type": order_type,
        "price": price,
        "client_order_id": client_order_id,
        "account_id": str(raw.get("account_id") or "").strip(),
        "account_type": str(
            raw.get("account_type") or ("stock" if channel == "tdx" else "STOCK")
        ).strip(),
        "strategy_name": str(raw.get("strategy_name") or "").strip(),
        "notify": raw.get("notify"),
        "stop_loss_pct": raw.get("stop_loss_pct"),
        "take_profit_pct": raw.get("take_profit_pct"),
        "trailing_stop_pct": raw.get("trailing_stop_pct"),
    }


def split_orders(orders, default_channel: str = "tdx") -> tuple[list, list]:
    """按渠道拆分，保持各自内部顺序。"""
    tdx: list[dict] = []
    qmt: list[dict] = []
    for item in orders or []:
        order = normalize_order(item, default_channel=default_channel)
        (qmt if order["channel"] == "qmt" else tdx).append(order)
    return tdx, qmt


def tdx_plan_dict(plan_id: str, orders, account: str = "",
                  account_type: str = "stock") -> dict:
    """统一结构 → 通达信 TradePlan 字典（price_type: 0=限价, 1=市价）。"""
    out = []
    for order in orders:
        market = order["order_type"] == "market"
        out.append({
            "stock_code": order["symbol"],
            "side": order["side"],
            "volume": int(order["volume"]),
            "price_type": 1 if market else 0,
            "price": None if market else float(order["price"]),
            "order_type": "market" if market else "limit",
            "notify": order.get("notify"),
            "stop_loss_pct": order.get("stop_loss_pct"),
            "take_profit_pct": order.get("take_profit_pct"),
            "trailing_stop_pct": order.get("trailing_stop_pct"),
        })
    return {
        "plan_id": str(plan_id or ("route-" + uuid.uuid4().hex)),
        "account": str(account or ""),
        "account_type": str(account_type or "stock"),
        "orders": out,
        "source": "bridge-router",
    }


def qmt_order_params(order: dict) -> dict:
    """统一结构 → 伴生桥 ``order_stock`` 参数。

    注意：kit 的 ``PRICE_TYPE_ALIASES`` 只有 ``LIMIT`` / ``LATEST_PRICE``，
    没有 ``MARKET``；发 ``MARKET`` 会静默落成限价 11。
    """
    market = order["order_type"] == "market"
    params = {
        "stock_code": order["symbol"],
        "action": order["side"].upper(),
        "volume": int(order["volume"]),
        "price": 0.0 if market else float(order["price"]),
        "price_type": "LATEST_PRICE" if market else "LIMIT",
        "strategy_name": order.get("strategy_name") or "bridge",
        "remark": order["client_order_id"],
        "wait_settlement": True,
    }
    if order.get("account_id"):
        params["account_id"] = order["account_id"]
    return params


def qmt_cancel_params(order: dict) -> dict:
    """统一结构 → 伴生桥 ``cancel_order_stock`` 参数。"""
    raw = dict(order or {})
    order_id = str(
        raw.get("order_sys_id") or raw.get("order_sysid")
        or raw.get("order_id") or ""
    ).strip()
    if not order_id:
        raise ValueError("撤单必须提供 order_id/order_sys_id")
    symbol = str(raw.get("symbol") or raw.get("stock_code") or "").strip().upper()
    params = {"order_sys_id": order_id}
    if symbol:
        params["stock_code"] = symbol
    if raw.get("account_id"):
        params["account_id"] = str(raw["account_id"])
    if raw.get("user_order_id"):
        params["user_order_id"] = str(raw["user_order_id"])
    return params
