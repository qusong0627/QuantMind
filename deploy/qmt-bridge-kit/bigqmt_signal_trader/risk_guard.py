"""信号执行前的轻量风控和数量计算。"""

from .code_utils import normalize_stock_code, round_buy_volume, round_sell_volume
from .models import SignalAction, TradeSignal


class RiskDecision:
    def __init__(self, allowed, reason="", volume=0, stock_code=""):
        self.allowed = allowed
        self.reason = reason
        self.volume = volume
        self.stock_code = stock_code


def build_trade_volume(signal: TradeSignal, positions):
    code = normalize_stock_code(signal.stock_code)
    if signal.action == SignalAction.BUY:
        return RiskDecision(True, volume=round_buy_volume(code, signal.amount), stock_code=code)

    if signal.action == SignalAction.SELL:
        position = positions.get(code)
        if not position or position.available <= 0:
            return RiskDecision(False, "no_available_position", stock_code=code)
        if signal.amount is not None:
            raw_volume = min(int(signal.amount), int(position.available))
            sell_all = raw_volume == int(position.available)
        else:
            pct = float(signal.percentage or 100)
            raw_volume = int(int(position.available) * pct / 100.0)
            sell_all = pct >= 100
        volume = round_sell_volume(code, raw_volume, sell_all=sell_all)
        if volume <= 0:
            return RiskDecision(False, "volume_below_min_lot", stock_code=code)
        return RiskDecision(True, volume=volume, stock_code=code)

    return RiskDecision(False, f"unsupported_action:{signal.action}", stock_code=code)


def validate_signal(signal, now, positions):
    if signal.is_expired(now):
        return RiskDecision(False, "expired", stock_code=signal.stock_code)
    decision = build_trade_volume(signal, positions)
    if decision.allowed and decision.volume <= 0:
        return RiskDecision(False, "invalid_volume", stock_code=decision.stock_code)
    return decision
