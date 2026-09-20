import os
import sys
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

project_root = os.path.join(os.path.dirname(__file__), "../../")
sys.path.append(project_root)

from backend.services.engine.qlib_app.utils.cn_exchange import CnExchange
from qlib.backtest.decision import OrderDir


class DummyQuote:
    def __init__(self, mapping):
        self.mapping = mapping

    def get_data(self, stock_id, start_time, end_time, field, method="ts_data_last"):
        key = (stock_id, pd.Timestamp(start_time).strftime("%Y-%m-%d"), field)
        return self.mapping.get(key)

    def get_all_stock(self):
        return {"SH600000"}


def make_exchange(mapping):
    exchange = CnExchange.__new__(CnExchange)
    exchange.quote = DummyQuote(mapping)
    exchange.buy_price = "$close"
    exchange.sell_price = "$close"
    exchange.quote_fallback_lookback_days = 3
    exchange.backtest_id = "test"
    exchange.has_price_limits = True
    return exchange


def test_get_close_falls_back_to_previous_valid_quote():
    exchange = make_exchange(
        {
            ("SH600000", "2026-03-25", "$close"): 0.0,
            ("SH600000", "2026-03-24", "$close"): 12.34,
        }
    )

    price = exchange.get_close(
        "SH600000", pd.Timestamp("2026-03-25"), pd.Timestamp("2026-03-25")
    )

    assert price == pytest.approx(12.34)


def test_get_factor_falls_back_to_previous_valid_quote():
    exchange = make_exchange(
        {
            ("SH600000", "2026-03-25", "$factor"): 0.0,
            ("SH600000", "2026-03-24", "$factor"): 0.256,
        }
    )

    factor = exchange.get_factor(
        "SH600000", pd.Timestamp("2026-03-25"), pd.Timestamp("2026-03-25")
    )

    assert factor == pytest.approx(0.256)


def test_get_deal_price_falls_back_to_recent_valid_close():
    exchange = make_exchange(
        {
            ("SH600000", "2026-03-25", "$close"): 0.0,
            ("SH600000", "2026-03-24", "$close"): 8.88,
        }
    )

    price = exchange.get_deal_price(
        "SH600000",
        pd.Timestamp("2026-03-25"),
        pd.Timestamp("2026-03-25"),
        OrderDir.BUY,
    )

    assert price == pytest.approx(8.88)


# ---- 涨跌停阈值：口径必须收敛到 local_market_data.limit_pct（唯一权威实现） ----
#
# 这组用例的判据刻意**不是**「某常量等于某数」，而是「阈值恒等于权威口径减取整
# 容差」。写死期望值只能钉住今天的分支；钉住「与权威一致」，才能在板规变化
# （新板块、ST 过渡期）时继续报警。
#
# 历史缺陷（本组用例即为其回归护栏）：旧实现按代码前缀直接返回
# 0.095/0.195/0.295，对 2020-08-24 注册制改革**之前**的创业板一律给 20%，
# 于是 +9.99% 的真涨停被判成「可交易」——回测里表现为在涨停价上成交，
# 正是「漂亮数据」而非真实市场。

_AUTHORITY_TOLERANCE = 0.005


def _authority_threshold(symbol: str, trade_date, *, is_st: bool = False) -> float:
    """权威口径（比例）减去 $change 的取整容差，即 cn_exchange 应当返回的值。"""
    from backend.services.simulation.services.local_market_data import limit_pct

    return (
        float(limit_pct(symbol, is_st=is_st, trade_date=trade_date))
        - _AUTHORITY_TOLERANCE
    )


