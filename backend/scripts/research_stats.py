"""研究统计小工具 —— 重叠样本的显著性、块自助法、HAC t 值。

为什么要单独一个模块：TRIN/广度这类日频信号做 N 日预测时，相邻样本的
预测窗口互相重叠（1 个 20 日窗口 = 20 个日样本），朴素 t 值会被高估约
``sqrt(N)`` 倍。所有「信号有效」的结论必须在这里过一道，否则就是自欺。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats


def spearman_ic(x: pd.Series, y: pd.Series) -> tuple[float, int]:
    """秩相关与有效样本数。缺失值成对删除。"""
    d = pd.concat([x.rename("x"), y.rename("y")], axis=1).dropna()
    if len(d) < 30:
        return float("nan"), len(d)
    return float(stats.spearmanr(d["x"], d["y"]).statistic), len(d)


def overlap_deflated_t(ic: float, n: int, horizon: int) -> float:
    """重叠样本下秩相关的保守 t 值。

    独立样本 n_eff ≈ n / horizon；t ≈ ic * sqrt(n_eff - 2) / sqrt(1 - ic²)。
    这是**下界**估计：日频信号即使窗口不重叠也仍有自相关，真实显著度只会更低。
    """
    n_eff = max(n / max(horizon, 1), 3.0)
    if not np.isfinite(ic) or abs(ic) >= 1:
        return float("nan")
    return float(ic * np.sqrt(n_eff - 2) / np.sqrt(1 - ic**2))


def newyork_west_tstat(y: np.ndarray, x: np.ndarray, lags: int) -> float:
    """单变量 OLS 斜率的 Newey-West HAC t 值（含截距）。"""
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    ok = np.isfinite(y) & np.isfinite(x)
    y, x = y[ok], x[ok]
    if len(y) < 30:
        return float("nan")
    X = np.column_stack([np.ones_like(x), x])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    n, k = X.shape
    xtx_inv = np.linalg.inv(X.T @ X)
    s = (X * resid[:, None]).T @ (X * resid[:, None])
    for lag in range(1, lags + 1):
        w = 1.0 - lag / (lags + 1.0)
        gamma = (X[lag:] * resid[lag:, None]).T @ (X[:-lag] * resid[:-lag, None])
        s += w * (gamma + gamma.T)
    cov = xtx_inv @ s @ xtx_inv
    se = np.sqrt(np.diag(cov))
    return float(beta[1] / se[1]) if se[1] > 0 else float("nan")


def block_bootstrap_corr(
    x: np.ndarray,
    y: np.ndarray,
    block: int = 20,
    n_boot: int = 2000,
    seed: int = 20261027,
) -> tuple[float, float, float]:
    """块自助法相关系数置信区间（保留自相关结构）。

    返回 ``(point, lo, hi)``。块长建议 >= 预测窗口，否则置信区间同样偏窄。
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    n = len(x)
    if n < block * 2:
        return float("nan"), float("nan"), float("nan")
    point = float(np.corrcoef(x, y)[0, 1])
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    boot = np.empty(n_boot)
    for b in range(n_boot):
        starts = rng.integers(0, n - block + 1, size=n_blocks)
        idx = np.concatenate([np.arange(s, s + block) for s in starts])[:n]
        boot[b] = np.corrcoef(x[idx], y[idx])[0, 1]
    lo, hi = np.nanpercentile(boot, [2.5, 97.5])
    return point, float(lo), float(hi)


def block_group_diff(
    y: np.ndarray,
    labels: np.ndarray,
    g1: str,
    g2: str,
    block: int = 20,
    n_boot: int = 2000,
    seed: int = 20261027,
) -> tuple[float, float, float]:
    """两组均值差的块自助法置信区间（保留时间结构）。返回 ``(obs, lo, hi)``。

    块长应 >= 预测窗口，否则重叠样本会把置信区间压窄、把噪声报成显著。
    任一组有效样本不足 30 时返回全 NaN，避免小样本结论。
    """
    y = np.asarray(y, dtype=float)
    labels = np.asarray(labels)
    ok = np.isfinite(y)
    y, labels = y[ok], labels[ok]
    if (labels == g1).sum() < 30 or (labels == g2).sum() < 30:
        return float("nan"), float("nan"), float("nan")
    n = len(y)
    if n < block * 2:
        return float("nan"), float("nan"), float("nan")
    obs = float(y[labels == g1].mean() - y[labels == g2].mean())
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    boot = []
    for _ in range(n_boot):
        starts = rng.integers(0, n - block + 1, size=n_blocks)
        idx = np.concatenate([np.arange(s, s + block) for s in starts])[:n]
        yy, ll = y[idx], labels[idx]
        m1, m2 = yy[ll == g1].mean(), yy[ll == g2].mean()
        if np.isfinite(m1) and np.isfinite(m2):
            boot.append(m1 - m2)
    if not boot:
        return obs, float("nan"), float("nan")
    lo, hi = np.nanpercentile(boot, [2.5, 97.5])
    return obs, float(lo), float(hi)


def forward_return(price: pd.Series, horizon: int) -> pd.Series:
    """t 收盘 → t+horizon 收盘的收益（%）。最后一根会自然产生 NaN。"""
    return (price.shift(-horizon) / price - 1.0) * 100.0


def expanding_pct_rank(values: pd.Series, min_periods: int = 250) -> pd.Series:
    """扩张窗口分位（0~1），只用当日及更早的数据 —— 防前视的唯一正确做法。

    ⚠️ 窗口里带着指标热身期的 NaN（52 周新高新低要 252 天、McClellan 要 39 天）。
    必须先把 NaN 剔掉再求分位：``(x < x[-1]).mean()`` 会把 NaN 当成 False 计入
    分母，热身期分位被严重稀释 —— 实测把 80.3 分的位置从 95% 报成 74%。
    """

    def _rank(x: np.ndarray) -> float:
        cur = x[-1]
        if not np.isfinite(cur):
            return np.nan
        valid = x[np.isfinite(x)]
        return float((valid < cur).mean())

    return values.expanding(min_periods=min_periods).apply(_rank, raw=True)
