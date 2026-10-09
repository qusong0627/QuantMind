"""风控模块：broker 前置网关。

所有交易（main.py agent / dsh agent / broker API）最终都经过
tool_trade.buy/sell 落盘，风控在此单点校验。

规则（backend.yaml 的 risk 段配置）：
- 单笔限额：order_value <= equity * max_order_ratio
- 持仓限额：买入后单标市值 <= equity * max_position_ratio
- 日亏熔断：当日已实现亏损 >= max_daily_loss_pct 时拒绝买入
- 现金保留：交易后 CASH >= min_cash_reserve
- 黑名单：禁用标的

熔断语义：position.jsonl 的 id=0 记录 CASH 为初始现金（初始权益），
当前 CASH 与它的差额为已实现盈亏（不含浮盈浮亏，保守且无价格依赖）。
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from agent_tools.brokers.base import Broker, BrokerError

load_dotenv()


@dataclass
class RiskPolicy:
    """风控策略（backend.yaml -> risk 段）。"""

    enabled: bool = True
    max_order_ratio: float = 0.20        # 单笔 ≤ 权益 20%
    max_position_ratio: float = 0.20     # 单标持仓 ≤ 权益 20%
    max_daily_loss_pct: float = 0.05     # 日亏 5% 熔断
    min_cash_reserve: float = 100.0      # 最低保留现金
    blacklist: list = field(default_factory=list)  # 禁用标的
    approval_required: bool = False      # 实盘人工审批（当前仅告警位）

    @classmethod
    def from_backend_config(cls, backend_config: Optional[dict] = None) -> "RiskPolicy":
        if backend_config is None:
            try:
                from backend.config import load_backend_config

                backend_config = load_backend_config()
            except Exception:
                backend_config = {}
        risk = (backend_config or {}).get("risk", {})
        return cls(
            enabled=risk.get("enabled", True),
            max_order_ratio=float(risk.get("max_order_ratio", 0.20)),
            max_position_ratio=float(risk.get("max_position_ratio", 0.20)),
            max_daily_loss_pct=float(risk.get("max_daily_loss_pct", 0.05)),
            min_cash_reserve=float(risk.get("min_cash_reserve", 100.0)),
            blacklist=list(risk.get("blacklist", [])),
            approval_required=bool(risk.get("approval_required", False)),
        )

    # ---- 快捷方法（供 tool_trade / broker 调用） ----

    def check_buy(self, symbol: str, amount: int, price: float,
                  cash: float, equity: float, position_value: float,
                  position_ratio: float) -> Optional[str]:
        """返回错误消息（None = 通过）。position_value 为买入前该标市值。"""
        if not self.enabled:
            return None
        if symbol in self.blacklist:
            return f"风控拒绝：{symbol} 在黑名单中"
        order_value = price * amount
        if order_value > equity * self.max_order_ratio:
            return (f"风控拒绝：单笔 {order_value:.2f} 超过权益 {self.max_order_ratio:.0%}"
                    f"（{equity * self.max_order_ratio:.2f}）")
        new_position_value = position_value + order_value
        if new_position_value > equity * self.max_position_ratio:
            return (f"风控拒绝：{symbol} 持仓 {new_position_value:.2f} 将超过权益"
                    f" {self.max_position_ratio:.0%}（{equity * self.max_position_ratio:.2f}）")
        if cash - order_value < self.min_cash_reserve:
            return (f"风控拒绝：交易后现金 {cash - order_value:.2f} 低于保留线"
                    f" {self.min_cash_reserve:.2f}")
        return None

    def check_sell(self, symbol: str, amount: int, held: float) -> Optional[str]:
        if not self.enabled:
            return None
        if amount > held:
            return f"风控拒绝：卖出 {amount} 超过持仓 {held}"
        return None

    def check_daily_loss(self, initial_cash: float, current_cash: float,
                         action: str = "buy") -> Optional[str]:
        """日亏熔断（仅对买入生效，卖出永远放行）。"""
        if not self.enabled or action != "buy":
            return None
        if initial_cash <= 0:
            return None
        loss_ratio = (initial_cash - current_cash) / initial_cash
        if loss_ratio >= self.max_daily_loss_pct:
            return (f"风控熔断：当日已实现亏损 {loss_ratio:.1%} ≥"
                    f" {self.max_daily_loss_pct:.1%}，暂停买入")
        return None


def get_trading_config(backend_config: Optional[dict] = None) -> Dict[str, float]:
    """交易成本模型（backend.yaml -> trading.fees）：买/卖手续费率 + 滑点。"""
    if backend_config is None:
        try:
            from backend.config import load_backend_config

            backend_config = load_backend_config()
        except Exception:
            backend_config = {}
    fees = (backend_config or {}).get("trading", {}).get("fees", {})
    return {
        "buy_rate": float(fees.get("buy_rate", 0.0003)),
        "sell_rate": float(fees.get("sell_rate", 0.0003)),
        "slippage": float(fees.get("slippage", 0.0005)),
    }


def get_initial_cash(position_file: Path) -> float:
    """position.jsonl 的 id=0 记录 CASH = 初始现金。"""
    try:
        with position_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if doc.get("id") == 0:
                    return float(doc.get("positions", {}).get("CASH", 0.0))
    except OSError:
        pass
    return 0.0


def get_current_equity(position_file: Path, today_date: str) -> float:
    """最后一条持仓的权益估值 = CASH + Σ(持仓 × 当日价格)。

    价格取不到时跳过该标的市值（保守）。无价格依赖时不误判现金支出为亏损。
    """
    last_positions: Dict[str, float] = {}
    try:
        with position_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                    last_positions = doc.get("positions", {})
                except json.JSONDecodeError:
                    continue
    except OSError:
        return 0.0

    cash = float(last_positions.get("CASH", 0.0))
    symbols = [s for s in last_positions if s != "CASH" and last_positions[s]]
    if not symbols:
        return cash
    try:
        from tools.price_tools import get_open_prices

        if any(s.endswith((".SH", ".SZ")) for s in symbols):
            market = "cn"
        elif any(s.endswith(".HK") for s in symbols):
            market = "hk"
        else:
            market = "us"
        prices = get_open_prices(today_date, symbols, market=market)
    except Exception:
        return cash
    market_value = 0.0
    for symbol in symbols:
        price = prices.get(f"{symbol}_price")
        if price:
            market_value += float(last_positions[symbol]) * float(price)
    return cash + market_value


def _position_file_for(signature: str) -> Path:
    from tools.general_tools import get_config_value

    log_path = get_config_value("LOG_PATH", "./data/agent_data")
    if log_path.startswith("./data/"):
        log_path = log_path[7:]
    return (
        Path(__file__).resolve().parents[1] / "data" / log_path
        / signature / "position" / "position.jsonl"
    )


def pre_trade_check(action: str, symbol: str, amount: int, price: float,
                    current_position: Dict[str, float],
                    signature: str) -> Optional[str]:
    """tool_trade.buy/sell 执行前的统一风控校验。

    Args:
        action: "buy" | "sell"
        current_position: 当前持仓（含 CASH）
    Returns:
        错误消息或 None
    """
    policy = RiskPolicy.from_backend_config()
    if not policy.enabled:
        return None

    cash = float(current_position.get("CASH", 0.0))
    equity = cash  # 保守口径：无价格时不把持仓市值计入权益

    if action == "buy":
        held_shares = float(current_position.get(symbol, 0.0))
        position_value = held_shares * price if held_shares else 0.0
        position_ratio = position_value / equity if equity > 0 else 0.0
        err = policy.check_buy(symbol, amount, price, cash, equity,
                               position_value, position_ratio)
        if err:
            return err
        # 日亏熔断（权益口径：CASH + 持仓市值，避免买入支出误判为亏损）
        from tools.general_tools import get_config_value

        position_file = _position_file_for(signature)
        today = get_config_value("TODAY_DATE") or today_date
        initial_equity = get_initial_cash(position_file)
        current_equity = get_current_equity(position_file, today)
        err = policy.check_daily_loss(initial_equity, current_equity, "buy")
        if err:
            return err
        return None
    else:  # sell
        held_shares = float(current_position.get(symbol, 0.0))
        return policy.check_sell(symbol, amount, held_shares)


class RiskGateway(Broker):
    """包装任意 Broker，交易前过风控。"""

    name = "risk_gateway"

    def __init__(self, broker: Broker, policy: Optional[RiskPolicy] = None):
        self._broker = broker
        self._policy = policy or RiskPolicy.from_backend_config()

    def get_positions(self, signature: str, today_date: str) -> Dict[str, float]:
        return self._broker.get_positions(signature, today_date)

    def get_cash(self, signature: str, today_date: str) -> float:
        return self._broker.get_cash(signature, today_date)

    def buy(self, signature: str, today_date: str, symbol: str, amount: int,
            price: Optional[float] = None) -> Dict[str, Any]:
        positions = self._broker.get_positions(signature, today_date)
        cash = positions.get("CASH", self._broker.get_cash(signature, today_date))
        quote = price
        if quote is None:
            try:
                q = self._broker.get_quote(symbol, today_date[:10])
                quote = q.get("buy price") if q else None
            except Exception:
                quote = None
        if quote is None:
            raise BrokerError(f"风控无法获取 {symbol} 价格，拒绝下单")
        err = pre_trade_check("buy", symbol, amount, float(quote),
                              {**positions, "CASH": cash}, signature)
        if err:
            raise BrokerError(err)
        return self._broker.buy(signature, today_date, symbol, amount, price)

    def sell(self, signature: str, today_date: str, symbol: str, amount: int,
             price: Optional[float] = None) -> Dict[str, Any]:
        positions = self._broker.get_positions(signature, today_date)
        held = float(positions.get(symbol, 0.0))
        err = pre_trade_check("sell", symbol, amount, float(price or 0.0),
                              {**positions, "CASH": positions.get("CASH", 0.0)}, signature)
        if err:
            raise BrokerError(err)
        return self._broker.sell(signature, today_date, symbol, amount, price)

    def get_quote(self, symbol: str, date: str, market: str = "us") -> Optional[Dict[str, Any]]:
        return self._broker.get_quote(symbol, date, market)

    def get_klines(self, symbol: str, start: str, end: str, interval: str = "daily",
                   market: str = "us") -> list:
        return self._broker.get_klines(symbol, start, end, interval, market)
