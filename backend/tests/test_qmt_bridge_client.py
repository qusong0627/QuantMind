"""``qmt_bridge_client`` 契约测试（不连真桥）。

守护三件事：
1. 账户/持仓只发**一次** HTTP（同步任务连续调用 get_asset + get_positions）；
2. 旧桥把 TDX 账户当成功返回时**必须拒绝**（否则 QMT 快照会被 TDX 数字污染）；
3. 失败语义：HTTP 错 / 业务错 / 非 JSON 都转成 ``QmtExecError``，交给同步任务记录。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from backend.services.live_trading.services.qmt_bridge_client import QmtBridgeClient
from backend.services.live_trading.services.qmt_exec_client import QmtExecError

ACCOUNT_OK = {
    "success": True,
    "account_id": "00000000",
    "account_source": "qmt",
    "channel_used": "qmt",
    "asset": {
        "currency": "CNY",
        "balance": 23818980.55,
        "cash": 21626929.45,
        "asset": 23818980.55,
        "market_value": 2201080.60,
        "frozen_cash": 0.0,
    },
    "positions": [
        {
            "stock_code": "600519.SH",
            "stock_name": "贵州茅台",
            "total_volume": 100,
            "available_volume": 100,
            "cost_price": 1350.0,
            "last_price": 1332.55,
            "market_value": 133255.0,
            "pnl": -1744.99,
        },
        {
            "stock_code": "501018.SH",
            "stock_name": "南方原油LOF",
            "total_volume": 96200,
            "available_volume": 96200,
            "cost_price": 1.9872,
            "last_price": None,
            "market_value": None,
            "pnl": None,
        },
    ],
}

ORDERS_OK = {
    "success": True,
    "channel_used": "qmt",
    "orders": [
        {
            "order_id": "1080000012",
            "stock_code": "600519.SH",
            "stock_name": "贵州茅台",
            "side": "buy",
            "order_price": 10.5,
            "total_volume": 200,
            "filled_volume": 100,
            "status": "partially_filled",
            "time": "09:30:00",
        }
    ],
}

TRADES_OK = {
    "success": True,
    "channel_used": "qmt",
    "trades": [
        {
            "trade_id": "T1",
            "order_id": "1080000012",
            "stock_code": "600519.SH",
            "stock_name": "贵州茅台",
            "side": "sell",
            "traded_volume": 100,
            "traded_price": 10.6,
            "traded_amount": 1060.0,
            "traded_at": "20261028 10:00:00",
        }
    ],
}


def _client(handler, calls: list | None = None) -> QmtBridgeClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append((request.method, str(request.url), request.content))
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return QmtBridgeClient(
        bridge_url="http://127.0.0.1:8550",
        bridge_token="t" * 64,
        timeout=5.0,
        http_client=http,
    )


def test_configured_requires_url_and_token():
    assert QmtBridgeClient(bridge_url="http://x", bridge_token="t").configured is True
    assert (
        QmtBridgeClient(bridge_url="", bridge_token="t", timeout=1).configured is False
    )


def test_asset_and_positions_share_one_http_call():
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/account/query"
        return httpx.Response(200, json=ACCOUNT_OK)

    client = _client(handler, calls)

    async def run():
        asset = await client.get_asset()
        positions = await client.get_positions()
        return asset, positions

    asset, positions = asyncio.run(run())
    assert len(calls) == 1, "get_asset + get_positions 应共用一次 HTTP"
    assert asset["account_id"] == "00000000"
    assert asset["total_asset"] == 23818980.55
    assert asset["cash"] == 21626929.45
    assert asset["market_value"] == 2201080.60
    assert positions[0]["stock_code"] == "600519.SH"
    assert positions[0]["symbol"] == "SH600519"
    assert positions[0]["volume"] == 100
    assert positions[0]["can_use_volume"] == 100
    assert positions[0]["avg_price"] == 1350.0
    assert positions[0]["market_value"] == 133255.0
    # 没现价的持仓保留 0，由同步任务用 QuantDB 收盘价补
    assert positions[1]["market_value"] == 0.0


def test_rejects_non_qmt_response():
    """旧桥（未加载 source=qmt）会返回 TDX 账户且 success=true —— 必须拒绝。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "account_id": "10000001",
                "asset": {"currency": "CNY", "balance": 1.0, "cash": 1.0},
                "positions": [],
                "channel_used": "http",
            },
        )

    client = _client(handler)

    async def run():
        with pytest.raises(QmtExecError) as excinfo:
            await client.get_asset()
        return excinfo.value

    err = asyncio.run(run())
    assert err.code == "NOT_QMT_SOURCE"


