"""统计派 · 换手/扣费证据（T-FE-03）：多头谓词与成本率均取单一出处。

- 多头集合 ``S_t`` = 当日截面 ``rank(pct) >= LONG_RANK_PCT_THRESHOLD``
  （0.7，前 30%）——常量**直接导入**回测路径
  ``mining_plugins.evaluators.turnover_cost``，不复制数值；统计派报告与
  回测页的换手口径由跨实现对拍测试（``test_statistical_costs.py``）钉住。
- 换手 ``to_t = |S_t∖S_{t−1}| / |S_t|``，首日无「昨日名单」= NaN（不计成本，
  回测路径同行为）；因子样本 < ``min_names`` 的日子整日剔除（阈值与逐日 IC
  同源，但计的是因子有效样本、IC 计的是 (因子, 标签) 有效对——近似同口径，
  ``S_{t−1}`` 跨过该日——休市日不产生交易）。
- **扣费净 IC**（:func:`net_rank_ic`）：当日新进 ``S_t`` 的股票从 1 日前向
  收益里扣 ``cost_rate``（双边，单源 ``mining_plugins.config.get_cost_rate``：
  env > yaml > ``factor_research.COST_RATE``）后，与因子值算逐日 RankIC 的
  均值——回答「预测力扛不扛得住它自己触发的交易」。首日无换新可言，不扣费
  但当天照常计入 IC。

``min_days`` 不足 → 均值置 NaN、实际天数照报（不硬凑，同
:func:`ic_stats.summarize_ic`)。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import ic_stats as _ic

ANNUALIZE = 252
LONG_RANK_PCT_THRESHOLD = 0.7

__all__ = [
    "ANNUALIZE",
    "LONG_RANK_PCT_THRESHOLD",
    "MAX_REBALANCE_DAYS",
    "daily_turnover_long",
    "net_rank_ic",
    "suggest_rebalance_days",
]

#: 调仓频率建议上限：自相关半衰期超过一交易年即封顶——ρ₁→1 时 ln2/−lnρ₁ 发散，
#: 没有上限的「建议」不是建议（也防御浮点 ρ₁=1 的除零）。
MAX_REBALANCE_DAYS = 252


def suggest_rebalance_days(rho: float) -> int | None:
    """因子自相关 ρ₁（lag-1 日截面 Rank 自相关）→ 调仓频率建议（天）。

    自相关半衰期 ``ln2/−ln ρ₁`` 向上取整：ρ₁=0.9 → 7 天，ρ₁=0.5 → 1 天
    （区别于多周期 IC 衰减的 ic_half_life——那是另一条证据线）。
    口径与护栏：

    - ``ρ₁ ≤ 0``（无持续性）→ **1**（每日重排，别囤隔夜名单）；
    - ``ρ₁ ≥ 1``（浮点病态）或半衰期超一年 → 封顶 ``MAX_REBALANCE_DAYS``；
    - 非有限（NaN/Inf = 没有自相关证据）→ **None**——不出建议，不是拿 1
      冒充「按日频」的结论（缺失与结论分得开，同「缺失一律 —」纪律）。
    """
    if not np.isfinite(rho):
        return None
    if rho >= 1.0:
        return MAX_REBALANCE_DAYS
    if rho <= 0.0:
        return 1
    days = math.ceil(math.log(2.0) / -math.log(rho))
    return min(MAX_REBALANCE_DAYS, max(1, days))


def _long_membership(
    factor: pd.Series, top_pct: float, min_names: int
) -> dict[pd.Timestamp, frozenset]:
    """逐日多头名单（rank pct ≥ ``top_pct``）；样本不足 ``min_names`` 的日子剔除。"""
    frame = factor.dropna()
    if frame.empty:
        return {}
    pct = frame.groupby(level=0).rank(pct=True)
    sizes = frame.groupby(level=0).size()
    keep = {day for day, n in sizes.items() if n >= min_names}
    longs = frame[pct >= top_pct]
    return {
        day: frozenset(sub.index.get_level_values(1))
        for day, sub in longs.groupby(level=0)
        if day in keep
    }


def daily_turnover_long(
    factor: pd.Series,
    *,
    top_pct: float = LONG_RANK_PCT_THRESHOLD,
    min_names: int = _ic.DEFAULT_MIN_NAMES_PER_DAY,
) -> pd.Series:
    """多头名单日换手 ``|S_t∖S_{t−1}|/|S_t|``（index=trade_date，首日 NaN）。"""
    membership = _long_membership(factor, top_pct, min_names)
    if not membership:
        empty = pd.Series(dtype="float64", name="turnover_daily")
        return empty.rename_axis("trade_date")
    rows: dict[pd.Timestamp, float] = {}
    prev: frozenset | None = None
    for day in sorted(membership):
        current = membership[day]
        if prev is None or not current:
            rows[day] = float("nan")
        else:
            rows[day] = len(current - prev) / len(current)
        prev = current
    out = pd.Series(rows, dtype="float64", name="turnover_daily")
    out.index.name = "trade_date"
    return out


def net_rank_ic(
    factor: pd.Series,
    fwd_ret: pd.Series,
    *,
    cost_rate: float | None = None,
    top_pct: float = LONG_RANK_PCT_THRESHOLD,
    min_names: int = _ic.DEFAULT_MIN_NAMES_PER_DAY,
    min_days: int = 0,
) -> tuple[float, int]:
    """扣费净 RankIC → ``(均值, 有效天数)``；``cost_rate=None`` 走单源默认。

    ``min_days`` 不足：均值 NaN、实际天数如实返回（审计要知道是「算出来不
    显著」还是「根本不够算」）。
    """
    # The source project read a mutable global plugin config. QuantMind binds the
    # default to its existing A-share backtest cost model; callers can pass an
    # immutable task-snapshot override explicitly. Import lazily: the inference
    # package __init__ pulls platform config, which must not enter the sandbox
    # when the caller already supplied a frozen cost rate.
    if cost_rate is None:
        from backend.services.engine.inference.trading_cost import CostModel

        rate = CostModel.resolve().round_trip_cost()
    else:
        rate = float(cost_rate)
    fwd = fwd_ret.astype(float)
    membership = _long_membership(factor, top_pct, min_names)
    if membership and rate:
        fresh: list[tuple] = []
        prev: frozenset | None = None
        for day in sorted(membership):
            current = membership[day]
            if prev is not None:
                fresh.extend((day, sym) for sym in current - prev)
            prev = current
        if fresh:
            fresh_idx = pd.MultiIndex.from_tuples(fresh, names=fwd.index.names)
            # reindex(fill_value=0.0) 已把不在 fwd 里的新进名单自然滤掉——
            # 不需要先 intersection（pandas3 下见 [[pandas3-index-intersection-empty]]：
            # 空集/口径陷阱，评审 M1 已删）
            cost = pd.Series(rate, index=fresh_idx)
            # 非就地：fwd 是新对象，调用方的序列不被改写
            fwd = fwd - cost.reindex(fwd.index, fill_value=0.0)
    daily = _ic.daily_ic(factor, fwd, min_names=min_names)
    n = int(len(daily))
    if n == 0:
        return float("nan"), 0
    if n < min_days:
        return float("nan"), n
    return float(daily["rank_ic"].mean()), n
