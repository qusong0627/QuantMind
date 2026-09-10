"""订单价格生成逻辑。"""

from .code_utils import is_bond_code, normalize_stock_code


def _price_precision(stock_code):
    """报价小数位。基金和债券都是 3 位，股票 2 位。

    债券这一条原来漏了：转债报价精度是 0.001，按 2 位取整会把 128.456 变成
    128.46，交易所直接拒单。
    """
    code = normalize_stock_code(stock_code)
    if is_bond_code(code):
        return 3
    pure = code.split(".")[0]
    return 3 if pure.startswith(("15", "16", "51", "52")) else 2


def _second_level(values):
    if isinstance(values, (list, tuple)) and len(values) > 1:
        try:
            value = float(values[1])
            return value if value > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def build_order_price(market_data, stock_code, action, price_type="AUTO_LIMIT", fixed_price=None):
    if str(price_type or "AUTO_LIMIT").upper() == "FIX_PRICE":
        if fixed_price is None:
            raise ValueError("fixed_price is required when price_type is FIX_PRICE")
        return float(fixed_price)

    code = normalize_stock_code(stock_code)
    ticks = market_data.get_ticks([code])
    tick = ticks.get(code)
    if not tick:
        raise ValueError(f"missing tick data for {code}")

    last_price = float(tick.get("lastPrice") or 0)
    if last_price <= 0:
        raise ValueError(f"invalid lastPrice for {code}")

    instrument = market_data.get_instrument(code)
    if int(instrument.get("InstrumentStatus") or 0) > 0:
        raise ValueError(f"{code} is suspended")

    precision = _price_precision(code)
    action_text = str(action).upper()
    if action_text == "BUY":
        up_stop = float(instrument.get("UpStopPrice") or last_price * 1.1)
        calculated = min(round(last_price * 1.002, precision), up_stop)
        ask2 = _second_level(tick.get("askPrice"))
        return round(ask2, precision) if ask2 and ask2 < calculated else calculated
    if action_text == "SELL":
        down_stop = float(instrument.get("DownStopPrice") or last_price * 0.9)
        calculated = max(round(last_price * 0.998, precision), down_stop)
        bid2 = _second_level(tick.get("bidPrice"))
        return round(bid2, precision) if bid2 and bid2 > calculated else calculated
    raise ValueError(f"unsupported action for price: {action}")
