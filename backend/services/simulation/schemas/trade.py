"""
Simulation trade schemas.
"""

from datetime import datetime

from pydantic import UUID4, BaseModel, ConfigDict, Field, field_serializer

from backend.services.simulation.models.order import OrderSide, TradingMode
from backend.shared.utc_datetime import to_utc_iso


class SimTradeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trade_id: UUID4
    order_id: UUID4
    tenant_id: str
    user_id: int
    portfolio_id: int
    symbol: str
    symbol_name: str | None = None
    side: OrderSide
    trading_mode: TradingMode
    quantity: float
    price: float
    trade_value: float
    commission: float
    executed_at: datetime
    price_source: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("executed_at", "created_at", "updated_at", when_used="json")
    def _serialize_datetime(self, value: datetime | None) -> str | None:
        return to_utc_iso(value)


class TradeStatsDailyPoint(BaseModel):
    timestamp: str
    value: int
    label: str = "trade_count"


class SimTradeStatsResponse(BaseModel):
    daily_counts: list[TradeStatsDailyPoint] = Field(default_factory=list)
    total_trades: int
    total_value: float
    total_commission: float
    buy_trades: int
    sell_trades: int
    # 已实现盈亏与胜率/盈亏比（手续费计入口径）
    realized_pnl: float = 0.0
    win_trades: int = 0
    loss_trades: int = 0
    win_rate: float = 0.0
    profit_loss_ratio: float = 0.0


class SimTradeListQuery(BaseModel):
    portfolio_id: int | None = None
    symbol: str | None = None
    limit: int = Field(50, ge=1, le=1000)
    offset: int = Field(0, ge=0)
