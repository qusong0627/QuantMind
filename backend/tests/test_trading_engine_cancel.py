"""TradingEngine.cancel_order_execution 撤单结果如实上报单测（Phase 4.2/4.3）。

柜台口径实测：全成/已撤后撤单被柜台拒绝（返回 -1），旧实现无条件 ``return True``，
前端显示「撤单已发送」。现在按原因返回 False + 备注 + 通知。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend.services.live_trading.services.trading_engine import TradingEngine
from backend.services.trade_shared.models.enums import OrderSide, OrderStatus, TradingMode


def _order(**over) -> SimpleNamespace:
    base = {
        "order_id": "11111111-2222-3333-4444-555555555555",
        "user_id": "1",
        "tenant_id": "default",
        "client_order_id": "cid-1",
        "exchange_order_id": "EX-1",
        "symbol": "600036.SH",
        "side": OrderSide.SELL,
        "status": OrderStatus.SUBMITTED,
        "trading_mode": TradingMode.REAL,
    }
    base.update(over)
    return SimpleNamespace(**base)


class FakeBroker:
    def __init__(self, verbose_result=None) -> None:
        self.verbose_result = verbose_result
        self.calls: list[tuple] = []

    async def cancel_order_verbose(self, exchange_id, **kwargs):
        self.calls.append(("verbose", exchange_id, kwargs))
        return self.verbose_result


class PlainBroker:
    """老券商：只有 cancel_order（无 verbose 版本）。"""

    def __init__(self, result: bool) -> None:
        self.result = result
        self.calls: list[tuple] = []

    async def cancel_order(self, exchange_id, **kwargs):
        self.calls.append(("plain", exchange_id, kwargs))
        return self.result


def _engine(broker) -> TradingEngine:
    engine = TradingEngine(db=MagicMock(), redis=MagicMock())
    engine._get_broker = MagicMock(return_value=broker)  # type: ignore[method-assign]
    engine.order_service.transition_order_status = AsyncMock()  # type: ignore[method-assign]
    return engine


class TestCancelOrderExecution:
    def test_verbose_accepted_returns_true(self) -> None:
        engine = _engine(FakeBroker(verbose_result=(True, "submitted")))
        with patch(
            "backend.services.live_trading.services.trading_engine.publish_notification_async",
            new=AsyncMock(),
        ) as notify:
            ok = asyncio.run(engine.cancel_order_execution(_order()))
        assert ok is True
        remarks = engine.order_service.transition_order_status.await_args.kwargs["remarks"]
        assert "撤单请求已发送" in remarks
        assert notify.await_count == 0

    def test_counter_rejected_returns_false_with_reason(self) -> None:
        engine = _engine(FakeBroker(verbose_result=(False, "counter_rejected")))
        with patch(
            "backend.services.live_trading.services.trading_engine.publish_notification_async",
            new=AsyncMock(),
        ) as notify:
            ok = asyncio.run(engine.cancel_order_execution(_order()))
        assert ok is False
        remarks = engine.order_service.transition_order_status.await_args.kwargs["remarks"]
        assert "柜台拒绝" in remarks
        assert notify.await_count == 1
        assert notify.await_args.kwargs["title"] == "撤单未成功"
        assert notify.await_args.kwargs["level"] == "warning"

    def test_timeout_returns_false(self) -> None:
        engine = _engine(FakeBroker(verbose_result=(False, "timeout")))
        with patch(
            "backend.services.live_trading.services.trading_engine.publish_notification_async",
            new=AsyncMock(),
        ):
            ok = asyncio.run(engine.cancel_order_execution(_order()))
        assert ok is False
        remarks = engine.order_service.transition_order_status.await_args.kwargs["remarks"]
        assert "超时" in remarks

    def test_plain_cancel_false_returns_false(self) -> None:
        """无 cancel_order_verbose 的券商（老 broker）：result=False 也要如实返回。"""
        engine = _engine(PlainBroker(result=False))
        with patch(
            "backend.services.live_trading.services.trading_engine.publish_notification_async",
            new=AsyncMock(),
        ) as notify:
            ok = asyncio.run(engine.cancel_order_execution(_order()))
        assert ok is False
        assert notify.await_count == 1

    def test_plain_cancel_true_returns_true(self) -> None:
        engine = _engine(PlainBroker(result=True))
        with patch(
            "backend.services.live_trading.services.trading_engine.publish_notification_async",
            new=AsyncMock(),
        ) as notify:
            ok = asyncio.run(engine.cancel_order_execution(_order()))
        assert ok is True
        assert notify.await_count == 0

    def test_broker_exception_returns_false_and_notifies(self) -> None:
        engine = _engine(FakeBroker(verbose_result=None))
        engine._get_broker.side_effect = RuntimeError("rpc down")  # type: ignore[attr-defined]
        with patch(
            "backend.services.live_trading.services.trading_engine.publish_notification_async",
            new=AsyncMock(),
        ) as notify:
            ok = asyncio.run(engine.cancel_order_execution(_order()))
        assert ok is False
        assert notify.await_count == 1
        remarks = engine.order_service.transition_order_status.await_args.kwargs["remarks"]
        assert "通道异常" in remarks

    def test_already_filled_short_circuits(self) -> None:
        broker = FakeBroker(verbose_result=(True, "submitted"))
        engine = _engine(broker)
        ok = asyncio.run(engine.cancel_order_execution(_order(status=OrderStatus.FILLED)))
        assert ok is False
        assert broker.calls == []
        engine.order_service.transition_order_status.assert_not_awaited()

    def test_notification_failure_does_not_break_cancel(self) -> None:
        engine = _engine(FakeBroker(verbose_result=(False, "counter_rejected")))
        with patch(
            "backend.services.live_trading.services.trading_engine.publish_notification_async",
            new=AsyncMock(side_effect=RuntimeError("notify down")),
        ):
            ok = asyncio.run(engine.cancel_order_execution(_order()))
        assert ok is False
