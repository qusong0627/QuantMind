"""手动/影子模拟下单应走统一 submit_and_fill（ledger + sim_orders/sim_trades）。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from backend.services.live_trading.services.internal_strategy_dispatcher import (
    _positive_or_none,
    dispatch_internal_strategy_order,
)
from backend.services.simulation.services.order_submission_service import (
    SimulationSubmissionOutcome,
)


class _FakeResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RiskPassed:
    """风控桩（T-RC-02 之后必需）。

    本文件的被测对象是**派发是否走统一 submit_and_fill**，不是风控。风控 fail-closed，
    而这里传的是 `MagicMock()` —— 读不出 `qm:risk:config.rules` 即拒单，于是闸门合入后
    本文件变红，报错还只显示 `status='failed'`、看不出是风控拒的。风控自身的覆盖在
    `test_risk_gate_wiring.py`。
    """

    passed = True
    rule_id = None
    reason = ""


def test_simulation_dispatch_uses_submit_and_fill_not_hand_insert():
    async def _run():
        db = MagicMock()
        db.execute = AsyncMock(return_value=_FakeResult(None))
        redis = MagicMock()
        outcome = SimulationSubmissionOutcome(
            success=True,
            order_id="ord-1",
            fill_price=10.5,
            filled_quantity=100,
            commission=1.2,
            price_source="quote",
            message="filled",
        )
        submit = AsyncMock(return_value=outcome)
        submission = MagicMock()
        submission.submit_and_fill = submit

        with (
            patch(
                "backend.services.simulation.services.order_submission_service."
                "SimulationOrderSubmissionService",
                return_value=submission,
            ),
            patch(
                "backend.services.trade.services.risk_gate_service.check_order",
                new=AsyncMock(return_value=_RiskPassed()),
            ),
            patch(
                "backend.services.live_trading.services.internal_strategy_dispatcher."
                "mirror_virtual_fill",
                new_callable=AsyncMock,
            ) as mirror,
        ):
            result = await dispatch_internal_strategy_order(
                order_data={
                    "trading_mode": "SIMULATION",
                    "symbol": "SH600036",
                    "side": "BUY",
                    "quantity": 100,
                    "price": 10.5,
                    "client_order_id": "manual-abc",
                    "remarks": "manual task",
                },
                user_id="1",
                tenant_id="default",
                redis=redis,
                db=db,
            )

        assert result["status"] == "success"
        assert result["execution"] == "virtual"
        assert result["order_id"] == "ord-1"
        assert result["result"]["success"] is True
        submit.assert_awaited_once()
        kwargs = submit.await_args.kwargs
        assert kwargs["order_type"] == "limit"
        assert kwargs["price"] == 10.5
        assert kwargs["client_order_id"] == "manual-abc"
        assert kwargs["trigger_source"] == "manual"
        assert str(kwargs["remarks"]).startswith("client_order_id=manual-abc")
        mirror.assert_awaited_once()

    asyncio.run(_run())


def test_simulation_dispatch_skips_duplicate_remark():
    async def _run():
        db = MagicMock()
        db.execute = AsyncMock(return_value=_FakeResult("existing-order"))
        redis = MagicMock()
        with patch(
            "backend.services.simulation.services.order_submission_service."
            "SimulationOrderSubmissionService",
        ) as ctor:
            result = await dispatch_internal_strategy_order(
                order_data={
                    "trading_mode": "SIMULATION",
                    "symbol": "SH600036",
                    "side": "BUY",
                    "quantity": 100,
                    "price": 10.5,
                    "client_order_id": "manual-dup",
                },
                user_id="1",
                tenant_id="default",
                redis=redis,
                db=db,
            )
        assert result["execution"] == "duplicate_skipped"
        ctor.assert_not_called()

    asyncio.run(_run())


# ── P1.6 TCA 基准价的入参守卫 ────────────────────────────────────────
def test_positive_or_none_drops_dirty_prices_instead_of_raising():
    """``ref_price`` 是观测字段：脏值只能**丢**，不能让 ``OrderCreate`` 的 ``gt=0``
    把整笔真单变成校验错。

    （与 ``_normalize_strategy_id`` 同一条纪律：镜像单曾因 ``strategy_id=0`` 触发
    ``gt=0`` 被 422 打回，压测里表现为"镜像单全部失败"。）
    """
    assert _positive_or_none(10.5) == 10.5
    assert _positive_or_none("10.5") == 10.5
    assert _positive_or_none(1) == 1.0
    for dirty in (None, "", 0, 0.0, -1, "abc", float("nan"), float("inf"),
                  float("-inf"), True, False, [], {}):
        assert _positive_or_none(dirty) is None, dirty
