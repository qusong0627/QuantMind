"""`enrich_sdl_data._recompute_limit_up` 的涨停判定回归。

旧实现是 `np.where(is_st, 4.8, 9.8)` —— **两条**扁平阈值：
ST 一律 4.8%，其余一律 9.8%（见原注释「创业板/科创板20%也能被9.8捕获」）。

"20% 能被 9.8 捕获"这句话在**向上**方向是对的：9.8 低于任何板块的涨停线，
所以真涨停不会漏。缺陷全在**另一个方向** —— 9.8 也会把一大片**没有涨停**的
普通阳线算成涨停：

| 场景 | 真实涨停线 | 旧阈值 | 结果 |
|---|---|---|---|
| 创业板 +12% | 20% | 9.8 | **误判为涨停** |
| 科创板 +12% | 20% | 9.8 | **误判为涨停** |
| 北交所 +15% | 30% | 9.8 | **误判为涨停** |
| ST 主板 +5%（2026-07-06 起） | 10% | 4.8 | **误判为涨停** |

连续涨停天数因此系统性虚高，下游
`backend/services/engine/ai_strategy/steps/step1_stock_selection.py` 选到的
是「今天涨得多」而不是「今天涨停」。

本文件同时钉住**容差**：`pct_change` 是四舍五入后的百分数，涨跌停价本身还要
按分取整，真正封板的票可能只显示 9.97%。旧实现在 10% 板上留了 0.2pp 余量
（9.8 = 10 − 0.2），收敛口径时**保留该余量**，否则会把真涨停判丢。
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pandas as pd
import pytest

project_root = os.path.join(os.path.dirname(__file__), "../../")
sys.path.append(project_root)

from backend.scripts.enrich_sdl_data import limit_up_flags

# 贴板余量（0.5pp）的来历：旧值 0.2pp 由实测证伪 —— 股价 < ¥2.50 时漏判真涨停。
# 现由 `local_market_data.LIMIT_TOLERANCE` 决定，且**北交所翻倍**（截尾取整）。
# `test_thresholds_track_authority` 直接取权威阈值函数，所以这里不再重述数值：
# 重述出来的数只会跟着板别一起错（0.5pp 对北交所偏宽）。


def _frame(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["symbol", "trade_date", "pct_change", "is_st"])


def _flags(rows: list[tuple]) -> list[bool]:
    return limit_up_flags(_frame(rows)).tolist()


# ───────────────────── 宽板：不再把普通阳线当涨停 ─────────────────────


@pytest.mark.parametrize(
    ("symbol", "pct_change"),
    [
        ("SZ300750", 12.0),  # 创业板
        ("SZ301001", 12.0),
        ("SH688111", 12.0),  # 科创板
        ("BJ430047", 15.0),  # 北交所
        ("430047.BJ", 15.0),
    ],
)
def test_wide_board_gain_below_its_own_limit_is_not_limit_up(symbol, pct_change):
    """12%/15% 在 20%/30% 板上是普通阳线 —— 旧实现把它们全算成涨停。"""
    assert _flags([(symbol, date(2026, 9, 1), pct_change, False)]) == [False]


@pytest.mark.parametrize(
    ("symbol", "pct_change"),
    [
        ("SZ300750", 19.9),
        ("SH688111", 19.9),
        ("BJ430047", 29.9),
        ("SH600000", 9.97),  # 主板，含取整余量的真实封板
    ],
)
def test_stock_at_its_own_limit_is_limit_up(symbol, pct_change):
    """反面控制：修完误判不得把真涨停一起修丢。"""
    assert _flags([(symbol, date(2026, 9, 1), pct_change, False)]) == [True]


# ───────────────────── ST：跟随 2026-07-06 的制度切换 ─────────────────────


def test_st_main_board_five_percent_is_not_limit_up_after_relaxation():
    """2026-07-06 起 ST 主板同为 10%，+5% 不再是涨停 —— 旧实现仍按 4.8 判。"""
    assert _flags([("SH600000", date(2026, 9, 1), 5.0, True)]) == [False]


def test_st_main_board_five_percent_is_limit_up_during_protection_window():
    """保护期内 ST 主板是 5% 板，+4.85% 即涨停。"""
    assert _flags([("SH600000", date(2026, 7, 3), 4.85, True)]) == [True]


def test_st_chinext_keeps_twenty_percent_after_relaxation():
    """ST 不缩创业板：ST 创业板始终是 20% 板。"""
    assert _flags([("SZ300750", date(2026, 9, 1), 12.0, True)]) == [False]


# ───────────────────── 制度日期：创业板注册制改革 ─────────────────────

#: 制度分界线两侧用的是**同一个**涨幅，差异必须全部来自交易日 —— 这样
#: 任何一个日期被吞掉，两条用例里必有一条翻转。
_REFORM_BOUNDARY_PCT = 9.9  # fidelity: allow-limit-threshold — 夹具字面量


def test_pre_reform_chinext_ten_percent_gain_is_limit_up():
    """2020-08-24 前创业板是 10% 板：+9.9% 是涨停。"""
    assert _flags([("SZ300750", date(2020, 8, 21), _REFORM_BOUNDARY_PCT, False)]) == [
        True
    ]


def test_post_reform_chinext_ten_percent_gain_is_not_limit_up():
    """改革后同一涨幅只是普通阳线 —— 日期必须真的参与判定。"""
    assert _flags([("SZ300750", date(2020, 8, 24), _REFORM_BOUNDARY_PCT, False)]) == [
        False
    ]


# ───────────────────── 缺值与形状 ─────────────────────


def test_missing_pct_change_is_not_limit_up():
    """`pct_change` 为空（停牌等）不得算涨停；旧实现用 fillna(-99) 表达同一语义。"""
    assert _flags([("SH600000", date(2026, 9, 1), None, False)]) == [False]


def test_flags_are_row_aligned_with_input():
    rows = [
        ("SH600000", date(2026, 9, 1), 9.97, False),  # 涨停
        ("SZ300750", date(2026, 9, 1), 12.0, False),  # 非涨停
        ("BJ430047", date(2026, 9, 1), 29.9, False),  # 涨停
        ("SH600000", date(2026, 9, 1), 1.0, False),  # 非涨停
    ]
    assert _flags(rows) == [True, False, True, False]


def test_empty_frame_returns_empty():
    assert limit_up_flags(_frame([])).tolist() == []


def test_thresholds_track_authority():
    """判据不是「等于某常量」而是「与权威口径一致」，板规变化时会继续报警。"""
    from backend.services.simulation.services.local_market_data import (
        limit_threshold,
    )

    cases = [
        ("SH600000", date(2026, 9, 1), False),
        ("SZ000001", date(2026, 9, 1), False),
        ("SZ300750", date(2026, 9, 1), False),
        ("SZ300750", date(2020, 8, 21), False),
        ("SH688111", date(2026, 9, 1), False),
        ("BJ430047", date(2026, 9, 1), False),
        ("SH600000", date(2026, 7, 3), True),
        ("SH600000", date(2026, 9, 1), True),
    ]
    for symbol, trade_date, is_st in cases:
        # 期望线取自权威的**阈值**函数（板别幅度 − 该板别容差）；北交所容差翻倍，
        # 自己写「比例×100 − 固定 0.5pp」会少减 0.5pp。
        limit = limit_threshold(symbol, is_st=is_st, trade_date=trade_date) * 100.0
        # 恰好在线上 → 涨停；线下 1pp → 非涨停。两侧都验，避免「恒真」也能过。
        assert _flags([(symbol, trade_date, limit, is_st)]) == [True], symbol
        assert _flags([(symbol, trade_date, limit - 0.01, is_st)]) == [False], symbol


@pytest.mark.parametrize("bad_date", [None, "", "not-a-date"])
def test_unparseable_trade_date_degrades_without_raising(bad_date):
    """坏交易日不得让整批补数崩掉（NaT 与 date 比较会 TypeError）。

    退化为「按当日板规」：宽板 → 20%，故 +12% 不是涨停。
    """
    assert _flags([("SZ300750", bad_date, 12.0, False)]) == [False]
