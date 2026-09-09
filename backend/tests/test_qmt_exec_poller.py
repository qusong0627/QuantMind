"""QMT 执行端成交回收轮询器单测（fake RPC，无真机/真库）。

覆盖：只认自己的单、状态去重、日切重置、成交去重、合成成交、轮询节奏、
查询失败隔离。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from backend.services.trade_shared.models.enums import OrderStatus
from backend.services.live_trading.services import qmt_exec_poller as mod
from backend.services.live_trading.services.qmt_exec_poller import QmtExecPoller

DAY = "20260909"


class FakeClient:
    def __init__(
        self,
        *,
        configured: bool = True,
        orders: list[dict[str, Any]] | None = None,
        trades: list[dict[str, Any]] | None = None,
        strategy_name: str = "quantmind",
    ):
        self.configured = configured
        self.account_id = "8888"
        self.orders = orders or []
        self.trades = trades or []
        self.strategy_name = strategy_name
        self.refresh_calls = 0

    async def refresh_settings(self) -> None:
        self.refresh_calls += 1

    def effective_config(self) -> dict[str, Any]:
        return {"strategy_name": self.strategy_name}

    async def query_orders(self) -> list[dict[str, Any]]:
        return list(self.orders)

    async def query_trades(self) -> list[dict[str, Any]]:
        return list(self.trades)

    async def resolve_client_order_id(self, remark: str) -> str:
        return ""


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def commit(self) -> None:
        self.commits += 1


def _fake_order(status: str = "SUBMITTED", filled: float = 0.0) -> Any:
    return SimpleNamespace(
        status=OrderStatus(status),
        filled_quantity=filled,
        order_id="9001",
        symbol="600519.SH",
        price=10.0,
        quantity=100.0,
    )


def _make_poller(client: FakeClient, **kwargs: Any) -> QmtExecPoller:
    poller = QmtExecPoller(client=client, **kwargs)
    poller._redis = object()  # 屏蔽真实 Redis 连接（事件推送在断言中打桩）
    return poller


def _run_poll(
    poller: QmtExecPoller, session: FakeSession | None = None
) -> dict[str, int]:
    session = session or FakeSession()
    with (
        patch.object(QmtExecPoller, "_session", staticmethod(lambda: session)),
        patch.object(mod, "trade_date_str", return_value=DAY),
        patch.object(mod, "publish_order_event"),
    ):
        return asyncio.run(poller.poll_once())


class TestMatchers:
    def test_is_ours_by_remark_prefix(self) -> None:
        assert QmtExecPoller._is_ours({"order_remark": "qmabc123"}, "") is True
        assert QmtExecPoller._is_ours({"order_remark": "manual"}, "quantmind") is False

    def test_is_ours_by_strategy_name(self) -> None:
        item = {"order_remark": "", "strategy_name": "quantmind"}
        assert QmtExecPoller._is_ours(item, "quantmind") is True
        assert QmtExecPoller._is_ours(item, "other") is False

    def test_order_key_prefers_sysid_then_id(self) -> None:
        assert (
            QmtExecPoller._order_key({"order_sysid": "123", "order_id": "9"}) == "123"
        )
        assert QmtExecPoller._order_key({"order_id": "9"}) == "9"

    def test_order_key_ignores_placeholder_values(self) -> None:
        item = {
            "order_sysid": "-1",
            "order_id": "0",
            "symbol": "600519.SH",
            "side": "BUY",
            "order_volume": 100,
        }
        assert QmtExecPoller._order_key(item) == "600519.SH:BUY:100"

    def test_trade_key_prefers_trade_id(self) -> None:
        assert QmtExecPoller._trade_key({"trade_id": "T1"}) == "T1"
        assert QmtExecPoller._trade_key({"traded_id": "T2"}) == "T2"

    def test_trade_key_fallback_composite(self) -> None:
        item = {
            "order_sysid": "123",
            "traded_volume": 100,
            "traded_price": 10.5,
            "traded_at": "20260909100000",
        }
        assert QmtExecPoller._trade_key(item) == "123:100:10.5:20260909100000"


class TestLoopPacing:
    def test_disabled_sleeps_long(self) -> None:
        assert _make_poller(FakeClient(configured=False))._next_sleep() == (
            mod.DISABLED_SLEEP_SECONDS
        )

    def test_off_hours_sleeps_longer(self) -> None:
        poller = _make_poller(FakeClient())
        with patch.object(mod, "is_trading_time", return_value=False):
            assert poller._next_sleep() == mod.OFF_HOURS_SLEEP_SECONDS

    def test_trading_uses_interval(self) -> None:
        poller = _make_poller(FakeClient(), interval=1.5)
        with patch.object(mod, "is_trading_time", return_value=True):
            assert poller._next_sleep() == 1.5

    def test_day_rollover_clears_caches(self) -> None:
        poller = _make_poller(FakeClient())
        poller._day = DAY
        poller._seen_orders["k"] = ("SUBMITTED", 0.0)
        poller._seen_trades.add("t")
        with patch.object(mod, "trade_date_str", return_value=DAY):
            poller._rollover_if_new_day()
        assert poller._seen_orders and poller._seen_trades
        with patch.object(mod, "trade_date_str", return_value="20260910"):
            poller._rollover_if_new_day()
        assert not poller._seen_orders and not poller._seen_trades


class TestPollOnce:
    def test_not_configured_skips(self) -> None:
        assert _run_poll(_make_poller(FakeClient(configured=False))) == {"skipped": 1}

    def test_foreign_orders_ignored(self) -> None:
        client = FakeClient(orders=[{"order_remark": "manual", "order_sysid": "1"}])
        with patch.object(mod, "apply_execution_report") as apply:
            result = _run_poll(_make_poller(client))
        assert result["changed"] == 0
        apply.assert_not_called()

    def test_status_change_applies_once(self) -> None:
        item = {
            "order_sysid": "123",
            "order_remark": "qmabc",
            "status": "FILLED",
            "traded_volume": 100,
            "traded_price": 10.5,
        }
        poller = _make_poller(FakeClient(orders=[item]))
        session = FakeSession()
        apply = AsyncMock(return_value=OrderStatus.FILLED)
        with (
            patch.object(
                QmtExecPoller, "_resolve", AsyncMock(return_value=_fake_order())
            ),
            patch.object(mod, "apply_execution_report", apply),
            patch.object(QmtExecPoller, "_has_trade", AsyncMock(return_value=True)),
        ):
            first = _run_poll(poller, session)
            second = _run_poll(poller, session)
        assert first["changed"] == 1
        assert second["changed"] == 0  # (状态, 成交量) 未变化 → 不重复落库
        assert apply.await_count == 1
        assert session.commits == 1

    def test_trade_dedup(self) -> None:
        order_item = {
            "order_sysid": "123",
            "order_remark": "qmabc",
            "status": "FILLED",
            "traded_volume": 100,
            "traded_price": 10.5,
        }
        trade_item = dict(order_item, trade_id="T1")
        poller = _make_poller(FakeClient(orders=[order_item], trades=[trade_item]))
        apply = AsyncMock(return_value=OrderStatus.FILLED)
        with (
            patch.object(
                QmtExecPoller,
                "_resolve",
                AsyncMock(return_value=_fake_order("FILLED", 100.0)),
            ),
            patch.object(mod, "apply_execution_report", apply),
            patch.object(QmtExecPoller, "_has_trade", AsyncMock(return_value=True)),
        ):
            _run_poll(poller)
            first_calls = apply.await_count
            _run_poll(poller)
        assert poller._seen_trades == {"T1"}
        # 第二轮委托状态未变 + 成交 id 已见 → 不再落库
        assert apply.await_count == first_calls

    def test_query_failure_isolated(self) -> None:
        from backend.services.live_trading.services.qmt_exec_client import QmtExecError

        class Broken(FakeClient):
            async def query_orders(self) -> list[dict[str, Any]]:
                raise QmtExecError("桥未连接", code="NOT_CONNECTED")

        assert _run_poll(_make_poller(Broken())) == {"error": 1}
