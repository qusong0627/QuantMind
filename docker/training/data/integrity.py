"""训练时点完整性与过拟合诊断（机构级护栏）。

三件事，全部为纯函数/只读，不改变训练结果：

1. `assert_no_feature_leak`：特征名命中标签/远期收益 → 训练入口直接失败。
   与提交 API 的 `backend.shared.feature_guard` 同一套规则；
   兼容镜像（无 /app/backend）降级为本地同规则副本，绝不 fail-open。
2. `time_integrity_report`：切分完成后复核 train/val/test 的时序与 embargo
   间隔。任何「训练段标签跨入验证段」都在这里硬失败，而不是只打日志。
3. `overfit_diagnostics`：把 train/val 的 rank IC 差、早停轮次比例转成
   可审计的标记（no_val_signal / train_val_gap / stopped_immediately /
   rounds_exhausted），供训练报告与模型对比表使用。诊断只标记，不自动改参。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Iterable, Mapping

import pandas as pd

logger = logging.getLogger("quantmind.train")

# ── 特征名泄漏护栏 ────────────────────────────────────────────────────────────

try:  # 新训练镜像 COPY backend /app/backend
    from backend.shared.feature_guard import (
        describe_leaks as _describe_leaks,
        find_hard_label_leaks as _find_hard,
        find_soft_return_features as _find_soft,
    )
except ImportError:  # 旧兼容镜像：本地同规则副本（不 fail-open）
    import re as _re

    _HARD = (
        _re.compile(r"^label($|_)", _re.IGNORECASE),
        _re.compile(r"^(fwd|future|forward)_?ret", _re.IGNORECASE),
        _re.compile(r"^(fwd|future|forward)_?\d*d_?ret", _re.IGNORECASE),
        _re.compile(r"^(next|future)_\d*d?_?ret", _re.IGNORECASE),
    )
    _SOFT = (_re.compile(r"^return_\d+d$", _re.IGNORECASE),)

    def _find_hard(features: Iterable[str]) -> list[str]:
        return [f for f in features if any(p.search(str(f)) for p in _HARD)]

    def _find_soft(features: Iterable[str]) -> list[str]:
        return [f for f in features if any(p.search(str(f)) for p in _SOFT)]

    def _describe_leaks(leaks: Iterable[str]) -> str:
        return "特征包含标签/远期收益列（前视泄漏）：" + ", ".join(leaks)


def assert_no_feature_leak(features: Iterable[str]) -> None:
    """训练入口硬闸：特征里出现标签/远期收益列 → RuntimeError。

    `return_{n}d` 语义随因子源不同（过去收益合法 / 远期收益泄漏），只告警；
    非直读因子源由 `data/loading.py` 的 `_LEAKY_RETURN_COLS` 直接剔除。
    """
    names = [str(x) for x in features]
    leaks = _find_hard(names)
    if leaks:
        raise RuntimeError(_describe_leaks(leaks))
    soft = _find_soft(names)
    if soft:
        logger.warning(
            "特征含 return_{n}d（%s）：非直读因子源会在加载时剔除；"
            "若为直读因子源，请确认该列定义为过去收益而非远期收益。",
            ", ".join(soft),
        )


# ── 时点完整性 ────────────────────────────────────────────────────────────────


def _span(frame: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    if frame.empty or "trade_date" not in frame.columns:
        return None
    return pd.Timestamp(frame["trade_date"].min()), pd.Timestamp(frame["trade_date"].max())


def _trading_gap(all_dates: list[pd.Timestamp], left: pd.Timestamp, right: pd.Timestamp) -> int:
    """left 与 right 之间的交易日数（不含两端）。"""
    return sum(1 for d in all_dates if left < d < right)


def time_integrity_report(
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    test_df: pd.DataFrame,
    all_dates: Iterable[Any],
    *,
    horizon: int,
    lag: int = 1,
) -> dict[str, Any]:
    """复核三段时序与 embargo；不足即 RuntimeError（不降级为告警）。

    embargo 口径与 `_take_segment` 一致：每段尾部裁掉 `horizon + lag` 个
    交易日。train→val、val→test 的交易日间隔都必须 ≥ 该值。
    """
    embargo = max(1, int(horizon)) + max(0, int(lag))
    dates = sorted({pd.Timestamp(d) for d in all_dates})
    spans = {
        "train": _span(train_df),
        "valid": _span(val_df),
        "test": _span(test_df),
    }
    missing = [name for name, span in spans.items() if span is None]
    if missing:
        raise RuntimeError(f"时点完整性检查失败：切分段为空 {missing}")

    tr, va, te = spans["train"], spans["valid"], spans["test"]
    if not (tr[1] < va[0] and va[1] < te[0]):
        raise RuntimeError(
            "时点完整性检查失败：三段顺序不是 train < valid < test "
            f"(train={tr[0].date()}~{tr[1].date()}, "
            f"valid={va[0].date()}~{va[1].date()}, "
            f"test={te[0].date()}~{te[1].date()})"
        )

    gap_train_val = _trading_gap(dates, tr[1], va[0])
    gap_val_test = _trading_gap(dates, va[1], te[0])
    for label, gap in (("train→valid", gap_train_val), ("valid→test", gap_val_test)):
        if gap < embargo:
            raise RuntimeError(
                f"时点完整性检查失败：{label} 交易日间隔 {gap} < embargo {embargo}。"
                "训练段尾部样本的标签会跨入下一段（前视泄漏）。"
                "请扩大 embargo 或把下一段起点后移。"
            )

    report = {
        "train": {"start": str(tr[0].date()), "end": str(tr[1].date()), "rows": int(len(train_df))},
        "valid": {"start": str(va[0].date()), "end": str(va[1].date()), "rows": int(len(val_df))},
        "test": {"start": str(te[0].date()), "end": str(te[1].date()), "rows": int(len(test_df))},
        "embargo_trading_days": embargo,
        "train_valid_gap_trading_days": gap_train_val,
        "valid_test_gap_trading_days": gap_val_test,
    }
    logger.info(
        "时点完整性 OK：train %s~%s | gap %d 交易日 | valid %s~%s | gap %d | test %s~%s"
        "（embargo=%d，horizon=%d，lag=%d）",
        report["train"]["start"], report["train"]["end"], gap_train_val,
        report["valid"]["start"], report["valid"]["end"], gap_val_test,
        report["test"]["start"], report["test"]["end"],
        embargo, horizon, lag,
    )
    return report


# ── 过拟合诊断 ────────────────────────────────────────────────────────────────

#: train 与 val 的 rank IC 差达到该值即标记（诊断阈值，不是晋级闸）。
OVERFIT_RANK_IC_GAP = 0.05


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def overfit_diagnostics(
    train_m: Mapping[str, Any],
    val_m: Mapping[str, Any],
    *,
    best_iteration: Any = None,
    num_boost_round: Any = None,
) -> dict[str, Any]:
    """把 train/val 指标与早停轮次转成结构化诊断。

    标记语义：
    - `no_val_signal`：验证集 rank IC 缺失或 ≤0（没有可用的截面排序信号）；
    - `train_val_gap`：train rank IC − val rank IC ≥ 0.05（记忆/过拟合嫌疑）；
    - `stopped_immediately`：best_iteration ≤1（学习率过高或信号太弱）；
    - `rounds_exhausted`：best_iteration ≥ 95% 预算（早停未触发，轮数不足）。
    """
    tr = _finite(train_m.get("rank_ic"))
    va = _finite(val_m.get("rank_ic"))
    gap = (tr - va) if (tr is not None and va is not None) else None

    flags: list[str] = []
    if va is None or va <= 0:
        flags.append("no_val_signal")
    if gap is not None and gap >= OVERFIT_RANK_IC_GAP and (va is not None and va > 0):
        flags.append("train_val_gap")

    bi = None
    ratio = None
    if best_iteration is not None:
        try:
            bi = int(best_iteration)
        except (TypeError, ValueError):
            bi = None
    if bi is not None and num_boost_round:
        try:
            n = int(num_boost_round)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            ratio = round(bi / n, 4)
            if bi <= 1:
                flags.append("stopped_immediately")
            elif ratio >= 0.95:
                flags.append("rounds_exhausted")

    return {
        "train_rank_ic": tr,
        "val_rank_ic": va,
        "rank_ic_gap": gap,
        "best_iteration": bi,
        "best_iter_ratio": ratio,
        "overfit_flags": flags,
    }
