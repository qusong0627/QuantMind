"""因子组合权重与合成 IC 的**唯一实现**。

收口自两处**参数完全一致**的重复实现：
  - ``services/engine/factor_report/portfolio.py``（推荐因子集权重）
  - ``scripts/factor_deep_dive.py``（深度体检报告里的同一套 Σ⁻¹μ）

加上本次因子报告的组合块，不收口就是第三份。行为由
``test_factor_report_optimize.py`` 的**冻结旧实现逐位对拍**锁死。

与两处内联旧实现的**三处有意的行为差异**（均为「旧实现静默产出垃圾 → 本实现显式
失败或显式降级」，正常输入上不触发，逐位对拍可证）：
  1. ``ic_mean`` 含 NaN/Inf → 抛 ``ValueError``（旧实现一路漏过三道守卫，发出 NaN 权重）
  2. ``ic_mean`` 全为 0 → 等权 + ``fallback=True``（旧实现是 0/0 → NaN 权重）
  3. ``composite_ic_series`` 误传相关矩阵（行数 ≤ 列数）→ 返回 None（旧实现静默出假 ICIR）

⚠️ **合成 IC 必须用逐日 IC 序列，不能用相关矩阵**：
截面秩相关（因子×因子）与 IC（因子×收益）量纲不同，把相关矩阵当协方差
参与 IC 合成会得出「分散化让 ICIR 变差」的假结论。这条警告原先在两处各写了
一遍，收口后只此一份。
"""

from __future__ import annotations

from typing import Any

import numpy as np

DEFAULT_SHRINK = 0.3
"""协方差收缩系数：``(1-shrink)·Σ + shrink·I``。与两处旧实现一致，勿改。"""

MIN_COMPOSITE_DAYS = 20
"""合成 IC 序列的最少有效天数，低于此不给 ICIR（避免小样本假数）。"""


def max_icir_weights(
    sigma: np.ndarray,
    ic_mean: np.ndarray,
    *,
    shrink: float = DEFAULT_SHRINK,
) -> dict[str, Any]:
    """最大 ICIR 权重：``w ∝ Σ⁻¹·(|μ|·sign)``，收缩后求解，gross = 1。

    方向符号取自 ``ic_mean`` 的符号（负 IC 因子反向使用）；若矩阵奇异
    或解非有限，退化为 ``|μ|·sign`` 再归一（不回退到等权，保留强度信息）。

    Returns:
        ``{weights, sign, scheme, shrunk, fallback}``。
    """
    mu_in = np.asarray(ic_mean, dtype=np.float64).ravel()
    n = mu_in.size
    if n == 0:
        return {
            "weights": np.zeros(0),
            "sign": np.zeros(0),
            "scheme": "max_icir_shrunk",
            "shrunk": bool(shrink),
            "fallback": True,
        }
    if not np.isfinite(mu_in).all():
        # 旧内联实现在这里会一路把 NaN 传到权重里（NaN 参与比较全为 False，
        # 三道守卫全部漏过），最终组合 IC 静默变 NaN。非有限值只可能来自上游
        # 取数坏损，属系统边界输入错误，必须显式失败而不是发一个 NaN 权重出去。
        raise ValueError(f"ic_mean 含非有限值：{mu_in[~np.isfinite(mu_in)][:5]}")
    sign = np.where(mu_in >= 0, 1.0, -1.0)
    mu = np.abs(mu_in)

    S = np.asarray(sigma, dtype=np.float64)
    if S.shape != (n, n):
        raise ValueError(f"相关矩阵形状 {S.shape} 与因子数 {n} 不匹配")
    S = (1.0 - shrink) * S + shrink * np.eye(n)
    fallback = False
    try:
        w = np.linalg.solve(S, mu * sign)
    except np.linalg.LinAlgError:
        w = mu * sign
        fallback = True
    if not np.isfinite(w).all() or np.abs(w).sum() <= 0:
        w = mu * sign
        fallback = True
    total = np.abs(w).sum()
    if total <= 0:  # 全部 ic_mean 为 0：无方向无强度 → 等权兜底
        w = np.ones(n) / n
        fallback = True
        return {
            "weights": w,
            "sign": sign,
            "scheme": "max_icir_shrunk",
            "shrunk": bool(shrink),
            "fallback": fallback,
        }
    w = sign * (np.abs(w) / total)  # 方向符号 + gross = 1 归一
    return {
        "weights": w,
        "sign": sign,
        "scheme": "max_icir_shrunk",
        "shrunk": bool(shrink),
        "fallback": fallback,
    }


def composite_ic_series(
    ic_matrix: np.ndarray,
    weights: np.ndarray,
) -> dict[str, Any] | None:
    """组合 IC 序列与 ICIR：``ic_t = Σ_k w_k · ic_k,t``（逐日线性合成）。

    ⚠️ 传进来的是**逐日 IC 矩阵**（T×F），不是相关矩阵 —— 见模块 docstring。

    Returns:
        ``{ic_mean, ic_std, icir, n_days, series}``；有效天数不足 → None。
    """
    M = np.asarray(ic_matrix, dtype=np.float64)
    w = np.asarray(weights, dtype=np.float64).ravel()
    if M.ndim != 2 or M.size == 0 or M.shape[1] != w.size or w.size == 0:
        return None
    if M.shape[0] <= M.shape[1]:
        # 逐日 IC 矩阵必然是「天数 × 因子数」且天数远多于因子数。若行数 ≤ 列数，
        # 几乎一定是把**相关矩阵**（F×F）当成 IC 矩阵传了进来 —— 形状校验抓不住
        # （F×F 与长度 F 的权重完全兼容），会静默合成出 F 个「IC」并算出假 ICIR。
        return None
    series = M @ w
    series = series[np.isfinite(series)]
    if series.size <= MIN_COMPOSITE_DAYS or series.std() <= 0:
        return None
    mean = float(series.mean())
    std = float(series.std())
    return {
        "ic_mean": mean,
        "ic_std": std,
        "icir": mean / std,
        "n_days": int(series.size),
        "series": series,
    }
