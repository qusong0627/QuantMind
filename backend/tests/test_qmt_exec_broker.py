"""``QmtExecBroker`` 参数映射与错误分类单测（注入 fake RPC 客户端）。

关注的是「本系统字段 → QMT RPC 字段」的映射和失败语义，不是 big-convert 本身。
"""

from __future__ import annotations

import asyncio
from typing import Any

from backend.services.live_trading.services.broker_client import (
    QmtExecBroker,
    create_broker,
)
from backend.services.live_trading.services.qmt_exec_client import QmtExecError


class FakeExecClient:
    configured = True
    account_id = "8888"

    def __init__(self, **over: Any):
        self.submitted: list[dict[str, Any]] = []
        self.cancelled: list[dict[str, Any]] = []
        self.submit_result: Any = {
            "order_id": 1001,
            "order_sysid": "1001",
            "remark": "qmabc",
        }
        self.submit_error: Exception | None = None
        self.asset: dict[str, Any] = {
            "cash": 12345.6,
            "total_asset": 20000.0,
            "market_value": 7000.0,
            "frozen_cash": 100.0,
        }
        self.positions: list[dict[str, Any]] = [
            {
                "symbol": "SH600519",
                "stock_code": "600519.SH",
                "volume": 200,
                "can_use_volume": 100,
                "avg_price": 9.5,
                "market_value": 2000.0,
            }
        ]
        self.cancel_error: Exception | None = None
        for key, value in over.items():
            setattr(self, key, value)

    async def submit_order(self, **kwargs: Any) -> Any:
        self.submitted.append(kwargs)
        if self.submit_error is not None:
            raise self.submit_error
        return self.submit_result

    async def get_asset(self) -> dict[str, Any]:
        return dict(self.asset)

    async def get_positions(self) -> list[dict[str, Any]]:
        return list(self.positions)

    async def cancel_order(self, **kwargs: Any) -> Any:
        self.cancelled.append(kwargs)
        if self.cancel_error is not None:
            raise self.cancel_error
        return True


def _place(client: FakeExecClient, **over: Any) -> Any:
    broker = QmtExecBroker(client=client)
    kwargs: dict[str, Any] = {
        "user_id": 1,
        "symbol": "SH600519",
        "side": "BUY",
        "quantity": 100.0,
        "order_type": "LIMIT",
        "price": 10.0,
        "client_order_id": "mir-cid-1",
    }
    kwargs.update(over)
    return asyncio.run(broker.place_order(**kwargs))


class TestPlaceOrder:
    def test_success_maps_params(self) -> None:
        client = FakeExecClient()
        result = _place(client)
        assert result.success is True
        assert result.exchange_order_id == "1001"
        sent = client.submitted[0]
        assert sent["symbol"] == "SH600519"
        assert sent["side"] == "BUY"
        assert sent["quantity"] == 100.0
        assert sent["order_type"] == "LIMIT"
        assert sent["price"] == 10.0
        assert sent["client_order_id"] == "mir-cid-1"

    def test_lowercase_side_normalized(self) -> None:
        client = FakeExecClient()
        assert _place(client, side="sell").success is True
        assert client.submitted[0]["side"] == "SELL"

    def test_market_order_without_price(self) -> None:
        client = FakeExecClient()
        assert _place(client, order_type="MARKET", price=None).success is True
        assert client.submitted[0]["price"] is None

    def test_invalid_side_rejected_without_rpc(self) -> None:
        client = FakeExecClient()
        result = _place(client, side="HOLD")
        assert result.success is False
        assert "非法方向" in result.message
        assert client.submitted == []

    def test_invalid_order_type_rejected(self) -> None:
        client = FakeExecClient()
        assert _place(client, order_type="STOP").success is False
        assert client.submitted == []

    def test_limit_order_requires_price(self) -> None:
        client = FakeExecClient()
        result = _place(client, price=0)
        assert result.success is False
        assert "限价单必须提供价格" in result.message
        assert client.submitted == []

    def test_rpc_error_maps_to_failure_with_code(self) -> None:
        client = FakeExecClient(
            submit_error=QmtExecError("桥未连接", code="NOT_CONNECTED")
        )
        result = _place(client)
        assert result.success is False
        assert "NOT_CONNECTED" in result.message

    def test_unexpected_error_maps_to_failure(self) -> None:
        client = FakeExecClient(submit_error=RuntimeError("boom"))
        result = _place(client)
        assert result.success is False
        assert "boom" in result.message


class TestQueryAccount:
    def test_maps_asset_and_positions(self) -> None:
        client = FakeExecClient()
        broker = QmtExecBroker(client=client)
        account = asyncio.run(broker.query_account("1"))
        assert account["broker"] == "qmt_exec"
        assert account["available_cash"] == 12345.6
        assert account["balance"] == 20000.0
        position = account["positions"][0]
        assert position["symbol"] == "SH600519"
        assert position["available_volume"] == 100
        assert position["cost_price"] == 9.5

    def test_rpc_error_returns_empty(self) -> None:
        class Broken(FakeExecClient):
            async def get_asset(self) -> dict[str, Any]:
                raise QmtExecError("超时", code="TIMEOUT")

        assert asyncio.run(QmtExecBroker(client=Broken()).query_account("1")) == {}


class TestCancelAndFactory:
    def test_cancel_order_delegates(self) -> None:
        client = FakeExecClient()
        broker = QmtExecBroker(client=client)
        assert asyncio.run(broker.cancel_order("1001", symbol="SH600519")) is True
        assert client.cancelled[0] == {"order_id": "1001", "symbol": "SH600519"}

    def test_cancel_failure_returns_false(self) -> None:
        client = FakeExecClient(cancel_error=QmtExecError("废单", code="REJECTED"))
        broker = QmtExecBroker(client=client)
        assert asyncio.run(broker.cancel_order("1001")) is False

    def test_query_quote_is_empty(self) -> None:
        assert (
            asyncio.run(QmtExecBroker(client=FakeExecClient()).query_quote("SH600519"))
            == {}
        )

    def test_create_broker_registers_qmt_exec(self) -> None:
        broker = create_broker(
            True,
            broker_type="qmt_exec",
            qmt_exec_account_id="8888",
            qmt_exec_strategy_name="quantmind",
        )
        assert isinstance(broker, QmtExecBroker)
        assert broker.client.account_id == "8888"
