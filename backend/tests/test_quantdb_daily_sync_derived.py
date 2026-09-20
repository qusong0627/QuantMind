"""quantdb_daily_sync 价格派生指标重算的单元测试。

2026-08-17 修复：stock_daily_latest 的 ma*/ma_gap*/vol_atr_14 原先取自
features_daily（后复权口径），与 OHLCV（前复权 26.08）混用导致 risk 评分
误判「跌破均线」（比音勒芬 002832：ma5=147 vs close=26.08）。
现在改为基于 forward OHLCV 重算，本文件验证公式正确性。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

pandas = None
try:
    import pandas as pd

    from backend.scripts.quantdb_daily_sync import (
        _add_price_derived_cols,
        _PRICE_DERIVED_COLS,
    )
except ImportError:
    pd = None
    _add_price_derived_cols = None
    _PRICE_DERIVED_COLS = None

pytestmark = pytest.mark.skipif(
    pd is None, reason="pandas 未安装，跳过价格派生测试"
)


def _frame(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame(
        {
            "symbol": ["SZ000001"] * n,
            "trade_date": pd.date_range("2026-01-01", periods=n, freq="D"),
            "open": [c * 0.99 for c in closes],
            "high": [c * 1.02 for c in closes],
            "low": [c * 0.98 for c in closes],
            "close": closes,
            "volume": [100] * n,
            "amount": [1000.0] * n,
        }
    )


def test_ma_matches_manual_rolling():
    # 常数序列：MA == 常数，gap == 0
    df = _add_price_derived_cols(_frame([10.0] * 80))
    last = df.iloc[-1]
    for n in (5, 10, 20, 60):
        assert last[f"ma{n}"] == pytest.approx(10.0)
    for n in (5, 10, 20):  # PG 只有 gap_5/10/20，无 gap_60
        assert last[f"ma_gap_{n}"] == pytest.approx(0.0)


def test_ma_gap_matches_percent_formula():
    # ma_gap_N = (close/maN − 1) × 100（百分数口径，与 features_daily 一致）
    closes = list(range(1, 81))  # 1..80 递增
    df = _add_price_derived_cols(_frame([float(c) for c in closes]))
    last = df.iloc[-1]
    assert last["ma5"] == pytest.approx(78.0)
    assert last["ma_gap_5"] == pytest.approx((80 / 78 - 1) * 100)
    assert last["ma60"] == pytest.approx(50.5)  # 21..80 均值


def test_atr_wilder_smoothing():
    # Wilder ATR：TR_t = max(high-low, |high-prev_close|, |low-prev_close|)
    # ATR_t = (ATR_{t-1} × 13 + TR_t) / 14
    # 常数 close=10、high=10.2、low=9.8 → TR 恒为 0.4 → ATR = 0.4
    closes = [10.0] * 30
    df = _frame(closes)
    df["high"] = 10.2
    df["low"] = 9.8  # fidelity: allow-limit-threshold — 非阈值：ATR 夹具的 low（凑 TR=0.4）
    out = _add_price_derived_cols(df)
    assert out["vol_atr_14"].iloc[-1] == pytest.approx(0.4, abs=1e-9)
    # 前 13 行 ewm 预热期也有值（adjust=False 从第 1 行开始）
    assert out["vol_atr_14"].iloc[13] == pytest.approx(0.4, abs=1e-9)


def test_derived_cols_all_present():
    df = _add_price_derived_cols(_frame([10.0] * 70))
    for c in _PRICE_DERIVED_COLS:
        assert c in df.columns
        assert not df[c].isna().all()
    # ma60 需要 60 行，第 59 行起有值
    assert df["ma60"].iloc[59] == pytest.approx(10.0)
    assert df["ma60"].iloc[58] != df["ma60"].iloc[58]  # NaN（不足 60 行）


def test_multi_symbol_isolation():
    # 多股票混在一个 df 里，MA 不得跨 symbol 混算
    closes = list(range(1, 71))
    f1 = _frame([float(c) for c in closes])
    f2 = _frame([float(c) * 10 for c in closes])
    f2["symbol"] = "SZ000002"
    df = pd.concat([f1, f2], ignore_index=True).sort_values("trade_date")
    out = _add_price_derived_cols(df)
    s1 = out[out.symbol == "SZ000001"].iloc[-1]
    s2 = out[out.symbol == "SZ000002"].iloc[-1]
    assert s1["ma5"] == pytest.approx(68.0)
    assert s2["ma5"] == pytest.approx(680.0)
