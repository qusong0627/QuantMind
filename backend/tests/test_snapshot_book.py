"""快照级撮合内核金样（T-P6-17，F2）：穿档/部分/涨跌停/单位换算/边界（≥10 例）。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _snap(**kw):
    """默认盘口：买 10.00/9.99/9.98（各 100 手）、卖 10.01/10.02/10.03（各 100 手），涨跌停 ±10%。"""
    base = {
        "Now": 10.00, "PreClose": 10.00, "LimitUp": 11.00, "LimitDown": 9.00,
        "timestamp": 1789600000,
        "bid1": 10.00, "bid2": 9.99, "bid3": 9.98, "bid4": 9.97, "bid5": 9.96,
        "bid_vol1": 100, "bid_vol2": 100, "bid_vol3": 100, "bid_vol4": 100, "bid_vol5": 100,
        "ask1": 10.01, "ask2": 10.02, "ask3": 10.03, "ask4": 10.04, "ask5": 10.05,
        "ask_vol1": 100, "ask_vol2": 100, "ask_vol3": 100, "ask_vol4": 100, "ask_vol5": 100,
    }
    base.update(kw)
    return base


def _book(**kw):
    from backend.services.simulation.services.snapshot_book import book_from_snapshot

    b = book_from_snapshot(_snap(**kw), symbol="600036.SH")
    assert b is not None
    return b


def test_unit_multiplier_shou_to_shares():
    """TDX 帧量纲=手 → Book 统一为股（×100）。"""
    book = _book()
    assert book.asks[0].volume == 10_000.0  # 100 手 = 1 万股
    assert book.completeness == 10


def test_single_level_fill():
    from backend.services.simulation.services.snapshot_book import walk_book

    fill = walk_book("buy", 200, _book())
    assert fill is not None
    assert fill.fill_price == pytest.approx(10.01)
    assert fill.fill_qty == 200 and fill.unfilled == 0 and fill.levels_consumed == 1


def test_multi_level_fill_weighted_price():
    from backend.services.simulation.services.snapshot_book import walk_book

    # 吃穿两档：1 万股 @10.01 + 1 万股 @10.02
    fill = walk_book("buy", 20_000, _book())
    assert fill is not None
    assert fill.levels_consumed == 2
    # 量加权 10.015 → 交易所四舍五入到分 = 10.02
    assert fill.fill_price == pytest.approx(10.02)


def test_partial_fill_leaves_remainder():
    from backend.services.simulation.services.snapshot_book import walk_book

    # 全部五档 = 5 万股；求 8 万股 → 部分成交 5 万，余 3 万
    fill = walk_book("buy", 80_000, _book())
    assert fill is not None
    assert fill.fill_qty == 50_000 and fill.unfilled == 30_000
    assert fill.levels_consumed == 5


def test_lot_rounding_down():
    from backend.services.simulation.services.snapshot_book import walk_book

    fill = walk_book("buy", 150, _book())  # 1.5 手 → 1 手
    assert fill is not None and fill.fill_qty == 100 and fill.unfilled == 50


def test_limit_buy_does_not_cross_price():
    from backend.services.simulation.services.snapshot_book import walk_book

    # 限价 10.015：只能吃 10.01 档（10.02 > 委托价不吃）
    fill = walk_book("buy", 30_000, _book(), order_type="limit", limit_price=10.01)
    assert fill is not None
    assert fill.fill_qty == 10_000 and fill.levels_consumed == 1
    assert fill.unfilled == 20_000


def test_limit_sell_does_not_cross_price():
    from backend.services.simulation.services.snapshot_book import walk_book

    fill = walk_book("sell", 30_000, _book(), order_type="limit", limit_price=10.00)
    assert fill is not None
    assert fill.fill_qty == 10_000  # 只吃 10.00 档（9.99 < 委托价不吃）
    assert fill.fill_price == pytest.approx(10.00)
    assert fill.unfilled == 20_000


def test_sell_walks_bids_downhill():
    from backend.services.simulation.services.snapshot_book import walk_book

    fill = walk_book("sell", 20_000, _book())
    assert fill is not None and fill.levels_consumed == 2
    assert fill.fill_price == pytest.approx(10.00)  # 9.995 → 四舍五入到分


def test_limit_up_queue_no_fill():
    from backend.services.simulation.services.snapshot_book import walk_book

    # 涨停封板：卖一无量且贴涨停价（其余卖档不存在）→ 买单排队
    book = _book(Now=11.00, ask1=11.00, ask_vol1=0, ask2=0, ask3=0, ask4=0, ask5=0)
    assert walk_book("buy", 10_000, book) is None


def test_limit_down_queue_no_fill():
    from backend.services.simulation.services.snapshot_book import walk_book

    book = _book(Now=9.00, bid1=9.00, bid_vol1=0, bid2=0, bid3=0, bid4=0, bid5=0)
    assert walk_book("sell", 10_000, book) is None


def test_no_counterparty_returns_none():
    from backend.services.simulation.services.snapshot_book import walk_book

    book = _book(ask1=0, ask2=0, ask3=0, ask4=0, ask5=0)
    assert walk_book("buy", 100, book) is None


def test_bands_clamp_fill_price():
    from backend.services.simulation.services.snapshot_book import walk_book

    # 全部卖档异常价 ≥ 涨停 → 每档钳制到涨停价（11.00）
    book = _book(ask1=12.00, ask2=12.01, ask3=12.02, ask4=12.03, ask5=12.04,
                 ask_vol1=100, ask_vol2=100, ask_vol3=100, ask_vol4=100, ask_vol5=100)
    fill = walk_book("buy", 10_000, book)
    assert fill is not None
    assert fill.fill_price == pytest.approx(11.00)
    assert "clamp_limit_up" in fill.notes


def test_incomplete_book_rejected():
    from backend.services.simulation.services.snapshot_book import book_from_snapshot

    # 全部有效档只有 1 个（< MIN_VALID_LEVELS）→ 不算可用盘口（宁可回退日频）
    snap = _snap(bid2=0, bid3=0, bid4=0, bid5=0,
                 ask1=0, ask2=0, ask3=0, ask4=0, ask5=0)
    assert book_from_snapshot(snap) is None


def test_freshness_gate():
    from backend.services.simulation.services.snapshot_book import is_fresh

    book = _book(timestamp=1789600000)
    assert is_fresh(book, now=1789600000 + 30) is True
    assert is_fresh(book, now=1789600000 + 400) is False
    # ts 缺失 → 不可判定 → 不新鲜（不猜）
    from backend.services.simulation.services.snapshot_book import book_from_snapshot

    b2 = book_from_snapshot(_snap(timestamp=None), symbol="600036.SH")
    assert b2 is not None and is_fresh(b2, now=1789600000) is False


def test_zero_quantity_and_bad_side():
    from backend.services.simulation.services.snapshot_book import walk_book

    assert walk_book("buy", 0, _book()) is None
    assert walk_book("hold", 100, _book()) is None
