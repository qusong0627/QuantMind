"""``QmtExecClient.bridge_status()`` 单测：不受本侧总闸约束的只读桥自述。

与 ``ping()`` 的分工是本方法存在的理由——arena 状态面在本侧关闸时也要看到
桥那头 ``rpc_allow_order_methods`` 的真实值（09-11 教训：把「自家未接线」
说成「对方未放开」会把排查带反方向）。上游 baymax
``QmtBridgeBroker.bridge_status`` 同语义同方法名。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.services.live_trading.services.qmt_exec_client import (
    QmtExecClient,
    QmtExecError,
)


class _FakeBackend:
    """只实现 ping 的假桥——bridge_status 不应触碰账户面。"""

    def __init__(self) -> None:
        self.ping_calls = 0

    def ping(self) -> dict[str, Any]:
        self.ping_calls += 1
        return {
            "ok": True,
            "result": {"allow_order_methods": True, "version": "bigqmt-1.2"},
            "account_id": "10000001",
        }

    def get_asset(self) -> dict[str, Any]:  # pragma: no cover - 本文件不触达
        raise AssertionError("bridge_status 不应触碰账户面")

    def get_positions(self) -> list[dict[str, Any]]:  # pragma: no cover
        raise AssertionError("bridge_status 不应触碰账户面")


def _client(
    *, enabled: bool, account_id: str = "10000001"
) -> tuple[QmtExecClient, _FakeBackend]:
    fake = _FakeBackend()
    client = QmtExecClient(enabled=enabled, account_id=account_id, timeout=1.0)
    # 注入假桥：绕开 BigConvertBackend（正常路径要连真 big-convert Redis）
    client._get_backend = lambda cfg: fake  # type: ignore[method-assign]
    return client, fake


def test_bridge_status_works_with_local_gate_closed():
    """本侧关闸：ping() 拒（DISABLED），bridge_status() 照打桥、如实回报。"""
    client, fake = _client(enabled=False)
    out = asyncio.run(client.bridge_status())
    assert out["result"]["allow_order_methods"] is True
    assert fake.ping_calls == 1

    with pytest.raises(QmtExecError) as exc:
        asyncio.run(client.ping())
    assert exc.value.code == "DISABLED"
    # 关闸的 ping 在 _require_enabled 就拒了，根本没打桥
    assert fake.ping_calls == 1


def test_bridge_status_without_account_fails_without_rpc():
    """没有账号就没有桥可问——NOT_CONFIGURED，且不产生任何 RPC。"""
    client, fake = _client(enabled=True, account_id="")
    with pytest.raises(QmtExecError) as exc:
        asyncio.run(client.bridge_status())
    assert exc.value.code == "NOT_CONFIGURED"
    assert fake.ping_calls == 0
