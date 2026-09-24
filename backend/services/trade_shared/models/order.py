"""
Order Model
"""

import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, Float, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from .base import Base, TimestampMixin
from .enums import (  # noqa: F401 (re-exported)
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TradeAction,
    TradingMode,
)


class Order(Base, TimestampMixin):
    """Order table"""

    __tablename__ = "orders"

    # Primary key
    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4, index=True
    )

    # Foreign keys
    tenant_id = Column(String(64), nullable=False, default="default", index=True)
    user_id = Column(String(32), nullable=False, index=True)
    portfolio_id = Column(Integer, nullable=False, index=True)
    strategy_id = Column(Integer, nullable=True, index=True)

    # Order info
    symbol = Column(String(20), nullable=False, index=True)
    symbol_name = Column(String(50), nullable=True)
    side = Column(
        Enum(OrderSide, values_callable=lambda x: [e.value for e in x]), nullable=False
    )
    trade_action = Column(
        Enum(TradeAction, values_callable=lambda x: [e.value for e in x]),
        nullable=True,
        index=True,
    )
    position_side = Column(
        Enum(PositionSide, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=PositionSide.LONG,
        index=True,
    )
    is_margin_trade = Column(Boolean, nullable=False, default=False)
    order_type = Column(
        Enum(OrderType, values_callable=lambda x: [e.value for e in x]), nullable=False
    )
    trading_mode = Column(
        Enum(TradingMode, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=TradingMode.SIMULATION,
        index=True,
    )
    status = Column(
        Enum(OrderStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=OrderStatus.PENDING,
        index=True,
    )

    # Quantity and price
    quantity = Column(Float, nullable=False)
    filled_quantity = Column(Float, nullable=False, default=0.0)
    price = Column(Float, nullable=True)  # For limit orders
    stop_price = Column(Float, nullable=True)  # For stop orders
    average_price = Column(Float, nullable=True)  # Average fill price

    # Amounts
    order_value = Column(Float, nullable=False)  # quantity * price
    filled_value = Column(Float, nullable=False, default=0.0)
    commission = Column(Float, nullable=False, default=0.0)

    # Timestamps
    submitted_at = Column(DateTime, nullable=True)
    filled_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    expired_at = Column(DateTime, nullable=True)

    # Additional info
    # 幂等键的唯一性由 (tenant_id, user_id, client_order_id) 部分唯一索引承担
    # （`uq_orders_scope_client_order_id`，db_init.sql + order_contract 启动自愈）——
    # 不再是列上的全库唯一：那与查重口径不一致，跨租户同键会 500（P2.7-⑧）。
    client_order_id = Column(String(100), nullable=True)
    exchange_order_id = Column(String(100), nullable=True)
    # T-P1-03 Order 契约列：REAL 成交来源（broker_fill）与订单来源分类（manual/mirror/...）
    price_source = Column(String(64), nullable=True)
    source = Column(String(32), nullable=True)
    # P2.7 分账契约列：这条单属于哪家模型（agent）。多模型共用一个券商账户时必须从
    # 订单本身读得出归属——成交回报只带来订单，不带决策上下文，而回写分账账本
    # （位置 + 虚拟现金）正是按这个字段落段。非 LLM 腿（人点/风控/托管）恒 NULL。
    # 不建索引：取值域是「账号下同时跑着几家模型」（个位数），任何真实查询都会带上
    # 日期/账户前缀，单列 agent 索引选不中——真需要时按查询形态建复合索引。
    agent = Column(String(64), nullable=True)
    # P1.6 TCA 基准价：**决策时点我们看到的那个价**（LLM 腿 = 决策报价；镜像腿 =
    # 模拟虚拟成交价/强平盘口价），执行损耗报告按它算滑点。可空，且**老单恒空**——
    # 空缺如实计入"不可定价"，不用成交价倒推（倒推出来的滑点是自证式的假读数）。
    # 与 ``price``（我方限价，衡量口径是"吃掉多少缓冲"）是两回事，别互相顶替。
    ref_price = Column(Float, nullable=True)
    remarks = Column(String(500), nullable=True)
    version = Column(Integer, nullable=False, default=1)

    # Indexes
    __table_args__ = (
        Index("idx_order_tenant_user_status", "tenant_id", "user_id", "status"),
        Index("idx_order_user_status", "user_id", "status"),
        Index("idx_order_portfolio_symbol", "portfolio_id", "symbol"),
        Index("idx_order_created", "created_at"),
    )

    def __repr__(self):
        return (
            f"<Order(id={self.id}, order_id={self.order_id}, "
            f"symbol={self.symbol}, side={self.side}, "
            f"trade_action={self.trade_action}, position_side={self.position_side}, "
            f"quantity={self.quantity}, status={self.status})>"
        )
