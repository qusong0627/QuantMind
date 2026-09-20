"""``factor_deep_dive.limit_threshold`` 收敛到权威口径的回归钉。

原实现是一张写死的前缀表（0.098/0.198/0.298），与
``local_market_data.limit_pct`` 有三处可证伪的偏差：

1. ``302`` 段落在创业板 20% 板内（``_GROWTH_PREFIXES`` 含 "302"），却被按主板算；
2. 北交所前缀 ``("4", "8")`` 比 ``_BSE_PREFIXES`` 宽，把 400xxx 老三板也当 30%；
3. 不知道创业板 2020-08-24 的 10%→20% 改革，回看更早窗口会把真涨停判丢。

期望值 = 板别带宽 − 0.2pp 贴板缓冲（沿用旧表的内置余量）。
"""
from __future__ import annotations

import pytest

from backend.scripts.factor_deep_dive import limit_threshold

# 期望值：板别带宽减贴板缓冲 —— 钉住既有口径
_MAIN = 0.098  # fidelity: allow-limit-threshold — 期望值，钉住既有口径
_WIDE = 0.198  # fidelity: allow-limit-threshold — 期望值，钉住既有口径
_BSE = 0.298  # fidelity: allow-limit-threshold — 期望值，钉住既有口径

_DAY = "20260918"


def test_main_board_is_ten_percent():
    assert limit_threshold("600036", _DAY) == pytest.approx(_MAIN)


def test_growth_board_is_twenty_percent():
    assert limit_threshold("300750", _DAY) == pytest.approx(_WIDE)


def test_segment_302_is_growth_board_not_main_board():
    """302 段属创业板 20% 板 —— 旧前缀表漏了它，会当主板判。"""
    assert limit_threshold("302132", _DAY) == pytest.approx(_WIDE)


def test_star_market_is_twenty_percent():
    assert limit_threshold("688111", _DAY) == pytest.approx(_WIDE)


def test_bse_is_thirty_percent():
    assert limit_threshold("830799", _DAY) == pytest.approx(_BSE)


def test_old_third_board_400_is_not_bse():
    """400xxx 老三板不在 ``_BSE_PREFIXES`` 内 —— 旧表按 "4" 前缀误判为 30%。"""
    assert limit_threshold("400001", _DAY) == pytest.approx(_MAIN)


def test_chinext_before_reform_is_ten_percent():
    """2020-08-24 之前创业板仍是 10% 板，旧表恒返 20%。"""
    assert limit_threshold("300750", "20200821") == pytest.approx(_MAIN)


def test_chinext_on_reform_day_is_twenty_percent():
    assert limit_threshold("300750", "20200824") == pytest.approx(_WIDE)


def test_accepts_partition_tag_and_iso_date_alike():
    """分区标签 20260918 与 ISO 2026-09-18 必须同解。"""
    assert limit_threshold("300750", "20260918") == pytest.approx(
        limit_threshold("300750", "2026-09-18")
    )


def test_date_defaults_to_today_when_omitted():
    """省略日期不报错（旧签名兼容），落到今日口径。"""
    from datetime import date

    assert limit_threshold("600036") == pytest.approx(
        limit_threshold("600036", date.today().isoformat())
    )


def test_symbol_form_does_not_change_the_band():
    """前缀式 / 后缀式 / 纯数字三形态同解。"""
    forms = ["300750", "SZ300750", "300750.SZ"]
    values = {limit_threshold(f, _DAY) for f in forms}
    assert len(values) == 1
    assert values.pop() == pytest.approx(_WIDE)


def test_no_bare_prefix_table_remains():
    """结构守卫：函数体里不得再出现写死的板别分支。"""
    import inspect

    src = inspect.getsource(limit_threshold)
    assert "startswith" not in src
    assert "limit_pct" in src
