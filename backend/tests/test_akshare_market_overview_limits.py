"""``AkShareProvider.get_market_overview`` 的涨跌停家数口径。

原实现按固定 9.9% 判定涨跌停，是一张写死的主板线，两个方向都错：

- 20%/30% 板（创业板/科创板/北交所）上任何 +9.9% 以上的普通阳线被计成涨停
  ——「涨停 N 家」虚高；
- 主板 ST 5% 板上真封死的票（+5.0%）永远漏计。

本文件钉住「逐票板别」新口径，并防止调用点退回写死的标量阈值。
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.services.engine.data_gateway.providers.akshare_provider import (
    AkShareProvider,
    _count_limits,
)

# 期望值：板别带宽减容差（沪深 0.5pp）—— 钉住既有口径
_MAIN_SEAL = 9.5  # fidelity: allow-limit-threshold — 期望值，钉住既有口径
_CHINEXT_SEAL = 19.5  # fidelity: allow-limit-threshold — 期望值，钉住既有口径
_BSE_SEAL = 29.0  # fidelity: allow-limit-threshold — 期望值，钉住既有口径


def _spot(*rows: tuple[str, str, float]) -> pd.DataFrame:
    """按 (代码, 名称, 涨跌幅%) 造一份 akshare 现货快照。"""
    return pd.DataFrame(
        {
            "代码": [r[0] for r in rows],
            "名称": [r[1] for r in rows],
            "涨跌幅": [r[2] for r in rows],
            "成交额": [1.0e8] * len(rows),
        }
    )


def test_main_board_ten_percent_counts_as_limit_up():
    """主板 +10% 是真涨停（带宽 10%，容差后 9.5%）。"""
    # Arrange
    df = _spot(("600036", "招商银行", 10.0))

    # Act
    up, down = _count_limits(df)

    # Assert
    assert (up, down) == (1, 0)


def test_chinext_ten_percent_is_not_a_limit_up():
    """创业板 +10% 不是涨停 —— 带宽 20%，这正是原 9.9 写死线的错处。"""
    # Arrange
    df = _spot(("300750", "宁德时代", 10.0))

    # Act
    up, down = _count_limits(df)

    # Assert
    assert (up, down) == (0, 0)


def test_chinext_twenty_percent_counts_as_limit_up():
    """创业板 +20% 才是涨停。"""
    # Arrange
    df = _spot(("300750", "宁德时代", 20.0))

    # Act
    up, down = _count_limits(df)

    # Assert
    assert (up, down) == (1, 0)


def test_star_market_uses_twenty_percent_band():
    """科创板 688 同创业板 20% 板：+10% 不是涨停，+20% 是。"""
    # Arrange
    mild = _spot(("688111", "金山办公", 10.0))
    sealed = _spot(("688111", "金山办公", 20.0))

    # Act
    mild_counts = _count_limits(mild)
    sealed_counts = _count_limits(sealed)

    # Assert
    assert mild_counts == (0, 0)
    assert sealed_counts == (1, 0)


def test_bse_band_is_thirty_percent_with_one_point_tolerance():
    """北交所 30% 板，容差 1pp → 29.0% 起算。"""
    # Arrange
    below = _spot(("830799", "艾融软件", _BSE_SEAL - 0.5))
    at = _spot(("830799", "艾融软件", _BSE_SEAL))

    # Act
    up_below, _ = _count_limits(below)
    up_at, _ = _count_limits(at)

    # Assert
    assert (up_below, up_at) == (0, 1)


def test_seal_boundaries_follow_band_minus_tolerance():
    """容差边界逐档复核：带宽 −0.5pp 起算（主板 9.5、创业板 19.5）。"""
    # Arrange
    main_below = _spot(("600036", "招商银行", _MAIN_SEAL - 0.1))
    main_at = _spot(("600036", "招商银行", _MAIN_SEAL))
    chinext_below = _spot(("300750", "宁德时代", _CHINEXT_SEAL - 0.1))
    chinext_at = _spot(("300750", "宁德时代", _CHINEXT_SEAL))

    # Act
    counts = (
        _count_limits(main_below)[0],
        _count_limits(main_at)[0],
        _count_limits(chinext_below)[0],
        _count_limits(chinext_at)[0],
    )

    # Assert
    assert counts == (0, 1, 0, 1)


def test_limit_down_uses_the_same_band_as_limit_up():
    """跌停与涨停同带宽 —— 创业板 -10% 不是跌停，-20% 才是。"""
    # Arrange
    mild = _spot(("300750", "宁德时代", -10.0))
    sealed = _spot(("300750", "宁德时代", -20.0))

    # Act
    mild_counts = _count_limits(mild)
    sealed_counts = _count_limits(sealed)

    # Assert
    assert mild_counts == (0, 0)
    assert sealed_counts == (0, 1)


def test_st_name_on_main_board_is_judged_at_ten_percent():
    """名称前缀带 ST 的主板票按 ST 档判定。

    2026-07-06 起主板 ST 与普通票同为 10%，故 +10% 仍是涨停 —— 这条断言在
    该规则不变的前提下对任意运行日期都成立。
    """
    # Arrange
    df = _spot(("600036", "*ST 示例", 10.0))

    # Act
    up, _ = _count_limits(df)

    # Assert
    assert up == 1


def test_nan_pct_is_not_counted_either_way():
    """停牌/缺涨跌幅的行不计入任何一侧。"""
    # Arrange
    df = _spot(("600036", "招商银行", float("nan")))

    # Act
    up, down = _count_limits(df)

    # Assert
    assert (up, down) == (0, 0)


def test_missing_columns_degrades_to_zero_counts():
    """快照缺列时不抛异常，返回 (0, 0)。"""
    # Arrange
    df = pd.DataFrame({"名称": ["招商银行"], "成交额": [1.0e8]})

    # Act
    up, down = _count_limits(df)

    # Assert
    assert (up, down) == (0, 0)


def test_counts_agree_with_the_shared_single_source():
    """cross-check：与 ``market_breadth.limit_up_down_counts`` 逐位一致。

    两处若各自维护一份板别逻辑，就会再次出现「同一指标两个口径」，
    这条断言把二者钉在一起。
    """
    # Arrange
    from datetime import date

    from backend.shared.market_breadth import limit_up_down_counts

    df = _spot(
        ("600036", "招商银行", 10.0),
        ("300750", "宁德时代", 10.0),
        ("300750", "宁德时代", 20.0),
        ("830799", "艾融软件", 30.0),
        ("000001", "平安银行", -10.0),
    )

    # Act
    mine = _count_limits(df)
    shared = limit_up_down_counts(
        pd.to_numeric(df["涨跌幅"], errors="coerce"),
        df["代码"].astype(str),
        trade_date=date.today(),
    )

    # Assert
    assert mine == shared


def test_overview_body_has_no_hardcoded_scalar_threshold():
    """结构守卫：防止 ``get_market_overview`` 退回写死的 9.9 标量线。"""
    # Arrange
    import inspect

    # Act
    src = inspect.getsource(AkShareProvider.get_market_overview)

    # Assert
    assert "9.9" not in src  # fidelity: allow-limit-threshold — 断言语料：钉住实现里没有 9.9 标量线，不是涨跌停判定
    assert "_count_limits" in src


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
