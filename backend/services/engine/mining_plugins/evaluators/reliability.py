"""RRE（排序可靠度）评估器：相邻日排名分布 KL 散度贴合度。

口径 = AlphaEval master ``backtest/modeltester.py:315-327``（已逐行核对）：
  ranks = 当日排名（rank(axis=1)，并列取平均秩）
  probs = ranks / Σranks（按日归一为「份额分布」）
  KL_t = Σ p_t·ln((p_t+ε)/(p_{t-1}+ε))，ε=1e-8
  RRE  = mean(1/(1+KL_t))

刻意保留的源行为：首日 probs_prev 全 NaN → pandas sum(skipna) 得 0.0 → 该日计 1.0；
某标的缺值日其 KL 项跳过（NaN 项不进和）。
刻意偏离：单日面板给 None——1.0 是伪值，没有「相邻日」就不该出数。
金样：``backend/tests/fixtures/miningMetricsGolden.json``（改公式必须过金样）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import EvalContext, MetricDescriptor
from ..registry import register_evaluator

EPSILON = 1e-8

_DESCRIPTORS = (
    MetricDescriptor(
        key="rre",
        label="RRE（排序可靠度）",
        group="robustness",
        unit="score",
        better="higher",
        precision=4,
        description="相邻日排名分布 KL 散度贴合度，1=分布完全不变（AlphaEval 口径）",
    ),
)


def compute_rre(paired: pd.DataFrame) -> float | None:
    """paired：列 datetime/symbol/factor（ret 不用）。样本不足返回 None。"""
    if paired is None or paired.empty:
        return None
    mat = paired.pivot(index="datetime", columns="symbol", values="factor").sort_index()
    mat = mat.dropna(how="all")
    if len(mat) < 2:
        return None
    ranks = mat.rank(axis=1)
    denom = ranks.sum(axis=1)
    probs = ranks.div(denom.replace(0.0, np.nan), axis=0)
    probs_prev = probs.shift(1)
    kl = (probs * np.log((probs + EPSILON) / (probs_prev + EPSILON))).sum(axis=1)
    rre_series = 1.0 / (1.0 + kl.dropna())
    if rre_series.empty:
        return None
    return float(rre_series.mean())


class _ReliabilityEvaluator:
    name = "reliability"
    descriptors = _DESCRIPTORS

    def evaluate(self, ctx: EvalContext) -> dict[str, float | None]:
        return {"rre": compute_rre(ctx.paired)}


register_evaluator(_ReliabilityEvaluator())
