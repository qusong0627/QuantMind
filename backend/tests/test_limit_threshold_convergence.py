"""涨跌停阈值收敛的**跨路径**回归：回测 / 模拟撮合 / 券商三条链路口径必须同源。

三条链路历史上各自复述板块常量，于是同一个标的在三处得到三个阈值 ——
回测里能成交的单，券商通道上被判成涨停；反之亦然。这类缺陷不会报错，
只会让回测好看、实盘不符。

本文件钉住的是「**恒等于权威口径减容差**」这一契约，而不是某个具体数值。
写死期望值只能钉住今天的分支；钉住与权威一致，才能在板规变化时继续报警。
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest

project_root = os.path.join(os.path.dirname(__file__), "../../")
sys.path.append(project_root)

#: 三处实现共用的取整容差（比例）。
_TOLERANCE = 0.005


def _authority(
    symbol: str, *, is_st: bool = False, trade_date=date(2026, 9, 1)
) -> float:
    from backend.services.simulation.services.local_market_data import limit_pct

    return float(limit_pct(symbol, is_st=is_st, trade_date=trade_date))


# ───────────────────── 模拟撮合：execution_engine ─────────────────────


def test_execution_engine_threshold_tracks_authority():
    """模拟撮合兜底阈值必须随板块走，而不是恒定 0.095。

    旧实现 `except: return 0.095` 把任何失败都压成主板 10%，于是创业板
    （真实 20%）上 +12% 的普通阳线被误判成涨停 —— 模拟盘因此「买不进」
    一支本可以买的票，回测收益被系统性压低（或抬高，取决于方向）。
    """
    from backend.services.simulation.services.execution_engine import (
        SimulationExecutionEngine,
    )

    threshold = SimulationExecutionEngine._board_limit_threshold

    assert threshold("SH600000") == pytest.approx(_authority("SH600000") - _TOLERANCE)
    assert threshold("SZ000001") == pytest.approx(_authority("SZ000001") - _TOLERANCE)
    assert threshold("SZ300750") == pytest.approx(_authority("SZ300750") - _TOLERANCE)
    assert threshold("SH688111") == pytest.approx(_authority("SH688111") - _TOLERANCE)
    assert threshold("BJ430047") == pytest.approx(_authority("BJ430047") - _TOLERANCE)


def test_execution_engine_wide_board_threshold_is_not_main_board():
    """反向控制：宽板阈值必须**大于**主板，否则上面那条用例恒真也能过。"""
    from backend.services.simulation.services.execution_engine import (
        SimulationExecutionEngine,
    )

    main = SimulationExecutionEngine._board_limit_threshold("SH600000")
    chinext = SimulationExecutionEngine._board_limit_threshold("SZ300750")
    bse = SimulationExecutionEngine._board_limit_threshold("BJ430047")

    assert chinext > main
    assert bse > chinext
    # 12% 在主板之上、在创业板之下 —— 正是旧实现误判的那条带
    assert main < 0.12 < chinext


def test_execution_engine_preserves_legacy_main_board_value():
    """主板数值必须逐位不变（0.095）：收敛口径不得顺手改掉已正确的判定。"""
    from backend.services.simulation.services.execution_engine import (
        SimulationExecutionEngine,
    )

    # fidelity: allow-limit-threshold — 断言值，钉住既有行为
    assert SimulationExecutionEngine._board_limit_threshold(
        "SH600000"
    ) == pytest.approx(0.095)


# ───────────────────── 券商通道：broker_client ─────────────────────


def test_broker_threshold_tracks_authority():
    """券商通道的涨停判定阈值同样必须随板块走。

    旧实现写死 0.095，对创业板/北交所只有真实线的一半 —— 一根 12% 的
    普通阳线在券商通道上被判成涨停，实盘据此拒绝下单。
    """
    from backend.services.live_trading.services.broker_client import (
        PaperTradingBroker,
    )

    threshold = PaperTradingBroker._limit_threshold

    for symbol in ("SH600000", "SZ000001", "SZ300750", "SH688111", "BJ430047"):
        assert threshold(symbol) == pytest.approx(_authority(symbol) - _TOLERANCE), (
            symbol
        )


def test_broker_wide_board_threshold_is_not_main_board():
    from backend.services.live_trading.services.broker_client import (
        PaperTradingBroker,
    )

    assert PaperTradingBroker._limit_threshold("SZ300750") > 0.15
    assert PaperTradingBroker._limit_threshold("BJ430047") > 0.25


def test_broker_and_execution_engine_agree():
    """两条链路的阈值必须**逐位相同** —— 这正是本次收敛要保证的事。

    旧实现下这里会是 0.095 vs 0.195（同一支创业板股票，回测说可买、
    券商说涨停），本用例即那条不一致的护栏。
    """
    from backend.services.live_trading.services.broker_client import (
        PaperTradingBroker,
    )
    from backend.services.simulation.services.execution_engine import (
        SimulationExecutionEngine,
    )

    for symbol in ("SH600000", "SZ000001", "SZ300750", "SH688111", "BJ430047"):
        assert PaperTradingBroker._limit_threshold(symbol) == pytest.approx(
            SimulationExecutionEngine._board_limit_threshold(symbol)
        ), symbol


# ───────────────────── 回测核心：backtest_engine ─────────────────────


def test_backtest_engine_threshold_tracks_authority_with_st():
    """回测核心的 `get_price_limit_threshold` 必须把 is_st 真的传下去。

    ST 保护期（<2026-07-06）内 ST 主板是 5% 板，写死 False 会按 10% 判，
    真涨停被当成可成交。
    """
    from backend.shared.backtest_engine.core.engine import get_price_limit_threshold

    d = date(2026, 7, 3)
    assert get_price_limit_threshold(
        "SH600000", is_st=True, trade_date=d
    ) == pytest.approx(_authority("SH600000", is_st=True, trade_date=d))
    assert get_price_limit_threshold(
        "SH600000", is_st=False, trade_date=d
    ) == pytest.approx(_authority("SH600000", is_st=False, trade_date=d))
    # 保护期内 ST 与主板必须**不同**，否则上面两条恒真
    assert get_price_limit_threshold(
        "SH600000", is_st=True, trade_date=d
    ) < get_price_limit_threshold("SH600000", is_st=False, trade_date=d)


def test_backtest_engine_threshold_tracks_authority_across_reform():
    from backend.shared.backtest_engine.core.engine import get_price_limit_threshold

    before = get_price_limit_threshold("SZ300750", trade_date=date(2020, 8, 21))
    after = get_price_limit_threshold("SZ300750", trade_date=date(2020, 8, 24))

    assert before == pytest.approx(0.10)
    assert after == pytest.approx(0.20)


# ───────────────────── resolve_is_st ─────────────────────


def test_resolve_is_st_returns_bool_and_never_raises():
    """`resolve_is_st` 是回测核心的 ST 兜底，契约是「必定返回 bool、不抛」。

    它取代的旧写法是 `except: is_st = False` —— 静默降级。现在降级要留痕，
    但**返回值类型**必须稳定，否则调用点会在 except 分支里炸。
    """
    from backend.shared.backtest_engine.core.engine import resolve_is_st

    for symbol in ("SH600000", "SZ300750", "BJ430047", "not-a-symbol", ""):
        assert isinstance(resolve_is_st(symbol), bool)


def test_resolve_is_st_tolerates_non_string_symbol():
    """传入非字符串（如 pandas 标量）不得抛。"""
    from backend.shared.backtest_engine.core.engine import resolve_is_st

    assert isinstance(resolve_is_st(None), bool)
