"""market_sentiment_snapshot 的 as-of（历史回看）纪律测试。

核心不变量：**as_of 的产物必须与「把数据截断到该日」后重算的结果一致** ——
任何用到未来数据的地方都会在这里露馅。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.market_sentiment_snapshot import (
    INDICATORS,
    actual_path,
    build_payload,
)


def _frame(n: int = 700) -> pd.DataFrame:
    """构造一份满足面板契约的最小帧（确定性随机，避免 flaky）。"""
    rng = np.random.default_rng(20261027)
    idx = pd.bdate_range("2020-01-01", periods=n)
    df = pd.DataFrame(index=idx)
    for k, sym in enumerate(["000300.SH", "000001.SH", "000852.SH", "399006.SZ"]):
        df[f"close_{sym}"] = 100 * np.cumprod(1 + rng.normal(0, 0.01 + k * 0.002, n))
    # 指标列：单调噪声即可，只要数值有分布
    for ind in INDICATORS:
        if ind.column in df:
            continue
        df[ind.column] = rng.normal(0.5, 0.15, n).cumsum() / np.sqrt(
            np.arange(1, n + 1)
        )
    df["pct_above_ma20"] = rng.uniform(0.05, 0.95, n)
    df["n_total"] = 5000.0
    # 预测模块需要成交额（量能特征）与各指数收盘价
    df["amt_total"] = 1e6 * (1.0 + 0.02 * np.arange(n) + rng.normal(0, 0.05, n))
    df["amt_total"] = df["amt_total"].abs() + 1.0
    df["ret_disp"] = np.abs(rng.normal(2.5, 0.4, n))
    return df


def test_actual_path_relative_and_length():
    close = pd.Series(
        np.linspace(100, 120, 30), index=pd.bdate_range("2024-01-01", periods=30)
    )
    as_of = close.index[5]
    pts = actual_path(close, as_of, horizon=20)
    assert len(pts) == 21
    assert pts[0] == {"k": 0, "pct": 0.0}
    assert pts[20]["pct"] == pytest.approx(
        (close.iloc[25] / close.iloc[5] - 1) * 100, abs=0.01
    )


def test_actual_path_returns_partial_when_future_short():
    """回看日离今天不足 20 天：给出已走出的部分，而不是空（空会被误读成回看坏了）。"""
    close = pd.Series(
        np.linspace(100, 120, 30), index=pd.bdate_range("2024-01-01", periods=30)
    )
    pts = actual_path(close, close.index[25], horizon=20)
    assert [p["k"] for p in pts] == [0, 1, 2, 3, 4]
    assert pts[0]["pct"] == 0.0
    assert pts[-1]["pct"] == pytest.approx(
        (close.iloc[29] / close.iloc[25] - 1) * 100, abs=0.01
    )


def test_actual_path_empty_for_unknown_date():
    close = pd.Series([1.0, 2.0], index=pd.bdate_range("2024-01-01", periods=2))
    assert actual_path(close, pd.Timestamp("2030-01-01")) == []


def test_asof_equals_truncated_rebuild():
    """as_of 产物 == 截断到该日重算的产物（除真实走势与元数据）。"""
    from backend.scripts.breadth_regime import build_features

    df = _frame()
    feats = build_features(df)
    cut = df.index[600]

    asof = build_payload(df, feats, as_of=cut)
    truncated = build_payload(df.loc[:cut], feats.loc[:cut], as_of=None)

    assert asof["trade_date"] == cut.strftime("%Y-%m-%d")
    assert asof["stress"] == truncated["stress"]
    assert asof["indicators"] == truncated["indicators"]
    # 用 pandas.equals 比较：NaN 与 NaN 在同一位置视为相等（dict 的 == 会把 nan 判不等）
    assert pd.DataFrame(asof["stress_history"]).equals(
        pd.DataFrame(truncated["stress_history"])
    )
    for sym, block in asof["indices"].items():
        assert block["last"] == truncated["indices"][sym]["last"]
        assert block["fan"] == truncated["indices"][sym]["fan"]
        assert block["stats"] == truncated["indices"][sym]["stats"]


def test_asof_does_not_use_future_values():
    """指标值必须是 as_of 当天的，而不是最新一天的。"""
    from backend.scripts.breadth_regime import build_features

    df = _frame()
    df["pct_above_ma20"] = np.linspace(0.05, 0.95, len(df))  # 单调，便于区分
    feats = build_features(df)
    cut = df.index[600]
    asof = build_payload(df, feats, as_of=cut)
    breadth = next(i for i in asof["indicators"] if i["key"] == "breadth20")
    assert breadth["value"] == pytest.approx(
        round(float(df.loc[cut, "pct_above_ma20"]) * 100, 1)
    )


def test_fallback_path_with_ohlc_never_crashes():
    """分市场面板缺失、指数日线存在：旧路径必须不崩，且不伪造涨跌分布。"""
    from backend.scripts.breadth_regime import build_features

    df = _frame()
    feats = build_features(df)
    sym = "000300.SH"
    dates = df.index[-250:]
    close = df.loc[dates, f"close_{sym}"].to_numpy()
    ohlc = pd.DataFrame(
        {
            "date": dates,
            "symbol": sym,
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "amount": 1e6,
        }
    )
    payload = build_payload(df, feats, uni=None, ohlc=ohlc)
    block = payload["indices"][sym]
    assert "kline" not in block
    assert block["distribution"] == {}
    assert block["actual_days"] == 0 and block["actual_horizon"] == 20


def test_forecast_review_is_causal_and_non_overlapping():
    """样本外复盘只用 as_of 及以前的数据，命中数按非重叠样本统计。"""
    from backend.scripts.breadth_regime import build_features
    from backend.scripts.index_forecast import forecast_index

    df = _frame(n=2000)
    feats = build_features(df)
    cut = df.index[1600]
    fc = forecast_index(df.loc[:cut], feats.loc[:cut], "000300.SH", cut)
    for h in ("5", "10", "20"):
        acc = fc[h]["accuracy"]
        assert acc is not None and acc["n_oos"] >= 200
        assert acc["n_eff"] <= acc["n_oos"] // int(h) + 1
        for row in fc[h]["review"]:
            assert pd.Timestamp(row["date"]) <= cut
            assert row["hit"] == (np.sign(row["pred"]) == np.sign(row["actual"]))


def test_asof_actual_path_present_and_latest_mode_empty():
    from backend.scripts.breadth_regime import build_features

    df = _frame()
    feats = build_features(df)
    cut = df.index[600]
    asof = build_payload(df, feats, as_of=cut)
    latest = build_payload(df, feats)
    assert len(asof["indices"]["000300.SH"]["actual"]) == 21
    assert latest["indices"]["000300.SH"]["actual"] == []
    assert latest["as_of"] is False
    assert asof["as_of"] is True
