"""
Order Schemas
"""

from datetime import datetime, timezone
from typing import Optional

from pydantic import UUID4, BaseModel, ConfigDict, Field
from pydantic import field_serializer

from backend.services.trade_shared.models.order import (
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

    # 0 = 无组合口径（内部策略/镜像真单在未挂组合时按 0 落账，与通达信桥一致）；
    # 前端下单仍应传真实组合 id。
    portfolio_id: int = Field(..., ge=0)
    strategy_id: int | None = Field(None, gt=0)
    trading_mode: TradingMode | None = None
    #: P2.7 分账：这条腿属于哪家模型（镜像单从模拟单继承）。非 LLM 腿留空。
    agent: str | None = Field(None, max_length=64)
    #: T-P1-03 Order 契约列：来源分类（``order_contract.SOURCE_*``）。**REAL 侧此前
    #: 全程不落库**（列在、写点缺，2026-10-14 乙1 补）：镜像/内部派发的 ``order_data``
    #: 早已带 ``source``（``mirror_virtual_fill`` 的队列载荷），是本 schema 把它丢了。
    #: 调用方应过 ``order_contract.normalize_source``（列宽 32；schema 的 max_length
    #: 是兜底——超宽会抛校验错，整笔真单发不出去）。
    source: str | None = Field(None, max_length=32)
    #: P1.6 TCA 基准价：**决策时点我们看到的那个价**（LLM 腿 = 决策报价；镜像腿 =
    #: 模拟虚拟成交价/强平盘口价）。**不是** ``price``（我方限价 = 愿意出的价），
    #: 两者量的是不同的事——前端直单没有决策参考价，一律留空。
    ref_price: float | None = Field(None, gt=0)


class OrderUpdate(BaseModel):
    """Update order schema"""

    quantity: float | None = Field(None, gt=0)
    price: float | None = Field(None, gt=0)
    stop_price: float | None = Field(None, gt=0)
    remarks: str | None = Field(None, max_length=500)


class OrderResponse(OrderBase):
    """Order response schema"""

    model_config = ConfigDict(from_attributes=True)

    # 市价单没有委托价（库里 price 为 0 或 NULL）。基类 price 用 gt=0 管**下单**
    # 入口是对的，但读侧必须容忍 0：否则只要当天有一笔市价单，
    # ``GET /api/v1/orders`` 整页 500（2026-11-06：「交易记录一条都没有」的真凶）。
    price: float | None = Field(None, ge=0)
    # 执行券商（qmt_exec/tdx/...）。记账/对账用；历史单为 None（当时只有全局选择）。
    broker: str | None = None

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
    # 执行券商过滤（qmt_exec/tdx/...）；历史单 broker 为 NULL，只在明确传值时过滤。
    broker: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    limit: int = Field(50, ge=1, le=1000)
    offset: int = Field(0, ge=0)
