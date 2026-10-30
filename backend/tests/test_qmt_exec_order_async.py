"""QMT 客户端发单三条改进的单测（fake 后端，无真机）：

1. **异步受理**（2026-10-30）：``wait_settlement=False`` 时不再等券商分配委托号，
   管道不被单笔占死；默认开启，可用页面配置 ``async_orders`` / 环境
   ``QMT_ASYNC_ORDERS`` 关闭。
2. **超时待核对**：客户端超时 ≠ 没下单——记入 ``qm:qmt:unverified:{account}``，
   由 ``qmt_pipe_guard`` 从 order_events 比对销账；**绝不重试**。
3. **直连台账**：受理后补写一条 ``orders``（source=qmt_direct），
   与既有单按 client_order_id 冲突时跳过；台账失败不影响交易。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from backend.services.live_trading.services.qmt_exec_client import (
    QmtExecClient,
    QmtExecError,
)


class _FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.values: dict[str, str] = {}
        self.expires: dict[str, int] = {}

    async def hset(self, key: str, field: str, value: str) -> None:
        self.hashes.setdefault(key, {})[field] = value

    async def expire(self, key: str, ttl: int) -> None:
        self.expires[key] = ttl

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value
        if ex:
            self.expires[key] = ex

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


class _AsyncBackend:
    """接受 wait_settlement 的后端，记录收到的值。"""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.wait_settlement: bool | None = None

    def submit_order(self, *, wait_settlement: bool = True, **kwargs: Any) -> dict:
        self.wait_settlement = wait_settlement
        if self.delay:
            time.sleep(self.delay)
        return {"order_id": "520", "remark": kwargs.get("remark", "")}


class _LegacyBackend:
    """旧签名（没有 wait_settlement）：客户端不得传该参数，否则 TypeError。"""

    def __init__(self) -> None:
        self.called = False

    def submit_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        price: float,
        remark: str,
        strategy_name: str,
    ) -> dict:
        self.called = True
        return {"order_id": "521", "remark": remark}


def _client(backend: Any, *, timeout: float = 5.0) -> QmtExecClient:
    client = QmtExecClient(enabled=True, account_id="8888", timeout=timeout)
    client._get_backend = lambda cfg: backend  # type: ignore[method-assign]
    return client


@pytest.mark.asyncio
async def test_async_mode_default_passes_wait_settlement_false(monkeypatch):
    monkeypatch.delenv("QMT_ASYNC_ORDERS", raising=False)
    monkeypatch.delenv("QMT_RECORD_ORDERS", raising=False)
    fake_redis = _FakeRedis()

    async def _redis():
        return fake_redis

    backend = _AsyncBackend()
    client = _client(backend)
    client._get_redis = _redis  # type: ignore[method-assign]
    client._page_settings = {"record_orders": "false"}

    result = await client.submit_order(
        symbol="600036.SH", side="BUY", quantity=100, price=40.0
    )
    assert result["order_id"] == "520"
    assert backend.wait_settlement is False


@pytest.mark.asyncio
async def test_sync_mode_when_flag_off(monkeypatch):
    monkeypatch.delenv("QMT_ASYNC_ORDERS", raising=False)
    fake_redis = _FakeRedis()

    async def _redis():
        return fake_redis

    backend = _AsyncBackend()
    client = _client(backend)
    client._get_redis = _redis  # type: ignore[method-assign]
    client._page_settings = {"async_orders": "false", "record_orders": "false"}

    await client.submit_order(symbol="600036.SH", side="BUY", quantity=100, price=40.0)
    assert backend.wait_settlement is True


@pytest.mark.asyncio
async def test_legacy_backend_without_wait_settlement_still_works(monkeypatch):
    monkeypatch.delenv("QMT_ASYNC_ORDERS", raising=False)
    fake_redis = _FakeRedis()

    async def _redis():
        return fake_redis

    backend = _LegacyBackend()
    client = _client(backend)
    client._get_redis = _redis  # type: ignore[method-assign]
    client._page_settings = {"record_orders": "false"}

    result = await client.submit_order(
        symbol="600036.SH", side="BUY", quantity=100, price=40.0
    )
    assert backend.called and result["order_id"] == "521"


@pytest.mark.asyncio
async def test_timeout_records_unverified_and_does_not_retry(monkeypatch):
    monkeypatch.delenv("QMT_ASYNC_ORDERS", raising=False)
    fake_redis = _FakeRedis()
    calls = {"n": 0}

    async def _redis():
        return fake_redis

    class _SlowBackend(_AsyncBackend):
        def submit_order(self, *, wait_settlement: bool = True, **kwargs: Any) -> dict:
            calls["n"] += 1
            time.sleep(0.3)
            return {"order_id": "999"}

    client = _client(_SlowBackend(), timeout=0.05)
    client._get_redis = _redis  # type: ignore[method-assign]
    client._page_settings = {"record_orders": "false"}

    with pytest.raises(QmtExecError) as excinfo:
        await client.submit_order(
            symbol="600036.SH",
            side="SELL",
            quantity=100,
            price=40.0,
            client_order_id="stab-timeout-1",
        )
    assert excinfo.value.code == "TIMEOUT"
    assert calls["n"] == 1  # 绝不重试

    key = "qm:qmt:unverified:8888"
    entries = fake_redis.hashes.get(key) or {}
    assert entries, "超时委托必须记入待核对清单"
    payload = json.loads(next(iter(entries.values())))
    assert payload["client_order_id"] == "stab-timeout-1"
    assert payload["symbol"] == "600036.SH"
    assert fake_redis.expires.get(key, 0) > 0


@pytest.mark.asyncio
async def test_ledger_failure_does_not_break_order(monkeypatch):
    monkeypatch.delenv("QMT_RECORD_ORDERS", raising=False)
    fake_redis = _FakeRedis()

    async def _redis():
        return fake_redis

    def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "backend.shared.database_manager_v2.get_session", _boom, raising=True
    )

    client = _client(_AsyncBackend())
    client._get_redis = _redis  # type: ignore[method-assign]
    client._page_settings = {"record_orders": "true"}

    result = await client.submit_order(
        symbol="600036.SH", side="BUY", quantity=100, price=40.0
    )
    assert result["order_id"] == "520"


class _CancelBackend:
    """占位号（xt*）撤单必败、柜台真号可撤；query_orders 返回在途委托。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.orders = [
            {
                "order_id": "7322",
                "order_sysid": "7322",
                "order_remark": "qmabc",
                "status": "SUBMITTED",
                "symbol": "SH601398",
            }
        ]

    def cancel_order(self, *, order_id: str, symbol: str) -> dict:
        self.calls.append(order_id)
        if order_id.startswith("xt"):
            raise QmtExecError("撤单被拒（返回 -1）", code="CANCEL_REJECTED")
        return {"success": True, "order_id": order_id}

    def query_orders(self, cancelable_only: bool = False) -> list[dict]:
        return list(self.orders)


