"""换手/扣成本评估器：多头名单变动 + 按换手扣双边成本后的净指标。

多头集合 S_t = 当日 rank(pct)>=0.7（与回测路径毛组合同一谓词，不另立口径）；
to_t = |S_t∖S_{t-1}|/|S_t|；首日无定义被排除（首日不计成本，AlphaEval 同行为）；
r_net = r − to×cost_rate（研究口径 0.2% 双边，来源
``factor_research.analysis.COST_RATE`` 单一出处）。

净指标与毛指标同款「>1 个点」门——毛指标见 ``_backtest_via_qlib``
（``if len(longs) > 1``）。年化 = ×252。
金样：``backend/tests/fixtures/miningMetricsGolden.json``（改公式必须过金样）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import EvalContext, MetricDescriptor
from ..registry import register_evaluator

LONG_RANK_PCT_THRESHOLD = 0.7  # 多头 = 截面 rank 前 30%
ANNUALIZE = 252.0

_DESCRIPTORS = (
    MetricDescriptor(
        key="turnover_daily",
        label="日均换手",
        group="trading",
        unit="ratio",
        better="lower",
        precision=4,
        description="多头组合（截面 rank 前 30%）名单日均变动比",
    ),
    MetricDescriptor(
        key="ann_turnover",
        label="年化换手",
        group="trading",
        unit="ratio",
        better="lower",
        precision=2,
        description="日均换手 × 252",
    ),
    MetricDescriptor(
        key="ann_return_net",
        label="扣费年化收益",
        group="trading",
        unit="pct",
        better="higher",
        precision=2,
        description="按换手比例扣除双边成本后的年化收益（研究口径 0.2%）",
    ),
    MetricDescriptor(
        key="sharpe_net",
        label="扣费夏普",
        group="trading",
        unit="ratio",
        better="higher",
        precision=2,
        description="扣费日收益的年化夏普",
    ),
    MetricDescriptor(
        key="max_drawdown_net",
        label="扣费最大回撤",
        group="trading",
        unit="pct",
        better="lower",
        precision=2,
        description="扣费净值最大回撤",
    ),
)


def daily_long_ret_and_turnover(
    paired: pd.DataFrame,
) -> tuple[pd.Series, pd.Series]:
    """多头日收益与日换手（本模块唯一出处；组合实验室的净曲线也走这里）。

    返回 ``(daily_ret, turnover)``：首日换手为 NaN（无「昨日名单」，不计成本，
    AlphaEval 同行为）；空输入/无多头时返回空 Series。
    """
    empty = pd.Series(dtype="float64")
    f_rank = paired.groupby("datetime")["factor"].rank(pct=True)
    longs = paired[f_rank >= LONG_RANK_PCT_THRESHOLD]
    daily = longs.groupby("datetime")["ret"].mean().dropna().sort_index()
    if daily.empty:
        return daily, empty

    holders = longs.groupby("datetime")["symbol"].apply(set)
    to_rows: list[float] = []
    prev: set | None = None
    for dt in daily.index:
        current = holders.get(dt) or set()
        if prev is None or not current:
            to_rows.append(np.nan)  # 首日无「昨日名单」，不计换手也不计成本
        else:
            to_rows.append(len(current - prev) / len(current))
        if current:
            prev = current
    return daily, pd.Series(to_rows, index=daily.index, dtype="float64")


def daily_net_returns(paired: pd.DataFrame, cost_rate: float) -> pd.Series:
    """扣成本后的多头日收益序列（净值的唯一口径出处，索引=日期升序）。"""
    daily, to_series = daily_long_ret_and_turnover(paired)
    if daily.empty:
        return daily
    return (daily - to_series * cost_rate).dropna()


def compute_turnover_cost(
    paired: pd.DataFrame, cost_rate: float
) -> dict[str, float | None]:
    """paired：列 datetime/symbol/factor/ret（均已有限）。样本不足的键返回 None。"""
    out: dict[str, float | None] = {
        "turnover_daily": None,
        "ann_turnover": None,
        "ann_return_net": None,
        "sharpe_net": None,
        "max_drawdown_net": None,
    }
    if paired is None or paired.empty:
        return out

    daily, to_series = daily_long_ret_and_turnover(paired)
    if daily.empty:
        return out

    valid_to = to_series.dropna()
    if len(valid_to) > 0:
        out["turnover_daily"] = float(valid_to.mean())
        out["ann_turnover"] = out["turnover_daily"] * ANNUALIZE

    r_net = (daily - to_series * cost_rate).dropna()
    if len(r_net) <= 1:
        return out
    out["ann_return_net"] = float(r_net.mean() * ANNUALIZE)
    out["sharpe_net"] = float(
        r_net.mean() / (r_net.std(ddof=1) + 1e-8) * np.sqrt(ANNUALIZE)
    )
    cum = (1.0 + r_net).cumprod()
    peak = cum.cummax()
    dd = (peak - cum) / peak
    out["max_drawdown_net"] = float(dd.max()) if len(dd) else None
    return out


class _TurnoverCostEvaluator:
    name = "turnover_cost"
    descriptors = _DESCRIPTORS

    def evaluate(self, ctx: EvalContext) -> dict[str, float | None]:
        return compute_turnover_cost(ctx.paired, ctx.cost_rate)


register_evaluator(_TurnoverCostEvaluator())
