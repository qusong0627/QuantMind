"""显著性 / BRAIN 头部指标 / 经济性 / 风格归因（§4–§7）—— 对外入口仍是 :mod:`metrics`。

本文件是 :mod:`metrics_core` 之上的**评估层**：只消费 core 的统计原语与口径常量，
不反向被 core 依赖（DAG：``core ← eval``、``core ← series``，eval 与 series 之间无耦合）。
口径常量与文案在 :mod:`metrics_core`，**改口径不要改这里**。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np

from .metrics_core import (
    DEFAULT_COST_BPS,
    DEFAULT_PARTICIPATION,
    MIN_SAMPLES,
    MIN_TSTAT_SAMPLES,
    TRADING_DAYS,
    TURNOVER_FLOOR,
    _finite,
    _none_if_nan,
    _pearson_raw,
    ic_autocorr,
)

# ═══════════════ 4. BRAIN 头部指标（7 指标环）═══════════════


def margin_of(returns: float | None, turnover: float | None) -> float | None:
    """Margin ≡ Returns / Turnover。"""
    r, t = _none_if_nan(returns), _none_if_nan(turnover)
    if r is None or t is None or t == 0:
        return None
    return float(r) / float(t)


def fitness_of(
    ir: float | None, returns: float | None, turnover: float | None
) -> float | None:
    """Fitness ≡ IR × √(|Returns| / max(Turnover, 0.125))。"""
    i, r, t = _none_if_nan(ir), _none_if_nan(returns), _none_if_nan(turnover)
    if i is None or r is None or t is None:
        return None
    floor = max(abs(float(t)), TURNOVER_FLOOR)
    return float(i) * math.sqrt(abs(float(r)) / floor)


def brain_headline(
    daily_ls: Sequence[float],
    turnover: Sequence[float] | float | None,
    ann: int = TRADING_DAYS,
) -> dict[str, Any]:
    """7 指标环的前五项：Returns / IR / Turnover / Fitness / Margin（+ 累计与分布摘要）。

    ``Returns`` 取**简单年化** ``μ×ann``（不是 CAGR）—— 口径依据见模块 docstring。
    """
    d = _finite(daily_ls)
    if isinstance(turnover, (int, float)) or turnover is None:
        t_mean = _none_if_nan(turnover)
    else:
        tv = _finite(turnover)
        t_mean = float(tv.mean()) if tv.size else None
    out: dict[str, Any] = {
        "n_days": int(d.size),
        "mu_daily": None,
        "sigma_daily": None,
        "ann_vol": None,
        "returns": None,
        "cum_return": None,
        "ir": None,
        "turnover": t_mean,
        "fitness": None,
        "margin": None,
    }
    if d.size == 0:
        return out
    mu = float(d.mean())
    sigma = float(d.std(ddof=1)) if d.size > 1 else 0.0
    returns = mu * ann
    ir = (mu / sigma * math.sqrt(ann)) if sigma > 0 else None
    out.update(
        {
            "mu_daily": mu,
            "sigma_daily": sigma,
            "ann_vol": sigma * math.sqrt(ann),
            "returns": returns,
            "cum_return": float(np.prod(1.0 + d) - 1.0),
            "ir": ir,
            "fitness": fitness_of(ir, returns, t_mean),
            "margin": margin_of(returns, t_mean),
        }
    )
    return out


# ═══════════════ 5. 显著性：多重检验 / NW / Bootstrap / DSR ═══════════════


def plain_tstat(series: Sequence[float]) -> float | None:
    """普通 t 值 = mean / (std/√n)（ddof=1）。零方差或样本不足 → None。"""
    x = _finite(series)
    if x.size < MIN_TSTAT_SAMPLES:
        return None
    sd = float(x.std(ddof=1))
    if sd <= 0:
        return None
    return float(x.mean() / (sd / math.sqrt(x.size)))


def newey_west_lag(n: int) -> int:
    """Newey-West 自动定阶：``4(n/100)^(2/9)``（Newey-West 1994 经验规则）。"""
    if n <= 1:
        return 0
    return max(1, min(int(4.0 * (n / 100.0) ** (2.0 / 9.0)), n - 1))


def nw_tstat(series: Sequence[float], lags: int | None = None) -> float | None:
    """Newey-West 调整 t 值（Bartlett 核）。

    IC 序列存在自相关时，OLS 标准误低估、普通 t 严重高估显著性；
    本函数按同方差下的一致估计量修正。自相关为 0 时两值接近。
    """
    x = _finite(series)
    n = x.size
    if n < MIN_TSTAT_SAMPLES:
        return None
    if float(x.std(ddof=1)) <= 0:
        return None
    L = newey_west_lag(n) if lags is None else max(0, min(int(lags), n - 1))
    d = x - x.mean()
    s = float(d @ d) / n
    for j in range(1, L + 1):
        gj = float(d[j:] @ d[:-j]) / n
        s += 2.0 * (1.0 - j / (L + 1)) * gj
    if s <= 0:
        return None
    return float(x.mean() / math.sqrt(s / n))


def normal_pvalue(t: float | None) -> float | None:
    """双侧正态 p 值。"""
    if t is None:
        return None
    return float(math.erfc(abs(float(t)) / math.sqrt(2.0)))


def normal_cdf(z: float) -> float:
    return float(0.5 * math.erfc(-float(z) / math.sqrt(2.0)))


def bhy_qvalues(p_values: Sequence[float]) -> np.ndarray:
    """Benjamini-Yekutieli（任意相依结构下的 FDR 控制）q 值，返回**原始顺序**。

    ``q_(i) = min_{j≥i} p_(j) · n · Σ_{k=1..n}(1/k) / j``，再截断到 [0,1]。

    Raises:
        ValueError: 空输入 —— 空集合静默返回空数组会让「零项参与即通过」的验收假通过。
    """
    p = np.asarray(p_values, dtype=np.float64).ravel()
    if p.size == 0:
        raise ValueError(
            "bhy_qvalues 需要至少一个 p 值（空输入必须显式失败，不得静默通过）"
        )
    p = np.clip(p, 0.0, 1.0)
    n = p.size
    c_n = sum(1.0 / k for k in range(1, n + 1))
    order = np.argsort(p, kind="stable")
    ranked = p[order] * n * c_n / np.arange(1, n + 1, dtype=np.float64)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n, dtype=np.float64)
    out[order] = np.clip(ranked, 0.0, 1.0)
    return out


def bootstrap_ci(
    series: Sequence[float],
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 20260919,
    stat: str = "mean",
) -> dict[str, Any] | None:
    """Bootstrap 百分位置信区间（不依赖正态假设）。``seed`` 固定 → 结果可复现。

    ``stat``: ``"mean"``（均值）或 ``"ir"``（mean/std 比值，不年化）。
    """
    x = _finite(series)
    if x.size == 0 or n_boot <= 0:
        return None
    rng = np.random.default_rng(seed)
    chunks: list[np.ndarray] = []
    done = 0
    while done < n_boot:  # 分块抽样，避免 n_boot×n 一次性占满内存
        size = min(200, n_boot - done)
        idx = rng.integers(0, x.size, size=(size, x.size))
        smp = x[idx]
        if stat == "ir":
            sd = smp.std(axis=1, ddof=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                chunks.append(np.where(sd > 0, smp.mean(axis=1) / sd, np.nan))
        else:
            chunks.append(smp.mean(axis=1))
        done += size
    boots = np.concatenate(chunks)
    boots = boots[np.isfinite(boots)]
    if boots.size == 0:
        return None
    lo, hi = np.quantile(boots, [alpha / 2.0, 1.0 - alpha / 2.0])
    point = float(x.mean())
    if stat == "ir":
        sd = float(x.std(ddof=1))
        point = float(x.mean() / sd) if sd > 0 else None
    return {
        "lo": float(lo),
        "hi": float(hi),
        "point": point,
        "level": 1.0 - alpha,
        "n_boot": int(boots.size),
        "stat": stat,
    }


def deflated_sharpe(
    ir: float | None,
    n_trials: int,
    n_obs: int,
    skew: float = 0.0,
    kurt: float = 3.0,
    ann: int = TRADING_DAYS,
) -> float | None:
    """Deflated Sharpe Ratio（Bailey & López de Prado）：给定「试了 N 次」后 SR 仍显著的概率。

    ``ir`` 传年化 IR，内部换算为非年化 SR 参与计算；``kurt`` 为**原始峰度**（正态 = 3）。
    """
    if ir is None or n_obs < 3 or n_trials < 1 or ann <= 0:
        return None
    sr = float(ir) / math.sqrt(ann)
    if n_trials == 1:
        e_max = 0.0
    else:
        from statistics import NormalDist

        nd = NormalDist()
        g = 0.5772156649015329
        e_max = (1 - g) * nd.inv_cdf(1 - 1.0 / n_trials) + g * nd.inv_cdf(
            1 - 1.0 / (n_trials * math.e)
        )
    denom_sq = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom_sq <= 0:
        return None
    z = (sr - e_max) * math.sqrt(n_obs - 1) / math.sqrt(denom_sq)
    return normal_cdf(z)


def crowding_score(
    ic_series: Sequence[float], turnover: Sequence[float], recent: int = 60
) -> dict[str, Any]:
    """拥挤度代理：近期换手在全history的分位 + IC 一阶自相关，各占一半。

    换手分位高（大家都在换）且 IC 自相关高（信号衰减慢、易被套利）时拥挤分高。
    """
    t = _finite(turnover)
    ic = np.asarray(ic_series, dtype=np.float64).ravel()
    if t.size == 0:
        return {
            "score": None,
            "turnover_pct": None,
            "ic_autocorr_lag1": None,
            "n_days": 0,
            "note": "无换手序列",
        }
    window = t[-max(1, recent) :]
    t_pct = float((t <= window.mean()).mean())
    a1 = ic_autocorr(ic, lags=1)[0]
    a1c = min(1.0, max(0.0, a1)) if a1 is not None else 0.0
    return {
        "score": 0.5 * t_pct + 0.5 * a1c,
        "turnover_pct": t_pct,
        "ic_autocorr_lag1": a1,
        "n_days": int(t.size),
        "note": "0.5×近期换手分位 + 0.5×IC 一阶自相关（截断到 [0,1]）",
    }


# ═══════════════ 6. 经济性：成本 / 持有期 / 容量 ═══════════════


def cost_sensitivity(
    ls_daily: Sequence[float],
    turnover: Sequence[float],
    bps_grid: Iterable[float] = (0, 5, 10, 15, 20, 30, 50),
    ann: int = TRADING_DAYS,
) -> dict[str, Any]:
    """各成本档下的净收益/净 IR/净 Fitness，并给出盈亏平衡成本。"""
    d = np.asarray(ls_daily, dtype=np.float64).ravel()
    t = np.asarray(turnover, dtype=np.float64).ravel()
    rows: list[dict[str, Any]] = []
    if d.size and t.size:
        n = min(d.size, t.size)
        d, t = d[:n], t[:n]
        for bps in bps_grid:
            net = d - t * (float(bps) / 10000.0)
            h = brain_headline(net, t, ann)
            rows.append(
                {
                    "bps": float(bps),
                    "net_return": _none_if_nan(h["returns"]),
                    "net_ir": _none_if_nan(h["ir"]),
                    "net_fitness": _none_if_nan(h["fitness"]),
                }
            )
    md, mt = (
        _none_if_nan(np.nanmean(d) if d.size else None),
        _none_if_nan(np.nanmean(t) if t.size else None),
    )
    break_even = (
        float(md / mt * 10000.0)
        if md is not None and mt is not None and mt > 0
        else None
    )
    # 毛收益为负时解出来是负数。直接印「盈亏平衡 -0.62bp」读者会当成 bug —— 它的实际
    # 含义是「要倒收 0.62bp 手续费才持平」，即这个方向没有正的盈亏平衡成本。
    # 有意义的那个数是**反向组合**的（多空两组互换后约 -break_even）。
    note = None
    if break_even is not None and break_even < 0:
        note = (
            f"该方向毛收益为负，不存在正的盈亏平衡成本（数学解 {break_even:.2f}bp 意为"
            f"「倒收手续费才持平」）；把多空两组互换后，盈亏平衡点约为 {-break_even:.2f}bp。"
        )
    return {
        "rows": rows,
        "break_even_bps": break_even,
        "break_even_note": note,
        "default_bps": DEFAULT_COST_BPS,
    }


def holding_period_sweep(
    ls_by_horizon: dict[int, Sequence[float]],
    turnover: Sequence[float],
    cost_bps: float = DEFAULT_COST_BPS,
    ann: int = TRADING_DAYS,
) -> list[dict[str, Any]]:
    """持有 1/2/5/10/20 日的净收益对比：直接吃 parquet 已有的 ``ls_*`` 多期列，近乎免费。

    持有 h 日的成员变动比例 ≈ ``1−(1−日换手)^h``；成本按 h 日摊到日均。
    """
    if not ls_by_horizon:
        return []
    tv = _finite(turnover)
    t_daily = float(tv.mean()) if tv.size else 0.0
    t_daily = min(1.0, max(0.0, t_daily))
    rate = float(cost_bps) / 10000.0
    rows: list[dict[str, Any]] = []
    for h in sorted(int(k) for k in ls_by_horizon):
        if h <= 0:
            continue
        arr = np.asarray(ls_by_horizon[h], dtype=np.float64).ravel()
        finite = _finite(arr)
        if finite.size == 0:
            continue
        gross_daily = float(finite.mean()) / h
        turn_h = 1.0 - (1.0 - t_daily) ** h
        net_daily = gross_daily - turn_h * rate / h
        sd = float(finite.std(ddof=1)) / h if finite.size > 1 else 0.0
        ir = net_daily / sd * math.sqrt(ann) if sd > 0 else None
        rows.append(
            {
                "hold_days": h,
                "gross_return": gross_daily * ann,
                "net_return": net_daily * ann,
                "turnover": turn_h,
                "ir": _none_if_nan(ir),
            }
        )
    return rows


def capacity_estimate(
    turnover: float | None,
    median_amount: float | None,
    n_positions: int | None,
    participation: float = DEFAULT_PARTICIPATION,
) -> dict[str, Any]:
    """简化容量估算：``AUM ≤ 参与率 × 持仓中位日成交额 × 持仓数 / 日换手``。

    ⚠️ 输出**必须**带 ``assumed_participation`` 与 ``note``：这是假设模型不是实测，
    不允许只给一个「可承载 X 亿」的裸数字。
    """
    t = _none_if_nan(turnover)
    a = _none_if_nan(median_amount)
    est = None
    if t is not None and t > 0 and a is not None and a > 0 and n_positions:
        est = float(participation) * a * int(n_positions) / t
    return {
        "est_aum": est,
        "assumed_participation": float(participation),
        "median_amount": a,
        "n_positions": int(n_positions) if n_positions else None,
        "turnover": t,
        "note": (
            f"简化模型，含显式假设：单标的单日成交不超过其日成交额的 "
            f"{float(participation):.0%}；未计入冲击成本、相关性与容量衰减。"
        ),
    }


# ═══════════════ 7. 风格归因 ═══════════════


def style_attribution(
    daily: Sequence[float], style_returns: dict[str, Sequence[float]]
) -> dict[str, Any] | None:
    """时序回归 ``daily = α + Σ β_k · f_k + ε``：分辨「超额是真本事还是风格 beta」。

    Returns:
        ``{alpha, t_alpha, betas:[{style,beta,t}], r_squared, n, note}``；输入不足 → None。
    """
    y_all = np.asarray(daily, dtype=np.float64).ravel()
    names = [str(k) for k in style_returns]
    if y_all.size == 0 or not names:
        return None
    cols = [
        np.asarray(style_returns[k], dtype=np.float64).ravel() for k in style_returns
    ]
    n = min([y_all.size] + [c.size for c in cols])
    if n < len(names) + 5:
        return None
    y = y_all[:n]
    X = np.column_stack([c[:n] for c in cols])
    ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
    y, X = y[ok], X[ok]
    if y.size < len(names) + 5 or float(y.std(ddof=1)) <= 0:
        return None

    A = np.column_stack([np.ones(y.size), X])
    k = A.shape[1]
    try:
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    resid = y - A @ coef
    dof = y.size - k
    if dof <= 0:
        return None
    sigma2 = float(resid @ resid) / dof
    XtX_inv = np.linalg.pinv(A.T @ A)
    se = np.sqrt(np.maximum(np.diag(sigma2 * XtX_inv), 0.0))
    with np.errstate(invalid="ignore", divide="ignore"):
        tvals = np.where(se > 0, coef / se, np.nan)
    sst = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float(resid @ resid) / sst if sst > 0 else None

    betas = [
        {
            "style": nm,
            "beta": _none_if_nan(coef[i + 1]),
            "t": _none_if_nan(tvals[i + 1]),
        }
        for i, nm in enumerate(names)
    ]
    return {
        "alpha": _none_if_nan(coef[0]),
        "t_alpha": _none_if_nan(tvals[0]),
        "betas": betas,
        "r_squared": _none_if_nan(r2),
        "n": int(y.size),
        "note": "α 是剥离十大风格纯因子收益后的日度超额；α 的 t 值看它是否显著非零。",
    }


def style_correlations(
    daily: Sequence[float], style_returns: dict[str, Sequence[float]]
) -> list[dict[str, Any]] | None:
    """逐风格与日收益序列的**单变量** Pearson 相关（「超额风格相关性数值汇总」表）。

    与 :func:`style_attribution` 并列使用，**不可互相替代**：本函数每个风格**单独**
    算相关，回归给的是**控制其余九个风格之后**的边际 β。两者在风格之间共线时会明显
    分歧（size 与 nlsize 天然如此），而分歧本身就是信息 ——
    单变量高、回归 β 不显著 = 这个相关性是被别的风格带出来的。

    逐风格各自丢弃非有限日（pairwise complete），故各行 ``n_days`` 可以不同：
    某个风格缺某一天，不该让其余九个风格跟着少一天。

    常量序列（无变化）→ ``corr`` 为 ``None`` 而不是 0 —— 相关的数学定义在此未定义，
    写 0 会被读成「确认无关」。
    """
    names = [str(k) for k in style_returns]
    y_all = np.asarray(daily, dtype=np.float64).ravel()
    if y_all.size == 0 or not names:
        return None
    rows: list[dict[str, Any]] = []
    for nm in names:
        col = np.asarray(style_returns[nm], dtype=np.float64).ravel()
        n = min(y_all.size, col.size)
        y, x = y_all[:n], col[:n]
        ok = np.isfinite(y) & np.isfinite(x)
        y, x = y[ok], x[ok]
        r = _pearson_raw(y, x) if y.size >= MIN_SAMPLES else None
        rows.append({"style": nm, "corr": _none_if_nan(r), "n_days": int(y.size)})
    return rows


