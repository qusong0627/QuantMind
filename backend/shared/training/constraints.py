"""Shared policy limits for model-training requests."""

from __future__ import annotations

from datetime import date, timedelta

from backend.shared.training.per_model import training_compute

MIN_TRAINING_SPAN_YEARS = 3
CPU_FEATURE_MIN = 20
CPU_FEATURE_MAX = 120
GPU_FEATURE_MIN = 40


def factor_count_limits(model_type: str) -> tuple[int, int | None]:
    """Return (minimum, maximum) selected-factor counts for a model framework."""
    if training_compute(model_type) == "gpu":
        return GPU_FEATURE_MIN, None
    return CPU_FEATURE_MIN, CPU_FEATURE_MAX


def validate_factor_count(model_type: str, count: int) -> None:
    minimum, maximum = factor_count_limits(model_type)
    if count < minimum:
        raise ValueError(f"{model_type} 至少需要选择 {minimum} 个因子（当前 {count} 个）")
    if maximum is not None and count > maximum:
        raise ValueError(f"{model_type} 最多允许选择 {maximum} 个因子（当前 {count} 个）")


def validate_training_span(start: str, end: str) -> None:
    """Require an inclusive sample window spanning at least three calendar years."""
    start_date = date.fromisoformat(str(start)[:10])
    end_date = date.fromisoformat(str(end)[:10])
    try:
        anniversary = start_date.replace(year=start_date.year + MIN_TRAINING_SPAN_YEARS)
    except ValueError:  # Feb 29 -> Feb 28 in a non-leap anniversary year.
        anniversary = start_date.replace(
            year=start_date.year + MIN_TRAINING_SPAN_YEARS, day=28
        )
    minimum_inclusive_end = anniversary - timedelta(days=1)
    if end_date < minimum_inclusive_end:
        raise ValueError(
            f"训练、验证与测试样本总跨度至少为 {MIN_TRAINING_SPAN_YEARS} 年"
            f"（当前 {start_date.isoformat()} 至 {end_date.isoformat()}）"
        )
