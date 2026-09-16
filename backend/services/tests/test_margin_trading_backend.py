import json
from pathlib import Path

import pandas as pd
import pytest

from backend.services.engine.services.pipeline_service import PipelineRunRequest, PipelineService
from backend.services.trade_shared.models.enums import OrderSide, OrderType, PositionSide, TradeAction
from backend.services.trade_shared.schemas.order import OrderCreate
from backend.services.trade_shared.services.order_service import OrderService
from backend.services.trade_shared.simulation_manager import SimulationAccountManager
from backend.shared.margin_stock_pool import MarginStockPoolService, normalize_symbol


class _FakeRedisClient:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value

    def eval(self, *args, **kwargs):
        raise AssertionError("margin branch should not call lua eval")


class _FakeRedis:
    def __init__(self):
        self.client = _FakeRedisClient()


def test_margin_stock_pool_service_loads_and_normalizes_symbols(tmp_path: Path):
    path = tmp_path / "margin.xlsx"
    df = pd.DataFrame({"股票代码": ["600000", "000001", "300750"], "股票简称": ["浦发银行", "平安银行", "宁德时代"]})
    df.to_excel(path, index=False)

    svc = MarginStockPoolService(path)
    snapshot = svc.refresh()

    assert snapshot.record_count == 3
    assert svc.is_margin_eligible("SH600000")
    assert svc.is_margin_eligible("000001")
    assert normalize_symbol("300750") == "SZ300750"


def test_order_service_resolves_short_trade_action():
    order_data = OrderCreate(
        portfolio_id=1,
        symbol="SH600000",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=10.0,
        position_side=PositionSide.SHORT,
        is_margin_trade=True,
    )

    action = OrderService._resolve_trade_action(order_data)
    assert action == TradeAction.SELL_TO_OPEN


def test_pipeline_service_marks_negative_scores_as_short_margin_signals():
    service = PipelineService()
    request = PipelineRunRequest(
        prompt="test",
        enable_short_selling=True,
        inference_data=[{"instrument": "SH600000", "close": 10.5}],
    )

    signals, source = service._build_signal_events(
        request=request,
        run_id="run-1",
        fusion_report={
            "selected_instruments": ["SH600000", "SZ000001"],
            "selected_scores": [-0.8, 0.6],
        },
        inference_result=None,
    )

    assert source == "fusion_report"
    assert signals[0]["trade_action"] == "sell_to_open"
    assert signals[0]["position_side"] == "short"
    assert signals[0]["is_margin_trade"] is True
    assert signals[1]["trade_action"] == "buy_to_open"
    assert signals[1]["position_side"] == "long"
    assert signals[1]["is_margin_trade"] is False


@pytest.mark.asyncio
async def test_simulation_manager_supports_margin_short_open_and_close():
    redis = _FakeRedis()
    manager = SimulationAccountManager(redis)

    await manager.init_account(user_id=42, initial_cash=1_000_000, tenant_id="default")
    open_res = await manager.update_balance(
        user_id=42,
        symbol="SH600000",
        delta_cash=0,
        delta_volume=-100,
        price=10.0,
        tenant_id="default",
        trade_action="sell_to_open",
        position_side="short",
        is_margin_trade=True,
    )
    assert open_res["success"] is True
    raw = redis.client.get("simulation:account:default:42")
    account = json.loads(raw)
    assert account["liabilities"] > 0
    assert any("::short" in key for key in account["positions"].keys())

    close_res = await manager.update_balance(
        user_id=42,
        symbol="SH600000",
        delta_cash=0,
        delta_volume=100,
        price=9.0,
        tenant_id="default",
        trade_action="buy_to_close",
        position_side="short",
        is_margin_trade=True,
    )
    assert close_res["success"] is True
