"""
Simulation order schemas.
"""

from datetime import datetime
from typing import Optional

from pydantic import UUID4, BaseModel, ConfigDict, Field

from backend.services.simulation.models.order import (
    OrderSide,
    OrderStatus,
    OrderType,
    TradingMode,
)


class SimOrderBase(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    side: OrderSide
    order_type: OrderType
    quantity: float = Field(..., gt=0)
    price: float | None = Field(None, gt=0)
    remarks: str | None = Field(None, max_length=500)


class SimOrderCreate(SimOrderBase):
    model_config = ConfigDict(extra="ignore")

    portfolio_id: int = Field(0, ge=0)
    strategy_id: int | None = Field(None, gt=0)
    trading_mode: TradingMode = TradingMode.SIMULATION
    # V2提交链路透传字段（旧SimOrder表不持久化，仅保证ValidationError不阻断）
    client_order_id: str | None = Field(None, max_length=64)
    time_in_force: str | None = Field(None, max_length=16)
    expires_at: datetime | None = None
    trade_action: str | None = Field(None, max_length=32)
    position_side: str | None = Field(None, max_length=16)
    is_margin_trade: bool | None = False


class SimOrderCancelRequest(BaseModel):
    reason: str | None = Field(None, max_length=200)


class SimOrderResponse(SimOrderBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: UUID4
    tenant_id: str
    user_id: int
    portfolio_id: int
    strategy_id: int | None
    trading_mode: TradingMode
    status: OrderStatus
    filled_quantity: float
    average_price: float | None
    order_value: float
    filled_value: float
    commission: float
    submitted_at: datetime | None
    filled_at: datetime | None
    cancelled_at: datetime | None
    execution_model: str
    price_source: str | None
    created_at: datetime
    updated_at: datetime
    symbol_name: str | None = None
