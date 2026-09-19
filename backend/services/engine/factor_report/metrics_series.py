"""分段 / 年度 / 月度 / 滚动 + 构建期截面原语（§8、§14）—— 对外入口仍是 :mod:`metrics`。

两个主题放一起是因为它们的依赖集合几乎相同（都只用 core 的统计原语），
且都不属于「评估单个因子」那一族：§8 是**时间维**的切分，§14 是**截面维**的矩阵原语
（构建期 ``build_factor_report.py`` 直接调用）。口径常量与文案在 :mod:`metrics_core`。
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from typing import Any

import numpy as np

from .metrics_core import (
    CLIP_MAD_K,
    MAD_TO_SIGMA,
    MIN_SAMPLES,
    TRADING_DAYS,
    _finite,
    _none_if_nan,
    _pearson_raw,
    half_ic,
    spearman,
)

# 秩的唯一实现（NaN 感知）在 neutralize，本层不另抄一份。
from .neutralize import rank_pct

# ═══════════════ 8. 分段 / 年度 / 月度 / 滚动 ═══════════════


def annual_breakdown(
    dates: Sequence[str],
    daily: Sequence[float],
    bench_daily: Sequence[float] | None = None,
) -> list[dict[str, Any]]:
    """分年度：年内复利收益 + 基准 + 超额。"""
    dts = [str(x) for x in dates]
    d = np.asarray(daily, dtype=np.float64).ravel()
    b = (
        np.asarray(bench_daily, dtype=np.float64).ravel()
        if bench_daily is not None
        else None
    )
    n = min([len(dts), d.size] + ([b.size] if b is not None else []))
    if n == 0:
        return []
    buckets: dict[str, list[int]] = {}
    for i in range(n):
        buckets.setdefault(dts[i][:4], []).append(i)

    def _compound(vals: np.ndarray) -> float | None:
        f = vals[np.isfinite(vals)]
        return float(np.prod(1.0 + f) - 1.0) if f.size else None

    rows: list[dict[str, Any]] = []
    for year in sorted(buckets):
        idx = buckets[year]
        ret = _compound(d[idx])
        bench = _compound(b[idx]) if b is not None else None
        rows.append(
            {
                "year": int(year) if year.isdigit() else year,
                "n_days": len(idx),
                "ret": _none_if_nan(ret),
                "bench_ret": _none_if_nan(bench),
                "excess": _none_if_nan(ret - bench)
                if ret is not None and bench is not None
                else None,
            }
        )
    return rows


def monthly_matrix(dates: Sequence[str], daily: Sequence[float]) -> dict[str, Any]:
    """年月收益矩阵（行 = 年，列 = 有数据的月份），供热力图使用。"""
    dts = [str(x) for x in dates]
    d = np.asarray(daily, dtype=np.float64).ravel()
    n = min(len(dts), d.size)
    if n == 0:
        return {"years": [], "months": [], "matrix": []}
    cells: dict[tuple[int, int], list[float]] = {}
    for i in range(n):
        s = dts[i]
        if len(s) < 7 or not s[:7].replace("-", "").isdigit():
            continue
        cells.setdefault((int(s[:4]), int(s[5:7])), []).append(float(d[i]))
    years = sorted({y for y, _ in cells})
    months = sorted({m for _, m in cells})
    matrix: list[list[float | None]] = []
    for y in years:
        row: list[float | None] = []
        for m in months:
            vals = np.asarray(cells.get((y, m), []), dtype=np.float64)
            f = vals[np.isfinite(vals)]
            row.append(float(np.prod(1.0 + f) - 1.0) if f.size else None)
        matrix.append(row)
    return {"years": years, "months": months, "matrix": matrix}


def sub_period_stats(ic: Sequence[float], k: int = 4) -> list[dict[str, Any]]:
    """把 IC 序列均分成 k 段，给出各段 IC 均值/ICIR —— 看「稳定还是一段行情撑的」。"""
    x = np.asarray(ic, dtype=np.float64).ravel()
    if x.size == 0 or k <= 0:
        return []
    k = min(int(k), x.size)
    bounds = np.linspace(0, x.size, k + 1).astype(int)
    rows: list[dict[str, Any]] = []
    for a, b in zip(bounds[:-1], bounds[1:], strict=False):
        seg = _finite(x[a:b])
        mean = float(seg.mean()) if seg.size else None
        sd = float(seg.std(ddof=1)) if seg.size > 1 else 0.0
        rows.append(
            {
                "i0": int(a),
                "i1": int(b),
                "n": int(b - a),
                "ic_mean": _none_if_nan(mean),
                "icir": _none_if_nan(mean / sd)
                if mean is not None and sd > 0
                else None,
            }
        )
    return rows


def rolling_ir(ic: Sequence[float], win: int = TRADING_DAYS) -> list[float | None]:
    """滚动 ICIR（**不年化**，与单因子 ICIR 同口径）。窗口不足处为 None。"""
    x = np.asarray(ic, dtype=np.float64).ravel()
    if x.size == 0:
        return []
    out: list[float | None] = []
    for i in range(x.size):
        if i + 1 < win:
            out.append(None)
            continue
        seg = _finite(x[i + 1 - win : i + 1])
        if seg.size < 2:
            out.append(None)
            continue
        sd = float(seg.std(ddof=1))
        out.append(float(seg.mean() / sd) if sd > 0 else None)
    return out


def rolling_mean(values: Sequence[float], win: int) -> list[float | None]:
    """滚动均值（窗口不足处 None）。"""
    x = np.asarray(values, dtype=np.float64).ravel()
    if x.size == 0 or win <= 0:
        return []
    out: list[float | None] = []
    for i in range(x.size):
        if i + 1 < win:
            out.append(None)
            continue
        seg = _finite(x[i + 1 - win : i + 1])
        out.append(float(seg.mean()) if seg.size else None)
    return out


# ═══════════════ 14. 构建期截面原语（矩阵形态）═══════════════
#
# 本节四个函数只在**构建期**（build_factor_report）用得上：它们要的是「当日截面
# 矩阵」，读时无法从逐日序列反推。全部是 §1–§13 里同名标量口径的**矩阵形态**，
# 等价性各自有测试断言 —— 两种执行形状、一份口径，与前文 orthogonalize 的处理一致。


def half_ic_matrix(
    rank_x: np.ndarray,
    y: Sequence[float],
    *,
    top: bool,
    min_n: int = MIN_SAMPLES,
) -> np.ndarray:
    """:func:`half_ic` 的矩阵形态：一次算完全部因子的半截面 IC。

    逐列调用 ``half_ic`` 在 2588 天 × 400+ 因子上是最贵的开销，这里把与列无关的
    部分（中位切点、有效性掩码）一次性算掉，只把「半区内给 y 定秩」留在循环里
    （该步的掩码逐列不同，无法共享）。结果与逐列调用逐位一致。

    Args:
        rank_x: (n, k) 当日因子值的**秩**（复用构建器已算好的秩矩阵，不重复排序）。
        y: 长度 n 的前瞻收益。
        top: True 取因子值高于中位的半区，False 取另一半。
        min_n: 每个半区的最少样本数。

    Returns:
        (k,) 秩相关系数数组；样本不足的列是 NaN。
    """
    R = np.asarray(rank_x, dtype=np.float64)
    yy = np.asarray(y, dtype=np.float64).ravel()
    if R.ndim != 2 or R.shape[0] != yy.size or R.shape[0] < min_n * 2:
        return np.full(R.shape[1] if R.ndim == 2 else 0, np.nan)
    out = np.full(R.shape[1], np.nan)
    finite = np.isfinite(R)
    for j in range(R.shape[1]):
        v = half_ic(R[:, j], yy, top=top, min_n=min_n)
        if v is not None:
            out[j] = v
    del finite
    return out


def domain_ic_matrix(
    rank_x: np.ndarray,
    y: Sequence[float],
    mv: Sequence[float],
    *,
    cuts: Sequence[float] = (1 / 3, 2 / 3),
    min_n: int = MIN_SAMPLES,
) -> dict[str, np.ndarray]:
    """分市值域 IC：按总市值分位切子域，各算一次域内秩相关。

    切点由**当日全体有效市值**确定（不按各因子的有效样本各切一份），这样不同因子
    的「大盘 IC」是同一批股票上的统计量、可比。某因子在某子域的有效样本不足
    ``min_n`` 时该格记 NaN —— 不缩小样本硬算（样本不足即无结论，不是「没效果」）。

    Args:
        rank_x: (n, k) 当日因子值的秩。
        y: 长度 n 的前瞻收益。
        mv: 长度 n 的总市值（原始值，仅用于排序分档）。
        cuts: 分位切点，默认三分位。
        min_n: 每个子域的最少样本数。

    Returns:
        ``{"small": (k,), "mid": (k,), "large": (k,)}``；不可定义的格为 NaN。
    """
    R = np.asarray(rank_x, dtype=np.float64)
    yy = np.asarray(y, dtype=np.float64).ravel()
    M = np.asarray(mv, dtype=np.float64).ravel()
    names = ("small", "mid", "large")
    bad_shape = (
        R.ndim != 2 or R.shape[0] != yy.size or M.size != R.shape[0] or R.shape[0] == 0
    )
    if bad_shape:
        k = R.shape[1] if R.ndim == 2 else 0
        return {n: np.full(k, np.nan) for n in names}
    mv_ok = np.isfinite(M)
    if int(mv_ok.sum()) < min_n:
        return {n: np.full(R.shape[1], np.nan) for n in names}
    edges = np.quantile(M[mv_ok], np.asarray(cuts, dtype=np.float64))
    bucket = np.where(mv_ok, np.digitize(M, edges), -1)
    out: dict[str, np.ndarray] = {}
    for b, name in enumerate(names):
        col = np.full(R.shape[1], np.nan)
        in_bucket = bucket == b
        if int(in_bucket.sum()) >= min_n:
            for j in range(R.shape[1]):
                sel = in_bucket & np.isfinite(R[:, j]) & np.isfinite(yy)
                if int(sel.sum()) < min_n or int(sel.sum()) < MIN_SAMPLES:
                    continue
                v = spearman(R[sel, j], yy[sel])
                if np.isfinite(v):
                    col[j] = v
        out[name] = col
    return out


def clip_frac_matrix(X: np.ndarray, k: float = CLIP_MAD_K) -> np.ndarray:
    """逐列「去极值比例」：偏离中位超过 ``k`` 倍 MAD 尺度（1.4826×MAD）的样本占比。

    数据质量信号，不是收益信号：比例突然升高通常意味着当日因子值分布变脏
    （口径漂移、复权断裂、上游回填）。常数列（MAD = 0）记 NaN 而非 1.0 ——
    「没有离散度」时这个比例无定义，返回 1.0 会被读成「全是极端值」。

    Args:
        X: (n, k) 原始因子值（**不是秩**）。
        k: 极端值倍数（默认 3，即 ±3σ 的稳健等价）。

    Returns:
        (k,) 占比数组。
    """
    A = np.asarray(X, dtype=np.float64)
    if A.ndim != 2 or A.shape[0] == 0:
        return np.full(A.shape[1] if A.ndim == 2 else 0, np.nan)
    finite = np.isfinite(A)
    if not finite.any():
        return np.full(A.shape[1], np.nan)
    with warnings.catch_warnings():  # 全 NaN 列的中位数未定义，该列结果随后被掩掉
        warnings.simplefilter("ignore", RuntimeWarning)
        med = np.nanmedian(A, axis=0)
        scale = MAD_TO_SIGMA * np.nanmedian(np.abs(A - med), axis=0)
    # 逐列算占比：各列有效样本数不同，逐列除自己的有效数（不是全局 n）
    out = np.full(A.shape[1], np.nan)
    for j in range(A.shape[1]):
        ok = finite[:, j]
        if not ok.any() or not np.isfinite(scale[j]) or scale[j] <= 0:
            continue
        out[j] = float(np.mean(np.abs(A[ok, j] - med[j]) > k * scale[j]))
    return out


def pairwise_rank_corr(
    A: np.ndarray, B: np.ndarray, min_n: int = MIN_SAMPLES
) -> np.ndarray:
    """两簇变量之间**每一对**的横截面秩相关（NaN 逐格有效）。

    用途：因子 × Barra 风格暴露的相关矩阵（k 因子 × 10 风格逐日算）。
    与逐对调用 :func:`spearman` 同口径（比值只依赖秩，故百分位秩与整数秩等价）。

    Args:
        A: (n, ka)；B: (n, kb)。
        min_n: 每一对的最少共同有效样本数，不足记 NaN。

    Returns:
        (ka, kb) 相关系数矩阵；不可定义的格为 NaN。
    """
    X = np.asarray(A, dtype=np.float64)
    Y = np.asarray(B, dtype=np.float64)
    ka = X.shape[1] if X.ndim == 2 else 0
    kb = Y.shape[1] if Y.ndim == 2 else 0
    if X.ndim != 2 or Y.ndim != 2 or X.shape[0] != Y.shape[0] or X.shape[0] == 0:
        return np.full((ka, kb), np.nan)
    rx = rank_pct(X)  # 与 neutralize 共用同一份 NaN 感知秩实现（缺失不前视、不排最大）
    ry = rank_pct(Y)
    out = np.full((ka, kb), np.nan)
    for j in range(ka):
        col_j = rx[:, j]
        for i in range(kb):
            ok = np.isfinite(col_j) & np.isfinite(ry[:, i])
            if int(ok.sum()) < min_n:
                continue
            v = _pearson_raw(col_j[ok], ry[ok, i])
            if v is not None:
                out[j, i] = v
    return out
