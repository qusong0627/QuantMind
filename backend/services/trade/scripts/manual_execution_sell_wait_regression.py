#!/usr/bin/env python3
"""
最小回归验证脚本：手动任务“先卖后买 + 卖单等待 + 买单重算”。

运行方式（项目根目录）：
  python backend/services/trade/scripts/manual_execution_sell_wait_regression.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import types
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = pathlib.Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.services.trade.services.manual_execution_service as mes  # noqa: E402


@dataclass
class _ScalarResult:
    value: object

    def scalar_one_or_none(self):
        return self.value


@dataclass
class _MappingResult:
    rows: list[dict]

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _FakeDB:
    def __init__(self) -> None:
        self.sell_poll_count = 0
        self.portfolio = SimpleNamespace(name="regression_portfolio", id=1)
        self.expected_sell_client_id = "manual-abcd1234-0001"

    async def execute(self, statement):
        sql = str(statement).lower()
        if "from portfolios" in sql:
            return _ScalarResult(self.portfolio)
        if "from orders" in sql and "client_order_id" in sql:
            self.sell_poll_count += 1
            if self.sell_poll_count == 1:
                rows = [
                    {
                        "client_order_id": self.expected_sell_client_id,
                        "status": "submitted",
                        "filled_quantity": 0.0,
                        "filled_value": 0.0,
                    }
                ]
            else:
                rows = [
                    {
                        "client_order_id": self.expected_sell_client_id,
                        "status": "filled",
                        "filled_quantity": 100.0,
                        "filled_value": 10000.0,
                    }
                ]
            return _MappingResult(rows)
        raise AssertionError(f"Unexpected SQL in regression script:\n{statement}")


class _FakeSessionCtx:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return False


async def _run() -> None:
    fake_db = _FakeDB()
    submitted_orders: list[dict] = []

    async def fake_dispatch_internal_strategy_order(**kwargs):
        order_data = dict(kwargs.get("order_data") or {})
        submitted_orders.append(order_data)
        return {
            "status": "success",
            "execution": "direct",
            "order_id": f"order-{len(submitted_orders)}",
            "result": {"success": True},
        }

    prepared = mes.PreparedManualExecution(
        task_id="manual_20260423000000_abcd1234",
        tenant_id="default",
        user_id="10001",
        strategy_id="strategy_demo",
        strategy_name="demo_strategy",
        run_id="run_demo",
        model_id="model_demo",
        prediction_trade_date=date(2026, 4, 23),
        trading_mode="SIMULATION",
        request_payload={},
        run={},
        strategy={},
    )

    task = {
        "task_id": "manual_20260423000000_abcd1234",
        "tenant_id": "default",
        "user_id": "10001",
        "strategy_id": "strategy_demo",
        "run_id": "run_demo",
        "trading_mode": "SIMULATION",
        "status": "queued",
        "request_json": {
            "run_id": "run_demo",
            "strategy_id": "strategy_demo",
            "trading_mode": "SIMULATION",
            "execution_plan": {
                "sell_orders": [
                    {
                        "symbol": "600000.SH",
                        "side": "SELL",
                        "trade_action": "SELL_TO_CLOSE",
                        "quantity": 100,
                        "price": 10.0,
                        "reference_price": 10.0,
                        "fusion_score": 0.91,
                        "reason": "regression-sell",
                    }
                ],
                "buy_orders": [
                    {
                        "symbol": "000001.SZ",
                        "side": "BUY",
                        "trade_action": "BUY_TO_OPEN",
                        "quantity": 100,
                        "price": 10.0,
                        "reference_price": 10.0,
                        "estimated_notional": 1000.0,
                        "fusion_score": 0.88,
                        "reason": "regression-buy",
                    }
                ],
                "skipped_items": [],
                "summary": {
                    "available_cash": 0.0,
                    "signal_count": 2,
                    "buy_order_count": 1,
                    "sell_order_count": 1,
                },
            },
        },
    }

    update_task_mock = AsyncMock()
    fake_redis = SimpleNamespace(client=SimpleNamespace(get=lambda _: "heartbeat"))
    fake_dispatch_module = types.ModuleType(
        "backend.services.trade.services.internal_strategy_dispatcher"
    )
    fake_dispatch_module.dispatch_internal_strategy_order = AsyncMock(
        side_effect=fake_dispatch_internal_strategy_order
    )

    with (
        patch.object(
            mes.manual_execution_service,
            "prepare_manual_execution",
            new=AsyncMock(return_value=prepared),
        ),
        patch.object(mes.manual_execution_persistence, "update_task", new=update_task_mock),
        patch.object(mes.manual_execution_log_stream, "append_log", new=lambda **_: None),
        patch.object(mes.manual_execution_log_stream, "update_state", new=lambda **_: None),
        patch("backend.services.trade.services.manual_execution_service.get_session", side_effect=lambda *_, **__: _FakeSessionCtx(fake_db)),
        patch("backend.services.trade.services.manual_execution_service.get_redis", return_value=fake_redis),
        patch("backend.services.trade.services.manual_execution_service.asyncio.sleep", new=AsyncMock(return_value=None)),
        patch.dict(sys.modules, {"backend.services.trade.services.internal_strategy_dispatcher": fake_dispatch_module}),
    ):
        await mes.manual_execution_service.process_task(task)

    assert len(submitted_orders) == 2, f"Expected 2 submits, got {len(submitted_orders)}"
    assert submitted_orders[0]["side"] == "SELL", f"First order is not SELL: {submitted_orders[0]}"
    assert submitted_orders[1]["side"] == "BUY", f"Second order is not BUY: {submitted_orders[1]}"
    assert fake_db.sell_poll_count >= 2, f"Expected >=2 sell status polls, got {fake_db.sell_poll_count}"
    assert int(submitted_orders[1]["quantity"]) == 1000, (
        "Buy quantity should be recalculated to 1000 "
        f"after sell filled 10000, got {submitted_orders[1]['quantity']}"
    )

    completed_calls = [
        call
        for call in update_task_mock.await_args_list
        if call.kwargs.get("status") == "completed"
    ]
    assert completed_calls, "No completed update_task call captured"
    final_result_payload = completed_calls[-1].kwargs.get("result_payload") or {}
    final_summary = final_result_payload.get("preview_summary") or {}
    assert final_summary.get("execution_phase") == "sell_wait_buy", (
        "Expected execution_phase=sell_wait_buy, "
        f"got {final_summary.get('execution_phase')}"
    )
    assert float(final_summary.get("actual_sell_filled_value") or 0.0) == 10000.0, (
        "Expected actual_sell_filled_value=10000.0, "
        f"got {final_summary.get('actual_sell_filled_value')}"
    )

    print("PASS: minimal regression for sell-wait-buy flow")
    print(f"  submit sequence: {[o.get('side') for o in submitted_orders]}")
    print(f"  sell poll count: {fake_db.sell_poll_count}")
    print(f"  recalculated buy quantity: {submitted_orders[1].get('quantity')}")


def main() -> int:
    try:
        asyncio.run(_run())
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
