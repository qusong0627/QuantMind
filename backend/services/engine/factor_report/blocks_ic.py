"""显著性块 + IC 块（§2、§3）—— 对外入口仍是 :mod:`blocks`。

两块都只读构建期落盘的逐日序列，**没有任何 IO**（基准取数在 :mod:`blocks_bench`），
因此可以整体搬到别处复用而不拖来依赖。

口径分界（与整个读时派生层同源，见 :mod:`blocks_common`）：本文件里名字带 ``_full``
的才是全窗口序列，其余「按天取均值」的量一律服从调用方给的 ``tail_slice``。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from . import metrics as M
from .blocks_common import (
    IC_AUTOCORR_LAGS,
    INDEPENDENCE_TOP,
    N_QUANTILES,
    ROLL_LONG,
    _col,
    _col_mean,
    _f,
    _hist,
    _seq,
)


# ─────────────────────────── 2. 显著性 ───────────────────────────


def _library_t_values(snapshot: dict[str, Any] | None) -> tuple[np.ndarray, int]:
    """全库因子的普通 t 值（多重检验校正的分母）。

    ``t = ICIR × √n``，与 :func:`metrics.plain_tstat` 同式 —— 序列级 t 值就是
    均值/标准误，ICIR=均值/标准差，两者恒等。校正必须**在全库上做**，
    只对本因子校正等于没校正。
    """
    snap = snapshot or {}
    meta = snap.get("meta") or {}
    n = int(meta.get("n_dates") or 0)
    tvals = []
    for f in snap.get("factors") or []:
        icir = _f(f.get("icir"))
        if icir is not None and n > 0:
            tvals.append(abs(icir) * math.sqrt(n))
    return np.asarray(tvals, dtype=np.float64), n


def significance_block(ic: np.ndarray, ls_daily: np.ndarray, snapshot: dict[str, Any] | None,
                       factor: str) -> dict[str, Any]:
    """普通 t / Newey-West t / p / BHY q / Deflated Sharpe / Bootstrap CI。"""
    t_plain = M.plain_tstat(ic)
    t_nw = M.nw_tstat(ic)
    p = M.normal_pvalue(t_plain)
    lib_t, n_lib = _library_t_values(snapshot)
    q = None
    if p is not None and lib_t.size:
        lib_p = np.asarray([M.normal_pvalue(t) or 0.0 for t in lib_t], dtype=np.float64)
        # 本因子的 p 一并进池（多重检验必须包含被检验者本人）
        pooled = np.append(lib_p, p)
        try:
            qs = M.bhy_qvalues(pooled)
            q = _f(qs[-1])
        except ValueError:
            q = None
    rd = M.return_distribution(ls_daily)
    kurt_raw = rd["kurt"] + 3.0 if rd["kurt"] is not None else 3.0
    dsr = M.deflated_sharpe(
        M.brain_headline(ls_daily, None)["ir"], n_trials=max(n_lib, 1),
        n_obs=int(rd["n"]), skew=rd["skew"] or 0.0, kurt=float(kurt_raw),
    )
    # NW 显著缩小 → 普通 t 高估了显著性；这是该函数存在的唯一理由，故显式给出判据
    shrunk = (
        t_plain is not None and t_nw is not None and abs(t_nw) < abs(t_plain) * 0.8
    )
    return {
        "available": True,
        "t_value": _f(t_plain),
        "nw_t_value": _f(t_nw),
        "nw_lag": M.newey_west_lag(int(np.isfinite(ic).sum())),
        "p_value": _f(p),
        "q_value_bhy": q,
        "n_factors_tested": int(n_lib) if n_lib else None,
        "nw_shrunk": bool(shrunk),
        "deflated_sharpe": _f(dsr),
        "bootstrap_ic_mean": M.bootstrap_ci(ic, stat="mean"),
        "bootstrap_ir": M.bootstrap_ci(ls_daily, stat="ir"),
    }


# ─────────────────────────── 3. IC 块 ───────────────────────────


def ic_block(df: Any, dates_all: list[str], tail_slice: slice, snapshot: dict[str, Any] | None,
             factor: str, k: int) -> dict[str, Any]:
    """IC 全体系：多口径均值、累计、衰减、自相关、分布、分域、滚动、独立性。"""
    ic_all = df["ic"].to_numpy(dtype=np.float64)
    ic = ic_all[tail_slice]
    win = df[tail_slice]
    # ⚠️ 口径分界：**按天取均值的统计量一律走请求窗口**，只有名字带 `_full` 的
    # 累计序列才走全窗口。之前 top/bot/neutral/domain/clip 全用整列，于是同一个
    # 卡片上「全截面 IC 均值」是 250 天、「Top 半 IC 均值」是 2588 天 —— 两个数
    # 并排显示却不同源，且「中性化 2564 天」会配一张 250 个点的图。
    top = _col(win, "ic_top")
    bot = _col(win, "ic_bot")
    # 累计半 IC 是**累计曲线**，与 ``ic_cum_full`` 同轴（全窗口 + ``cum_dates_full``）。
    # 必须读整列：250 点的序列配 2600 个类目标签，前端那张三线图里两条半 IC 只会
    # 被画在最左侧 ~10% —— 看着有图，其实只画了窗口那一小段（静默错图）。
    top_all = _col(df, "ic_top")
    bot_all = _col(df, "ic_bot")
    neu = _col(win, "ic_neutral")
    dom = {name: _col(win, f"ic_{name}") for name in ("large", "mid", "small")}
    out: dict[str, Any] = {
        "available": True,
        "dates": dates_all[tail_slice],
        "ic_series": _seq(ic),
        "ic_rolling": M.rolling_mean(ic, 20),
        "ic_rolling_long": M.rolling_mean(ic, ROLL_LONG),
        "ic_cum_full": _seq(M.cum_ic(ic_all)),
        "cum_dates_full": dates_all,
        "cum_ic_top_full": _seq(M.cum_ic(top_all)) if top_all is not None else None,
        "cum_ic_bot_full": _seq(M.cum_ic(bot_all)) if bot_all is not None else None,
        "ic_mean": _f(np.nanmean(ic)) if np.isfinite(ic).any() else None,
        "ic_std": _f(np.nanstd(ic)) if np.isfinite(ic).any() else None,
        "win_rate": M.win_rate(ic),
        "ic_top_mean": _f(np.nanmean(top)) if top is not None and np.isfinite(top).any() else None,
        "ic_bot_mean": _f(np.nanmean(bot)) if bot is not None and np.isfinite(bot).any() else None,
        "ic_neutral_mean": _f(np.nanmean(neu)) if neu is not None and np.isfinite(neu).any() else None,
        "ic_neutral_days": int(np.isfinite(neu).sum()) if neu is not None else 0,
        "ic_neutral_series": _seq(neu) if neu is not None else None,
        "ic_domain": {
            name: (_f(np.nanmean(a)) if a is not None and np.isfinite(a).any() else None)
            for name, a in dom.items()
        },
        "ic_domain_series": {name: (_seq(a) if a is not None else None) for name, a in dom.items()},
        "ic_hist": _hist(ic),
        "ic_autocorr": M.ic_autocorr(ic, lags=IC_AUTOCORR_LAGS),
        "decay": _decay_curve(win),
        # 独立性来自构建期在**全窗口**上算的相关矩阵，按天切不了 —— 保持全窗口口径
        "independence": _independence(snapshot, factor),
        "clip_frac_mean": _col_mean(win, "clip_frac"),
        "n_valid_mean": _col_mean(win, "n_valid"),
    }
    out["half_life_days"] = M.half_life_days(
        {h: v for h, v in (out["decay"] or {}).items() if v is not None}
    )
    out["ir_rolling_full"] = M.rolling_ir(ic_all, win=ROLL_LONG)
    out["monotonicity"] = M.monotonicity(
        [_f(np.nanmean(df[f"q{i}"].to_numpy(dtype=np.float64))) for i in range(1, N_QUANTILES + 1)]
    )
    return out


def _decay_curve(df: Any) -> dict[int, float | None] | None:
    """IC 衰减：各前瞻期的 IC 均值（``ic_{h}`` 列）。一期都没有 → None。"""
    out: dict[int, float | None] = {}
    for h in (1, 2, 3, 5, 10, 20):
        a = _col(df, f"ic_{h}")
        if a is not None and np.isfinite(a).any():
            out[h] = _f(np.nanmean(a))
    return out or None


def _independence(snapshot: dict[str, Any] | None, factor: str) -> dict[str, Any] | None:
    """与全库最强相关的 Top-N 因子的 |ρ| —— 回答「是不是又一个复制品」。"""
    snap = snapshot or {}
    corr = snap.get("correlation") or {}
    names = list(corr.get("factors") or [])
    matrix = list(corr.get("matrix") or [])
    if factor not in names or not matrix:
        return None
    i = names.index(factor)
    peers = [
        {"name": n, "corr": _f(matrix[i][j])}
        for j, n in enumerate(names)
        if j != i and j < len(matrix[i])
    ]
    peers = [p for p in peers if p["corr"] is not None]
    peers.sort(key=lambda p: abs(p["corr"]), reverse=True)
    top = peers[:INDEPENDENCE_TOP]
    absv = [abs(p["corr"]) for p in top]
    return {
        "max_corr": absv[0] if absv else None,
        "mean_corr_top": float(np.mean(absv)) if absv else None,
        "n_peers": len(top),
        "peers": top[:8],
        "note": f"与全库 |ρ| 最大的 {INDEPENDENCE_TOP} 个因子；与评分卡的独立性维度同源口径。",
    }
