import pytest

from backend.shared.backtest_engine.core.portfolio import Portfolio
from backend.shared.simulation_margin_math import maintenance_margin_ratio


@pytest.mark.parametrize(
    "exit_price,expected", [(8, 100200), (10, 100000), (12, 99800)]
)
def test_short_mark_and_close_preserve_equity(exit_price, expected):
    p = Portfolio(100000)
    p.short_sell("600000.SH", 100, 10)
    assert p.total_value == 100000
    p.positions["600000.SH"].update_market_value(exit_price)
    p._update_total_value()
    assert p.total_value == expected
    p.buy_to_cover("600000.SH", 100, exit_price)
    assert p.total_value == p.cash == expected
    assert p.short_proceeds == 0


def test_partial_short_cover_releases_only_entry_proceeds():
    p = Portfolio(100000)
    p.short_sell("600000.SH", 100, 10)
    p.buy_to_cover("600000.SH", 40, 8)
    assert p.cash == 100080
    assert p.short_proceeds == 600
    assert p.total_value == 100200


def test_open_short_rejects_long_without_mutating_cash():
    p = Portfolio(100000)
    p.buy("600000.SH", 100, 10)
    with pytest.raises(ValueError):
        p.short_sell("600000.SH", 100, 10, commission=5)
    assert p.cash == 99000
    assert p.short_proceeds == 0


def test_margin_ratio_tracks_short_market_value():
    assert maintenance_margin_ratio(1200, 2000, 1000) == 0.6
    assert maintenance_margin_ratio(1200, 500, 1000) == 2.4
    assert maintenance_margin_ratio(1200, 0, 1000) == 1.2


def test_margin_ratio_counts_financing_debt():
    # 融资负债（负现金）必须计入分母，否则混合账户比例虚高
    assert maintenance_margin_ratio(1200, 2000, 1000, -500) == pytest.approx(0.48)
    # 无融资负债时不受影响
    assert maintenance_margin_ratio(1200, 2000, 1000, 800) == 0.6
    # 无空头时回退本金负债，融资负债同样叠加
    assert maintenance_margin_ratio(1200, 0, 1000, -200) == 1.0


@pytest.mark.parametrize("exit_price,expected", [(8, 100200), (12, 99800)])
def test_qlib_short_close_uses_entry_price_after_remark(exit_price, expected):
    pytest.importorskip("qlib")
    from backend.services.engine.qlib_app.utils.margin_position import MarginPosition

    p = MarginPosition(cash=100000)
    p._sell_stock("sh600000", 1000, 0, 10)
    p.update_stock_price("sh600000", exit_price)
    assert p.calculate_value() == expected
    p._buy_stock("sh600000", 100 * exit_price, 0, exit_price)
    assert p.position["cash"] == expected
    assert p.position["short_proceeds"] == 0


def test_qlib_increased_short_uses_original_cost_after_remark():
    pytest.importorskip("qlib")
    from backend.services.engine.qlib_app.utils.margin_position import MarginPosition

    p = MarginPosition(cash=100000)
    p._sell_stock("sh600000", 1000, 0, 10)
    p.update_stock_price("sh600000", 8)
    p._sell_stock("sh600000", 800, 0, 8)
    assert p.position["sh600000"]["short_entry_price"] == 9
    p.update_stock_price("sh600000", 7)
    p._buy_stock("sh600000", 700, 0, 7)
    assert p.position["cash"] == 100200
    assert p.position["short_proceeds"] == 900
    assert p.calculate_value() == 100400
