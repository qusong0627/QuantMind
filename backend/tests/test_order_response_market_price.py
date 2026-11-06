"""OrderResponse 读侧容忍市价单 price=0（2026-11-06 交易记录 500 回归）。

基类 OrderBase.price 是 gt=0（下单入口校验）；而市价单落库 price=0/NULL，
若响应模型沿用它，只要当天有一笔市价单，列表接口就整页 500。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace

from backend.services.trade_shared.models.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TradingMode,
)
from backend.services.trade_shared.schemas.order import OrderResponse


def _row(price):
    return SimpleNamespace(
        id=1,
        order_id=uuid.uuid4(),
        tenant_id="default",
        user_id=10000001,
        portfolio_id=0,
        strategy_id=None,
        symbol="300308.SZ",
        symbol_name="中际旭创",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=100.0,
        price=price,
        stop_price=None,
        trade_action=None,
        position_side=PositionSide.LONG,
        is_margin_trade=False,
        client_order_id="sltpfix-300308.SZ-1",
        remarks="",
        trading_mode=TradingMode.REAL,
        status=OrderStatus.REJECTED,
        filled_quantity=0.0,
        average_price=None,
        order_value=0.0,
        filled_value=0.0,
        commission=0.0,
        submitted_at=None,
        filled_at=None,
        cancelled_at=None,
        expired_at=None,
        exchange_order_id=None,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )


def test_market_order_price_zero_is_valid():
    resp = OrderResponse.model_validate(_row(0.0))
    assert resp.price == 0.0


def test_market_order_price_null_is_valid():
    resp = OrderResponse.model_validate(_row(None))
    assert resp.price is None
