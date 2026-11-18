"""TDX 委托 ``notify`` 参数：智能体/止损单请求免客户端人工确认（2026-11-18）。

背景：TDX 柜台下单默认 ``notify=0``（需在通达信客户端点确认）。5 笔过期委托
证明「等人点」的链路会让智能体单 30 分钟后过期。``notify=1`` 是桥侧支持的
「请求免确认」；客户端不支持时仍会退回人工确认。
"""

from __future__ import annotations

import asyncio
from typing import Any

from backend.services.live_trading.services import broker_client as bc


class _FakeResponse:
    status_code = 200
    text = ""

    def json(self) -> dict[str, Any]:
        return {"status": "submitted", "orders": [{"order_id": "W1", "status": "submitted"}]}


class _FakeClient:
    def __init__(self, sink: list[dict[str, Any]]) -> None:
        self._sink = sink

    async def post(self, url, json=None, headers=None):
        self._sink.append(dict(json or {}))
        return _FakeResponse()


def _run_place(monkeypatch, env_value: str | None) -> dict[str, Any]:
    sink: list[dict[str, Any]] = []
    fake = _FakeClient(sink)

    async def _get_client():
        return fake

    monkeypatch.setattr(bc, "_session_refusal", lambda: None)
    monkeypatch.setattr(bc.TdxBroker, "_get_client", lambda self: _get_client())
    if env_value is None:
        monkeypatch.delenv("TDX_ORDER_NOTIFY", raising=False)
    else:
        monkeypatch.setenv("TDX_ORDER_NOTIFY", env_value)

    broker = bc.TdxBroker(bridge_url="http://bridge.invalid", bridge_token="t")
    res = asyncio.run(
        broker.place_order(
            user_id=10000001,
            symbol="600011.SH",
            side="SELL",
            quantity=3300,
            order_type="LIMIT",
            price=7.66,
            client_order_id="sltp-test",
        )
    )
    assert res.success
    return sink[-1]["orders"][0]


def test_notify_defaults_to_request_no_confirm(monkeypatch) -> None:
    order = _run_place(monkeypatch, None)
    assert order["notify"] == 1


def test_notify_can_be_disabled_by_env(monkeypatch) -> None:
    order = _run_place(monkeypatch, "0")
    assert order["notify"] == 0
