"""T-FB-19 单测：基准序列读数（``benchmarks.load_benchmark_returns``）。

钉死的边：
- ``equal_weight`` / 未登记 id 不读数（直接 None，调用方等权兜底）；
- 对齐：指数收盘按 ``trade_date`` 归一，**次日收益**（``pct_change().shift(-1)``，
  与评估侧 ``_forward_return`` 同口径）后 reindex 到调用方轴；轴末日靠轴外
  多取的次日收盘才有值；重复日期 keep='last'（拼接缝防护，与 QuantDB 全仓
  口径一致）；
- 覆盖闸：有效收益日 / 轴长 < ``BENCH_MIN_COVERAGE`` 判不可用 → None
  （宁可等权兜底，不给断续指数序列）；
- 读数异常/空表/缺列 → None（数据面异常绝不上抛）。
"""

import numpy as np
import pandas as pd
import pytest

from backend.services.engine.factor_backtest import benchmarks as B

pytestmark = pytest.mark.unit


def _index_df(dates, closes):
    return pd.DataFrame({"trade_date": pd.to_datetime(dates), "close": closes})


def test_equal_weight_and_unknown_ids_short_circuit(monkeypatch):
    """equal_weight 与未登记 id 直接 None——不触发任何 hub 读取。"""
    called = {"n": 0}

    def boom(*a, **kw):
        called["n"] += 1
        return _index_df(["2024-01-02"], [1.0])

    monkeypatch.setattr(B, "_load_index_close", boom)
    assert (
        B.load_benchmark_returns(
            "equal_weight", pd.bdate_range("2024-01-01", periods=5)
        )
        is None
    )
    assert (
        B.load_benchmark_returns("nonexistent", pd.bdate_range("2024-01-01", periods=5))
        is None
    )
    assert called["n"] == 0


def test_aligned_returns_with_gap_and_duplicate(monkeypatch):
    """对齐正确性：次日收益 + reindex；重复日期 keep='last'；轴外日期被丢弃。"""
    # Arrange：轴内 10 日 + dates[4] 重复 + 轴外 1 个次日收盘（末日前向收益要用）
    dates = pd.bdate_range("2024-01-01", periods=10)
    beyond = dates[-1] + pd.tseries.offsets.BDay(1)
    idx_dates = [*dates[:4], dates[4], dates[4], *dates[5:], beyond]
    closes = [
        100.0,
        101.0,
        102.0,
        103.0,
        103.2,
        103.5,
        104.0,
        105.0,
        106.0,
        107.0,
        108.0,
        109.0,
    ]
    monkeypatch.setattr(
        B, "_load_index_close", lambda b, s, e: _index_df(idx_dates, closes)
    )

    # Act
    ret = B.load_benchmark_returns("csi300", dates)

    # Assert：10/10 全有效（末日靠轴外的次日收盘）；轴外日期不进输出
    assert ret is not None
    assert list(ret.index) == list(dates)
    assert ret.notna().sum() == 10
    # 重复日期 keep='last'：dates[4] 收盘取 103.5 → 次日收益 = 104/103.5 − 1
    assert ret.iloc[4] == pytest.approx(104.0 / 103.5 - 1.0, rel=1e-12)
    # 手算锚：前向口径 date=dates[8] ← close(9)=108 / close(8)=107
    assert ret.iloc[8] == pytest.approx(108.0 / 107.0 - 1.0, rel=1e-12)
    # 末日 dates[9] ← 轴外次日收盘 109 / 108（前向天数缺它则末日 NaN）
    assert ret.iloc[9] == pytest.approx(109.0 / 108.0 - 1.0, rel=1e-12)


def test_coverage_gate_falls_back(monkeypatch):
    """覆盖不足（< 90%）→ None，宁可等权兜底也不给断续序列。"""
    # Arrange：10 日轴只覆盖 5 日
    dates = pd.bdate_range("2024-01-01", periods=10)
    sparse = dates[::2]
    closes = np.linspace(100.0, 104.0, len(sparse))
    monkeypatch.setattr(
        B, "_load_index_close", lambda b, s, e: _index_df(sparse, closes)
    )

    # Act / Assert
    assert B.load_benchmark_returns("hsi", dates) is None


def test_read_exception_returns_none(monkeypatch):
    """数据面异常（目录缺失/解析炸）唯一归宿是 None——绝不上抛。"""

    def boom(b, s, e):
        raise OSError("no such parquet")

    monkeypatch.setattr(B, "_load_index_close", boom)
    assert (
        B.load_benchmark_returns("spx", pd.bdate_range("2024-01-01", periods=5)) is None
    )


def test_empty_or_malformed_frame_returns_none(monkeypatch):
    """空表/缺列 → None（不猜测列名，不给半边数据）。"""
    dates = pd.bdate_range("2024-01-01", periods=5)
    monkeypatch.setattr(B, "_load_index_close", lambda b, s, e: pd.DataFrame())
    assert B.load_benchmark_returns("spx", dates) is None

    monkeypatch.setattr(
        B,
        "_load_index_close",
        lambda b, s, e: pd.DataFrame({"trade_date": [dates[0]], "px": [1.0]}),
    )
    assert B.load_benchmark_returns("spx", dates) is None


def test_short_axis_returns_none():
    """轴 < 2 天做不出任何收益——直接 None（不空跑数据面）。"""
    assert B.load_benchmark_returns("spx", [pd.Timestamp("2024-01-02")]) is None
