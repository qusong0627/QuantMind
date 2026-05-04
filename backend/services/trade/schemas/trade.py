"""
Trade Schemas
"""

from datetime import datetime, timezone
from typing import Optional

from pydantic import UUID4, BaseModel, ConfigDict, Field
from pydantic import field_serializer

from backend.services.trade.models.order import (
    OrderSide,
    PositionSide,
    TradeAction,
    TradingMode,
)


class TradeResponse(BaseModel):
    """Trade response schema"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    trade_id: UUID4
    order_id: UUID4
    tenant_id: str
    user_id: int
    portfolio_id: int
    symbol: str
    side: OrderSide
    trade_action: TradeAction | None
    position_side: PositionSide
    is_margin_trade: bool
    trading_mode: TradingMode
    quantity: float
    price: float
    trade_value: float
    commission: float
    executed_at: datetime
    exchange_trade_id: str | None
    exchange_name: str | None
    remarks: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("executed_at", "created_at", "updated_at", when_used="json")
    def _serialize_datetime(self, value: datetime) -> str:
        # 统一处理：无时区字符串视为上海本地时间，再转 UTC 输出
        if value.tzinfo:
            aware_value = value
        else:
            from datetime import timedelta
            shanghai_tz = timezone(timedelta(hours=8))
            aware_value = value.replace(tzinfo=shanghai_tz)

        return aware_value.astimezone(timezone.utc).isoformat()


class TradeListQuery(BaseModel):
    """Trade list query"""

    tenant_id: str | None = None
    user_id: str | None = None
    portfolio_id: int | None = None
    order_id: UUID4 | None = None
    symbol: str | None = None
    side: OrderSide | None = None
    trading_mode: TradingMode | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    limit: int = Field(50, ge=1, le=1000)
    offset: int = Field(0, ge=0)
