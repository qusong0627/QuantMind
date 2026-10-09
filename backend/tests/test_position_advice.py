"""§6.5-L2 降险建议（纯函数）：弱区判定 → POSITION_BY_STATE 阶梯下调仓位系数。

- 判据与 §6.4 同源（``is_weak_bucket``：桶 mean_ic ≤ 0 且 ≥15 天），不另写谓词；
- **只出「有下调空间」的建议**：bull（阶梯 1.0）不出建议——绝不造 1.0→1.0 的假动作；
- 阶梯是市场口径单源 ``POSITION_BY_STATE``（与实时轨 position_hint 同源），
  返回的是副本（改返回值不得污染单源）。
"""

from __future__ import annotations

import pytest

from backend.shared.market_regime import POSITION_BY_STATE
from backend.shared.regime_buckets import bucket_of, position_reduction_advice

pytestmark = pytest.mark.unit


def _stats(values: list[float], state: str = "neutral") -> dict:
    days = [f"2026-09-{d:02d}" for d in range(1, len(values) + 1)]
    daily_ic = [{"date": day, "value": v} for day, v in zip(days, values, strict=True)]
    states = dict.fromkeys(days, state)
    return bucket_of(daily_ic, states, state)


#: 16 天、均值 -0.02（≤0 且 ≥15 天 → 弱区）
_WEAK = [-0.01, -0.03] * 8
#: 16 天、均值 +0.02（非弱区）
_STRONG = [0.01, 0.03] * 8


def test_weak_neutral_suggests_step_down_to_ladder():
    stats = _stats(_WEAK)
    assert stats["weak"] is True

    advice = position_reduction_advice("neutral", stats)
    assert advice is not None
    assert advice["state"] == "neutral"
    assert advice["from_factor"] == 1.0
    assert advice["to_factor"] == 0.7  # POSITION_BY_STATE[neutral]
    assert advice["bucket_mean_ic"] == pytest.approx(-0.02)
    assert advice["bucket_days"] == 16
    assert advice["ladder"] == POSITION_BY_STATE


def test_weak_bear_uses_bear_rung():
    stats = _stats(_WEAK, state="bear")
    advice = position_reduction_advice("bear", stats)
    assert advice is not None
    assert advice["to_factor"] == 0.3


def test_weak_bull_has_no_reduction_room():
    """弱区出现在 bull：阶梯已是 1.0 → None（诚实不出建议，而非 1.0→1.0 假动作）。"""
    stats = _stats(_WEAK, state="bull")
    assert position_reduction_advice("bull", stats) is None


def test_not_weak_or_insufficient_days_no_advice():
    strong = _stats(_STRONG)
    assert strong["weak"] is False
    assert position_reduction_advice("neutral", strong) is None

    short = _stats(_WEAK[:10])  # 10 天 < 15 天下限
    assert short["weak"] is False
    assert position_reduction_advice("neutral", short) is None


def test_missing_or_unknown_inputs_no_advice():
    stats = _stats(_WEAK)
    assert position_reduction_advice(None, stats) is None
    assert position_reduction_advice("", stats) is None
    assert position_reduction_advice("neutral", None) is None
    assert position_reduction_advice("sideways", stats) is None  # 词汇表外状态


def test_full_factor_below_rung_no_advice():
    """调用方自报的现行系数已低于阶梯（如已手动减到 0.5）→ 无下调空间 → None。"""
    stats = _stats(_WEAK)
    assert position_reduction_advice("neutral", stats, full_factor=0.5) is None
    assert position_reduction_advice("neutral", stats, full_factor=0.8) is not None


def test_returned_ladder_is_a_copy():
    stats = _stats(_WEAK)
    advice = position_reduction_advice("neutral", stats)
    advice["ladder"]["neutral"] = 999.0
    assert POSITION_BY_STATE["neutral"] == 0.7  # 单源不被返回值污染
