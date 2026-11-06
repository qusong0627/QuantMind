"""大盘方向模型的纯函数层：截面聚合、滚动分位、标签、切分与评估。

设计边界（2026-11-06）：

- **只用 T 日及以前的数据**：截面聚合只读当日分区；滚动分位只用尾随窗口；
  标签用 T 日之后的价格，且一律以 ``fwd_`` 前缀标注并在特征侧排除。
- **大盘股口径 = 当日流通市值排名前 N**（``fun_mv_rank`` 是逐日截面分位，
  由因子侧 PIT 计算），不用 ``index_weights`` 的当前成分快照（幸存者偏差）。
- 聚合先做 1%/99% 缩尾，避免极端值主导等权均值。

本模块不 import lightgbm / catboost，可在宿主机直接单测。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

MIN_UNIVERSE = 100
TOP_N_LARGE_CAP = 300
MIN_LEG_SIDE = 20
WINSOR_LOW = 0.01
WINSOR_HIGH = 0.99

AGG_KINDS = ("mean", "capw", "ls", "std")


@dataclass(frozen=True)
class AggSpec:
    """一条「因子 → 市场级聚合」定义。"""

    factor: str
    kind: str

    def __post_init__(self) -> None:
        if self.kind not in AGG_KINDS:
            raise ValueError(f"unknown aggregation kind: {self.kind}")

    @property
    def name(self) -> str:
        return f"fa_{self.factor}_{self.kind}"


def winsorize(values: np.ndarray) -> np.ndarray:
    """1%/99% 缩尾；NaN 原样保留。"""
    x = np.asarray(values, dtype=float)
    out = np.full_like(x, np.nan)
    ok = np.isfinite(x)
    if ok.sum() == 0:
        return out
    lo, hi = np.quantile(x[ok], [WINSOR_LOW, WINSOR_HIGH])
    out[ok] = np.clip(x[ok], lo, hi)
    return out


def _large_cap_masks(rank: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """按当日流通市值分位切「大盘组 / 小盘组」。"""
    valid = np.isfinite(rank)
    top_n = min(TOP_N_LARGE_CAP, max(1, n // 10))
    threshold = 1.0 - top_n / max(n, 1)
    top = valid & (rank >= threshold)
    bottom = valid & (rank < 0.5)
    return top, bottom


def aggregate_partition(
    frame: pd.DataFrame, specs: Sequence[AggSpec]
) -> dict[str, float]:
    """单日截面 → 市场级聚合特征。

    需要列：``fun_mv_rank``（0~1 分位）、``fun_total_mv``（对数市值）与各因子列。
    股票数不足 ``MIN_UNIVERSE`` 时全部返回 NaN（该日不参与训练）。
    """
    n = len(frame)
    out: dict[str, float] = {"fa_n_stocks": float(n)}
    if n < MIN_UNIVERSE:
        for spec in specs:
            out[spec.name] = float("nan")
        return out

    rank = pd.to_numeric(frame["fun_mv_rank"], errors="coerce").to_numpy(float)
    logcap = pd.to_numeric(frame["fun_total_mv"], errors="coerce").to_numpy(float)
    top_mask, bottom_mask = _large_cap_masks(rank, n)

    if np.isfinite(logcap).any():
        weights = np.exp(logcap - np.nanmax(logcap))
    else:
        weights = np.zeros(n, dtype=float)
    weights = np.where(np.isfinite(weights), weights, 0.0)

    cache: dict[str, np.ndarray] = {}
    for spec in specs:
        if spec.factor not in cache:
            cache[spec.factor] = winsorize(
                pd.to_numeric(frame[spec.factor], errors="coerce").to_numpy(float)
            )
        values = cache[spec.factor]
        ok = np.isfinite(values)
        if not ok.any():
            out[spec.name] = float("nan")
            continue
        if spec.kind == "mean":
            out[spec.name] = float(np.mean(values[ok]))
        elif spec.kind == "std":
            out[spec.name] = float(np.std(values[ok], ddof=0))
        elif spec.kind == "capw":
            w = weights[ok]
            total = float(w.sum())
            out[spec.name] = (
                float(np.sum(values[ok] * w) / total) if total > 0 else float("nan")
            )
        else:  # ls：大盘组均值 − 小盘组均值
            large = values[ok & top_mask]
            small = values[ok & bottom_mask]
            if len(large) < MIN_LEG_SIDE or len(small) < MIN_LEG_SIDE:
                out[spec.name] = float("nan")
            else:
                out[spec.name] = float(np.mean(large) - np.mean(small))
    return out


def rolling_pct_rank(
    values: np.ndarray, window: int = 252, min_periods: int = 120
) -> np.ndarray:
    """尾随窗口分位（PIT）：返回当前值在过去 ``window`` 个有效值中的分位。

    完全向量化：前置 ``window - 1`` 个 NaN 后做滑窗，逐行比较。
    窗口内有效值 < ``min_periods`` 或当前值缺失 → NaN。
    """
    x = np.asarray(values, dtype=float)
    n = len(x)
    out = np.full(n, np.nan, dtype=float)
    if n == 0:
        return out
    padded = np.concatenate([np.full(window - 1, np.nan), x])
    windows = sliding_window_view(padded, window)  # (n, window)
    current = x[:, None]
    valid = np.isfinite(windows)
    counts = valid.sum(axis=1)
    less = np.sum(valid & (windows < current), axis=1)
    equal = np.sum(valid & (windows == current), axis=1)
    ok = (counts >= min_periods) & np.isfinite(x)
    out[ok] = (less[ok] + 0.5 * equal[ok]) / counts[ok]
    return out


def rolling_pct_rank_frame(
    frame: pd.DataFrame, columns: Iterable[str], **kwargs: int
) -> pd.DataFrame:
    """对整列做滚动分位，列名加 ``_pct`` 后缀。"""
    out: dict[str, np.ndarray] = {}
    for col in columns:
        out[f"{col}_pct"] = rolling_pct_rank(frame[col].to_numpy(float), **kwargs)
    return pd.DataFrame(out, index=frame.index)


# ---------------------------------------------------------------------------
# 标签
# ---------------------------------------------------------------------------


def compute_labels(
    dates: pd.Series,
    close: pd.Series,
    open_: pd.Series,
    horizon: int,
) -> pd.DataFrame:
    """构造 H 日方向标签。

    - ``fwd_ret_h``    ：T 收盘 → T+h 收盘（主口径）；
    - ``fwd_ret_open_h``：T+1 开盘 → T+h 收盘（次日开盘成交的稳健口径）；
    - ``y_h``          ：``fwd_ret_h > 0``。
    """
    close = pd.Series(close, index=dates.index).astype(float)
    open_ = pd.Series(open_, index=dates.index).astype(float)
    fwd = close.shift(-horizon) / close - 1.0
    fwd_open = close.shift(-horizon) / open_.shift(-1) - 1.0
    frame = pd.DataFrame(
        {
            "trade_date": dates.to_numpy(),
            "close": close.to_numpy(),
            "fwd_ret": fwd.to_numpy(),
            "fwd_ret_open": fwd_open.to_numpy(),
        }
    )
    frame["y"] = np.where(
        np.isfinite(frame["fwd_ret"]), (frame["fwd_ret"] > 0).astype(float), np.nan
    )
    frame = frame.rename(
        columns={
            "fwd_ret": f"fwd_ret_{horizon}",
            "fwd_ret_open": f"fwd_ret_open_{horizon}",
            "y": f"y_{horizon}",
        }
    )
    return frame


# ---------------------------------------------------------------------------
# 切分与清洗
# ---------------------------------------------------------------------------

SPLIT_BOUNDS: dict[str, tuple[str, str]] = {
    "train": ("2018-01-02", "2024-05-31"),
    "valid": ("2024-06-03", "2025-06-30"),
    "test": ("2025-07-01", "2026-06-30"),
    "oos": ("2026-07-01", "2026-11-05"),
}
SPLIT_ORDER = ("train", "valid", "test", "oos")


def assign_split(dates: pd.Series, bounds: dict[str, tuple[str, str]]) -> np.ndarray:
    """按日期区间打切分标签；区间外为 NaN。"""
    d = pd.to_datetime(pd.Series(dates).to_numpy())
    labels = np.full(len(d), None, dtype=object)
    for name in SPLIT_ORDER:
        if name not in bounds:
            continue
        start, end = bounds[name]
        mask = (d >= pd.Timestamp(start)) & (d <= pd.Timestamp(end))
        labels[mask] = name
    return labels


def purge_overlapping_labels(
    split_labels: np.ndarray, horizon: int, embargo_extra: int = 2
) -> np.ndarray:
    """丢弃切分尾部「标签窗口伸进下一段」的样本。

    对每个非末段：段内按时间排序后，末尾 ``horizon + embargo_extra`` 行剔除。
    返回布尔保留掩码（True = 保留）。
    """
    keep = np.ones(len(split_labels), dtype=bool)
    present = [name for name in SPLIT_ORDER if np.any(split_labels == name)]
    if len(present) < 2:
        return keep
    last_present = present[-1]
    for name in present:
        if name == last_present:
            continue
        positions = np.flatnonzero(split_labels == name)
        drop_n = horizon + embargo_extra
        keep[positions[-drop_n:]] = False
    return keep


# ---------------------------------------------------------------------------
# 评估指标（纯 numpy）
# ---------------------------------------------------------------------------


def auc_score(y: np.ndarray, p: np.ndarray) -> float:
    """Mann-Whitney AUC；y 需为 0/1，p 为分数。"""
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    ok = np.isfinite(y) & np.isfinite(p)
    y, p = y[ok], p[ok]
    pos = y == 1
    neg = y == 0
    n_pos, n_neg = int(pos.sum()), int(neg.sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty(len(p), dtype=float)
    sorted_p = p[order]
    # 处理并列：平均秩
    i = 0
    while i < len(sorted_p):
        j = i
        while j + 1 < len(sorted_p) and sorted_p[j + 1] == sorted_p[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    rank_sum = ranks[pos].sum()
    return float((rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def block_bootstrap_auc(
    y: np.ndarray, p: np.ndarray, block: int = 5, n_boot: int = 400, seed: int = 7
) -> tuple[float, float]:
    """块自助法 AUC 95% 置信区间（保留重叠标签的自相关结构）。"""
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    n = len(y)
    if n < 20:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    stats: list[float] = []
    for _ in range(n_boot):
        starts = rng.integers(0, max(1, n - block + 1), size=n_blocks)
        idx = np.concatenate([np.arange(s, min(s + block, n)) for s in starts])
        if len(idx) == 0:
            continue
        value = auc_score(y[idx], p[idx])
        if np.isfinite(value):
            stats.append(value)
    if not stats:
        return (float("nan"), float("nan"))
    return (
        float(np.percentile(stats, 2.5)),
        float(np.percentile(stats, 97.5)),
    )


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """二项比例的 Wilson 置信区间（高置信子集的命中率用）。"""
    if total <= 0:
        return (float("nan"), float("nan"))
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = z * np.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (float(center - half), float(center + half))


def long_flat_curve(
    returns: np.ndarray, signals: np.ndarray, cost_per_switch: float = 0.0005
) -> dict[str, float]:
    """非重叠采样后的 long/flat 曲线指标；signals ∈ {0,1}。"""
    r = np.asarray(returns, dtype=float)
    s = np.asarray(signals, dtype=float)
    ok = np.isfinite(r) & np.isfinite(s)
    r, s = r[ok], s[ok]
    if len(r) == 0:
        return {}
    switches = np.abs(np.diff(np.concatenate([[0.0], s])))
    net = s * r - switches * cost_per_switch
    equity = np.cumprod(1.0 + net)
    total = float(equity[-1] - 1.0)
    n = len(net)
    ann = (1.0 + total) ** (252.0 / max(n, 1)) - 1.0
    peak = np.maximum.accumulate(equity)
    mdd = float(np.min(equity / peak - 1.0))
    std = float(np.std(net))
    sharpe = float(np.mean(net) / std * np.sqrt(252.0)) if std > 0 else float("nan")
    return {
        "n": float(n),
        "total_return": total,
        "cagr": ann,
        "max_drawdown": mdd,
        "sharpe": sharpe,
        "exposure": float(np.mean(s)),
        "switches": float(np.sum(switches)),
    }
