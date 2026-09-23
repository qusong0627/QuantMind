"""
Simulation order schemas.
"""

from datetime import datetime
from typing import Optional

from pydantic import UUID4, BaseModel, ConfigDict, Field, field_serializer

from backend.services.simulation.models.order import (
    OrderSide,
    OrderStatus,
    OrderType,
    TradingMode,
)
from backend.shared.utc_datetime import to_utc_iso


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
    #: P2.7 分账：这条腿属于哪家模型（多模型共用一个账户）。非 LLM 腿留空。
    #: 注意本模型的 ``extra="ignore"``：**字段没在这里声明就会静默丢掉**，
    #: 传参方以为写进去了而台账是 NULL——故它必须显式在场（有测试钉住）。
    agent: str | None = Field(None, max_length=64)


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

    @field_serializer(
        "submitted_at",
        "filled_at",
        "cancelled_at",
        "created_at",
        "updated_at",
        when_used="json",
    )
    def _serialize_datetime(self, value: datetime | None) -> str | None:
        return to_utc_iso(value)