def test_http_error_raises_qmt_exec_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="upstream down")

    client = _client(handler)

    async def run():
        with pytest.raises(QmtExecError) as excinfo:
            await client.get_asset()
        return excinfo.value

    err = asyncio.run(run())
    assert err.code == "BRIDGE_HTTP_ERROR"


def test_business_error_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "success": False,
                "error": {
                    "code": "QMT_SOURCE_ERROR",
                    "message": "named pipe rpc timeout",
                },
            },
        )

    client = _client(handler)

    async def run():
        with pytest.raises(QmtExecError) as excinfo:
            await client.get_asset()
        return excinfo.value

    err = asyncio.run(run())
    # 桥信封里的上游错误码要保下来（能区分管道超时 vs 桥没起来）
    assert err.code == "QMT_SOURCE_ERROR"
    assert "400" in str(err)
    assert "named pipe rpc timeout" in str(err)


def test_orders_and_trades_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content or b"{}")
        if body.get("kind") == "trades":
            return httpx.Response(200, json=TRADES_OK)
        return httpx.Response(200, json=ORDERS_OK)

    client = _client(handler)

    async def run():
        orders = await client.query_orders()
        trades = await client.query_trades()
        return orders, trades

    orders, trades = asyncio.run(run())
    assert orders[0]["order_id"] == "1080000012"
    assert orders[0]["symbol"] == "SH600519"
    assert orders[0]["side"] == "BUY"
    assert orders[0]["order_volume"] == 200
    assert orders[0]["traded_volume"] == 100
    assert trades[0]["trade_id"] == "T1"
    assert trades[0]["side"] == "SELL"
    assert trades[0]["traded_amount"] == 1060.0


def test_accepts_bare_account_object():
    """桥沿用 TDX 契约返回裸对象（无 success 字段）——必须当成功。"""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = {k: v for k, v in ACCOUNT_OK.items() if k != "success"}
        return httpx.Response(200, json=payload)

    client = _client(handler)

    async def run():
        return await client.get_asset()

    asset = asyncio.run(run())
    assert asset["total_asset"] == 23818980.55


def test_stale_flag_exposed():
    """休市回落的历史快照必须带 stale=True，同步任务据此跳过落库。"""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = dict(ACCOUNT_OK)
        payload.update({"stale": True, "snapshot_at": "2026-10-28 11:25:46"})
        return httpx.Response(200, json=payload)

    client = _client(handler)

    async def run():
        asset = await client.get_asset()
        return asset

    asyncio.run(run())
    assert client.last_stale is True
    assert client.last_snapshot_at == "2026-10-28 11:25:46"


class _FakeStaleClient:
    configured = True
    account_id = "00000000"
    last_stale = True
    last_snapshot_at = "2026-10-28 11:25:46"

    async def get_asset(self):
        return {
            "account_id": "00000000",
            "cash": 1.0,
            "total_asset": 2.0,
            "market_value": 1.0,
            "frozen_cash": 0.0,
        }

    async def get_positions(self):
        return [
            {
                "stock_code": "600519.SH",
                "symbol": "SH600519",
                "volume": 100,
                "can_use_volume": 100,
                "avg_price": 10.0,
                "open_price": 10.0,
                "market_value": 1000.0,
            }
        ]

    async def refresh_settings(self):
        return None


def test_sync_skips_stale_snapshot(monkeypatch):
    """休市快照不能写成新行：否则库里每个 30s 都多一条旧的「新快照」。"""
    from backend.services.live_trading.services import qmt_account_sync_task as task

    monkeypatch.setattr(task, "batch_quantdb_last_close", lambda symbols: {})
    service = task.QmtAccountSyncService(client=_FakeStaleClient())
    result = asyncio.run(service.sync_account_to_pg())
    assert result["success"] is True
    assert result["skipped"] is True
    assert result["reason"] == "stale_snapshot"
    assert result["position_count"] == 1


def test_factory_prefers_bridge(monkeypatch):
    from backend.services.live_trading.services import qmt_account_sync_task as task

    monkeypatch.setenv("QMT_ACCOUNT_SOURCE", "bridge")
    monkeypatch.setenv("TDX_BRIDGE_URL", "http://127.0.0.1:8550")
    monkeypatch.setenv("TDX_BRIDGE_TOKEN", "t" * 64)
    client = task.get_qmt_account_client()
    assert isinstance(client, QmtBridgeClient)

    monkeypatch.setenv("QMT_ACCOUNT_SOURCE", "qmt_exec")
    client = task.get_qmt_account_client()
    assert not isinstance(client, QmtBridgeClient)
