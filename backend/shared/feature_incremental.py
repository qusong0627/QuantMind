"""增量特征快车道（T-P6-07）：窗口数组 → TIER 列逐值求值（numpy，无 pandas 开销）。

**口径契约（评审红线）**：本模块每个公式都必须是 ``feature_defs.compute_features_for_group``
同名输出的**逐值等价实现**（含 NaN 语义/min_periods/clip 顺序）——由真实数据金样测试强制：
``backend/tests/test_incremental_features.py``（20 标的 × 20 日，ε=1e-9；新列必须同时进注册表
与金样，否则 G 测试失败）。批量定义为唯一口径源；本模块只是其**受锁定校验的优化求值路径**
（批量函数单标的 ≈112ms 无法满足实时节拍——2026-09-17 实测）。

覆盖范围 = ``TIER_REGISTRY``：仅声明依赖窗 ≤ ``TIER_MAX_WINDOW`` 的列（EMA/MACD/RSI 等
无限记忆特征与长窗特征不在列内，实时层走 T-1 携带，见引擎的 provenance）。

``Window`` 约定：numpy 一维数组，时间正序，**末位 = 当日形成中 bar**；长度 ≤ TIER_MAX_WINDOW。
"""

from __future__ import annotations

from typing import Any

import numpy as np

TIER_MAX_WINDOW = 46  # 45 历史 + 形成中 bar（flow_vpin_ma_20 依赖 40 行）

_NAN = np.nan


# ── NaN 语义对齐的滚动统计（与 pandas rolling 相同的 skipna/min_periods）────


