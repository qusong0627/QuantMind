"""
Order Schemas
"""

from datetime import datetime, timezone
from typing import Optional

from pydantic import UUID4, BaseModel, ConfigDict, Field
from pydantic import field_serializer

from backend.services.trade.models.order import (
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TradeAction,
    TradingMode,
)


class OrderBase(BaseModel):
    """Base order schema"""

    symbol: str = Field(..., min_length=1, max_length=20)
    symbol_name: str | None = Field(None, max_length=50)
    side: OrderSide
    order_type: OrderType
    quantity: float = Field(..., gt=0)
    price: float | None = Field(None, gt=0)
    stop_price: float | None = Field(None, gt=0)
    trade_action: TradeAction | None = None
    position_side: PositionSide = PositionSide.LONG
    is_margin_trade: bool = False
    client_order_id: str | None = Field(None, max_length=100)
    remarks: str | None = Field(None, max_length=500)


class OrderCreate(OrderBase):
    """Create order schema"""

    portfolio_id: int = Field(..., gt=0)
    strategy_id: int | None = Field(None, gt=0)
    trading_mode: TradingMode | None = None


class OrderUpdate(BaseModel):
    """Update order schema"""

    quantity: float | None = Field(None, gt=0)
    price: float | None = Field(None, gt=0)
    stop_price: float | None = Field(None, gt=0)
    remarks: str | None = Field(None, max_length=500)


class OrderResponse(OrderBase):
    """Order response schema"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: UUID4
    tenant_id: str
    user_id: int
    portfolio_id: int
    strategy_id: int | None
    trading_mode: TradingMode
    status: OrderStatus
    trade_action: TradeAction | None
    position_side: PositionSide
    is_margin_trade: bool
    filled_quantity: float
    average_price: float | None
    order_value: float
    filled_value: float
    commission: float
    submitted_at: datetime | None
    filled_at: datetime | None
    cancelled_at: datetime | None
    expired_at: datetime | None
    exchange_order_id: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer(
        "submitted_at",
        "filled_at",
        "cancelled_at",
        "expired_at",
        "created_at",
        "updated_at",
        when_used="json",
    )
    def _serialize_datetime(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        # 后端数据库存储的是本地时间 (naive), 贴有时区的保持不变，
        # 无时区的统一视为上海时间并转换为 UTC 序列化，避免前端因“假 UTC”导致再次叠加 8 小时
        if value.tzinfo:
            aware_value = value
        else:
            from datetime import timedelta
            shanghai_tz = timezone(timedelta(hours=8))
            aware_value = value.replace(tzinfo=shanghai_tz)

        return aware_value.astimezone(timezone.utc).isoformat()


class OrderCancelRequest(BaseModel):
    """Cancel order request"""

    order_id: UUID4
    reason: str | None = Field(None, max_length=200)


class OrderListQuery(BaseModel):
    """Order list query"""

    tenant_id: str | None = None
    user_id: str | None = None
    portfolio_id: int | None = None
    symbol: str | None = None
    status: OrderStatus | None = None
    side: OrderSide | None = None
    trading_mode: TradingMode | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    limit: int = Field(50, ge=1, le=1000)
    offset: int = Field(0, ge=0)
