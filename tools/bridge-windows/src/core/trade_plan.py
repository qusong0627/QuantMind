from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Optional


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Order:
    """单个订单指令. stop_loss_pct / take_profit_pct / trailing_stop_pct 可选, 含于其中任何一个即在成交后注册止损监控."""
    stock_code: str
    side: str                    # "buy" | "sell"
    volume: int
    price_type: int = 0          # 0=限价 1=市价 2=涨停 3=跌停
    price: Optional[float] = None
    order_type: str = "limit"    # "limit" | "market"
    notify: Optional[int] = None  # 0=需客户端确认 1=请求免确认（None=用桥默认）
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    trailing_stop_pct: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TradePlan:
    plan_id: str
    orders: List[Order]
    account: str = ""
    account_type: str = "stock"
    created_at: str = field(default_factory=now_iso)
    timeout_seconds: int = 300
    source: str = ""             # 来源标识, 如 "quandmind"

    @classmethod
    def from_dict(cls, d: dict) -> "TradePlan":
        return cls(
            plan_id=d["plan_id"],
            account=d.get("account", ""),
            account_type=d.get("account_type", "stock"),
            orders=[Order(**o) for o in d.get("orders", [])],
            created_at=d.get("created_at", now_iso()),
            timeout_seconds=d.get("timeout_seconds", 300),
            source=d.get("source", ""),
        )

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "account": self.account,
            "account_type": self.account_type,
            "orders": [o.to_dict() for o in self.orders],
            "created_at": self.created_at,
            "timeout_seconds": self.timeout_seconds,
            "source": self.source,
        }
