"""涨停/跌停家数（`limit_up_down_counts`）测试。

市场分析页显示的「涨停 N 家 / 跌停 N 家」来自这里。旧实现是两条写死的
`pct >= 9.8` / `pct <= -9.8`，两个方向同时错：

- **虚高**：20%（创业板/科创板）与 30%（北交所）板上，任何 +9.8% 以上的
  普通阳线都被计成涨停 —— 一根 +12% 的宽板阳线根本没封板；
- **漏计**：ST 主板 5% 板（2026-07-06 之前）上真封死的 +5.0% 永远进不了数。

哪个方向占上风取决于当日哪个板块活跃，所以这个数字既不能当上界也不能当下界。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from backend.shared.market_breadth import (
    CAT_LIMIT_DOWN,
    CAT_LIMIT_UP,
    classify_by_pct,
    limit_up_down_counts,
)

_D = date(2024, 1, 2)
#: ST 保护期内的日期（ST 主板 5% 板）
_D_ST_ERA = date(2026, 7, 3)


def _counts(pcts, syms, **kw):
    return limit_up_down_counts(
        pd.Series(pcts, dtype=float), pd.Series(syms), trade_date=kw.pop("d", _D), **kw
    )


def test_main_board_ten_percent_board():
    # Arrange：主板票 +9.9%（容差 0.5pp → 阈值 9.5）算涨停，+9.0% 不算
    up, down = _counts([9.9, 9.0, -9.9, -9.0], ["600000.SH"] * 4)  # fidelity: allow-limit-threshold — 用例入参：涨幅百分比（阈值由被测函数内部取权威口径）

    assert (up, down) == (1, 1)


def test_chinext_twelve_percent_is_not_limit_up():
    """核心反向控制：创业板 20% 板上 +12% 不是涨停 —— 旧实现把它计进去。"""
    up, down = _counts([12.0], ["300750.SZ"])

    assert (up, down) == (0, 0)
    # 同一数值放在主板上必须算涨停，否则上面那条恒真
    assert _counts([12.0], ["600000.SH"])[0] == 1


def test_chinext_twenty_percent_is_limit_up():
    assert _counts([19.6], ["300750.SZ"])[0] == 1
    assert _counts([19.2], ["300750.SZ"])[0] == 0


def test_bse_uses_one_percent_tolerance():
    """北交所容差 1pp（截尾到分最多压低 1%），阈值 29.0。"""
    assert _counts([29.2], ["430047.BJ"])[0] == 1
    assert _counts([28.5], ["430047.BJ"])[0] == 0


def test_st_main_board_five_percent_is_counted():
    """ST 保护期内 ST 主板 +5.0% 是封板 —— 旧实现（9.8 线）永远漏计。"""
    up, _ = _counts([5.0], ["600000.SH"], d=_D_ST_ERA, st_symbols={"600000.SH"})

    assert up == 1
    # 同一价非 ST 主板只是普通阳线，否则上面那条恒真
    assert _counts([5.0], ["600000.SH"], d=_D_ST_ERA)[0] == 0


def test_st_relaxation_date_flips_the_count():
    """2026-07-06 起 ST 主板同为 10% —— 同一涨幅、同一 ST 标记，两侧结论必须相反。"""
    before = _counts([5.0], ["600000.SH"], d=date(2026, 7, 3), st_symbols={"600000.SH"})
    after = _counts([5.0], ["600000.SH"], d=date(2026, 7, 6), st_symbols={"600000.SH"})

    assert before[0] == 1
    assert after[0] == 0


def test_chinext_reform_boundary():
    """2020-08-24 前创业板是 10% 板 —— 同一代码同一涨幅，两侧结论相反。"""
    assert _counts([12.0], ["300750.SZ"], d=date(2020, 8, 21))[0] == 1
    assert _counts([12.0], ["300750.SZ"], d=date(2020, 8, 24))[0] == 0


def test_nan_and_zero_are_not_counted():
    up, down = _counts([float("nan"), 0.0, -0.0], ["600000.SH"] * 3)

    assert (up, down) == (0, 0)


def test_agrees_with_classify_by_pct_row_by_row():
    """两条入口必须同口径 —— classify_by_pct 是逐行版，本函数是整列版。

    两者分叉就会让「复盘」与「市场分析页」对同一天给出不同的涨停家数。
    """
    pcts = [9.9, 12.0, -9.9, 5.0, 19.6, -19.6, 0.0]  # fidelity: allow-limit-threshold — 用例入参：涨幅百分比（阈值由被测函数内部取权威口径）
    syms = ["600000.SH", "300750.SZ", "600000.SH", "600000.SH",
            "300750.SZ", "300750.SZ", "000001.SZ"]

    for p, s in zip(pcts, syms, strict=True):
        cat = classify_by_pct(p, s, False, _D)
        up, down = _counts([p], [s])
        assert up == (1 if cat == CAT_LIMIT_UP else 0), (p, s)
        assert down == (1 if cat == CAT_LIMIT_DOWN else 0), (p, s)


def test_counts_are_index_independent():
    """入参是两条 Series，位置对齐即可 —— 不得依赖各自的 index。

    调用点传的是 `snap["symbol"]` 与 `pct`，两者 index 未必一致（一个来自
    merge、一个来自 fillna）。
    """
    pct = pd.Series([9.9, 12.0], index=[10, 20], dtype=float)  # fidelity: allow-limit-threshold — 用例入参：涨幅百分比（阈值由被测函数内部取权威口径）
    sym = pd.Series(["600000.SH", "300750.SZ"], index=[7, 8])

    assert limit_up_down_counts(pct, sym, trade_date=_D) == (1, 0)


@pytest.mark.parametrize("field", ["limit_up", "limit_down"])
def test_real_symbol_format_is_suffix(field):
    """符号按后缀式（000001.SZ）传入 —— 这是量化层的统一口径。"""
    sym = "000001.SZ"
    pct = 9.9 if field == "limit_up" else -9.9  # fidelity: allow-limit-threshold — 用例入参：涨幅百分比（阈值由被测函数内部取权威口径）

    assert _counts([pct], [sym]) == ((1, 0) if field == "limit_up" else (0, 1))
