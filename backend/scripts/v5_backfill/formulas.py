"""V5 因子重建 · 第一批公式（价量/换手）。

口径（2026-11-19 对账确认）：
- 逐标的按自身 bar 序列计算（不做日历 ffill）；
- 收益率用 qfq（与 backward 同收益）；
- volume/amount 用原始值；turn 的股本用 capital 表 PIT（报告期 as-of）；
- std 用样本标准差 ddof=1；
- 截面 1%/99% 缩尾（与官方 V5 每列 32 个顶值一致）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

LN2 = np.log(2.0)
GK_COEF = 2.0 * LN2 - 1.0


def _roll(s: pd.Series, n: int, how: str = "mean", ddof: int = 1) -> pd.Series:
    r = s.rolling(n, min_periods=n)
    if how == "mean":
        return r.mean()
    if how == "std":
        return r.std(ddof=ddof)
    if how == "sum":
        return r.sum()
    if how == "max":
        return r.max()
    if how == "min":
        return r.min()
    raise ValueError(how)


def compute_panel(bars: pd.DataFrame, circ_daily: pd.DataFrame | None = None) -> pd.DataFrame:
    """全区间长表（symbol, dt, 各列），供逐日切片物化。"""
    df = bars.copy()
    if circ_daily is not None and not circ_daily.empty:
        df = df.merge(circ_daily, on=["symbol", "dt"], how="left")
    else:
        df["circ"] = np.nan
    df = df.sort_values(["symbol", "time"], kind="stable").reset_index(drop=True)
    g = df.groupby("symbol", sort=False)
    close, high, low, open_ = df["close"], df["high"], df["low"], df["open"]
    volume, amount = df["volume"], df["amount"]
    out: dict[str, pd.Series] = {}

    ret = g["close"].pct_change(fill_method=None)
    df["_ret"] = ret

    # ── mom ────────────────────────────────────────────────────────────────
    for n in (1, 3, 5, 10, 20, 60):
        out[f"mom_ret_{n}d"] = ret if n == 1 else g["close"].pct_change(n, fill_method=None)
    for n in (5, 20):
        ma = g["close"].transform(lambda s, n=n: _roll(s, n))
        out[f"mom_ma_gap_{n}"] = close / ma - 1.0

    # ── vol（日频子集）────────────────────────────────────────────────────
    for n in (5, 10, 20):
        out[f"vol_std_{n}"] = g["_ret"].transform(lambda s, n=n: _roll(s, n, "std"))
    prev_close = g["close"].shift(1)
    df["_tr"] = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    out["vol_atr_14"] = g["_tr"].transform(lambda s: _roll(s, 14))
    df["_gk"] = 0.5 * np.log(high / low) ** 2 - GK_COEF * np.log(close / open_) ** 2
    # V5 口径 = sqrt(mean(GK,20))（2026-11-19 对账：比值精确 1.0000）
    out["vol_gk_20"] = np.sqrt(
        g["_gk"].transform(lambda s: _roll(s, 20)).clip(lower=0)
    )
    df["_amp"] = (high - low) / close
    out["vol_amp_1"] = df["_amp"]
    out["vol_amp_20"] = g["_amp"].transform(lambda s: _roll(s, 20))

    # ── amt ────────────────────────────────────────────────────────────────
    df["_log_amt"] = np.log(amount.where(amount > 0))
    out["amt_log"] = df["_log_amt"]
    for n in (5, 20, 60):
        out[f"amt_ma_{n}"] = np.log(
            g["amount"].transform(lambda s, n=n: _roll(s.where(s > 0), n))
        )
    mu20 = g["_log_amt"].transform(lambda s: _roll(s, 20))
    sd20 = g["_log_amt"].transform(lambda s: _roll(s, 20, "std"))
    out["amt_z_20"] = (df["_log_amt"] - mu20) / sd20
    amt20 = g["amount"].transform(lambda s: _roll(s, 20))
    amt5 = g["amount"].transform(lambda s: _roll(s, 5))
    out["amt_ratio_1_20"] = amount / amt20
    out["amt_ratio_5_20"] = amt5 / amt20
    out["amt_skew_20"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=20).skew())
    out["amt_close_pos"] = (close - low) / (high - low).replace(0, np.nan)
    df["_amt_up"] = amount * (ret > 0).astype(float)
    for n in (5, 20):
        up = g["_amt_up"].transform(lambda s, n=n: _roll(s, n, "sum"))
        tot = g["amount"].transform(lambda s, n=n: _roll(s, n, "sum"))
        out[f"amt_up_ratio_{n}"] = up / tot
    df["_obv"] = g.apply(
        lambda x: (np.sign(x["_ret"].fillna(0)) * x["volume"]).cumsum(),
        include_groups=False,
    ).reset_index(level=0, drop=True).to_numpy()
    for n in (5, 20):
        d_obv = g["_obv"].diff(n)
        vsum = g["volume"].transform(lambda s, n=n: _roll(s, n, "sum"))
        out[f"amt_net_flow_{n}"] = d_obv / vsum
    ma_amt = g["amount"].transform(lambda s: _roll(s, 20))
    df["_high_day"] = (amount > ma_amt).astype(float)
    out["amt_high_days_10"] = g["_high_day"].transform(
        lambda s: s.rolling(10, min_periods=10).sum().shift(1))
    corr = g.apply(
        lambda x: x["_ret"].rolling(20, min_periods=20).corr(x["volume"]),
        include_groups=False,
    ).reset_index(level=0, drop=True).to_numpy()
    out["amt_vol_ratio_20"] = corr

    # ── turn ───────────────────────────────────────────────────────────────
    df["_turn1"] = volume / df["circ"].replace(0, np.nan)
    out["turn_1"] = df["_turn1"]
    for n in (3, 10, 20, 60):
        out[f"turn_{n}"] = g["_turn1"].transform(lambda s, n=n: _roll(s, n))
    out["turn_std_20"] = g["_turn1"].transform(lambda s: _roll(s, 20, "std"))
    t1m20 = out["turn_20"] = g["_turn1"].transform(lambda s: _roll(s, 20))
    t1m5 = g["_turn1"].transform(lambda s: _roll(s, 5))
    out["turn_z_20"] = (df["_turn1"] - t1m20) / out["turn_std_20"]
    out["turn_ratio_1_5"] = df["_turn1"] / t1m5
    out["turn_ratio_1_20"] = df["_turn1"] / t1m20
    out["turn_trend_5_20"] = t1m5 / t1m20
    out["turn_acc_5"] = g["_turn1"].transform(lambda s: _roll(s, 5, "sum"))
    out["turn_acc_20"] = g["_turn1"].transform(lambda s: _roll(s, 20, "sum"))
    t1max20 = g["_turn1"].transform(lambda s: _roll(s, 20, "max"))
    t1min20 = g["_turn1"].transform(lambda s: _roll(s, 20, "min"))
    out["turn_breakout_20"] = df["_turn1"] / t1max20
    out["turn_hl_pos_20"] = (df["_turn1"] - t1min20) / (t1max20 - t1min20)
    df["_above"] = (df["_turn1"] > t1m20).astype(float)
    out["turn_high_days_10"] = g["_above"].transform(
        lambda s: s.rolling(10, min_periods=10).sum().shift(1))

    # ── 全区间长表 ─────────────────────────────────────────────────────────
    res = pd.DataFrame(out)
    res.index = df.index
    res.insert(0, "symbol", df["symbol"].to_numpy())
    res.insert(1, "dt", df["dt"].to_numpy())
    return res


def compute_batch(bars: pd.DataFrame, circ_daily: pd.DataFrame | None,
                  target: str) -> pd.DataFrame:
    panel = compute_panel(bars, circ_daily)
    return panel[panel["dt"] == target].set_index("symbol").drop(columns=["dt"])


def winsorize_cross_section(frame: pd.DataFrame, lower: float = 0.01,
                            upper: float = 0.99) -> pd.DataFrame:
    out = frame.copy()
    for c in out.columns:
        s = pd.to_numeric(out[c], errors="coerce")
        if s.notna().sum() < 20:
            continue
        lo, hi = s.quantile(lower), s.quantile(upper)
        out[c] = s.clip(lower=lo, upper=hi)
    return out
