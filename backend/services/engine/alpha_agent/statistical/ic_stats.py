"""统计派 · 截面 IC 统计核心（移植 P2 / Tier 1）。

口径全部有测试钉住：

- **逐日截面**：Pearson IC（对收益）与 Spearman RankIC（双向先秩再算 Pearson，
  与 ``scipy.spearmanr`` 同值）；单日有效样本 < ``DEFAULT_MIN_NAMES_PER_DAY``
  （10，参考实现口径）不计；零方差截面（停牌/一字板）corr=NaN 自然剔除；
- **ICIR = mean(RankIC) / std(RankIC)**（日频、非年化——与参考实现一致）；
  t 值走 **Newey–West 修正**（Bartlett HAC 标准误；逐日 IC 序列自相关会让
  朴素 ``ICIR·√n`` 高估显著性，见 :func:`newey_west_t_stat`），朴素 t 保留
  为 ``rank_ic_t_naive`` 审计对照；p 值 Student-t(df=n−1)（NW 只修正标准误，
  参考分布口径不变——金样钉住）；有效日 < ``DEFAULT_MIN_VALID_DAYS``（40）
  不出统计（样本不足 → ``None``，不硬凑）；
- **分组收益**：逐日按因子值等份分 Q 组（rank 法，避免并列），报组均收益、
  Q_top−Q_bottom 价差均值/t 值与单调性 τ（分位序 × 组均收益的 Spearman）——
  IC 低但分组单调的因子与 IC 高但乱序的因子不是一回事，入库证据要分开看；
- **多重检验**：候选集内对 t 值做 Benjamini–Hochberg FDR（q=0.10）——在 N 个
  候选里挑 Top-K 本身就是选择偏差，不修正的「显著」是彩票；
- **因子间相关**（:func:`daily_rank_corr`）：逐日 Spearman 的时间均值——相关性
  去重（Tier 3）与 IC 走同一批逐日截面口径，不另起炉灶。

前向收益按 **symbol 内交易 bar 序** shift（停牌自然跳过），close-to-close。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats as _scipy_stats

#: 单日截面最少有效样本（参考实现口径）
DEFAULT_MIN_NAMES_PER_DAY = 10
#: 出统计所需的最少有效 IC 日（参考实现口径）
DEFAULT_MIN_VALID_DAYS = 40
#: 分组收益默认组数
DEFAULT_QUANTILES = 5
#: BH-FDR 默认水平
DEFAULT_FDR_Q = 0.10


@dataclass(frozen=True)
class IcSummary:
    """单候选 × 单周期的截面 IC 统计（``n_days`` = 有效 IC 日）。"""

    n_days: int
    ic_mean: float
    rank_ic_mean: float
    rank_ic_std: float
    icir: float
    #: Newey–West 修正 t（规范统计量：喂 p 值 / FDR / 落库 t_stat）
    rank_ic_t: float
    rank_ic_p: float
    rank_ic_win_rate: float
    #: 朴素 t = ICIR·√n（审计对照列；自相关强时 NW 与它分离才是正常）
    rank_ic_t_naive: float = float("nan")
    #: 实际使用的 Bartlett 带宽（自动式结果；审计用）
    rank_ic_t_lags: int = 0


@dataclass(frozen=True)
class QuantileSummary:
    """分组收益证据（组序自因子值低到高）。"""

    n_days: int
    quantile_means: tuple[float, ...]
    spread_mean: float
    spread_t: float
    spread_p: float
    monotonicity_tau: float


def forward_returns(panel: pd.DataFrame, horizon: int) -> pd.Series:
    """close-to-close 前向收益：``close[t+h]/close[t] - 1``（symbol 内 bar 序）。

    ``panel`` 需为 MultiIndex ``(trade_date, symbol)`` 且按日期升序（每 symbol
    组内天然升序）；停牌缺 bar 时标签跨更长的自然日——与 参考实现同口径，
    可交易性过滤在面板侧剔除不可成交样本。
    """
    if horizon < 1:
        raise ValueError(f"horizon 必须是正整数（收到 {horizon!r}）")
    close = panel["close"]
    fwd = close.groupby(level=1).shift(-horizon) / close - 1
    return fwd.rename(f"fwd_ret_{horizon}d")


def _corr_by_day(df: pd.DataFrame, x: str, y: str) -> pd.Series:
    """逐日（MultiIndex level 0）Pearson 相关，向量化（零方差 → NaN）。"""
    grouped = df.groupby(level=0, sort=True)
    xm = df[x] - grouped[x].transform("mean")
    ym = df[y] - grouped[y].transform("mean")
    numerator = (xm * ym).groupby(level=0).sum()
    denominator = np.sqrt(
        (xm**2).groupby(level=0).sum() * (ym**2).groupby(level=0).sum()
    )
    return numerator / denominator


def daily_ic(
    factor: pd.Series,
    fwd_ret: pd.Series,
    *,
    min_names: int = DEFAULT_MIN_NAMES_PER_DAY,
) -> pd.DataFrame:
    """逐日截面 IC/RankIC（index=trade_date，columns=ic/rank_ic/n）。

    NaN 对（因子缺失/标签缺失/停牌）先丢；有效样本不足或 corr 退化的日子
    整日剔除（有效日列表本身就是统计量的一部分）。
    """
    frame = pd.concat({"factor": factor, "fwd": fwd_ret}, axis=1).dropna()
    if frame.empty:
        return pd.DataFrame(columns=["ic", "rank_ic", "n"]).rename_axis("trade_date")
    ranked = frame.assign(
        rank_factor=frame.groupby(level=0)["factor"].rank(),
        rank_fwd=frame.groupby(level=0)["fwd"].rank(),
    )
    out = pd.DataFrame(
        {
            "ic": _corr_by_day(frame, "factor", "fwd"),
            "rank_ic": _corr_by_day(ranked, "rank_factor", "rank_fwd"),
            "n": frame.groupby(level=0).size(),
        }
    ).dropna(subset=["ic", "rank_ic"])
    out = out[out["n"] >= min_names]
    out.index.name = "trade_date"
    return out


def daily_rank_corr(
    a: pd.Series,
    b: pd.Series,
    *,
    min_names: int = DEFAULT_MIN_NAMES_PER_DAY,
) -> tuple[float, int]:
    """两个截面序列的逐日 Spearman 相关 **时间均值**（因子间相关性口径）。

    与 IC 同族：先各自逐日转秩再逐日 Pearson（= 逐日 Spearman）；有效样本
    < ``min_names`` 的日子不计；零方差日（全并列）corr=NaN 自然剔除。
    返回 ``(均值, 有效日数)``；无有效日 → ``(NaN, 0)``（没证据，不是 0 相关）。
    """
    frame = pd.concat({"a": a, "b": b}, axis=1).dropna()
    if frame.empty:
        return float("nan"), 0
    ranked = frame.assign(
        rank_a=frame.groupby(level=0)["a"].rank(),
        rank_b=frame.groupby(level=0)["b"].rank(),
    )
    per_day = _corr_by_day(ranked, "rank_a", "rank_b").dropna()
    if per_day.empty:
        return float("nan"), 0
    counts = frame.groupby(level=0).size().reindex(per_day.index)
    per_day = per_day[counts >= min_names]
    if per_day.empty:
        return float("nan"), 0
    return float(per_day.mean()), int(len(per_day))


def two_sided_p(t_stat: float, dof: int) -> float:
    """Student-t 双侧 p 值（dof≤0 或非有限 t → NaN）。"""
    if not np.isfinite(t_stat) or dof <= 0:
        return float("nan")
    return float(2 * _scipy_stats.t.sf(abs(t_stat), dof))


def newey_west_lag(n: int) -> int:
    """Newey–West(1994) 自动带宽 ``L = ⌊4·(n/100)^{2/9}⌋``，钳到 [1, n//4]。"""
    if n < 2:
        return 0
    lag = int(np.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))
    return max(1, min(lag, max(1, n // 4)))


def newey_west_t_stat(
    series: pd.Series, *, lags: int | None = None
) -> tuple[float, int]:
    """序列均值的 Newey–West（Bartlett 核）t 值 → ``(t, 实际带宽)``。

    逐日 IC 序列自带自相关（重叠周期标签、信号持续），朴素 ``均值/(s/√n)``
    按独立样本算标准误 → 系统性高估显著性。NW 用 HAC 标准误修正：
    ``SE² = (γ0 + 2Σ_{l=1..L} (1−l/(L+1))·γ_l) / n``。

    口径实锤（2026-10-11 与 statsmodels
    ``OLS(...).fit(cov_type="HAC", cov_kwds={"maxlags": L, "use_correction": False})``
    六组 AR(1) 用例逐位一致，<1.1e-15；金样 ``fixtures/neweyWestGolden.json``）：

    - 带宽默认走 :func:`newey_west_lag` 自动式（可显式覆盖做敏感度）；
    - γ_l 除数 = n、核权 ``1−l/(L+1)``、**不做** statsmodels 的
      ``use_correction`` 小样本放大（n/(n−k)）；
    - HAC 方差估值非正时退回朴素方差 γ0——Bartlett 核数学上正定（真值
      ≥0），触发只可能来自浮点舍入或 NaN 病态；退的是「不拿负方差开根、
      不吐假 t」的守卫。

    **带宽非周期感知（已知口径边界）**：自动带宽只看 n（A 股 ~4 年窗
    n≈1000 → L≈6），而重叠前向标签（周期 h）的结构性自相关到 h−1；h≥10
    时自动带宽只**部分**修掉重叠自相关。要更保守的口径须显式传
    ``lags=h−1`` 起（本函数已支持，调用方自决——不默认放大，避免把
    带宽选择变成隐性假设）。
    """
    x = np.asarray(series, dtype=float)
    n = x.size
    if n < 2:
        return float("nan"), 0
    if lags is None:
        lags = newey_west_lag(n)
    lags = max(0, min(int(lags), n - 1))
    mean = float(x.mean())
    xc = x - mean
    gamma0 = float(xc @ xc) / n
    hac = gamma0
    for ell in range(1, lags + 1):
        weight = 1.0 - ell / (lags + 1.0)
        gamma_l = float(xc[ell:] @ xc[:-ell]) / n
        hac += 2.0 * weight * gamma_l
    if not hac > 0:  # 含 NaN 与负值：退回朴素方差
        hac = gamma0
    if not hac > 0:
        return float("nan"), lags
    return mean / float(np.sqrt(hac / n)), lags


def summarize_ic(
    daily: pd.DataFrame, *, min_days: int = DEFAULT_MIN_VALID_DAYS
) -> IcSummary | None:
    """逐日 IC 序列 → 汇总统计；有效日不足返回 ``None``（不硬凑统计量）。"""
    n = int(len(daily))
    if n < min_days:
        return None
    rank_ic = daily["rank_ic"].astype(float)
    mean = float(rank_ic.mean())
    std = float(rank_ic.std(ddof=1))
    icir = mean / std if std > 0 else float("nan")
    t_naive = icir * np.sqrt(n) if std > 0 else float("nan")
    # 规范 t = Newey–West（自相关修正）；朴素 t 仅作审计对照
    t_nw, nw_lags = newey_west_t_stat(rank_ic)
    return IcSummary(
        n_days=n,
        ic_mean=float(daily["ic"].mean()),
        rank_ic_mean=mean,
        rank_ic_std=std,
        icir=float(icir),
        rank_ic_t=float(t_nw),
        rank_ic_p=two_sided_p(t_nw, n - 1),
        rank_ic_win_rate=float((rank_ic > 0).mean()),
        rank_ic_t_naive=float(t_naive),
        rank_ic_t_lags=int(nw_lags),
    )


def quantile_returns(
    factor: pd.Series,
    fwd_ret: pd.Series,
    *,
    quantiles: int = DEFAULT_QUANTILES,
    min_names: int = DEFAULT_MIN_NAMES_PER_DAY,
) -> QuantileSummary | None:
    """逐日等份分组的组均收益 / 顶底价差 / 单调性 τ（日频均值口径）。

    分组用 ``rank(method="first")`` + 等份切分：并列值不产生偏组；当日有效
    样本不足一个完整分组（任一组为空）该日整日剔除。
    """
    if quantiles < 2:
        raise ValueError(f"quantiles 至少为 2（收到 {quantiles!r}）")
    frame = pd.concat({"factor": factor, "fwd": fwd_ret}, axis=1).dropna()
    if frame.empty:
        return None

    per_day_means: list[np.ndarray] = []
    spreads: list[float] = []
    taus: list[float] = []
    for _day, sub in frame.groupby(level=0, sort=True):
        if len(sub) < min_names or len(sub) < quantiles:
            continue
        ranks = sub["factor"].rank(method="first")
        try:
            bins = pd.qcut(ranks, quantiles, labels=False)
        except ValueError:  # 极端并列导致无法等份
            continue
        means = sub.groupby(bins)["fwd"].mean()
        if len(means) != quantiles:
            continue
        values = means.to_numpy(dtype=float)
        per_day_means.append(values)
        spreads.append(float(values[-1] - values[0]))
        taus.append(
            float(_scipy_stats.spearmanr(np.arange(quantiles), values).statistic)
        )

    if not per_day_means:
        return None
    n = len(per_day_means)
    spread_arr = np.asarray(spreads, dtype=float)
    spread_mean = float(spread_arr.mean())
    spread_std = float(spread_arr.std(ddof=1)) if n > 1 else float("nan")
    # 刻意朴素（与 rank_ic_t 的 NW 口径不同）：spread_t 不落库、不进任何
    # 闸，消费方只用 monotonicity_tau/spread_mean——保持「独立样本」直读，
    # 不做 NW 修正以免读者误以为它是判决量
    spread_t = (
        spread_mean / (spread_std / np.sqrt(n)) if spread_std > 0 else float("nan")
    )
    return QuantileSummary(
        n_days=n,
        quantile_means=tuple(np.mean(per_day_means, axis=0).tolist()),
        spread_mean=spread_mean,
        spread_t=float(spread_t),
        spread_p=two_sided_p(spread_t, n - 1),
        monotonicity_tau=float(np.mean(taus)),
    )


def bh_fdr(p_values: list[float], *, q: float = DEFAULT_FDR_Q) -> list[bool]:
    """Benjamini–Hochberg 步进拒绝：返回逐项是否在水平 ``q`` 下显著。

    NaN（不可评估项）视为不显著，且不计入多重检验总数 m（没有统计量的项
    不该稀释其他项的校正门槛）。
    """
    valid = [(i, p) for i, p in enumerate(p_values) if np.isfinite(p)]
    passed = [False] * len(p_values)
    m = len(valid)
    if m == 0:
        return passed
    ordered = sorted(valid, key=lambda item: item[1])
    k_max = 0
    for rank, (_idx, p) in enumerate(ordered, start=1):
        if p <= q * rank / m:
            k_max = rank
    for rank, (idx, _p) in enumerate(ordered, start=1):
        if rank <= k_max:
            passed[idx] = True
    return passed
