"""``QmtExecClient`` 只读 RPC 重试与桥侧错误码分类单测（fake 后端，无真机）。

背景（2026-10-29 实测）：pipe 桥单线程，``QmtExecPoller``（2s）+ ``QmtSync``（30s）
并发时只读查询会偶发 ``TransportTimeout: named pipe rpc timeout``（桥**主动抛回**，
线程已结束）——只读查询可重试一次；**外层 wait_for 超时（线程仍卡着）不重试**；
下单/撤单任何超时都不重试（委托可能已在柜台，重试会重复下单）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.services.live_trading.services.qmt_exec_client import (
    QmtExecClient,
    QmtExecError,
)

TRANSPORT_TIMEOUT = "TransportTimeout: named pipe rpc timeout"


class _FlakyBackend:
    """前 ``fail_times`` 次调用抛异常，之后成功；记录真实调用次数。"""

    def __init__(self, *, fail_times: int = 1, exc: Exception | None = None) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self._exc = exc or RuntimeError(TRANSPORT_TIMEOUT)

    def _tick(self) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self._exc

    def ping(self) -> dict[str, Any]:
        self._tick()
        return {"pong": True}

    def get_asset(self) -> dict[str, Any]:
        self._tick()
        return {"total_asset": 1.0}

    def get_positions(self) -> list[dict[str, Any]]:
        self._tick()
        return []

    def query_orders(self, cancelable_only: bool = False) -> list[dict[str, Any]]:
        self._tick()
        return []

    def query_trades(self) -> list[dict[str, Any]]:
        self._tick()
        return []

    def get_full_tick(self, codes: list[str]) -> dict[str, Any]:
        self._tick()
        return {}

    def get_instrument_detail(self, code: str) -> dict[str, Any]:
        self._tick()
        return {}

    def submit_order(self, **kwargs: Any) -> dict[str, Any]:
        self._tick()
        return {"order_id": "1"}

    def cancel_order(self, *, order_id: str, symbol: str) -> dict[str, Any]:
        self._tick()
        return {"success": True}


def _client(fake: _FlakyBackend) -> QmtExecClient:
    client = QmtExecClient(enabled=True, account_id="8888", timeout=0.5)
    client._get_backend = lambda cfg: fake  # type: ignore[method-assign]
    return client


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.get_asset(),
        lambda c: c.get_positions(),
        lambda c: c.query_orders(),
        lambda c: c.query_trades(),
        lambda c: c.get_full_tick(["600519.SH"]),
        lambda c: c.get_instrument_detail("600519.SH"),
    ],
)
def test_read_only_call_retries_once_on_transport_timeout(call) -> None:
    """只读查询遇 TransportTimeout 重试一次即成功，调用方无感。"""
    fake = _FlakyBackend(fail_times=1)
    client = _client(fake)
    asyncio.run(call(client))
    assert fake.calls == 2  # 第一次超时 + 重试成功


def test_read_only_call_gives_up_after_one_retry() -> None:
    """连续失败只重试一次，最终抛 TIMEOUT（不无限重试）。"""
    fake = _FlakyBackend(fail_times=99)
    client = _client(fake)
    with pytest.raises(QmtExecError) as exc:
        asyncio.run(client.get_asset())
    assert exc.value.code == "TIMEOUT"
    assert fake.calls == 2


def test_read_only_non_retryable_error_is_not_retried() -> None:
    """业务性错误（RPC_ERROR）不重试——重试只针对超时/桥忙。"""
    fake = _FlakyBackend(fail_times=99, exc=RuntimeError("boom"))
    client = _client(fake)
    with pytest.raises(QmtExecError) as exc:
        asyncio.run(client.get_asset())
    assert exc.value.code == "RPC_ERROR"
    assert fake.calls == 1


def test_submit_order_is_never_retried() -> None:
    """下单超时绝不重试：委托可能已在柜台，重试=重复下单。"""
    fake = _FlakyBackend(fail_times=99)
    client = _client(fake)
    with pytest.raises(QmtExecError) as exc:
        asyncio.run(
            client.submit_order(symbol="600519.SH", side="BUY", quantity=100, price=1.0)
        )
    assert exc.value.code == "TIMEOUT"
    assert fake.calls == 1


def test_cancel_order_is_never_retried() -> None:
    """撤单同样不自动重试（保持既有设计：超时先查委托再人工决定）。"""
    fake = _FlakyBackend(fail_times=99)
    client = _client(fake)
    with pytest.raises(QmtExecError) as exc:
        asyncio.run(client.cancel_order(order_id="1", symbol="600519.SH"))
    assert exc.value.code == "TIMEOUT"
    assert fake.calls == 1


def test_price_deviation_rejection_gets_its_own_code() -> None:
    """桥侧风控拒单（price deviation）不再笼统归 RPC_ERROR，便于上层识别。"""
    fake = _FlakyBackend(
        fail_times=99, exc=RuntimeError("risk: price deviation 5.13% > 5.00%")
    )
    client = _client(fake)
    with pytest.raises(QmtExecError) as exc:
        asyncio.run(
            client.submit_order(
                symbol="600519.SH", side="BUY", quantity=100, price=10.73
            )
        )
    assert exc.value.code == "PRICE_DEVIATION"
    assert fake.calls == 1


def test_pipe_busy_is_retryable_for_reads() -> None:
    """Windows 侧 ``ERROR_PIPE_BUSY``（管道实例忙）按 BRIDGE_BUSY 重试一次。"""
    fake = _FlakyBackend(fail_times=1, exc=RuntimeError("ERROR_PIPE_BUSY"))
    client = _client(fake)
    asyncio.run(client.get_positions())
    assert fake.calls == 2


def test_pipe_busy_does_not_retry_submit() -> None:
    fake = _FlakyBackend(fail_times=99, exc=RuntimeError("ERROR_PIPE_BUSY"))
    client = _client(fake)
    with pytest.raises(QmtExecError) as exc:
        asyncio.run(
            client.submit_order(symbol="600519.SH", side="BUY", quantity=100, price=1.0)
        )
    assert exc.value.code == "BRIDGE_BUSY"
    assert fake.calls == 1


class _SlowBackend(_FlakyBackend):
    """调用阻塞超过外层超时：模拟线程仍卡在单线程管道上。"""

    def __init__(self, delay: float) -> None:
        super().__init__(fail_times=0)
        self.delay = delay

    def get_positions(self) -> list[dict[str, Any]]:
        import time

        self.calls += 1
        time.sleep(self.delay)
        return []


def test_outer_wait_for_timeout_is_not_retried() -> None:
    """线程仍卡在桥上时，外层 wait_for 超时不得重试（会在管道上再排一个请求）。"""
    fake = _SlowBackend(delay=0.5)
    client = _client(fake)
    client.timeout = 0.1
    with pytest.raises(QmtExecError) as exc:
        asyncio.run(client.get_positions())
    assert exc.value.code == "TIMEOUT"
    assert fake.calls == 1
