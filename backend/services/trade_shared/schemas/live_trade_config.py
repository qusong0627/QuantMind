from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class ScheduleType(str, Enum):
    INTERVAL = "interval"
    WEEKLY = "weekly"


class TradeWeekday(str, Enum):
    MON = "MON"
    TUE = "TUE"
    WED = "WED"
    THU = "THU"
    FRI = "FRI"


class TradingSession(str, Enum):
    AM = "AM"
    PM = "PM"
    # T-P2-07：盘后固定价格交易 15:05–15:30（按收盘价成交）——模拟托管支持；
    # 实盘通道待核实（real_trading_utils 显式拒绝）
    AFTER_HOURS = "AFTER_HOURS"


class LiveOrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class ExecutionConfigSchema(BaseModel):
    max_buy_drop: float = Field(default=-0.03, ge=-0.10, le=-0.01)
    stop_loss: float = Field(default=-0.08, ge=-0.20, le=-0.03)


class LiveTradeConfigSchema(BaseModel):
    rebalance_days: int | None = Field(default=5)
    schedule_type: ScheduleType = Field(default=ScheduleType.INTERVAL)
    trade_weekdays: list[TradeWeekday] = Field(default_factory=list)
    enabled_sessions: list[TradingSession] = Field(default_factory=lambda: [TradingSession.PM])
    sell_time: str = Field(default="14:30")
    buy_time: str = Field(default="14:45")
    sell_first: bool = Field(default=True)
    order_type: LiveOrderType = Field(default=LiveOrderType.LIMIT)
    max_price_deviation: float | None = Field(default=0.02, ge=0.0, le=0.05)
    max_orders_per_cycle: int = Field(default=20, ge=1, le=100)

    @field_validator("rebalance_days")
    @classmethod
    def validate_rebalance_days(cls, value: int | None) -> int | None:
        if value is None:
            return value
        if value not in {1, 3, 5, 10, 20}:
            raise ValueError("rebalance_days must be one of 1/3/5/10/20")
        return value

    @field_validator("sell_time", "buy_time")
    @classmethod
    def validate_hhmm(cls, value: str) -> str:
        if len(value) != 5 or value[2] != ":":
            raise ValueError("time must be HH:MM")
        hours = int(value[:2])
        minutes = int(value[3:])
        if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
            raise ValueError("invalid time")
        return value

    @model_validator(mode="after")
    def validate_logic(self):
        if self.schedule_type == ScheduleType.INTERVAL and self.rebalance_days is None:
            raise ValueError("rebalance_days is required when schedule_type=interval")
        if self.schedule_type == ScheduleType.WEEKLY and not self.trade_weekdays:
            raise ValueError("trade_weekdays is required when schedule_type=weekly")
        if self.sell_time >= self.buy_time:
            raise ValueError("buy_time must be later than sell_time")
        if self.order_type == LiveOrderType.LIMIT and self.max_price_deviation is None:
            raise ValueError("max_price_deviation is required for LIMIT order")
        return self
