"""训练标签诊断（W3）：NaN 率 / 正类率 / 每日截面 / top-tail 稳定性。

机构级原则：标签质量不合格必须在训练前失败，而不是训练完看到“指标奇怪”。
门默认 strict；`QM_LABEL_DIAGNOSTIC_GATE=warn|off` 可降级，但会写入
metadata.contract_flags 与 label_stats.warnings，便于审计。
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger("quantmind.train")

MAX_NAN_RATIO = 0.5
MIN_POS_RATE = 0.01
MAX_POS_RATE = 0.99
#: 每日有效标签数下限按市场：CN/HK/US/CUSTOM 50；CRYPTO/FUTURES 标的天然少，
#: 用更低的退化闸，避免把合法的小宇宙训练误杀。
MIN_DAILY_LABELED_BY_MARKET = {
    "CN": 50,
    "HK": 50,
    "US": 50,
    "CUSTOM": 50,
    "CRYPTO": 20,
    "FUTURES": 10,
}
MIN_DAILY_LABELED = 50  # 向后兼容的默认（CN）
TOP_TAIL_REL_TOL = 0.5


class LabelDiagnosticError(RuntimeError):
    code = "target_label_invalid"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


def _json_num(value: Any) -> float | int | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None
    return f


def compute_label_stats(
    df: pd.DataFrame,
    *,
    target_mode: str,
    transform_type: str,
    top_pct: float = 0.2,
    test_start: str | None = None,
    test_end: str | None = None,
    horizon_days: int = 1,
    lag_days: int = 1,
) -> dict[str, Any]:
    """计算标签诊断字典（JSON 安全）。标签列为 `label`。"""
    if "label" not in df.columns:
        raise LabelDiagnosticError(
            "数据帧缺少 label 列，无法计算标签诊断", code="target_label_missing"
        )
    labels = pd.to_numeric(df["label"], errors="coerce")
    valid = labels.notna()
    n_rows = int(len(df))
    n_valid = int(valid.sum())
    nan_ratio = float(1.0 - (n_valid / n_rows)) if n_rows else 1.0

    daily_counts: pd.Series | None = None
    if "trade_date" in df.columns and n_valid:
        daily_counts = (
            df.loc[valid].groupby("trade_date", sort=True).size().astype("int64")
        )

    pos_rate: float | None = None
    top_tail_actual_pct: float | None = None
    mode = str(target_mode or "").strip().lower()
    transform = str(transform_type or "").strip().lower()
    if n_valid and mode == "classification":
        pos_rate = float((labels[valid] > 0).mean())
    if n_valid and transform == "top_tail":
        if "trade_date" in df.columns:
            by_day = labels[valid].gt(0).groupby(df.loc[valid, "trade_date"]).mean()
            top_tail_actual_pct = float(by_day.mean())
        else:
            top_tail_actual_pct = float(labels[valid].gt(0).mean())

    test_label_max_date = None
    tail_truncated_days = 0
    if test_start and "trade_date" in df.columns and n_valid:
        dates = pd.to_datetime(df.loc[valid, "trade_date"], errors="coerce")
        mask = dates >= pd.Timestamp(test_start)
        if mask.any():
            test_label_max_date = dates[mask].max().date().isoformat()
    if test_end and test_label_max_date:
        gap = (pd.Timestamp(test_end).date() - pd.Timestamp(test_label_max_date).date()).days
        tail_truncated_days = max(0, int(gap) - int(horizon_days) - int(lag_days))

    stats = {
        "rows": n_rows,
        "label_valid_rows": n_valid,
        "nan_ratio": _json_num(nan_ratio),
        "mean": _json_num(labels[valid].mean()) if n_valid else None,
        "std": _json_num(labels[valid].std(ddof=0)) if n_valid else None,
        "min": _json_num(labels[valid].min()) if n_valid else None,
        "max": _json_num(labels[valid].max()) if n_valid else None,
        "pos_rate": _json_num(pos_rate),
        "daily_count_min": int(daily_counts.min()) if daily_counts is not None else None,
        "daily_count_p50": (
            int(daily_counts.median()) if daily_counts is not None else None
        ),
        "daily_count_max": int(daily_counts.max()) if daily_counts is not None else None,
        "top_tail_actual_pct": _json_num(top_tail_actual_pct),
        "top_tail_target_pct": float(top_pct),
        "test_label_max_date": test_label_max_date,
        "test_tail_truncated_days": tail_truncated_days,
        "warnings": [],
    }
    return stats


def enforce_label_diagnostics(
    stats: dict[str, Any],
    *,
    target_mode: str,
    transform_type: str,
    gate_mode: str = "strict",
    market: str = "CN",
) -> dict[str, Any]:
    """按诊断门判定；strict 失败抛错，warn 只记录，off 跳过。"""
    gate = str(gate_mode or "strict").strip().lower()
    if gate not in {"strict", "warn", "off"}:
        gate = "strict"
    stats = dict(stats)
    stats["gate"] = gate
    stats["market"] = str(market or "CN").strip().upper()
    if gate == "off":
        stats["warnings"] = list(stats.get("warnings") or [])
        return stats

    violations: list[tuple[str, str]] = []
    if float(stats.get("nan_ratio") or 0.0) > MAX_NAN_RATIO:
        violations.append(
            (
                "target_label_incomplete",
                f"标签缺失率 {stats['nan_ratio']:.1%} > {MAX_NAN_RATIO:.0%}",
            )
        )
    if str(target_mode).lower() == "classification":
        pos = stats.get("pos_rate")
        if pos is not None and (float(pos) < MIN_POS_RATE or float(pos) > MAX_POS_RATE):
            violations.append(
                (
                    "target_label_degenerate",
                    f"分类正类率 {float(pos):.4f} 超出 [{MIN_POS_RATE}, {MAX_POS_RATE}]",
                )
            )
    daily_min = stats.get("daily_count_min")
    min_daily = MIN_DAILY_LABELED_BY_MARKET.get(stats["market"], MIN_DAILY_LABELED)
    if daily_min is not None and int(daily_min) < min_daily:
        violations.append(
            (
                "target_label_cross_section_too_small",
                f"每日有效标签数最小值 {daily_min} < {min_daily}（market={stats['market']}）",
            )
        )
    if str(transform_type).lower() == "top_tail":
        actual = stats.get("top_tail_actual_pct")
        target = float(stats.get("top_tail_target_pct") or 0.2)
        if actual is not None and target > 0:
            rel = abs(float(actual) - target) / target
            if rel > TOP_TAIL_REL_TOL:
                violations.append(
                    (
                        "target_label_tail_unstable",
                        f"top-tail 实际比例 {float(actual):.4f} 偏离目标 {target:.4f} "
                        f"超过 {TOP_TAIL_REL_TOL:.0%}",
                    )
                )

    warnings = list(stats.get("warnings") or [])
    if int(stats.get("test_tail_truncated_days") or 0) > 0:
        warnings.append(
            "target_label_tail_truncated: test 末尾 "
            f"{stats['test_tail_truncated_days']} 天标签不完整（已按 NaN 剔除）"
        )
    if violations:
        message = "; ".join(f"[{code}] {msg}" for code, msg in violations)
        if gate == "strict":
            raise LabelDiagnosticError(
                f"{violations[0][0]}: {message}",
                code=violations[0][0],
            )
        warnings.extend(f"{code}: {msg}" for code, msg in violations)
        logger.warning("label diagnostic gate (warn): %s", message)
    stats["warnings"] = warnings
    return stats


__all__ = [
    "LabelDiagnosticError",
    "compute_label_stats",
    "enforce_label_diagnostics",
]
