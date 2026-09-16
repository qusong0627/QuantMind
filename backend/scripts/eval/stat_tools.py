"""回测体检统计工具箱（T-P4-05a）——**九项检验纯函数唯一实现**。

对标设计《评估与打分体系》§6.1（Bailey & López de Prado 系列方法）：
1 因子回归（CAPM α/t/R²，风格因子可注入）  2 PSR 概率夏普  3 DSR 紧缩夏普（N 试验去胀）
4 MinTRL 最短样本  5 PBO 回测过拟合概率（CSCV）  6 Block Bootstrap 置信区间
7 收益集中度（剔 Top5 日）  8 跨 regime 分段  9 成本敏感性（换手×费率上浮）

纪律：全部纯函数（输入 numpy 数组 → 输出 dict 证据），无 IO、无全局状态。
年化基准 252 交易日；不足样本返回 sufficient=False 而非抛错（E 区判定原料）。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy import stats as _stats

TRADING_DAYS = 252
EULER_MASCHERONI = 0.5772156649015329


def _clean(returns: Any) -> np.ndarray:
    arr = np.asarray(returns, dtype=float).ravel()
    return arr[np.isfinite(arr)]


def _sharpe_per_period(r: np.ndarray) -> float:
    if len(r) < 2:
        return 0.0
    std = float(np.std(r, ddof=1))
    return float(np.mean(r) / std) if std > 0 else 0.0


def _skew_kurt(r: np.ndarray) -> tuple[float, float]:
    if len(r) < 4:
        return 0.0, 3.0
    return float(_stats.skew(r)), float(_stats.kurtosis(r, fisher=False))


# ---------------------------------------------------------------------------
# 1) 因子回归（CAPM + 可选风格因子）
# ---------------------------------------------------------------------------


def factor_regression(
    returns: Any,
    benchmark_returns: Any,
    style_factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """OLS: r_p = α + β·r_m + Σβ_i·f_i + ε → 年化 α、t 值、R²、beta。

    风格因子（市值/价值/动量等）经 ``style_factors`` 注入（等长序列数组）。
    """
    r = _clean(returns)
    m = _clean(benchmark_returns)
    n = min(len(r), len(m))
    if n < 30:
        return {"sufficient": False, "reason": f"样本不足（{n} < 30）", "n": n}
    r, m = r[-n:], m[-n:]
    cols = [np.ones(n), m]
    names = ["const", "benchmark"]
    for name, series in (style_factors or {}).items():
        f = _clean(series)
        if len(f) >= n:
            cols.append(f[-n:])
            names.append(str(name))
    x = np.column_stack(cols)
    coef, _res, _rank, _sv = np.linalg.lstsq(x, r, rcond=None)
    fitted = x @ coef
    resid = r - fitted
    dof = max(1, n - x.shape[1])
    sigma2 = float(resid @ resid) / dof
    try:
        xtx_inv = np.linalg.inv(x.T @ x)
    except np.linalg.LinAlgError:
        xtx_inv = np.linalg.pinv(x.T @ x)
    se = np.sqrt(np.maximum(0.0, sigma2 * np.diag(xtx_inv)))
    # 退化守卫：完美拟合（残差方差≈0）时 se≈0，0/0 会产出伪 t 值——
    # 判为"无证据"（t=0），而不是让浮点噪声决定显著性
    degenerate = se <= np.maximum(1e-12, 1e-12 * np.maximum(np.abs(coef), 1.0))
    t_stats = np.divide(
        coef, se, out=np.zeros_like(coef), where=np.logical_and(se > 0, ~degenerate)
    )
    ss_tot = float(((r - np.mean(r)) ** 2).sum())
    r2 = 1.0 - float(resid @ resid) / ss_tot if ss_tot > 0 else 0.0
    alpha_daily = float(coef[0])
    return {
        "sufficient": True,
        "n": n,
        "alpha_annual": round(alpha_daily * TRADING_DAYS, 6),
        "alpha_t": round(float(t_stats[0]), 4),
        "alpha_significant": bool(abs(float(t_stats[0])) > 2.0),
        "beta": round(float(coef[1]), 4),
        "beta_t": round(float(t_stats[1]), 4),
        "r2": round(r2, 4),
        "style_coefs": {
            names[i]: round(float(coef[i]), 4) for i in range(2, len(names))
        },
    }


# ---------------------------------------------------------------------------
# 2/3) PSR 概率夏普 / DSR 紧缩夏普
# ---------------------------------------------------------------------------


def psr(returns: Any, sr_benchmark_per_period: float = 0.0) -> dict[str, Any]:
    """PSR(SR*)：Sharpe 大于基准（默认 0）的概率（偏度/峰度校正，Bailey-LdP 2012）。"""
    r = _clean(returns)
    n = len(r)
    if n < 10:
        return {"sufficient": False, "reason": f"样本不足（{n} < 10）", "psr": None}
    sr = _sharpe_per_period(r)
    g3, g4 = _skew_kurt(r)
    denom_sq = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr
    if denom_sq <= 0:
        return {
            "sufficient": False,
            "reason": "方差结构退化（偏度峰度组合异常）",
            "psr": None,
        }
    z = (sr - sr_benchmark_per_period) * math.sqrt(n - 1) / math.sqrt(denom_sq)
    return {
        "sufficient": True,
        "n": n,
        "sharpe_per_period": round(sr, 6),
        "skew": round(g3, 4),
        "kurtosis": round(g4, 4),
        "psr": round(float(_stats.norm.cdf(z)), 6),
    }


def deflated_sharpe_ratio(
    returns: Any,
    n_trials: int = 1,
    sr_variance: float | None = None,
) -> dict[str, Any]:
    """DSR：扣除"试了 N 组参数"的期望最大 Sharpe 后的显著性概率。

    ``sr_variance``（试验间 Sharpe 方差）缺省时用本策略 Sharpe 抽样方差近似
    （denominator 结构），并在结果中如实标注 ``variance_estimated``。
    """
    base = psr(returns)
    if not base.get("sufficient"):
        return {"sufficient": False, "reason": base.get("reason"), "dsr": None}
    r = _clean(returns)
    n = len(r)
    sr = float(base["sharpe_per_period"])
    g3, g4 = float(base["skew"]), float(base["kurtosis"])
    denom_sq = max(1e-12, 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr)
    estimated = sr_variance is None
    var_sr = float(sr_variance) if sr_variance is not None else denom_sq / (n - 1)
    trials = max(1, int(n_trials))
    if trials <= 1:
        sr0 = 0.0
    else:
        z1 = float(_stats.norm.ppf(1.0 - 1.0 / trials))
        z2 = float(_stats.norm.ppf(1.0 - 1.0 / (trials * math.e)))
        sr0 = math.sqrt(max(0.0, var_sr)) * (
            (1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2
        )
    z = (sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom_sq)
    return {
        "sufficient": True,
        "n": n,
        "n_trials": trials,
        "sr0_per_period": round(sr0, 6),
        "variance_estimated": estimated,
        "dsr": round(float(_stats.norm.cdf(z)), 6),
        "passes_095": bool(_stats.norm.cdf(z) >= 0.95),
    }


# ---------------------------------------------------------------------------
# 4) MinTRL 最短样本
# ---------------------------------------------------------------------------


def min_track_record_length(
    returns: Any, confidence: float = 0.95, sr_benchmark_per_period: float = 0.0
) -> dict[str, Any]:
    """MinTRL：在给定置信水平下证明 SR>SR* 所需的最少样本（期数/年）。"""
    r = _clean(returns)
    if len(r) < 10:
        return {
            "sufficient": False,
            "reason": f"样本不足（{len(r)} < 10）",
            "min_trl_years": None,
        }
    sr = _sharpe_per_period(r)
    g3, g4 = _skew_kurt(r)
    denom_sq = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr
    gap = sr - sr_benchmark_per_period
    if gap <= 0:
        return {
            "sufficient": False,
            "reason": "SR ≤ 基准（负 Sharpe 无法用有限样本证明）",
            "sharpe_per_period": round(sr, 6),
            "min_trl_years": None,
        }
    z = float(_stats.norm.ppf(confidence))
    periods = 1.0 + max(0.0, denom_sq) * (z / gap) ** 2
    return {
        "sufficient": True,
        "confidence": confidence,
        "sharpe_per_period": round(sr, 6),
        "min_trl_periods": round(periods, 1),
        "min_trl_years": round(periods / TRADING_DAYS, 2),
        "observed_years": round(len(r) / TRADING_DAYS, 2),
        "adequate": bool(len(r) >= periods),
    }


# ---------------------------------------------------------------------------
# 5) PBO 回测过拟合概率（CSCV）
# ---------------------------------------------------------------------------


def pbo_cscv(performance_matrix: Any, n_splits: int = 10) -> dict[str, Any]:
    """CSCV：IS 最优参数组合在 OOS 落到中位数以下的组合占比（Bailey et al. 2015）。

    ``performance_matrix``：T×N（行=时间，列=参数组合的逐期收益）。
    块数取偶数 n_splits（默认 10）；OOS 分位用秩统计（<0.5 视为翻车）。
    """
    mat = np.asarray(performance_matrix, dtype=float)
    if mat.ndim != 2 or mat.shape[1] < 2:
        return {"sufficient": False, "reason": "参数矩阵需要 T×N（N≥2）", "pbo": None}
    t, n = mat.shape
    s = int(n_splits)
    if s % 2 == 1:
        s += 1
    if t < s * 2:
        return {
            "sufficient": False,
            "reason": f"样本过短（T={t} < 2×{s}）",
            "pbo": None,
        }
    blocks = np.array_split(np.arange(t), s)

    def _sharpe(x: np.ndarray) -> float:
        std = float(np.std(x, ddof=1)) if x.size > 1 else 0.0
        return float(np.mean(x) / std) if std > 0 else 0.0

    from itertools import combinations

    half = s // 2
    n_below = 0
    n_total = 0
    for combo in combinations(range(s), half):
        is_idx = np.concatenate([blocks[i] for i in combo])
        oos_idx = np.concatenate([blocks[i] for i in range(s) if i not in combo])
        is_metric = [_sharpe(mat[is_idx, j]) for j in range(n)]
        best = int(np.argmax(is_metric))
        oos_metric = np.array([_sharpe(mat[oos_idx, j]) for j in range(n)])
        rank = float(np.sum(oos_metric < oos_metric[best])) / max(1, n - 1)
        if rank < 0.5:
            n_below += 1
        n_total += 1
    pbo = n_below / max(1, n_total)
    return {
        "sufficient": True,
        "n_params": n,
        "n_splits": s,
        "n_combinations": n_total,
        "pbo": round(pbo, 4),
        "overfit_risk": "high" if pbo >= 0.5 else ("medium" if pbo >= 0.25 else "low"),
    }


# ---------------------------------------------------------------------------
# 6) Block Bootstrap
# ---------------------------------------------------------------------------


def block_bootstrap(
    returns: Any,
    *,
    block_size: int | None = None,
    n_boot: int = 1000,
    confidence: float = 0.90,
    seed: int = 42,
) -> dict[str, Any]:
    """块自助：保留自相关的年化收益/Sharpe 置信区间（跨 0 即"运气嫌疑"证据之一）。"""
    r = _clean(returns)
    n = len(r)
    if n < 30:
        return {
            "sufficient": False,
            "reason": f"样本不足（{n} < 30）",
            "return_ci": None,
        }
    bs = int(block_size or max(2, round(math.sqrt(n))))
    rng = np.random.default_rng(seed)
    n_blocks = int(math.ceil(n / bs))
    ann_returns = np.empty(n_boot)
    sharpes = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, max(1, n - bs + 1), size=n_blocks)
        sample = np.concatenate([r[s : s + bs] for s in starts])[:n]
        ann_returns[i] = float(np.mean(sample)) * TRADING_DAYS
        std = float(np.std(sample, ddof=1))
        sharpes[i] = float(np.mean(sample) / std) if std > 0 else 0.0
    lo, hi = (1.0 - confidence) / 2.0, 1.0 - (1.0 - confidence) / 2.0
    ret_lo, ret_hi = np.quantile(ann_returns, [lo, hi])
    sr_lo, sr_hi = np.quantile(sharpes, [lo, hi])
    return {
        "sufficient": True,
        "n": n,
        "block_size": bs,
        "n_boot": n_boot,
        "confidence": confidence,
        "return_ci": [round(float(ret_lo), 6), round(float(ret_hi), 6)],
        "sharpe_ci": [round(float(sr_lo), 4), round(float(sr_hi), 4)],
        "return_ci_crosses_zero": bool(ret_lo <= 0.0 <= ret_hi),
    }


# ---------------------------------------------------------------------------
# 7) 收益集中度
# ---------------------------------------------------------------------------


def return_concentration(returns: Any, top_k: int = 5) -> dict[str, Any]:
    """集中度：剔除最好的 k 期后复利收益是否仍为正（"几笔神操作"检测）。"""
    r = _clean(returns)
    n = len(r)
    if n < top_k + 10:
        return {
            "sufficient": False,
            "reason": f"样本不足（{n} < {top_k + 10}）",
            "kills_alpha": None,
        }
    order = np.argsort(r)[::-1]
    keep_mask = np.ones(n, dtype=bool)
    keep_mask[order[:top_k]] = False
    remaining = r[keep_mask]
    full_total = float(np.prod(1.0 + r) - 1.0)
    ex_total = float(np.prod(1.0 + remaining) - 1.0)
    return {
        "sufficient": True,
        "n": n,
        "top_k": top_k,
        "full_total_return": round(full_total, 6),
        "ex_top_total_return": round(ex_total, 6),
        "top_k_share": round(
            (full_total - ex_total) / full_total if full_total > 0 else 0.0, 4
        ),
        "kills_alpha": bool(full_total > 0 and ex_total <= 0),
    }


# ---------------------------------------------------------------------------
# 8) 跨 regime 分段
# ---------------------------------------------------------------------------


def regime_split(
    returns: Any,
    index_closes: Any,
    *,
    lookback: int = 60,
    bull_pct: float = 0.05,
    bear_pct: float = -0.05,
) -> dict[str, Any]:
    """按指数 60 日趋势把每期归入 牛/熊/震荡，统计各段策略累计收益与存活。

    指数窗口不足的预热期不计入任何 regime（如实标注 excluded 数）。
    """
    r = _clean(returns)
    idx = _clean(index_closes)
    n = min(len(r), len(idx))
    if n < lookback + 30:
        return {
            "sufficient": False,
            "reason": f"样本不足（{n} < {lookback + 30}）",
            "regimes": None,
        }
    r, idx = r[-n:], idx[-n:]
    buckets: dict[str, list[float]] = {"牛": [], "熊": [], "震荡": []}
    excluded = 0
    for i in range(n):
        if i < lookback:
            excluded += 1
            continue
        base = idx[i - lookback]
        if base <= 0:
            excluded += 1
            continue
        trend = idx[i] / base - 1.0
        label = "牛" if trend >= bull_pct else ("熊" if trend <= bear_pct else "震荡")
        buckets[label].append(float(r[i]))
    regimes = {}
    for label, values in buckets.items():
        if not values:
            regimes[label] = {"n": 0, "cum_return": None, "alive": None}
            continue
        arr = np.asarray(values)
        cum = float(np.prod(1.0 + arr) - 1.0)
        regimes[label] = {
            "n": len(values),
            "cum_return": round(cum, 6),
            "mean_annual": round(float(np.mean(arr)) * TRADING_DAYS, 6),
            "alive": bool(cum > -0.05),
        }
    covered = [k for k, v in regimes.items() if v["n"] >= 10]
    return {
        "sufficient": True,
        "n": n,
        "excluded_warmup": excluded,
        "regimes": regimes,
        "regimes_covered": covered,
        "all_alive": bool(all(v["alive"] for v in regimes.values() if v["n"] >= 10)),
    }


# ---------------------------------------------------------------------------
# 9) 成本敏感性
# ---------------------------------------------------------------------------


def cost_sensitivity(
    returns: Any,
    daily_turnover: Any,
    uplift_bps: float = 5.0,
) -> dict[str, Any]:
    """费率上浮敏感性：净收益 − Σ(单边换手 × 上浮费率)，看成本后是否仍显著。

    ``daily_turnover``：日单边换手率序列（成交额/净值，0.1 = 10%）；缺失处按 0 计。
    """
    r = _clean(returns)
    t = np.asarray(daily_turnover, dtype=float).ravel()
    n = min(len(r), len(t))
    if n < 30:
        return {
            "sufficient": False,
            "reason": f"样本不足（{n} < 30）",
            "adjusted_annual": None,
        }
    r, t = r[-n:], np.nan_to_num(t[-n:], nan=0.0)
    drag = t * float(uplift_bps) / 10000.0
    adjusted = r - drag
    base_ann = float(np.mean(r)) * TRADING_DAYS
    adj_ann = float(np.mean(adjusted)) * TRADING_DAYS
    return {
        "sufficient": True,
        "n": n,
        "uplift_bps": uplift_bps,
        "avg_daily_turnover": round(float(np.mean(np.abs(t))), 4),
        "base_annual": round(base_ann, 6),
        "adjusted_annual": round(adj_ann, 6),
        "still_positive_after_cost": bool(adj_ann > 0),
        "cost_drag_annual": round(base_ann - adj_ann, 6),
    }
