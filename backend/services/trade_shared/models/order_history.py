"""
OrderHistory Model — 历史委托归档表（多交易所）

设计目标：
1. 持久化「历史委托」：桥/券商接口的当日委托列表有上限，历史委托会滚出实时列表，
   本表作为不可变归档，防止历史交易记录丢失。
2. 多交易所支持：A股（SSE/SZSE/BSE）、港股（HKEX）、美股（NASDAQ/NYSE/AMEX）、
   期货（SHFE/DCE/CZCE/CFFEX/INE）、加密（CRYPTO）等，通过 market/exchange 维度区分。
3. 多券商来源：tdx / futu / tiger / ib / qmt / manual，通过 broker_type 区分。
4. 幂等归档：broker_type + exchange_order_id + trade_date 唯一，重复归档自动跳过（ON CONFLICT DO NOTHING）。

与 orders 表的区别：
- orders：运行态订单（最新状态可被桥 30s UPSERT 刷新），当日委托视角
- order_history：终态历史归档（一条委托一条记录，不再变更），跨日回溯视角

字段口径：
- 金额单位：CNY（A股）/ HKD（港股）/ USD（美股），由 market 决定，raw_payload 保留券商原始字段
- 涨跌/费用：commission 佣金、stamp_duty 印花税、transfer_fee 过户费（CN 专属）、total_fee 合计
"""

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID

from .base import Base, TimestampMixin


class OrderHistory(Base, TimestampMixin):
    """历史委托归档表（不可变，跨交易所/跨券商）"""

    __tablename__ = "order_history"

    # ---- 主键 ----
    id = Column(Integer, primary_key=True, autoincrement=True)
    history_id = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4, index=True
    )

    # ---- 账户维度 ----
    tenant_id = Column(String(64), nullable=False, default="default", index=True)
    user_id = Column(String(32), nullable=False, index=True)
    account_id = Column(String(64), nullable=True)  # 券商账户号（桥/券商侧）
    portfolio_id = Column(Integer, nullable=False, default=0, index=True)
    strategy_id = Column(Integer, nullable=True)

    # ---- 市场/交易所维度（多交易所核心） ----
    market = Column(String(16), nullable=False, index=True)  # CN / HK / US / FUTURES / CRYPTO
    exchange = Column(String(16), nullable=False, index=True)  # SSE / SZSE / BSE / HKEX / NASDAQ / NYSE / AMEX / SHFE / DCE / CZCE / CFFEX / INE / CRYPTO
    currency = Column(String(8), nullable=False, default="CNY")  # CNY / HKD / USD / USDT
    broker_type = Column(String(16), nullable=False, index=True)  # tdx / futu / tiger / ib / qmt / manual

    # ---- 标的 ----
    symbol = Column(String(32), nullable=False, index=True)  # 600206.SH / 00700.HK / AAPL
    symbol_name = Column(String(64), nullable=True)

    # ---- 委托内容 ----
    side = Column(String(8), nullable=False)  # buy / sell
    order_type = Column(String(16), nullable=False, default="market")  # market / limit / stop / stop_limit
    status = Column(String(20), nullable=False, index=True)  # filled / partially_filled / cancelled / rejected / expired / submitted
    quantity = Column(Float, nullable=False)
    filled_quantity = Column(Float, nullable=False, default=0.0)
    price = Column(Float, nullable=True)  # 委托价（限价单）
    average_price = Column(Float, nullable=True)  # 成交均价
    stop_price = Column(Float, nullable=True)  # 止损/止盈价

    # ---- 金额与费用 ----
    order_value = Column(Float, nullable=False, default=0.0)  # quantity * price
    filled_value = Column(Float, nullable=False, default=0.0)
    commission = Column(Float, nullable=False, default=0.0)  # 佣金
    stamp_duty = Column(Float, nullable=False, default=0.0)  # 印花税（CN 卖出 / HK 双边）
    transfer_fee = Column(Float, nullable=False, default=0.0)  # 过户费（CN 专属）
    total_fee = Column(Float, nullable=False, default=0.0)  # 费用合计

    # ---- 时间 ----
    trade_date = Column(DateTime, nullable=False, index=True)  # 交易日/委托提交时间
    submitted_at = Column(DateTime, nullable=True)
    filled_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    expired_at = Column(DateTime, nullable=True)
    archived_at = Column(DateTime, nullable=False, default=datetime.utcnow)  # 归档时间

    # ---- 溯源 ----
    client_order_id = Column(String(100), nullable=True)  # 系统侧订单号
    exchange_order_id = Column(String(100), nullable=True)  # 券商/桥侧委托号
    source = Column(String(32), nullable=False, default="bridge")  # bridge / manual / import / backfill
    remarks = Column(String(500), nullable=True)
    raw_payload = Column(JSONB, nullable=True)  # 券商原始报文，完整保留

    # ---- 约束与索引 ----
    __table_args__ = (
        UniqueConstraint(
            "broker_type", "exchange_order_id", "trade_date",
            name="uq_order_history_broker_exchange_date",
        ),
        Index("idx_order_history_market_exchange", "market", "exchange"),
        Index("idx_order_history_symbol_date", "symbol", "trade_date"),
        Index("idx_order_history_user_status", "user_id", "status"),
        Index("idx_order_history_archived", "archived_at"),
    )

    def __repr__(self):
        return (
            f"<OrderHistory(id={self.id}, broker={self.broker_type}, "
            f"market={self.market}, symbol={self.symbol}, side={self.side}, "
            f"status={self.status}, trade_date={self.trade_date})>"
        )
