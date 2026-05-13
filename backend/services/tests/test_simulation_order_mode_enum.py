from backend.services.trade.simulation.models.order import TradingMode
from backend.services.trade.simulation.schemas.order import SimOrderCreate


def test_simulation_order_accepts_lowercase_trading_mode():
    payload = SimOrderCreate(
        symbol="SH600000",
        side="buy",
        order_type="market",
        quantity=100,
        trading_mode="simulation",
    )

    assert payload.trading_mode == TradingMode.SIMULATION
    assert payload.trading_mode.value == "SIMULATION"
