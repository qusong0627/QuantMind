"""推理回测（`inference_backtest_service`）涨跌停判定的回归。

旧实现是两处 `9.8` 字面量：

- `_select_stocks_daily` 的 `exclude_limit_moves`：`abs(pct) >= 9.8`
- `_SimulationEngine._is_limit_down`：`pct <= -9.8`

`pct` 的单位是**百分数**（与 9.8 比较），而权威实现 `limit_pct` 返回的是
**比例**（0.20），换算时要点 ×100。

扁平 9.8 在两个方向上同时出错：

| 场景 | 真实线 | 旧阈值 | 结果 |
|---|---|---|---|
| 创业板 +12% | 20% | 9.8 | **误剔**（本可买入的普通阳线被当作涨停） |
| 北交所 -20% | 30% | -9.8 | **误剔**（本可卖出的票被当作跌停） |
| ST 主板 +5%（<2026-07-06） | 5% | 9.8 | **漏剔**（真涨停被当成可买入） |

漏剔那一条会让回测在**实际买不进的涨停价上成交**——正是「漂亮数据」的来源。
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

project_root = os.path.join(os.path.dirname(__file__), "../../")
sys.path.append(project_root)

from backend.services.engine.inference.inference_backtest_service import (
    StrategyConfig,
    _SimulationEngine,
    _is_star_market,
    _limit_threshold_pct,
    _select_stocks_daily,
)
from backend.shared.stock_utils import StockCodeUtil

#: 与实现同源的取整余量（百分点），沿用旧值 9.8 = 10 − 0.2。
_SLACK_PCT = 0.2

#: ST 主板 5% 保护期内的一个**未封板**跌幅：-4.9% 已贴住 5% 线以内，
#: 但它本身不是断言的目标，只是喂给判定的输入。
_ST_PROTECTION_DROP = -4.9  # fidelity: allow-limit-threshold — 夹具字面量

_CONFIG = replace(
    StrategyConfig(),
    main_board_only=False,
    exclude_st=False,
    exclude_limit_moves=True,
)


def _selected(symbol: str, pct_change: float, trade_date: str, is_st: int = 0) -> list:
    """跑一遍单日选股，返回被选中的标的；只用涨跌停这一层过滤。

    **必须走 suffix 形态**：`_select_stocks_daily` 内部把 symbol 归一成
    `600000.SH` 后再查 `price_day` 与 `industry_map`。传前缀形态会让
    `industry` 变 NaN，函数在「行业必须有信号」那一步把所有候选丢光 ——
    于是「期望被剔除」的断言会**空集通过**（`== []` 恒真），测试全绿但什么都没验。
    """
    suffix = StockCodeUtil.to_suffix(symbol)
    day_scores = pd.DataFrame({"symbol": [symbol], "score": [0.11]})
    price_day = pd.DataFrame(
        {
            "symbol": [suffix],
            "pct_change": [pct_change],
            "is_st": [is_st],
            "trade_date": [trade_date],
            "close": [10.0],
        }
    )
    return _select_stocks_daily(day_scores, {suffix: "行业A"}, _CONFIG, price_day)


def test_harness_is_not_vacuous():
    """护栏：确认夹具真的能选出票。

    没有这一条，上面所有 `== []` 的断言在夹具坏掉时会**全部通过** ——
    「零项参与即通过」正是本仓库栽过的坑（见 verification-vacuous-pass-guard）。
    """
    assert len(_selected("SH600000", 1.0, "2026-09-01")) == 1


def _engine(panel: pd.DataFrame) -> _SimulationEngine:
    eng = _SimulationEngine.__new__(_SimulationEngine)
    eng.price_panel = panel
    return eng


def _panel(
    symbol: str, trade_date: str, pct_change: float, is_st: int = 0
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [symbol],
            "trade_date": [trade_date],
            "pct_change": [pct_change],
            "is_st": [is_st],
        }
    )


# ───────────────────── 阈值本身：与权威口径一致 ─────────────────────


@pytest.mark.parametrize(
    ("symbol", "trade_date", "is_st"),
    [
        ("SH600000", date(2026, 9, 1), False),
        ("SZ300750", date(2026, 9, 1), False),
        ("SZ300750", date(2020, 8, 21), False),
        ("SH688111", date(2026, 9, 1), False),
        ("BJ430047", date(2026, 9, 1), False),
        ("SH600000", date(2026, 7, 3), True),
    ],
)
def test_threshold_tracks_authority(symbol, trade_date, is_st):
    from backend.services.simulation.services.local_market_data import limit_pct

    expected = (
        float(limit_pct(symbol, is_st=is_st, trade_date=trade_date)) * 100.0
        - _SLACK_PCT
    )
    assert _limit_threshold_pct(symbol, trade_date, is_st) == pytest.approx(expected)


# ───────────────────── 选股：宽板不再误剔，ST 不再漏剔 ─────────────────────


def test_chinext_and_star_never_reach_the_limit_filter():
    """**可达性**：300/301/688 在涨跌停过滤之前就被整块剔除了。

    所以本文件里创业板/科创板的阈值分支在这条选股路径上**跑不到** ——
    为它们写「涨 12% 应保留」之类的用例只会得到一个**恒真**的断言。
    这里把可达性本身钉住，将来 `_is_star_market` 放宽时会立刻报警。
    """
    for symbol in ("SZ300750", "SZ301001", "SH688111"):
        assert _is_star_market(StockCodeUtil.to_suffix(symbol)) is True
        assert _selected(symbol, 1.0, "2026-09-01") == []


def test_bse_ordinary_gain_is_kept():
    """北交所 30% 板：+15% 是普通阳线 —— 旧实现按 9.8 把它剔掉了。

    这是本路径上**唯一可达**的宽板（非 688/300/301），所以它才是真正
    验证「阈值随板块」的那一条。
    """
    assert _selected("BJ430047", 15.0, "2026-09-01") != []


def test_bse_actual_limit_up_is_excluded():
    assert _selected("BJ430047", 29.9, "2026-09-01") == []


def test_st_limit_up_is_excluded_during_protection_window():
    """+5% 就是涨停 —— 旧实现按 9.8 判，会把它当成可买入。"""
    assert _selected("SH600000", 5.0, "2026-07-03", is_st=1) == []


def test_main_board_ordinary_gain_is_kept():
    assert _selected("SH600000", 5.0, "2026-09-01") != []


# ───────────────────── 跌停：宽板不再误剔，ST 不再漏剔 ─────────────────────


def test_is_limit_down_ignores_wide_board_move_within_its_own_band():
    """北交所 -20% 不是跌停（真跌停线是 -30%）—— 旧实现按 9.8 判成卖不出。"""
    eng = _engine(_panel("BJ430047", "2026-09-01", -20.0))
    assert eng._is_limit_down("BJ430047", "2026-09-01") is False


def test_is_limit_down_detects_wide_board_actual_limit():
    eng = _engine(_panel("BJ430047", "2026-09-01", -29.9))
    assert eng._is_limit_down("BJ430047", "2026-09-01") is True


def test_is_limit_down_detects_st_limit_during_protection_window():
    """ST 主板 -4.9% 即跌停 —— 旧实现按 9.8 判会漏掉。"""
    eng = _engine(_panel("SH600000", "2026-07-03", _ST_PROTECTION_DROP, is_st=1))
    assert eng._is_limit_down("SH600000", "2026-07-03") is True


def test_is_limit_down_ignores_chinext_move_within_its_own_band():
    """直接单测跌停判定本身。选股路径不会持有创业板（见可达性用例），
    但 `_is_limit_down` 是公开的判定单元，口径仍须与权威一致。"""
    eng = _engine(_panel("SZ300750", "2026-09-01", -15.0))
    assert eng._is_limit_down("SZ300750", "2026-09-01") is False
