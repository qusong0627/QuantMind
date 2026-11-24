"""时光回放（历史单步推演）数据模型。

与 sim_orders / sim_trades 刻意分表，不共用：
- 回放账户是独立会话账户，丢弃会话即 DELETE WHERE session_id，生命周期完全分离
- sim_* 的既有查询（尤其 SimTradeService.get_stats 按 executed_at 分组）
  不需要为回放加过滤条件，零回归风险

关键字段 trade_date 是**模拟交易日**，不是墙钟时间：5 分钟内回放 60 天，
所有成交的 created_at 会挤在同一天，只有 trade_date 能做逐日盈亏归因。
"""

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.services.simulation.models import Base, TimestampMixin
from backend.services.simulation.models.order import (
    OrderSide,
    OrderStatus,
    OrderType,
)


def _enum(py_enum):
    """枚举列一律存 VARCHAR（native_enum=False）。

    replay_* 的 DDL 用的是 VARCHAR(20)，不像 sim_orders 那样依赖 PG 原生
    枚举类型（orderside/orderstatus/...）。这样回放建表不需要额外的
    CREATE TYPE 迁移，删表也不会留下孤儿类型。
    """
    return Enum(
        py_enum,
        native_enum=False,
        length=20,
        values_callable=lambda x: [e.value for e in x],
    )


class ReplayStatus(str, enum.Enum):
    CREATING = "creating"
    GENERATING = "generating"  # 批量预生成信号中
    READY = "ready"
    # 手动模式：已生成当日提案，等用户勾选确认。提案存在 pending_orders。
    AWAITING_CONFIRM = "awaiting_confirm"
    STEPPING = "stepping"  # 单步执行中，用于防连点
    FINISHED = "finished"
    FAILED = "failed"
    DISCARDED = "discarded"


class OrderOrigin(str, enum.Enum):
    SIGNAL = "signal"
    MANUAL = "manual"
    STOP_LOSS = "stop_loss"
    CODE = "code"  # 策略代码模式（code_runner 用户 hooks 产物）


class ReplaySession(Base, TimestampMixin):
    __tablename__ = "replay_sessions"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", index=True
    )
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    strategy_params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    initial_cash: Mapped[float] = mapped_column(Float, nullable=False)

    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    # 游标：已结算的最后一个交易日；None 表示还没走第一步。
    # 不能叫 current_date —— 那是 SQL 保留函数名，建表会语法错误。
    cursor_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 预计算的下一步，避免每次点击都查日历
    next_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    sessions_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sessions_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[ReplayStatus] = mapped_column(
        _enum(ReplayStatus),
        nullable=False,
        default=ReplayStatus.CREATING,
        index=True,
    )
    signal_progress: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    auto_trade: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    stop_loss_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 手动模式下当日待用户确认的提案
    pending_orders: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_replay_session_scope_status", "tenant_id", "user_id", "status"),
    )


class ReplayOrder(Base, TimestampMixin):
    __tablename__ = "replay_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("replay_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 模拟交易日，非墙钟；逐日盈亏归因只能靠这一列
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)

    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[OrderSide] = mapped_column(
        _enum(OrderSide),
        nullable=False,
    )
    order_type: Mapped[OrderType] = mapped_column(
        _enum(OrderType),
        nullable=False,
        default=OrderType.MARKET,
    )
    status: Mapped[OrderStatus] = mapped_column(
        _enum(OrderStatus),
        nullable=False,
        default=OrderStatus.PENDING,
    )
    origin: Mapped[OrderOrigin] = mapped_column(
        _enum(OrderOrigin),
        nullable=False,
        default=OrderOrigin.SIGNAL,
    )

    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    filled_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    reject_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    price_source: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("idx_replay_order_session_date", "session_id", "trade_date"),
    )


class ReplayTrade(Base, TimestampMixin):
    __tablename__ = "replay_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("replay_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("replay_orders.order_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)

    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[OrderSide] = mapped_column(
        _enum(OrderSide),
        nullable=False,
    )
    origin: Mapped[OrderOrigin] = mapped_column(
        _enum(OrderOrigin),
        nullable=False,
        default=OrderOrigin.SIGNAL,
    )

    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    trade_value: Mapped[float] = mapped_column(Float, nullable=False)
    commission: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    stamp_duty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    transfer_fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    price_source: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── 盈亏归因（R1）──
    # 卖出撮合**前**的移动加权成本。必须在 apply_fill 之前抓取：
    # Lua 在 volume<=0.0001 时会删掉整个持仓 dict，清仓后 cost 永久丢失。
    avg_cost_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 已实现盈亏（扣费后）。买入为 None —— 买入不产生已实现盈亏。
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 持有天数（自然日，首次买入日 → 卖出日）。买入为 None。
    holding_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 墙钟时间仅供排障，盈亏归因一律用 trade_date
    executed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    __table_args__ = (
        Index("idx_replay_trade_session_date", "session_id", "trade_date"),
    )


class ReplaySignal(Base, TimestampMixin):
    """回放专用信号表 —— 与 engine_signal_scores 完全隔离。

    隔离理由：
    - engine_signal_scores 按保留窗口清理（默认 365 天，
      `INFERENCE_PREDICTION_RETENTION_DAYS`），回放历史信号会被误删
    - 写入 engine_signal_scores 会覆写 Redis qm:signal:latest，干扰实盘门禁
    - ST 名单按当天算，历史回放会引入前视偏差
    - 回放信号只需 (symbol, score)，不需要 run_id/feature_version 等推理管线字段
    """

    __tablename__ = "replay_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("replay_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    score: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "trade_date",
            "symbol",
            name="uq_replay_signal_session_date_symbol",
        ),
        Index("idx_replay_signal_session_date", "session_id", "trade_date"),
    )


class ReplayEquitySnapshot(Base, TimestampMixin):
    __tablename__ = "replay_equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("replay_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)

    cash: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    market_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_asset: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    day_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cum_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # ── 盈亏拆分（R1）──
    # 累计已实现盈亏（当日及之前所有卖出的 realized_pnl 之和）
    realized_pnl_cum: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 当日浮动盈亏 = Σ (收盘价 - 移动加权成本) × 持仓量
    unrealized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    position_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    positions: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint(
            "session_id", "trade_date", name="uq_replay_equity_session_date"
        ),
    )