@pytest.mark.parametrize(
    ("symbol", "trade_date"),
    [
        ("SH600000", date(2026, 9, 1)),  # 主板
        ("SZ000001", date(2026, 9, 1)),  # 主板
        ("SZ300750", date(2026, 9, 1)),  # 创业板（注册制后 20%）
        ("SZ300750", date(2020, 8, 21)),  # 创业板（注册制前 10%）—— 旧实现错在这里
        ("SZ301001", date(2020, 8, 21)),  # 301 前缀同规则
        ("SH688111", date(2026, 9, 1)),  # 科创板
        ("BJ430047", date(2026, 9, 1)),  # 北交所（前缀式）
        ("430047.BJ", date(2026, 9, 1)),  # 北交所（后缀式）
        ("600000", date(2026, 9, 1)),  # 无市场前缀
    ],
)
def test_limit_threshold_tracks_authority(symbol, trade_date):
    assert CnExchange._get_limit_threshold(symbol, trade_date) == pytest.approx(
        _authority_threshold(symbol, trade_date)
    )


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("SH600000", 0.095),  # fidelity: allow-limit-threshold — 断言值
        ("SZ300750", 0.195),  # fidelity: allow-limit-threshold — 断言值
        ("SH688111", 0.195),  # fidelity: allow-limit-threshold — 断言值
        ("BJ430047", 0.295),  # fidelity: allow-limit-threshold — 断言值
    ],
)
def test_limit_threshold_preserves_legacy_values_for_current_dates(symbol, expected):
    """四个原有档位在「今天」必须逐位不变：收敛口径不得顺手改掉已正确的判定。"""
    assert CnExchange._get_limit_threshold(symbol, date(2026, 9, 1)) == pytest.approx(
        expected
    )


def test_limit_threshold_honours_st_flag_during_st_protection_window():
    """ST 主板 5% 保护期（<2026-07-06）内，is_st=True 必须落到 5% 档。"""
    assert CnExchange._get_limit_threshold(
        "SH600000", date(2026, 7, 3), is_st=True
    ) == pytest.approx(0.045)


def test_limit_threshold_ignores_st_flag_after_relaxation():
    """2026-07-06 起 ST 主板同为 10%，is_st 不再改变阈值。"""
    assert CnExchange._get_limit_threshold(
        "SH600000", date(2026, 7, 6), is_st=True
    ) == pytest.approx(0.095)  # fidelity: allow-limit-threshold — 断言值


def test_check_stock_limit_blocks_pre_reform_chinext_at_ten_percent():
    """2020-08-24 前创业板是 10% 板：+9.99% 即涨停，必须拦。"""
    exchange = make_exchange({("SZ300750", "2020-08-21", "$change"): 0.0999})

    blocked = exchange.check_stock_limit(
        "SZ300750", pd.Timestamp("2020-08-21"), pd.Timestamp("2020-08-22")
    )

    assert blocked is True


def test_check_stock_limit_allows_pre_reform_chinext_below_ten_percent():
    """同一日期同一票，+5% 不是涨停 —— 反面控制，防止「一律拦」也能过。"""
    exchange = make_exchange({("SZ300750", "2020-08-21", "$change"): 0.05})

    blocked = exchange.check_stock_limit(
        "SZ300750", pd.Timestamp("2020-08-21"), pd.Timestamp("2020-08-22")
    )

    assert blocked is False


def test_check_stock_limit_reads_signal_date_not_current_regime():
    """同一个涨幅跨 2020-08-24 两侧结论必须相反 —— 日期必须真的被透传。"""
    change = 0.0999
    before = make_exchange({("SZ300750", "2020-08-21", "$change"): change})
    after = make_exchange({("SZ300750", "2020-08-24", "$change"): change})

    blocked_before = before.check_stock_limit(
        "SZ300750", pd.Timestamp("2020-08-21"), pd.Timestamp("2020-08-22")
    )
    blocked_after = after.check_stock_limit(
        "SZ300750", pd.Timestamp("2020-08-24"), pd.Timestamp("2020-08-25")
    )

    assert (blocked_before, blocked_after) == (True, False)


def test_check_stock_limit_buy_direction_ignores_limit_down():
    exchange = make_exchange({("SH600000", "2026-09-01", "$change"): -0.0999})

    blocked = exchange.check_stock_limit(
        "SH600000",
        pd.Timestamp("2026-09-01"),
        pd.Timestamp("2026-09-02"),
        direction=OrderDir.BUY,
    )

    assert blocked is False
