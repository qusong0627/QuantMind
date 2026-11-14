#!/usr/bin/env python3
"""「量价-广度背离」体系证伪探针。

验证对象（视频口径的 5 个指标）：
  1) 涨家占比（广度）：指数创新高但涨家占比低 → 熊市反弹，不是反转
  2) TRIN = (涨家/跌家) / (涨额/跌额)：TRIN 极低 + 广度低 → 资金畸形抱团
  3) 额价差 = 涨额占比 − 涨家占比：极端正 → 钱挤在少数股，不可持续
  4) TRIN_额「上涨股人均成交额是下跌股的 N 倍」→ 恒等于 1/TRIN（非独立证据）
  5) McClellan 摆动（EMA19−EMA39 of 涨跌净家数）：指数涨而 MCL 缩 → 动能衰竭

与 ``trin_study.py`` / ``breadth_regime.py`` 的分工：
- 那两份测的是**单指标**连续 IC 与单侧尾部触发；
- 本文件测的是**合取条件**（指数新高 ∧ 广度差 ∧ 额价差极端 …）——
  也就是「五个指标互相验证」这个说法本身。

纪律：
- 阈值全部是**绝对阈值**（39%、+20pp、TRIN<0.5），不使用全样本分位，
  因此不存在阈值层面的前视；同时给出阈值敏感性表，避免「挑到显著为止」。
- 事件段去重：连续触发的日样本折叠为 ``n_episodes``，后者才是独立样本量。
- 显著性用 Newey-West HAC t（滞后 = 预测窗口）。
- 前后两半样本必须同向才算通过；``n_episodes < 10`` 直接判「样本不足」。

用法::

    python -m backend.scripts.breadth_divergence_probe
    python -m backend.scripts.breadth_divergence_probe --current
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

log = logging.getLogger("breadth_divergence_probe")


# --------------------------------------------------------------------------
# 统计小工具（不依赖 scipy：宿主 /usr/bin/python3 没有装）
# --------------------------------------------------------------------------
def forward_return(price: pd.Series, horizon: int) -> pd.Series:
    """未来 horizon 日收益（%）。"""
    return (price.shift(-horizon) / price - 1.0) * 100.0


def newyork_west_tstat(y: np.ndarray, x: np.ndarray, lags: int) -> float:
    """单变量 OLS 斜率的 Newey-West HAC t 值（含截距）。"""
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    n = len(y)
    if n <= lags + 2 or np.nanstd(x) == 0:
        return float("nan")
    X = np.column_stack([np.ones(n), x])
    XtX_inv = np.linalg.pinv(X.T @ X)
    beta = XtX_inv @ X.T @ y
    resid = y - X @ beta
    s = X * resid[:, None]
    omega = s.T @ s
    for lag in range(1, int(lags) + 1):
        w = 1.0 - lag / (lags + 1.0)
        gamma = s[lag:].T @ s[:-lag]
        omega += w * (gamma + gamma.T)
    cov = XtX_inv @ omega @ XtX_inv
    se = float(np.sqrt(max(cov[1, 1], 0.0)))
    return float(beta[1] / se) if se > 0 else float("nan")


def spearman(a: pd.Series, b: pd.Series) -> float:
    """秩相关（pandas 秩 + Pearson，不需要 scipy）。"""
    d = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna()
    if len(d) < 30:
        return float("nan")
    return float(d["a"].rank().corr(d["b"].rank()))


PANEL_PATH = Path("data/quantdb/5_technical_derived/market_breadth/trin_daily.parquet")
TARGETS = {"000001.SH": "上证指数", "000300.SH": "沪深300", "000852.SH": "中证1000"}
HORIZONS = (1, 5, 10, 20, 40)
SPLIT_DATE = pd.Timestamp("2021-07-01")
MIN_EPISODES = 10


# --------------------------------------------------------------------------
# 特征
# --------------------------------------------------------------------------
def build_features(panel: pd.DataFrame) -> pd.DataFrame:
    """原始面板 → 视频口径的特征（全部为 T 日收盘可得）。"""
    df = panel.set_index("date").sort_index().copy()
    df["up_share"] = df["n_up"] / df["n_total"] * 100.0
    df["amt_share"] = df["amt_up"] / df["amt_total"] * 100.0
    df["amt_gap_pp"] = df["amt_share"] - df["up_share"]
    # 视频里的「上涨股人均成交额 / 下跌股人均成交额」= 1 / TRIN，恒等，不是新信息
    df["per_capita_ratio"] = 1.0 / df["trin_amt"]
    # 额价差与广度、TRIN 的恒等关系（用于展示冗余度）
    df["amt_gap_recon"] = ((df["n_up"] / df["n_down"]) / df["trin_amt"]) / (
        1.0 + (df["n_up"] / df["n_down"]) / df["trin_amt"]
    )
    df["amt_gap_recon_pp"] = df["amt_gap_recon"] * 100.0 - df["up_share"]
    # MCL 5 日变化（「MCL 缩」）
    df["mcl_chg5"] = df["mcclellan"] - df["mcclellan"].shift(5)
    for sym in TARGETS:
        c = df[f"close_{sym}"]
        df[f"ret5_{sym}"] = c / c.shift(5) - 1.0
        df[f"ma120_{sym}"] = c.rolling(120, min_periods=120).mean()
        df[f"ma250_{sym}"] = c.rolling(250, min_periods=250).mean()
        df[f"bear_{sym}"] = c < df[f"ma250_{sym}"]
        df[f"stage20_{sym}"] = c >= c.rolling(20, min_periods=20).max() - 1e-9
        for n in (60, 120, 250):
            df[f"high{n}_{sym}"] = c >= c.rolling(n, min_periods=n).max() - 1e-9
    return df


def add_events(df: pd.DataFrame) -> pd.DataFrame:
    """视频口径的事件定义（绝对阈值，T 日收盘即可判定）。"""
    weak = df["up_share"] < 40.0
    gap_hi = df["amt_gap_pp"] > 20.0
    trin_low = df["trin_amt"] < 0.5
    mcl_shrink = df["mcl_chg5"] < 0
    out = df.copy()
    out["ev_weak_breadth"] = weak
    out["ev_gap_hi"] = gap_hi
    out["ev_trin_low"] = trin_low
    out["ev_mcl_shrink"] = mcl_shrink
    for sym in TARGETS:
        hi = out[f"high250_{sym}"]
        out[f"ev_newhigh_{sym}"] = hi
        out[f"ev_newhigh_weak_{sym}"] = hi & weak
        out[f"ev_newhigh_weak_gap_{sym}"] = hi & weak & gap_hi
        out[f"ev_newhigh_weak_trin_{sym}"] = hi & weak & trin_low
        out[f"ev_newhigh_weak_mcl_{sym}"] = hi & weak & mcl_shrink
        out[f"ev_combo_{sym}"] = hi & weak & gap_hi & trin_low & mcl_shrink
        out[f"ev_strong_breadth_{sym}"] = hi & (out["up_share"] > 60.0)
        # 视频说的「熊市反弹」：指数在 250 日均线下方的阶段新高
        stage = out[f"stage20_{sym}"] & out[f"bear_{sym}"]
        out[f"ev_bear_stage_{sym}"] = stage
        out[f"ev_bear_stage_weak_{sym}"] = stage & weak
        out[f"ev_bear_stage_weak_gap_{sym}"] = stage & weak & gap_hi
        out[f"ev_bear_stage_combo_{sym}"] = (
            stage & weak & gap_hi & trin_low & mcl_shrink
        )
        out[f"ev_bear_stage_strong_{sym}"] = stage & (out["up_share"] > 60.0)
    return out


# --------------------------------------------------------------------------
# 事件统计
# --------------------------------------------------------------------------
def _episodes(mask: pd.Series) -> int:
    """连续触发段数（上升沿计数）。"""
    m = mask.fillna(False).to_numpy(dtype=bool)
    if len(m) == 0:
        return 0
    prev = np.concatenate([[False], m[:-1]])
    return int((m & ~prev).sum())


def _path_stats(
    close: pd.Series, horizon: int
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """未来 horizon 日内的最大涨幅（MFE）与最大跌幅（MAE，负数）。"""
    fwd = close.shift(-horizon) / close - 1.0
    mfe = pd.Series(np.nan, index=close.index)
    mae = pd.Series(np.nan, index=close.index)
    vals = close.to_numpy(dtype=float)
    for i in range(len(vals) - horizon):
        path = vals[i + 1 : i + horizon + 1] / vals[i] - 1.0
        if np.isfinite(path).all():
            mfe.iloc[i] = float(path.max())
            mae.iloc[i] = float(path.min())
    return mfe, mae, fwd


@dataclass(frozen=True)
class EventStat:
    n_days: int
    n_episodes: int
    mean: float
    median: float
    win: float
    base_mean: float
    delta: float
    t_nw: float
    p_drop5: float
    base_p_drop5: float
    mae: float
    base_mae: float


def event_stats(
    df: pd.DataFrame,
    mask: pd.Series,
    sym: str,
    horizon: int,
    base_mask: pd.Series | None = None,
) -> EventStat:
    close = df[f"close_{sym}"]
    y = forward_return(close, horizon) / 100.0
    _, mae, _ = _path_stats(close, horizon)
    m = mask.fillna(False).to_numpy(dtype=bool)
    ok = np.isfinite(y.to_numpy(dtype=float)) & np.isfinite(mae.to_numpy(dtype=float))
    ev = m & ok
    base = (
        base_mask.fillna(False).to_numpy(dtype=bool)
        if base_mask is not None
        else np.ones(len(df), dtype=bool)
    )
    base = base & ok
    if ev.sum() < 3:
        return EventStat(
            int(ev.sum()),
            _episodes(mask),
            np.nan,
            np.nan,
            np.nan,
            float(y[base].mean()) if base.any() else np.nan,
            np.nan,
            np.nan,
            np.nan,
            np.nan,
            np.nan,
            np.nan,
        )
    t = newyork_west_tstat(
        y.to_numpy(dtype=float)[ok], m[ok].astype(float), lags=horizon
    )
    return EventStat(
        n_days=int(ev.sum()),
        n_episodes=_episodes(mask),
        mean=float(y.to_numpy()[ev].mean() * 100),
        median=float(np.median(y.to_numpy()[ev]) * 100),
        win=float((y.to_numpy()[ev] > 0).mean()),
        base_mean=float(y.to_numpy()[base].mean() * 100),
        delta=float((y.to_numpy()[ev].mean() - y.to_numpy()[base].mean()) * 100),
        t_nw=float(t),
        p_drop5=float((mae.to_numpy()[ev] < -0.05).mean()),
        base_p_drop5=float((mae.to_numpy()[base] < -0.05).mean()),
        mae=float(mae.to_numpy()[ev].mean() * 100),
        base_mae=float(mae.to_numpy()[base].mean() * 100),
    )


def _md_table(df: pd.DataFrame) -> str:
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, row in df.iterrows():
        cells = []
        for v in row:
            if isinstance(v, (float, np.floating)):
                cells.append("" if not np.isfinite(v) else f"{v:.3g}")
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------
def redundancy_check(df: pd.DataFrame) -> pd.DataFrame:
    """五个指标到底有几个自由度：看彼此秩相关。"""
    cols = {
        "涨家占比": "up_share",
        "TRIN": "trin_amt",
        "额价差pp": "amt_gap_pp",
        "1/TRIN(人均额比)": "per_capita_ratio",
        "MCL5日变化": "mcl_chg5",
    }
    sub = df[list(cols.values())].dropna()
    corr = sub.rank().corr()
    corr.index = list(cols.keys())
    corr.columns = list(cols.keys())
    return corr.round(3)


def identity_check(df: pd.DataFrame) -> pd.DataFrame:
    """额价差是否被 涨家占比 + TRIN 完全决定；1/TRIN 是否恒等于人均额比。"""
    d = df[["amt_gap_pp", "amt_gap_recon_pp", "per_capita_ratio", "trin_amt"]].dropna()
    gap_err = float((d["amt_gap_pp"] - d["amt_gap_recon_pp"]).abs().max())
    inv_err = float((d["per_capita_ratio"] * d["trin_amt"] - 1.0).abs().max())
    return pd.DataFrame(
        [
            {
                "check": "额价差 = f(涨家占比, TRIN) 最大重构误差(pp)",
                "value": round(gap_err, 6),
            },
            {"check": "1/TRIN × TRIN − 1 最大误差", "value": round(inv_err, 12)},
            {
                "check": "1/TRIN 与 TRIN 秩相关",
                "value": round(spearman(d["per_capita_ratio"], d["trin_amt"]), 4),
            },
        ]
    )


def event_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for sym, name in TARGETS.items():
        events = [
            ("指数250日新高", f"ev_newhigh_{sym}"),
            ("新高 ∧ 涨家<40%", f"ev_newhigh_weak_{sym}"),
            ("新高 ∧ 涨家<40% ∧ 额价差>20pp", f"ev_newhigh_weak_gap_{sym}"),
            ("新高 ∧ 涨家<40% ∧ TRIN<0.5", f"ev_newhigh_weak_trin_{sym}"),
            ("新高 ∧ 涨家<40% ∧ MCL缩", f"ev_newhigh_weak_mcl_{sym}"),
            ("五条件合取", f"ev_combo_{sym}"),
            ("对照：新高 ∧ 涨家>60%", f"ev_strong_breadth_{sym}"),
        ]
        for label, col in events:
            for h in (5, 20, 40):
                s = event_stats(df, df[col], sym, h)
                rows.append(
                    {
                        "target": name,
                        "event": label,
                        "h": h,
                        "n_days": s.n_days,
                        "n_episodes": s.n_episodes,
                        "mean_%": round(s.mean, 2) if np.isfinite(s.mean) else np.nan,
                        "median_%": round(s.median, 2)
                        if np.isfinite(s.median)
                        else np.nan,
                        "win": s.win,
                        "base_%": round(s.base_mean, 2),
                        "delta_%": round(s.delta, 2)
                        if np.isfinite(s.delta)
                        else np.nan,
                        "t_nw": round(s.t_nw, 2) if np.isfinite(s.t_nw) else np.nan,
                        "p_drop5": s.p_drop5,
                        "base_p_drop5": round(s.base_p_drop5, 3),
                    }
                )
    return pd.DataFrame(rows)


def split_table(df: pd.DataFrame) -> pd.DataFrame:
    """前后两半样本的同向性检查。"""
    rows = []
    halves = {
        "前半 2016-2021H1": df.index < SPLIT_DATE,
        "后半 2021H2-2026": df.index >= SPLIT_DATE,
    }
    for sym, name in TARGETS.items():
        for label, col in (
            ("新高 ∧ 涨家<40%", f"ev_newhigh_weak_{sym}"),
            ("熊市阶段新高 ∧ 涨家<40%", f"ev_bear_stage_weak_{sym}"),
            ("五条件合取", f"ev_combo_{sym}"),
        ):
            row = {"target": name, "event": label}
            base_col = f"ev_bear_stage_{sym}" if "熊市" in label else None
            for hname, hmask in halves.items():
                sub = df[hmask]
                s = event_stats(
                    sub,
                    sub[col],
                    sym,
                    20,
                    base_mask=sub[base_col] if base_col else None,
                )
                row[f"{hname} n_ep"] = s.n_episodes
                row[f"{hname} delta_%"] = (
                    round(s.delta, 2) if np.isfinite(s.delta) else np.nan
                )
            rows.append(row)
    return pd.DataFrame(rows)


def threshold_surface(df: pd.DataFrame, sym: str = "000001.SH") -> pd.DataFrame:
    """阈值敏感性：新高 ∧ 涨家<thr 的未来 20 日超额（对新高基准）。"""
    base_mask = df[f"ev_newhigh_{sym}"]
    base = event_stats(df, base_mask, sym, 20)
    rows = []
    for thr in (30, 35, 40, 45, 50, 55, 60):
        mask = df[f"high250_{sym}"] & (df["up_share"] < thr)
        s = event_stats(df, mask, sym, 20)
        rows.append(
            {
                "涨家阈值": f"<{thr}%",
                "n_days": s.n_days,
                "n_ep": s.n_episodes,
                "mean_%": round(s.mean, 2) if np.isfinite(s.mean) else np.nan,
                "delta_vs_新高_%": (
                    round(s.mean - base.mean, 2)
                    if np.isfinite(s.mean) and np.isfinite(base.mean)
                    else np.nan
                ),
            }
        )
    for thr in (10, 15, 20, 25, 30):
        mask = df[f"high250_{sym}"] & (df["amt_gap_pp"] > thr)
        s = event_stats(df, mask, sym, 20)
        rows.append(
            {
                "涨家阈值": f"额价差>{thr}pp",
                "n_days": s.n_days,
                "n_ep": s.n_episodes,
                "mean_%": round(s.mean, 2) if np.isfinite(s.mean) else np.nan,
                "delta_vs_新高_%": (
                    round(s.mean - base.mean, 2)
                    if np.isfinite(s.mean) and np.isfinite(base.mean)
                    else np.nan
                ),
            }
        )
    return pd.DataFrame(rows)


def conditional_ic(df: pd.DataFrame) -> pd.DataFrame:
    """只看「指数 250 日新高」的样本内，各指标对未来的秩相关。"""
    rows = []
    feats = {
        "涨家占比": "up_share",
        "TRIN": "trin_amt",
        "额价差pp": "amt_gap_pp",
        "MCL5日变化": "mcl_chg5",
    }
    for sym, name in TARGETS.items():
        sub = df[df[f"high250_{sym}"]]
        for h in (5, 20):
            y = forward_return(sub[f"close_{sym}"], h)
            for label, col in feats.items():
                rho = spearman(sub[col], y)
                n = int(pd.concat([sub[col], y], axis=1).dropna().shape[0])
                rows.append(
                    {
                        "target": name,
                        "h": h,
                        "signal": label,
                        "n": n,
                        "IC": round(rho, 3) if np.isfinite(rho) else np.nan,
                        "t_deflated": (
                            round(rho * np.sqrt(max(n / h - 2, 1)), 2)
                            if np.isfinite(rho)
                            else np.nan
                        ),
                    }
                )
    return pd.DataFrame(rows)


def current_reading(df: pd.DataFrame) -> pd.DataFrame:
    last = df.iloc[-1]
    rows = [
        {"item": "日期", "value": f"{df.index[-1]:%Y-%m-%d}"},
        {"item": "涨家占比", "value": round(float(last["up_share"]), 2)},
        {"item": "涨额占比", "value": round(float(last["amt_share"]), 2)},
        {"item": "额价差(pp)", "value": round(float(last["amt_gap_pp"]), 2)},
        {"item": "TRIN(当日)", "value": round(float(last["trin_amt"]), 3)},
        {"item": "TRIN 10日EMA", "value": round(float(last["trin_ema10"]), 3)},
        {"item": "1/TRIN 人均额比", "value": round(float(last["per_capita_ratio"]), 2)},
        {"item": "MCL", "value": round(float(last["mcclellan"]), 1)},
        {"item": "MCL 5日变化", "value": round(float(last["mcl_chg5"]), 1)},
    ]
    for sym, name in TARGETS.items():
        rows.append(
            {
                "item": f"{name}距250日高(%)",
                "value": round(
                    float(
                        last[f"close_{sym}"]
                        / df[f"close_{sym}"]
                        .rolling(250, min_periods=250)
                        .max()
                        .iloc[-1]
                        * 100
                        - 100
                    ),
                    2,
                ),
            }
        )
    return pd.DataFrame(rows)


def bear_event_table(df: pd.DataFrame) -> pd.DataFrame:
    """「熊市反弹」口径：指数在 250 日线下方、20 日阶段新高。

    基准取同一形态内（bear_stage）而不是全样本 —— 否则测到的是熊市 beta。
    """
    rows = []
    for sym, name in TARGETS.items():
        base = df[f"ev_bear_stage_{sym}"]
        for label, col in (
            ("熊市阶段新高", f"ev_bear_stage_{sym}"),
            ("熊市阶段新高 ∧ 涨家<40%", f"ev_bear_stage_weak_{sym}"),
            ("熊市阶段新高 ∧ 涨家<40% ∧ 额价差>20pp", f"ev_bear_stage_weak_gap_{sym}"),
            ("熊市阶段新高 ∧ 五条件合取", f"ev_bear_stage_combo_{sym}"),
            ("对照：熊市阶段新高 ∧ 涨家>60%", f"ev_bear_stage_strong_{sym}"),
        ):
            for h in (5, 20, 40):
                s = event_stats(df, df[col], sym, h, base_mask=base)
                rows.append(
                    {
                        "target": name,
                        "event": label,
                        "h": h,
                        "n_days": s.n_days,
                        "n_episodes": s.n_episodes,
                        "mean_%": round(s.mean, 2) if np.isfinite(s.mean) else np.nan,
                        "median_%": round(s.median, 2)
                        if np.isfinite(s.median)
                        else np.nan,
                        "win": s.win,
                        "base_%": round(s.base_mean, 2),
                        "delta_%": round(s.delta, 2)
                        if np.isfinite(s.delta)
                        else np.nan,
                        "t_nw": round(s.t_nw, 2) if np.isfinite(s.t_nw) else np.nan,
                        "p_drop5": s.p_drop5,
                        "base_p_drop5": round(s.base_p_drop5, 3),
                    }
                )
    return pd.DataFrame(rows)


def incremental_gap_test(df: pd.DataFrame, threshold: float = 20.0) -> pd.DataFrame:
    """额价差极端是否只是「近期涨多了」的代理：控制指数动量后看剩余效应。"""
    rows = []
    for sym, name in TARGETS.items():
        price = df[f"close_{sym}"]
        for h in (20, 40):
            y = forward_return(price, h).to_numpy(dtype=float)
            for ctrl_n in (0, 20, 60, 120):
                if ctrl_n == 0:
                    X = np.ones((len(df), 1))
                    ctrl = "无"
                else:
                    mom = (price / price.shift(ctrl_n) - 1).to_numpy(dtype=float)
                    X = np.column_stack([np.ones(len(df)), mom])
                    ctrl = f"指数{ctrl_n}日动量"
                ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
                beta = np.linalg.lstsq(X[ok], y[ok], rcond=None)[0]
                r = np.full(len(df), np.nan)
                r[ok] = y[ok] - X[ok] @ beta
                m = (df["amt_gap_pp"] > threshold).to_numpy(dtype=bool) & ok
                if m.sum() < 3:
                    continue
                t = newyork_west_tstat(r[ok], m[ok].astype(float), lags=h)
                rows.append(
                    {
                        "target": name,
                        "h": h,
                        "control": ctrl,
                        "n_days": int(m.sum()),
                        "delta_%": round(
                            float(np.nanmean(r[m]) - np.nanmean(r[ok])), 2
                        ),
                        "t_nw": round(t, 2) if np.isfinite(t) else np.nan,
                    }
                )
    return pd.DataFrame(rows)


def expanding_decile_test(df: pd.DataFrame) -> pd.DataFrame:
    """用扩张分位（不用未来分布）复核额价差两端，避免绝对阈值挑选。"""
    rank = df["amt_gap_pp"].expanding(min_periods=500).rank(pct=True)
    rows = []
    for sym, name in TARGETS.items():
        for side, mask in (("前10%", rank > 0.9), ("后10%", rank < 0.1)):
            for h in (5, 20, 40):
                s = event_stats(df, mask, sym, h)
                rows.append(
                    {
                        "target": name,
                        "side": side,
                        "h": h,
                        "n_days": s.n_days,
                        "n_episodes": s.n_episodes,
                        "delta_%": round(s.delta, 2)
                        if np.isfinite(s.delta)
                        else np.nan,
                        "t_nw": round(s.t_nw, 2) if np.isfinite(s.t_nw) else np.nan,
                    }
                )
    return pd.DataFrame(rows)


def event_dates(df: pd.DataFrame, sym: str = "000001.SH") -> pd.DataFrame:
    """把关键事件的实际日期打出来 —— 判断是不是同一段行情反复计数。"""
    cols = {
        "新高∧涨家<40%": f"ev_newhigh_weak_{sym}",
        "新高∧涨家<40%∧额价差>20pp": f"ev_newhigh_weak_gap_{sym}",
        "熊市阶段新高∧涨家<40%": f"ev_bear_stage_weak_{sym}",
        "熊市阶段新高∧额价差>20pp": f"ev_bear_stage_weak_gap_{sym}",
    }
    out = pd.DataFrame(index=df.index)
    for label, col in cols.items():
        out[label] = df[col].fillna(False).map({True: "✔", False: ""})
    out["涨家%"] = df["up_share"].round(1)
    out["额价差pp"] = df["amt_gap_pp"].round(1)
    out["TRIN"] = df["trin_amt"].round(2)
    out["指数后20日%"] = (forward_return(df[f"close_{sym}"], 20)).round(2)
    mask = out[list(cols.keys())].apply(lambda s: (s == "✔").any(), axis=1)
    return out[mask]


def latest_tail(days: int = 15) -> pd.DataFrame:
    """面板可能落后几天：直接从原始日线补算最近 ``days`` 个交易日的广度。"""
    root = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "data/quantdb"))
    kdir = root / "1_kline_data" / "daily_backward"
    tags = sorted(os.listdir(kdir))[-(days + 2) :]
    frames = {}
    for tag in tags:
        t = pq.read_table(
            kdir / tag / "data.parquet", columns=["symbol", "close", "amount"]
        )
        frames[pd.Timestamp(tag.split("=")[1])] = t.to_pandas().set_index("symbol")
    rows = []
    prev = None
    for d in sorted(frames):
        cur = frames[d]
        if prev is not None:
            j = cur.join(prev[["close"]], rsuffix="_prev", how="inner").dropna()
            up = j["close"] > j["close_prev"]
            down = j["close"] < j["close_prev"]
            n_up, n_down = int(up.sum()), int(down.sum())
            amt_up = float(j.loc[up, "amount"].sum())
            amt_down = float(j.loc[down, "amount"].sum())
            rows.append(
                {
                    "date": d,
                    "n_up": n_up,
                    "n_down": n_down,
                    "up_share": n_up / len(j) * 100,
                    "amt_gap_pp": (amt_up / (amt_up + amt_down) - n_up / len(j)) * 100,
                    "trin_amt": (n_up / n_down) / (amt_up / amt_down)
                    if n_down and amt_down
                    else np.nan,
                    "net": n_up - n_down,
                }
            )
        prev = cur
    out = pd.DataFrame(rows).set_index("date")
    panel = pd.read_parquet(PANEL_PATH).set_index("date").sort_index()
    net = pd.concat([panel["n_up"] - panel["n_down"], out["net"]]).astype(float)
    mcc = (
        net.ewm(span=19, adjust=False, min_periods=19).mean()
        - net.ewm(span=39, adjust=False, min_periods=39).mean()
    )
    trin_ema = (
        pd.concat([panel["trin_amt"], out["trin_amt"]])
        .ewm(span=10, adjust=False, min_periods=10)
        .mean()
    )
    out["mcclellan"] = mcc.reindex(out.index)
    out["mcl_chg5"] = mcc.reindex(out.index) - mcc.shift(5).reindex(out.index)
    out["trin_ema10"] = trin_ema.reindex(out.index)
    return out


def index_positions(days: int = 270) -> pd.DataFrame:
    """最新指数位置（距 250 日高、MA120/MA250、是否 20 日新高）。"""
    root = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "data/quantdb"))
    idir = root / "1_kline_data" / "index_daily"
    vals: dict[str, dict[pd.Timestamp, float]] = {s: {} for s in TARGETS}
    for tag in sorted(os.listdir(idir))[-days:]:
        t = pq.read_table(
            idir / tag / "data.parquet", columns=["symbol", "close"]
        ).to_pandas()
        for sym in TARGETS:
            r = t[t["symbol"] == sym]
            if not r.empty:
                vals[sym][pd.Timestamp(tag.split("=")[1])] = float(r["close"].iloc[0])
    rows = []
    for sym, name in TARGETS.items():
        s = pd.Series(vals[sym]).sort_index()
        last = float(s.iloc[-1])
        rows.append(
            {
                "target": name,
                "date": f"{s.index[-1]:%Y-%m-%d}",
                "close": f"{last:.2f}",
                "MA120": f"{float(s.rolling(120).mean().iloc[-1]):.2f}",
                "MA250": f"{float(s.rolling(250).mean().iloc[-1]):.2f}",
                "距250日高_%": round(
                    last / float(s.rolling(250, min_periods=250).max().iloc[-1]) * 100
                    - 100,
                    2,
                ),
                "20日新高": bool(
                    last >= float(s.rolling(20, min_periods=20).max().iloc[-1]) - 1e-9
                ),
                "站上250日线": bool(last >= float(s.rolling(250).mean().iloc[-1])),
            }
        )
    return pd.DataFrame(rows)


def build_report(df: pd.DataFrame) -> str:
    lines: list[str] = []
    a = lines.append
    a("# 量价-广度背离体系：证伪报告")
    a("")
    a(
        f"数据区间 {df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d}，{len(df)} 个交易日。"
        "数据源 QuantDB 本地 parquet（后复权日线，全 A 含北交所）。"
    )
    a("")
    a("## 零、先看这五个指标是不是独立的")
    a("")
    a(_md_table(identity_check(df)))
    a("")
    a("**「TRIN_额 / 人均成交额倍数」不是第五个指标，它就是 1/TRIN。**")
    a("额价差也不是独立指标：给定涨家占比与 TRIN，涨额占比（进而额价差）被完全决定。")
    a("")
    a("秩相关矩阵：")
    a("")
    corr = redundancy_check(df)
    a(_md_table(corr.reset_index().rename(columns={"index": ""})))
    a("")
    a("## 一、事件研究（绝对阈值，含重叠样本 HAC t）")
    a("")
    a(_md_table(event_table(df)))
    a("")
    a("`delta_%` = 事件日未来收益均值 − 全样本均值；`n_episodes` 是独立事件段数。")
    a("")
    a("## 一·B、「熊市反弹」口径（指数在 250 日线下方 + 20 日阶段新高）")
    a("")
    a(
        "基准 = 同一「熊市阶段新高」形态（不是全样本），这样测的是**广度是否提供增量信息**。"
    )
    a("")
    a(_md_table(bear_event_table(df)))
    a("")
    a("## 一·C、关键事件的实际日期")
    a("")
    a("（上证口径；同一天多列打勾说明条件重叠。看看这些事件是不是集中在同一段行情。）")
    a("")
    ed = event_dates(df)
    a(_md_table(ed.reset_index().rename(columns={"date": "日期"})))
    a("")
    a("## 二、前后两半同向性（20 日）")
    a("")
    a(_md_table(split_table(df)))
    a("")
    a("## 三、阈值敏感性（上证指数，未来 20 日）")
    a("")
    a(_md_table(threshold_surface(df)))
    a("")
    a("## 四、只在「指数 250 日新高」样本内的条件 IC")
    a("")
    a(_md_table(conditional_ic(df)))
    a("")
    a("## 四·B、额价差极端（>20pp）控制指数动量后的剩余效应")
    a("")
    a("若控制后 delta 与 t 明显衰减，说明额价差只是「涨多了」的代理，不是独立信息。")
    a("")
    a(_md_table(incremental_gap_test(df)))
    a("")
    a("## 四·C、额价差扩张分位两端（避免绝对阈值挑选）")
    a("")
    a(_md_table(expanding_decile_test(df)))
    a("")
    a("## 五、当前读数（原始日线补算到最新交易日）")
    a("")
    try:
        tail = latest_tail(8)
        tail = tail.reset_index()
        tail["date"] = tail["date"].dt.strftime("%Y-%m-%d")
        tail = tail.rename(columns={"date": "日期"})
        for c in ("up_share", "amt_gap_pp"):
            tail[c] = tail[c].round(2)
        for c in ("trin_amt", "trin_ema10", "mcclellan", "mcl_chg5"):
            tail[c] = tail[c].round(2)
        a(_md_table(tail))
        a("")
        a(_md_table(index_positions()))
    except Exception as exc:  # 数据目录缺失时不让整份报告失败
        a(f"（原始日线补算失败：{exc}）")
    a("")
    a(
        "注：视频里「40 分钟放量 6633 亿」是盘中口径，日线无法复现；"
        "本报告只验证收盘口径的广度和量价结构。"
    )
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="量价-广度背离体系证伪探针")
    p.add_argument("--panel", default=str(PANEL_PATH))
    p.add_argument("--out", default=None)
    p.add_argument("--current", action="store_true")
    p.add_argument(
        "--tail", action="store_true", help="只打印原始日线补算的最新广度与指数位置"
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args(argv)
    if args.tail:
        t = latest_tail(10).reset_index()
        num = t.select_dtypes("number").columns
        t[num] = t[num].round(3)
        print(t.to_string(index=False))
        print()
        print(index_positions().to_string(index=False))
        return 0
    panel = pd.read_parquet(args.panel)
    df = add_events(build_features(panel))
    if args.current:
        print(_md_table(current_reading(df)))
        return 0
    report = build_report(df)
    out = (
        Path(args.out)
        if args.out
        else Path(args.panel).parent / "breadth_divergence.md"
    )
    out.write_text(report, encoding="utf-8")
    log.info("报告写出 %s", out)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
