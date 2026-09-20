"""`as41_crash_dip` 抄底候选的「贴板」带宽测试。

该策略的买入清单由 `_find_oversold` 产出，它用涨跌停带宽把「已经封死在板上、
次日买不进」的票滤掉。旧实现是一条写死的 ±0.095，而沪深 300 成分股里有大量
创业板/科创板（真实 20% 板）标的 —— 一根 −12% 的普通阴线在 20% 板上离板还远，
却被当成贴板剔除。方向是**系统性**的：越该抄的底越被扔掉。
"""

from __future__ import annotations

import pandas as pd

from backend.services.engine.qlib_app.utils.extended_strategies import _limit_band

#: 主板 10% 扣掉 0.5pp 取整余量；宽板 20% 同理。
_MAIN_BAND = 0.095  # fidelity: allow-limit-threshold — 期望值，钉住既有口径
_WIDE_BAND = 0.195  # fidelity: allow-limit-threshold — 期望值，钉住既有口径

_REFORM_BEFORE = "2019-06-03"
_REFORM_AFTER = "2024-01-02"


def test_band_main_board():
    band = _limit_band(pd.Index(["SH600036", "SZ000001"]), _REFORM_AFTER)

    assert band["SH600036"] == _MAIN_BAND
    assert band["SZ000001"] == _MAIN_BAND


def test_band_wide_boards():
    """创业板 / 科创板是 20% 板，带宽必须随之上抬。"""
    band = _limit_band(pd.Index(["SZ300750", "SH688981", "SZ301001"]), _REFORM_AFTER)

    assert band["SZ300750"] == _WIDE_BAND
    assert band["SH688981"] == _WIDE_BAND
    assert band["SZ301001"] == _WIDE_BAND


def test_band_pre_reform_chinext_is_main_board():
    """2020-08-24 之前创业板是 10% 板 —— 带宽不能一律按 20% 给。"""
    band = _limit_band(pd.Index(["SZ300750"]), _REFORM_BEFORE)

    assert band["SZ300750"] == _MAIN_BAND


def test_band_is_indexed_by_symbol_in_input_order():
    """返回的 Series 必须按入参对齐 —— 错位会把 A 的带宽安到 B 头上且不报错，
    而调用点是用 `dd["$change"].abs() < limit_band` 做 index 对齐比较的。"""
    idx = pd.Index(["SZ300750", "SH600036", "SH688981"])
    band = _limit_band(idx, _REFORM_AFTER)

    assert list(band.index) == list(idx)
    assert band["SZ300750"] > band["SH600036"]


def test_deep_chinext_drop_is_inside_band_but_main_board_drop_is_not():
    """这条是本文件的核心：同样是 −12%，在 20% 板上是普通阴线（可抄），
    在 10% 板上已封死（不可买）。

    旧实现下两者都会被 `abs(change) < 0.095` 判掉 —— 抄底策略把自己最想买的
    那批深跌创业板票全部排除，而它恰恰叫「暴跌抄底」。
    """
    band = _limit_band(pd.Index(["SZ300750", "SH600036"]), _REFORM_AFTER)
    change = pd.Series({"SZ300750": -0.12, "SH600036": -0.12})
    inside = change.abs() < band

    assert bool(inside["SZ300750"]) is True
    assert bool(inside["SH600036"]) is False


def test_band_boundary_is_strict_tolerance_adjusted():
    """带宽是 `limit_pct - 容差`：差一点点没到板仍算可抄，正好到板则剔除。"""
    band = _limit_band(pd.Index(["SH600036"]), _REFORM_AFTER)

    assert band["SH600036"] < 0.10
    assert band["SH600036"] > 0.09


def test_band_accepts_timestamp_trade_date():
    """调用点传的是 `pd.Timestamp`（来自 `_all_dates`），不是 date/str。"""
    band = _limit_band(pd.Index(["SZ300750"]), pd.Timestamp("2019-06-03"))

    assert band["SZ300750"] == _MAIN_BAND