def _roll(a: np.ndarray, k: int, min_periods: int, stat: str) -> np.ndarray:
    """尾窗滚动统计；左补 k-1 个 NaN 使早期位置与 pandas 部分窗一致（skipna 计数）。"""
    k = int(k)
    n = len(a)
    out = np.full(n, _NAN)
    if n == 0:
        return out
    pad = np.concatenate([np.full(k - 1, _NAN), a.astype(float)])
    from numpy.lib.stride_tricks import sliding_window_view

    w = sliding_window_view(pad, k)  # shape (n, k)，w[i] = a[i-k+1..i]
    cnt = np.sum(~np.isnan(w), axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        if stat == "mean":
            s = np.nansum(w, axis=1)
            vals = s / cnt
        elif stat == "sum":
            vals = np.nansum(w, axis=1)
        elif stat == "std":
            s = np.nansum(w, axis=1)
            s2 = np.nansum(w * w, axis=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                var = (s2 - s * s / cnt) / (cnt - 1)
            vals = np.sqrt(np.clip(var, 0, None))
        elif stat == "max":
            vals = np.where(
                cnt > 0, np.nanmax(np.where(np.isnan(w), -np.inf, w), axis=1), _NAN
            )
            vals = np.where(np.isinf(vals), _NAN, vals)
        elif stat == "min":
            vals = np.where(
                cnt > 0, np.nanmin(np.where(np.isnan(w), np.inf, w), axis=1), _NAN
            )
            vals = np.where(np.isinf(vals), _NAN, vals)
        else:  # pragma: no cover
            raise ValueError(stat)
    ok = cnt >= max(1, int(min_periods))
    if stat == "std":
        ok = ok & (cnt >= 2)  # pandas std 单样本 → NaN
    out = np.where(ok, vals, _NAN)
    return out


def _pct(a: np.ndarray) -> np.ndarray:
    """pct_change：a[i]/a[i-1]-1（首位 NaN）；除零产生 ±inf 保留（调用方按 pandas 顺序处理）。"""
    out = np.full(len(a), _NAN)
    with np.errstate(invalid="ignore", divide="ignore"):
        out[1:] = a[1:] / a[:-1] - 1.0
    return out


def _shift(a: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(a), _NAN)
    if n < len(a):
        out[n:] = a[: len(a) - n]
    return out


def _inf_to_nan(a: np.ndarray) -> np.ndarray:
    return np.where(np.isinf(a), _NAN, a)


# ── 快车道求值（每列为 feature_defs 同名输出的逐值等价）────────────────────


def compute_tier(w: Window) -> dict[str, Any]:
    """窗口（末位=形成中 bar）→ TIER 全列 dict；不足窗口按 min_periods 语义自然 NaN。"""
    c = w.close
    h = w.high
    lo = w.low
    v = w.volume
    amt = w.amount

    ln_c = np.log(np.clip(c, 1e-8, None))
    log_ret = _inf_to_nan(
        np.append([_NAN], np.diff(ln_c))
    )  # pandas: ln_c.diff() 首 NaN；inf→NaN
    ret = np.clip(_pct(c), -1.0, 10.0)  # mom_ret_1d 基底（clip 后无 inf，pandas 同序）

    out: dict[str, Any] = {}

    # ── 动量（价格） ──
    out["mom_ret_1d"] = _last(ret)
    out["mom_ret_5d"] = _last(c / _shift(c, 5) - 1.0)
    out["mom_ret_20d"] = _last(c / _shift(c, 20) - 1.0)
    ma5 = _roll(c, 5, 1, "mean")
    ma20 = _roll(c, 20, 1, "mean")
    out["mom_ma_gap_5"] = _last(c / ma5 - 1.0)
    out["mom_ma_gap_20"] = _last(c / ma20 - 1.0)
    hi20 = _roll(c, 20, 1, "max")
    out["mom_breakout_20d"] = _last(c / hi20 - 1.0)
    ret_1d = _pct(c)
    out["ret_1d_lag1"] = _last(_shift(ret_1d, 1))
    out["ret_1d_lag2"] = _last(_shift(ret_1d, 2))

    # ── 波动率 ──
    out["vol_std_10"] = _last(_roll(log_ret, 10, 3, "std"))
    out["vol_std_20"] = _last(_roll(log_ret, 20, 5, "std"))
    tr = _true_range(c, h, lo)
    out["vol_atr_14"] = _last(_roll(tr, 14, 1, "mean"))
    out["vol_true_range"] = _last(tr)
    hl_ratio = np.log(h / np.clip(lo, 1e-8, None))
    out["vol_parkinson_20"] = _last(
        np.sqrt(np.clip(_roll(hl_ratio**2, 20, 5, "mean"), 0, None) / (4 * np.log(2)))
    )
    gk_term = 0.5 * (log_ret**2) - (2 * np.log(2) - 1) * (log_ret**2)
    out["vol_gk_20"] = _last(np.sqrt(np.clip(_roll(gk_term, 20, 5, "mean"), 0, None)))
    out["vol_rs_20"] = _last(
        np.sqrt(np.clip(_roll(np.clip(log_ret, 0, None) ** 2, 20, 5, "mean"), 0, None))
    )
    out["vol_downside_20"] = _last(_roll(np.clip(log_ret, None, 0), 20, 5, "std"))

    # ── 流动性 ──
    out["liq_volume"] = _last(v)
    out["liq_amount"] = _last(amt)
    out["liq_volume_ma_20"] = _last(_roll(v, 20, 1, "mean"))
    out["liq_amount_ma_20"] = _last(_roll(amt, 20, 1, "mean"))
    v5 = _roll(v, 5, 1, "mean")
    a5 = _roll(amt, 5, 1, "mean")
    out["liq_volume_ratio_5"] = _last(v / np.clip(v5, 1, None) - 1.0)
    out["liq_amount_ratio_5"] = _last(amt / np.clip(a5, 1, None) - 1.0)
    abs_ret = np.abs(ret)
    out["liq_amihud_20"] = _last(_roll(abs_ret / np.clip(amt, 1, None), 20, 1, "mean"))
    hl_range = h - lo
    clv = ((c - lo) - (h - c)) / np.where(hl_range == 0, _NAN, hl_range)
    clv = np.where(np.isnan(clv), 0.0, clv)
    out["liq_accdist_20"] = _last(_roll(clv * v, 20, 1, "sum"))

    # ── 资金流 ──
    diff_c = np.append([_NAN], np.diff(c))
    direction = np.sign(diff_c)
    out["flow_net_amount"] = _last(_roll(amt * direction, 5, 1, "sum"))
    amt_sum20 = _roll(amt, 20, 1, "sum")
    with np.errstate(invalid="ignore", divide="ignore"):
        out["flow_net_amount_ratio"] = _last(
            _roll(amt * direction, 5, 1, "sum") / np.clip(amt_sum20, 1, None)
        )
    buy_vol = np.where(c > _shift(c, 1), v, 0.0)
    sell_vol = np.where(c <= _shift(c, 1), v, 0.0)
    vpin = _roll(np.abs(buy_vol - sell_vol), 20, 5, "sum") / np.clip(
        _roll(v, 20, 5, "sum"), 1, None
    )
    out["flow_vpin"] = _last(vpin)
    out["flow_vpin_ma_5"] = _last(_roll(vpin, 5, 1, "mean"))
    out["flow_vpin_ma_20"] = _last(_roll(vpin, 20, 1, "mean"))

    # ── 位置/距离 ──
    low_20 = _roll(lo, 20, 1, "min")
    high_20 = _roll(h, 20, 1, "max")
    out["price_position_20"] = _last(
        (c - low_20) / np.clip(high_20 - low_20, 1e-8, None)
    )
    out["dist_to_high_20"] = _last(c / np.clip(high_20, 1e-8, None) - 1.0)
    out["dist_to_low_20"] = _last(c / np.clip(low_20, 1e-8, None) - 1.0)

    return out


def _last(a: np.ndarray) -> float:
    value = float(a[-1]) if len(a) else _NAN
    return value


def _true_range(c: np.ndarray, h: np.ndarray, lo: np.ndarray) -> np.ndarray:
    """TR = max(h-lo, |h-c_prev|, |lo-c_prev|)，行内 skipna（pandas 同语义）。"""
    prev = _shift(c, 1)
    parts = np.stack([h - lo, np.abs(h - prev), np.abs(lo - prev)], axis=0)
    cnt = np.sum(~np.isnan(parts), axis=0)
    with np.errstate(invalid="ignore"):
        vals = np.nanmax(np.where(np.isnan(parts), -np.inf, parts), axis=0)
    return np.where(cnt > 0, vals, _NAN)


class Window:
    """快车道输入窗口（numpy 数组，末位=形成中 bar）。"""

    __slots__ = ("close", "high", "low", "volume", "amount")

    def __init__(
        self,
        close: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        volume: np.ndarray,
        amount: np.ndarray,
    ) -> None:
        self.close = np.asarray(close, dtype=float)
        self.high = np.asarray(high, dtype=float)
        self.low = np.asarray(low, dtype=float)
        self.volume = np.asarray(volume, dtype=float)
        self.amount = np.asarray(amount, dtype=float)


# ── 注册表（G 测试：注册表与金样测试必须覆盖同一集合）──────────────────────

TIER_COLUMNS: tuple[str, ...] = tuple(
    sorted(
        [
            "mom_ret_1d",
            "mom_ret_5d",
            "mom_ret_20d",
            "mom_ma_gap_5",
            "mom_ma_gap_20",
            "mom_breakout_20d",
            "ret_1d_lag1",
            "ret_1d_lag2",
            "vol_std_10",
            "vol_std_20",
            "vol_atr_14",
            "vol_true_range",
            "vol_parkinson_20",
            "vol_gk_20",
            "vol_rs_20",
            "vol_downside_20",
            "liq_volume",
            "liq_amount",
            "liq_volume_ma_20",
            "liq_amount_ma_20",
            "liq_volume_ratio_5",
            "liq_amount_ratio_5",
            "liq_amihud_20",
            "liq_accdist_20",
            "flow_net_amount",
            "flow_net_amount_ratio",
            "flow_vpin",
            "flow_vpin_ma_5",
            "flow_vpin_ma_20",
            "price_position_20",
            "dist_to_high_20",
            "dist_to_low_20",
        ]
    )
)
