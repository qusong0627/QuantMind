"""因子报告机构级指标层 —— **对外唯一入口**（纯函数、无 IO、无第三方依赖）。

口径的**唯一事实源**与全部常量/文案在 :mod:`metrics_core`（那份 docstring 是这一层
的说明书，改口径先读它）。本文件只是把三块实现拼成原来的那一个命名空间，
``import metrics as M; M.xxx`` 的既有调用方一行都不用改：

============  ============================================================
``metrics_core``    口径常量 + ``DEFINITIONS`` 文案 + 基础统计原语 + IC 体系
                    + 多空/回撤 + 收益分布（§0–§3）
``metrics_eval``    BRAIN 头部指标 + 显著性（NW/BHY/Bootstrap/DSR/拥挤度）
                    + 经济性（成本/持有期/容量）+ 风格归因（§4–§7）
``metrics_series``  分段/年度/月度/滚动 + 构建期截面矩阵原语（§8、§14）
============  ============================================================

依赖是单向的：``core ← eval``、``core ← series``，eval 与 series 之间**互不依赖**。
新增指标时按上表选文件；只有在「改口径」时才动 ``metrics_core``。
"""

from __future__ import annotations

from .metrics_core import (
    CLIP_MAD_K,
    DEFAULT_COST_BPS,
    DEFAULT_PARTICIPATION,
    DEGENERATE_REL_STD,
    DEFINITIONS,
    MAD_TO_SIGMA,
    MIN_SAMPLES,
    MIN_TSTAT_SAMPLES,
    TRADING_DAYS,
    TURNOVER_FLOOR,
    cum_curve,
    cum_ic,
    drawdown_episodes,
    half_ic,
    half_life_days,
    ic_autocorr,
    ls_daily,
    max_drawdown,
    monotonicity,
    pearson,
    rank_of,
    return_distribution,
    spearman,
    tail_drawdown,
    win_rate,
)
from .metrics_eval import (
    bootstrap_ci,
    brain_headline,
    bhy_qvalues,
    capacity_estimate,
    cost_sensitivity,
    crowding_score,
    deflated_sharpe,
    fitness_of,
    holding_period_sweep,
    margin_of,
    normal_cdf,
    normal_pvalue,
    newey_west_lag,
    nw_tstat,
    plain_tstat,
    style_attribution,
    style_correlations,
)
from .metrics_series import (
    annual_breakdown,
    clip_frac_matrix,
    domain_ic_matrix,
    half_ic_matrix,
    monthly_matrix,
    pairwise_rank_corr,
    rolling_ir,
    rolling_mean,
    sub_period_stats,
)

__all__ = [
    # 口径常量（改口径 = 改 metrics_core，且只有那里）
    "CLIP_MAD_K", "DEFAULT_COST_BPS", "DEFAULT_PARTICIPATION", "DEGENERATE_REL_STD",
    "MAD_TO_SIGMA", "MIN_SAMPLES", "MIN_TSTAT_SAMPLES", "TRADING_DAYS", "TURNOVER_FLOOR",
    "DEFINITIONS",
    # §0 基础统计原语
    "rank_of", "pearson", "spearman",
    # §1 IC 体系
    "half_ic", "cum_ic", "win_rate", "monotonicity", "half_life_days", "ic_autocorr",
    # §2 多空组合与回撤
    "ls_daily", "cum_curve", "max_drawdown", "tail_drawdown", "drawdown_episodes",
    # §3 收益分布与尾部
    "return_distribution",
    # §4 BRAIN 头部指标
    "margin_of", "fitness_of", "brain_headline",
    # §5 显著性
    "plain_tstat", "newey_west_lag", "nw_tstat", "normal_pvalue", "normal_cdf",
    "bhy_qvalues", "bootstrap_ci", "deflated_sharpe", "crowding_score",
    # §6 经济性
    "cost_sensitivity", "holding_period_sweep", "capacity_estimate",
    # §7 风格归因
    "style_attribution", "style_correlations",
    # §8 分段 / 年度 / 月度 / 滚动
    "annual_breakdown", "monthly_matrix", "sub_period_stats", "rolling_ir", "rolling_mean",
    # §14 构建期截面原语
    "half_ic_matrix", "domain_ic_matrix", "clip_frac_matrix", "pairwise_rank_corr",
]
