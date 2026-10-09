"""Broker 抽象层：统一券商接入接口。

所有真实/模拟券商都实现 Broker 接口，tool_trade.py 与未来实盘通道
通过 registry 按名称路由，互不影响。
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BrokerError(Exception):
    """Broker 操作异常（含用户可读消息）。"""


class Broker(ABC):
    """券商统一接口。"""

    name: str = "base"
    #: 支持的市场："us" | "cn" | "both"
    markets: str = "both"

    @abstractmethod
    def get_positions(self, signature: str, today_date: str) -> Dict[str, float]:
        """返回 {symbol: shares, CASH: cash}，symbol 不含 CASH。"""

    @abstractmethod
    def get_cash(self, signature: str, today_date: str) -> float:
        """当前可用现金。"""

    @abstractmethod
    def buy(self, signature: str, today_date: str, symbol: str, amount: int,
            price: Optional[float] = None) -> Dict[str, Any]:
        """买入；返回最新持仓 dict 或 {"error": ...}。"""

    @abstractmethod
    def sell(self, signature: str, today_date: str, symbol: str, amount: int,
             price: Optional[float] = None) -> Dict[str, Any]:
        """卖出；返回最新持仓 dict 或 {"error": ...}。"""

    @abstractmethod
    def get_quote(self, symbol: str, date: str, market: str = "us") -> Optional[Dict[str, Any]]:
        """单标的当日行情（buy/high/low/sell price, volume）。"""

    @abstractmethod
    def get_klines(self, symbol: str, start: str, end: str, interval: str = "daily",
                   market: str = "us") -> List[Dict[str, Any]]:
        """K 线序列（起止日期含小时级）。"""


class BrokerRegistry:
    """按名称注册/获取 broker 实例。"""

    def __init__(self) -> None:
        self._brokers: Dict[str, type[Broker]] = {}

    def register(self, cls: type[Broker]) -> type[Broker]:
        self._brokers[cls.name] = cls
        return cls

    def create(self, name: str, config: Optional[Dict[str, Any]] = None) -> Broker:
        if name not in self._brokers:
            raise BrokerError(f"未知 broker: {name}，可用: {sorted(self._brokers)}")
        return self._brokers[name](config or {})

    def available(self) -> List[str]:
        return sorted(self._brokers)


registry = BrokerRegistry()
