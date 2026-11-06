"""全市场多分类模型的纯函数层：前视收益、截面 IC、分位分类、筛选与限价过滤。

口径（2026-11-06）：
- 价格用 ``l1_l2_factors`` 自带 ``close``（与 ``mom_ret_1d`` 同口径，已复权）；
- 标签 ``fwd_ret_h = close[T+h]/close[T]-1``；
- 分类标签 = **逐日截面分位**（默认 5 等分），保证类别均衡、只比较同日相对强弱；
- 因子筛选只用训练窗：日频 Rank IC 的均值/ICIR/覆盖率 + 截面相关去重；
- 剔除 T 日涨停样本（收盘买不进），按板块涨跌停幅度近似。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

OHLCV_COLUMNS = ("symbol", "date", "open", "high", "low", "close", "volume", "amount")
N_CLASSES_DEFAULT = 5
MIN_AMOUNT_WAN = 1000.0  # 成交额下限（**万元**，因子表 amount 单位），剔除僵尸股
LIMIT_EPS = 0.005  # 距涨停价的容差


def _code_digits(symbol: str) -> str:
    """取 6 位数字代码（兼容 600000.SH / SH600000 / 600000）。"""
    code = str(symbol)
    if "." in code:
        code = code.split(".", 1)[0]
    digits = "".join(ch for ch in code if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def limit_up_threshold(symbol: str) -> float:
    """按板块返回涨停幅度（近似；ST 的 5% 无法识别，按主板 10% 处理）。"""
    digits = _code_digits(symbol)
    if digits.startswith(("300", "301", "688", "689")):
        return 0.20
    if digits.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


def is_limit_up(symbols: pd.Series, returns: pd.Series) -> np.ndarray:
    """T 日收盘涨停（含容差）→ True。"""
    limits = symbols.astype(str).map(limit_up_threshold).to_numpy(float)
    ret = returns.to_numpy(float)
    return np.isfinite(ret) & (ret >= limits - LIMIT_EPS)


def assign_quantile_classes(
    y: np.ndarray, n_classes: int = N_CLASSES_DEFAULT
) -> np.ndarray:
    """把一日截面收益切成 ``n_classes`` 个等量组（0=最差，n-1=最好）。

    只用有限值参与分位；缺失保持 -1。并列用平均秩，保证稳定。
    """
    y = np.asarray(y, dtype=float)
    out = np.full(len(y), -1, dtype=np.int8)
    ok = np.isfinite(y)
    n = int(ok.sum())
    if n < n_classes * 5:
        return out
    order = np.argsort(y[ok], kind="mergesort")
    sorted_y = y[ok][order]
    # 平均秩（1..n）
    avg_rank = np.empty(n, dtype=float)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and sorted_y[j + 1] == sorted_y[i]:
            j += 1
        avg_rank[i : j + 1] = (i + j) / 2.0 + 1.0
        i = j + 1
    rank_full = np.full(len(y), np.nan)
    rank_full[np.flatnonzero(ok)[order]] = avg_rank
    pct = (rank_full - 0.5) / n
    out[ok] = np.clip(
        np.floor(pct[ok] * n_classes).astype(int), 0, n_classes - 1
    ).astype(np.int8)
    return out


def rankdata_nan(values: np.ndarray) -> np.ndarray:
    """平均秩（1..n），NaN 保留为 NaN；纯 numpy 向量化实现。"""
    x = np.asarray(values, dtype=float)
    out = np.full(len(x), np.nan)
    ok = np.isfinite(x)
    n = int(ok.sum())
    if n == 0:
        return out
    v = x[ok]
    order = np.argsort(v, kind="mergesort")
    sorted_v = v[order]
    obs = np.r_[True, sorted_v[1:] != sorted_v[:-1]]
    dense = np.cumsum(obs) - 1
    counts = np.bincount(dense)
    cum = np.cumsum(counts)
    avg = (cum - counts + 1 + cum) / 2.0
    ranks_sorted = avg[dense]
    ranks = np.empty(n, dtype=float)
    ranks[order] = ranks_sorted
    out[np.flatnonzero(ok)] = ranks
    return out


def daily_rank_ic(block: np.ndarray, y: np.ndarray) -> np.ndarray:
    """单日截面 Rank IC（Spearman），逐列处理缺失。``block``: (n, k)。"""
    x = np.asarray(block, dtype=float)
    y = np.asarray(y, dtype=float)
    k = x.shape[1]
    out = np.full(k, np.nan)
    if len(y) < 20:
        return out
    y_ok = np.isfinite(y)
    if y_ok.sum() < 20:
        return out
    yr = rankdata_nan(y)
    for j in range(k):
        col = x[:, j]
        ok = y_ok & np.isfinite(col)
        if ok.sum() < 20:
            continue
        xr = rankdata_nan(col[ok])
        yv = yr[ok]
        xd = xr - xr.mean()
        yd = yv - yv.mean()
        denom = np.sqrt((xd * xd).sum() * (yd * yd).sum())
        if denom > 0:
            out[j] = float((xd * yd).sum() / denom)
    return out


def select_by_ic(
    ic_matrix: np.ndarray,
    coverage: np.ndarray,
    min_abs_ic: float = 0.01,
    min_abs_icir: float = 0.15,
    min_coverage: float = 0.8,
) -> list[int]:
    """按 |ICIR| 选「有预测力」的因子（正反方向都留）。"""
    mean_ic = np.nanmean(ic_matrix, axis=0)
    std_ic = np.nanstd(ic_matrix, axis=0)
    icir = np.divide(mean_ic, std_ic, out=np.zeros_like(mean_ic), where=std_ic > 0)
    keep: list[int] = []
    for j in range(ic_matrix.shape[1]):
        if coverage[j] < min_coverage:
            continue
        if not np.isfinite(mean_ic[j]) or abs(mean_ic[j]) < min_abs_ic:
            continue
        if abs(icir[j]) < min_abs_icir:
            continue
        keep.append(j)
    keep.sort(key=lambda j: -abs(icir[j]))
    return keep


def dedup_by_corr(
    keep: list[int],
    corr: np.ndarray,
    max_features: int = 60,
    corr_threshold: float = 0.9,
) -> list[int]:
    """按 |ICIR| 顺序贪心去重（与已选因子 |ρ|≥阈值 则剔除）。"""
    selected: list[int] = []
    for j in keep:
        if len(selected) >= max_features:
            break
        if any(abs(corr[j, s]) >= corr_threshold for s in selected):
            continue
        selected.append(j)
    return selected


def class_mean_returns(
    y: np.ndarray, classes: np.ndarray, n_classes: int
) -> np.ndarray:
    """各分类的平均收益（训练窗内估计，用于把概率还原成期望收益）。"""
    out = np.full(n_classes, np.nan)
    for k in range(n_classes):
        mask = classes == k
        if mask.any():
            out[k] = float(np.nanmean(y[mask]))
    return out


@dataclass(frozen=True)
class ScreenResult:
    feature_names: tuple[str, ...]
    mean_ic: np.ndarray
    icir: np.ndarray
    coverage: np.ndarray
    n_days: int
