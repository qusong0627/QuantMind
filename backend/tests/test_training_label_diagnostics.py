"""标签诊断门（W3）：NaN/正类率/每日截面/top-tail/尾部截断。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "docker" / "training"))

from label_diagnostics import (  # noqa: E402
    LabelDiagnosticError,
    compute_label_stats,
    enforce_label_diagnostics,
)


def _frame(*, symbols: int = 60, days: int = 5, values=None) -> pd.DataFrame:
    dates = pd.date_range("2026-01-05", periods=days, freq="D")
    rows = []
    for d in dates:
        for k in range(symbols):
            v = values(k) if callable(values) else (values if values is not None else 0.01)
            rows.append({"symbol": f"S{k:03d}", "trade_date": d, "label": v})
    return pd.DataFrame(rows)


def _stats(frame: pd.DataFrame, **kwargs):
    defaults = dict(
        target_mode="return",
        transform_type="rank",
        top_pct=0.2,
        test_start="2026-01-05",
        test_end="2026-01-09",
        horizon_days=1,
        lag_days=1,
    )
    defaults.update(kwargs)
    return compute_label_stats(frame, **defaults)


def test_normal_stats() -> None:
    stats = _stats(_frame())
    assert stats["rows"] == 300
    assert stats["label_valid_rows"] == 300
    assert stats["nan_ratio"] == 0.0
    assert stats["daily_count_min"] == 60
    out = enforce_label_diagnostics(
        stats, target_mode="return", transform_type="rank", gate_mode="strict"
    )
    assert out["gate"] == "strict"
    assert out["warnings"] == []


def test_nan_ratio_gate() -> None:
    frame = _frame()
    frame.loc[:199, "label"] = np.nan  # 2/3 缺失
    stats = _stats(frame)
    with pytest.raises(LabelDiagnosticError) as exc:
        enforce_label_diagnostics(
            stats, target_mode="return", transform_type="rank", gate_mode="strict"
        )
    assert exc.value.code == "target_label_incomplete"


def test_classification_degenerate_gate() -> None:
    frame = _frame(symbols=200, values=lambda k: 1.0 if k == 0 else 0.0)  # 5/1000 正类
    stats = _stats(frame, target_mode="classification")
    with pytest.raises(LabelDiagnosticError) as exc:
        enforce_label_diagnostics(
            stats, target_mode="classification", transform_type="rank", gate_mode="strict"
        )
    assert exc.value.code == "target_label_degenerate"


def test_daily_cross_section_gate() -> None:
    stats = _stats(_frame(symbols=10))
    with pytest.raises(LabelDiagnosticError) as exc:
        enforce_label_diagnostics(
            stats, target_mode="return", transform_type="rank", gate_mode="strict"
        )
    assert exc.value.code == "target_label_cross_section_too_small"


def test_daily_threshold_is_market_aware() -> None:
    # FUTURES 阈值 10：20 个合约合法
    futures = _stats(_frame(symbols=20))
    out = enforce_label_diagnostics(
        futures,
        target_mode="return",
        transform_type="rank",
        gate_mode="strict",
        market="FUTURES",
    )
    assert out["market"] == "FUTURES"
    # CRYPTO 阈值 20：15 个标的应被拦
    crypto = _stats(_frame(symbols=15))
    with pytest.raises(LabelDiagnosticError) as exc:
        enforce_label_diagnostics(
            crypto,
            target_mode="return",
            transform_type="rank",
            gate_mode="strict",
            market="CRYPTO",
        )
    assert exc.value.code == "target_label_cross_section_too_small"


def test_top_tail_instability_gate() -> None:
    frame = _frame(values=lambda k: 1.0 if k < 2 else 0.0)  # 实际 2/60≈3.3%
    stats = _stats(frame, transform_type="top_tail", top_pct=0.2)
    assert stats["top_tail_actual_pct"] is not None
    with pytest.raises(LabelDiagnosticError) as exc:
        enforce_label_diagnostics(
            stats, target_mode="return", transform_type="top_tail", gate_mode="strict"
        )
    assert exc.value.code == "target_label_tail_unstable"


def test_warn_and_off_modes() -> None:
    frame = _frame()
    frame.loc[:199, "label"] = np.nan
    stats = _stats(frame)
    warned = enforce_label_diagnostics(
        stats, target_mode="return", transform_type="rank", gate_mode="warn"
    )
    assert any("target_label_incomplete" in w for w in warned["warnings"])
    off = enforce_label_diagnostics(
        stats, target_mode="return", transform_type="rank", gate_mode="off"
    )
    assert off["warnings"] == []


def test_tail_truncation_is_warning_only() -> None:
    frame = _frame(days=3)
    stats = _stats(frame, test_start="2026-01-05", test_end="2026-01-20", horizon_days=5)
    out = enforce_label_diagnostics(
        stats, target_mode="return", transform_type="rank", gate_mode="strict"
    )
    assert any("tail_truncated" in w for w in out["warnings"])