@pytest.mark.asyncio
async def test_cancel_resolves_placeholder_id_by_remark():
    """本地占位号 xt* 撤不动 → 按 remark 反查柜台真号再撤（2026-10-30 实测）。"""
    fake_redis = _FakeRedis()

    async def _redis():
        return fake_redis

    backend = _CancelBackend()
    client = _client(backend)
    client._get_redis = _redis  # type: ignore[method-assign]
    await client._remember_order_id("xt1082143155", "qmabc")

    result = await client.cancel_order(order_id="xt1082143155", symbol="601398.SH")
    assert backend.calls == ["xt1082143155", "7322"]
    assert result["success"] is True


@pytest.mark.asyncio
async def test_cancel_without_mapping_raises_original_error():
    fake_redis = _FakeRedis()

    async def _redis():
        return fake_redis

    backend = _CancelBackend()
    client = _client(backend)
    client._get_redis = _redis  # type: ignore[method-assign]

    with pytest.raises(QmtExecError) as excinfo:
        await client.cancel_order(order_id="xt999", symbol="601398.SH")
    assert excinfo.value.code == "CANCEL_REJECTED"
    assert backend.calls == ["xt999"]


@pytest.mark.asyncio
async def test_cancel_timeout_is_not_retried():
    """撤单超时不重试（委托可能已撤，重试只是噪音）。"""

    class _TimeoutBackend(_CancelBackend):
        def cancel_order(self, *, order_id: str, symbol: str) -> dict:
            self.calls.append(order_id)
            raise QmtExecError("撤单超时", code="TIMEOUT")

    fake_redis = _FakeRedis()

    async def _redis():
        return fake_redis

    backend = _TimeoutBackend()
    client = _client(backend)
    client._get_redis = _redis  # type: ignore[method-assign]
    await client._remember_order_id("xt1", "qmabc")

    with pytest.raises(QmtExecError) as excinfo:
        await client.cancel_order(order_id="xt1", symbol="601398.SH")
    assert excinfo.value.code == "TIMEOUT"
    assert backend.calls == ["xt1"]


class _RemarkBackend:
    def __init__(self) -> None:
        self.remarks: list[str] = []

    def submit_order(
        self, *, remark: str, wait_settlement: bool = True, **kwargs
    ) -> dict:
        self.remarks.append(remark)
        return {"order_id": "1", "remark": remark}


@pytest.mark.asyncio
async def test_submit_without_cid_generates_unique_remark():
    """无 client_order_id 时备注必须唯一：确定性备注会撞幂等键 → 第二笔被静默丢弃。"""
    fake_redis = _FakeRedis()

    async def _redis():
        return fake_redis

    backend = _RemarkBackend()
    client = _client(backend)
    client._get_redis = _redis  # type: ignore[method-assign]
    client._page_settings = {"record_orders": "false"}

    await client.submit_order(symbol="600036.SH", side="BUY", quantity=100, price=40.0)
    await client.submit_order(symbol="600036.SH", side="BUY", quantity=100, price=40.0)

    assert len(backend.remarks) == 2
    assert backend.remarks[0] != backend.remarks[1]


def test_classify_bridge_busy_code():
    from backend.services.live_trading.services.qmt_exec_client import _classify_error

    assert (
        _classify_error(RuntimeError("BRIDGE_BUSY: order queue backlog=4 >= 3"))
        == "BRIDGE_BUSY"
    )


class _TickBackend:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def get_full_tick(self, codes: list[str]) -> dict:
        self.calls.append(list(codes))
        return {code: {"lastPrice": 1.0} for code in codes}


@pytest.mark.asyncio
async def test_get_full_tick_chunks_large_batches(monkeypatch):
    """大批量拉行情必须分批：单个请求占住单线程管道的时间要短。"""
    monkeypatch.setenv("QMT_TICK_CHUNK", "100")
    backend = _TickBackend()
    client = _client(backend)
    client._get_redis = _noop_redis  # type: ignore[method-assign]

    codes = [f"60{i:04d}.SH" for i in range(250)]
    data = await client.get_full_tick(codes)

    assert [len(call) for call in backend.calls] == [100, 100, 50]
    assert len(data) == 250


async def _noop_redis():
    return _FakeRedis()
