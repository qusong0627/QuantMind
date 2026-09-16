"""A股模拟撮合器单元测试。

纯逻辑测试，不依赖 Redis / 数据库 / 网络。
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from backend.services.simulation.services.ashare_matcher import (
    MatchConfig,
    compute_fees,
    match_order,
)
from backend.services.simulation.services.local_market_data import DailyBar


def _bar(
    close: float = 10.0,
    pre_close: float = 9.5,
    limit_up: float = 10.45,
    limit_down: float = 8.55,
    is_st: bool = False,
    suspended: bool = False,
    vwap: float = 0.0,
    open_price: float = 9.6,
) -> DailyBar:
    return DailyBar(
        symbol="600036.SH",
        trade_date=date(2026, 7, 30),
        open=open_price,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=1_000_000,
        amount=10_000_000,
        vwap=vwap if vwap > 0 else close,
        pre_close=pre_close,
        limit_up=limit_up,
        limit_down=limit_down,
        is_st=is_st,
        suspended=suspended,
    )


_CFG = MatchConfig()


# ── 涨跌停 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_buy_at_limit_up_rejected():
    bar = _bar(close=10.45, limit_up=10.45)
    r = match_order("buy", 100, bar, _CFG)
    assert not r.success
    assert r.reason == "LIMIT_UP"


@pytest.mark.unit
def test_sell_at_limit_down_rejected():
    bar = _bar(close=8.55, limit_down=8.55)
    r = match_order("sell", 100, bar, _CFG)
    assert not r.success
    assert r.reason == "LIMIT_DOWN"


@pytest.mark.unit
def test_buy_below_limit_up_ok():
    bar = _bar(close=10.44, limit_up=10.45)
    r = match_order("buy", 100, bar, _CFG)
    assert r.success


@pytest.mark.unit
def test_sell_above_limit_down_ok():
    bar = _bar(close=8.56, limit_down=8.55)
    r = match_order("sell", 100, bar, _CFG)
    assert r.success


# ── 停牌 ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_suspended_rejected():
    bar = _bar(suspended=True)
    r = match_order("buy", 100, bar, _CFG)
    assert not r.success
    assert r.reason == "SUSPENDED"


# ── T+1 可卖量 ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_sell_exceeds_available_volume_rejected():
    bar = _bar()
    r = match_order("sell", 500, bar, _CFG, available_volume=300)
    assert not r.success
    assert "INSUFFICIENT_AVAILABLE_VOLUME" in r.reason


@pytest.mark.unit
def test_sell_within_available_volume_ok():
    bar = _bar()
    r = match_order("sell", 200, bar, _CFG, available_volume=300)
    assert r.success
    assert r.fill_quantity == 200


# ── 整手 ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_buy_floored_to_lot():
    bar = _bar()
    r = match_order("buy", 150, bar, _CFG)
    assert r.success
    assert r.fill_quantity == 100


@pytest.mark.unit
def test_buy_below_lot_rejected():
    bar = _bar()
    r = match_order("buy", 50, bar, _CFG)
    assert not r.success
    assert r.reason == "BELOW_LOT_SIZE"


@pytest.mark.unit
def test_star_board_buy_over_200_not_floored():
    """科创板 2026-07-06 新规口径（T-P2-02）：200 股起、1 股递增，350 不截断。"""
    bar = _bar()
    bar.symbol = "688001.SH"
    r = match_order("buy", 350, bar, _CFG)
    assert r.success
    assert r.fill_quantity == 350


@pytest.mark.unit
def test_star_board_buy_below_200_rejected():
    bar = _bar()
    bar.symbol = "SH688001"
    r = match_order("buy", 100, bar, _CFG)
    assert not r.success
    assert r.reason == "BELOW_LOT_SIZE"


@pytest.mark.unit
def test_sell_allows_odd_lot():
    """卖出允许清仓零头（不满一手也可以卖完）。"""
    bar = _bar()
    r = match_order("sell", 50, bar, _CFG, available_volume=50)
    assert r.success
    assert r.fill_quantity == 50


# ── 成交价模式 ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_close_price_mode():
    bar = _bar(close=10.0, vwap=9.8, open_price=9.6)
    cfg = MatchConfig(price_mode="close", slippage_bps=0)
    r = match_order("buy", 100, bar, cfg)
    assert r.success
    assert r.fill_price == 10.0


@pytest.mark.unit
def test_vwap_price_mode():
    bar = _bar(close=10.0, vwap=9.8, open_price=9.6)
    cfg = MatchConfig(price_mode="vwap", slippage_bps=0)
    r = match_order("buy", 100, bar, cfg)
    assert r.success
    assert r.fill_price == 9.8


@pytest.mark.unit
def test_open_price_mode():
    bar = _bar(close=10.0, vwap=9.8, open_price=9.6)
    cfg = MatchConfig(price_mode="open", slippage_bps=0)
    r = match_order("buy", 100, bar, cfg)
    assert r.success
    assert r.fill_price == 9.6


# ── 滑点 ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_buy_slippage_increases_price():
    bar = _bar(close=10.0)
    cfg = MatchConfig(slippage_bps=10)  # 0.1%
    r = match_order("buy", 100, bar, cfg)
    assert r.success
    assert r.fill_price > 10.0
    expected = round(10.0 * 1.001, 4)
    assert abs(r.fill_price - expected) < 1e-6


@pytest.mark.unit
def test_sell_slippage_decreases_price():
    bar = _bar(close=10.0)
    cfg = MatchConfig(slippage_bps=10)
    r = match_order("sell", 100, bar, cfg)
    assert r.success
    assert r.fill_price < 10.0
    expected = round(10.0 * 0.999, 4)
    assert abs(r.fill_price - expected) < 1e-6


# ── 费用 ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_buy_fees_no_stamp_duty():
    commission, stamp_duty, transfer_fee, total = compute_fees(
        100, 10.0, "buy", _CFG,
    )
    assert stamp_duty == 0.0
    assert commission == max(1000 * 0.0003, 5.0)  # 5 元最低
    assert transfer_fee > 0
    assert total == commission + stamp_duty + transfer_fee


@pytest.mark.unit
def test_sell_fees_include_stamp_duty():
    commission, stamp_duty, transfer_fee, total = compute_fees(
        100, 10.0, "sell", _CFG,
    )
    assert stamp_duty == 1000 * 0.0005  # 0.05%
    assert commission == max(1000 * 0.0003, 5.0)
    assert total == commission + stamp_duty + transfer_fee


@pytest.mark.unit
def test_commission_minimum_5_yuan():
    """小额交易佣金最低 5 元。"""
    commission, _, _, _ = compute_fees(10, 1.0, "buy", _CFG)
    assert commission == 5.0  # 10 * 1 * 0.0003 = 0.003 < 5


@pytest.mark.unit
def test_match_result_contains_all_fees():
    bar = _bar(close=10.0)
    r = match_order("sell", 100, bar, MatchConfig(slippage_bps=0))
    assert r.success
    assert r.commission > 0
    assert r.stamp_duty > 0
    assert r.transfer_fee > 0
    assert r.total_fee == r.commission + r.stamp_duty + r.transfer_fee


# ── 涨跌停价格钳制 ──────────────────────────────────────────────────


@pytest.mark.unit
def test_slippage_clamped_to_limit_up():
    """滑点把买入价推过涨停价时，钳制到涨停价。"""
    bar = _bar(close=10.44, limit_up=10.45)
    cfg = MatchConfig(slippage_bps=50)  # 0.5% → 10.44 * 1.005 = 10.4922
    r = match_order("buy", 100, bar, cfg)
    assert r.success
    assert r.fill_price <= 10.45


@pytest.mark.unit
def test_slippage_clamped_to_limit_down():
    bar = _bar(close=8.56, limit_down=8.55)
    cfg = MatchConfig(slippage_bps=50)
    r = match_order("sell", 100, bar, cfg)
    assert r.success
    assert r.fill_price >= 8.55
