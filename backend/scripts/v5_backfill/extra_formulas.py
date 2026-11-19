"""V5 第二批公式：gtja / tech / style / risk（纯日频，2026-11-19）。

口径约定（与第一批一致）：逐标的序列、qfq 收益、样本 std ddof=1、
截面 1%/99% 缩尾；RANK/TSRANK 先按 pct 口径实现，对账后按需调整。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

LN2 = np.log(2.0)


def _g(df: pd.DataFrame):
    return df.groupby("symbol", sort=False)


def _sma(s: pd.Series, n: int, m: int = 1) -> pd.Series:
    """中国公式 SMA(X,N,M) = alpha=M/N 的 EMA。"""
    return s.ewm(alpha=m / n, adjust=False).mean()


def _tsrank(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).rank(pct=True)


def _decaylinear(s: pd.Series, n: int) -> pd.Series:
    """DECAYLINEAR(X,N)：最近值权重最高（1..N 归一化）。"""
    w = np.arange(1, n + 1, dtype=float)
    w /= w.sum()
    return s.rolling(n, min_periods=n).apply(lambda x: float(np.dot(x, w)), raw=True)


def _xs_rank(s: pd.Series, dates: pd.Series) -> pd.Series:
    """截面 pct rank（按日）。"""
    return s.groupby(dates).rank(pct=True)


def compute_extra(df: pd.DataFrame, mret: pd.Series) -> dict[str, pd.Series]:
    """输入长表（symbol/time/open/high/low/close/volume/amount 排序后），返回各列。"""
    g = _g(df)
    close, high, low = df["close"], df["high"], df["low"]
    volume, amount = df["volume"], df["amount"]
    vwap = amount * 1e4 / volume.replace(0, np.nan)
    ret = g["close"].pct_change(fill_method=None)
    df["_ret"] = ret
    out: dict[str, pd.Series] = {}

    # ── gtja ───────────────────────────────────────────────────────────────
    prev = g["close"].shift(1)
    std20 = g["close"].transform(lambda s: s.rolling(20, min_periods=20).std(ddof=1))
    up = close > prev
    a = std20.where(up, 0.0).groupby(df["symbol"]).transform(
        lambda s: s.ewm(alpha=1 / 20, adjust=False).mean())
    b = std20.where(~up, 0.0).groupby(df["symbol"]).transform(
        lambda s: s.ewm(alpha=1 / 20, adjust=False).mean())
    out["gtja_023"] = a / (a + b) * 100

    sign3 = (np.sign(close - prev) + np.sign(prev - g["close"].shift(2))
             + np.sign(g["close"].shift(2) - g["close"].shift(3)))
    r3 = _xs_rank(sign3, df["dt"])
    out["gtja_048"] = -1 * r3 * (
        g["volume"].transform(lambda s: s.rolling(5, min_periods=5).sum())
        / g["volume"].transform(lambda s: s.rolling(20, min_periods=20).sum())
    )

    hl = high + low
    phl = g["high"].shift(1) + g["low"].shift(1)
    cond1 = np.where(hl >= phl, 0.0, np.maximum((high - g["high"].shift(1)).abs(),
                                                (low - g["low"].shift(1)).abs()))
    cond2 = np.where(hl <= phl, 0.0, np.maximum((high - g["high"].shift(1)).abs(),
                                                (low - g["low"].shift(1)).abs()))
    s1 = pd.Series(cond1, index=df.index).groupby(df["symbol"]).transform(
        lambda s: s.rolling(12, min_periods=12).sum())
    s2 = pd.Series(cond2, index=df.index).groupby(df["symbol"]).transform(
        lambda s: s.rolling(12, min_periods=12).sum())
    out["gtja_049"] = s1 / (s1 + s2)

    mixed = close * 0.35 + vwap * 0.65
    delta2 = mixed - mixed.groupby(df["symbol"]).shift(2)
    dl3 = delta2.groupby(df["symbol"]).transform(lambda s: _decaylinear(s, 3))
    rank_dl = _xs_rank(dl3, df["dt"])
    mvol = g["volume"].transform(lambda s: s.rolling(180, min_periods=180).mean())
    corr = mvol.groupby(df["symbol"]).transform(
        lambda s: s.rolling(13, min_periods=13).corr(close.loc[s.index]))
    dl5 = corr.abs().groupby(df["symbol"]).transform(lambda s: _decaylinear(s, 5))
    ts5 = dl5.groupby(df["symbol"]).transform(lambda s: _tsrank(s, 15))
    out["gtja_092"] = -1 * np.maximum(rank_dl, ts5)

    ratio20 = close / close.groupby(df["symbol"]).shift(20)
    out["gtja_135"] = ratio20.groupby(df["symbol"]).transform(
        lambda s: s.shift(1).ewm(alpha=1 / 20, adjust=False).mean())

    m9 = g["volume"].transform(lambda s: s.rolling(9, min_periods=9).mean())
    m26 = g["volume"].transform(lambda s: s.rolling(26, min_periods=26).mean())
    m12 = g["volume"].transform(lambda s: s.rolling(12, min_periods=12).mean())
    out["gtja_145"] = (m9 - m26) / m12 * 100

    upv = df["volume"].where(close > prev, 0.0)
    dnv = df["volume"].where(close <= prev, 0.0)
    su = upv.groupby(df["symbol"]).transform(lambda s: s.rolling(26, min_periods=26).sum())
    sd = dnv.groupby(df["symbol"]).transform(lambda s: s.rolling(26, min_periods=26).sum())
    out["gtja_040"] = su / sd.replace(0, np.nan) * 100

    mix2 = (high + low) / 2 * 0.2 + vwap * 0.8
    delta4 = mix2 - mix2.groupby(df["symbol"]).shift(4)
    out["gtja_008"] = _xs_rank(-delta4, df["dt"])

    # ── tech ───────────────────────────────────────────────────────────────
    ma20 = g["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    sd20 = g["close"].transform(lambda s: s.rolling(20, min_periods=20).std(ddof=1))
    upb, dnb = ma20 + 2 * sd20, ma20 - 2 * sd20
    out["tech_bb_width"] = (upb - dnb) / ma20
    out["tech_bb_pos"] = (close - dnb) / (upb - dnb)
    tp = (high + low + close) / 3
    ma_tp = tp.groupby(df["symbol"]).transform(lambda s: s.rolling(20, min_periods=20).mean())
    md = (tp - ma_tp).abs().groupby(df["symbol"]).transform(
        lambda s: s.rolling(20, min_periods=20).mean())
    out["tech_cci_20"] = (tp - ma_tp) / (0.015 * md)
    # Wilder ADX(14)
    prev_c = close.groupby(df["symbol"]).shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_c).abs(), (low - prev_c).abs()],
                   axis=1).max(axis=1)
    upm = high - high.groupby(df["symbol"]).shift(1)
    dnm = low.groupby(df["symbol"]).shift(1) - low
    pdm = pd.Series(np.where((upm > dnm) & (upm > 0), upm, 0.0), index=df.index)
    ndm = pd.Series(np.where((dnm > upm) & (dnm > 0), dnm, 0.0), index=df.index)
    atr = tr.groupby(df["symbol"]).transform(lambda s: s.ewm(alpha=1 / 14, adjust=False).mean())
    pdi = 100 * pdm.groupby(df["symbol"]).transform(
        lambda s: s.ewm(alpha=1 / 14, adjust=False).mean()) / atr.replace(0, np.nan)
    ndi = 100 * ndm.groupby(df["symbol"]).transform(
        lambda s: s.ewm(alpha=1 / 14, adjust=False).mean()) / atr.replace(0, np.nan)
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    out["tech_adx_14"] = dx.groupby(df["symbol"]).transform(
        lambda s: s.ewm(alpha=1 / 14, adjust=False).mean())
    out["tech_close_to_high_20"] = 1 - close / g["high"].transform(
        lambda s: s.rolling(20, min_periods=20).max())
    out["tech_max_drawdown_20"] = 1 - close / g["close"].transform(
        lambda s: s.rolling(20, min_periods=20).max())

    # ── style（rb = 000905.SH 日收益）────────────────────────────────────
    rb = df["dt"].map(mret)
    for n in (20, 60):
        cov = ret.groupby(df["symbol"]).transform(
            lambda s, n=n: s.rolling(n, min_periods=n).cov(rb.loc[s.index]))
        var = rb.groupby(df["symbol"]).transform(
            lambda s, n=n: s.rolling(n, min_periods=n).var(ddof=1))
        beta = cov / var.replace(0, np.nan)
        out[f"style_beta_{n}"] = beta
        idio = ret - beta * rb
        out[f"style_idio_vol_{n}"] = idio.groupby(df["symbol"]).transform(
            lambda s, n=n: s.rolling(n, min_periods=n).std(ddof=1))
        if n == 20:
            out["style_residual_ret_20"] = idio.groupby(df["symbol"]).transform(
                lambda s: s.rolling(20, min_periods=20).sum())

    # ── risk ───────────────────────────────────────────────────────────────
    out["risk_fac_longterm_reversal"] = g["close"].transform(
        lambda s: s.rolling(756, min_periods=252).rank(pct=True))

    res = pd.DataFrame(out)
    res.index = df.index
    res.insert(0, "symbol", df["symbol"].to_numpy())
    res.insert(1, "dt", df["dt"].to_numpy())
    return res
