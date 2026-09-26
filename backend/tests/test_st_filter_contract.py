"""ST 过滤静默失效（BUG-11）的回归契约。

QuantDB 直读数据集（l1_factors 120 列 / l1_l2_factors 330 列）都没有 `is_st` 列，
而 `docker/training/data/loading.py` 原实现是：

    if "is_st" in df.columns:
        ...过滤...

缺列时**静默跳过**过滤，训练集里混入 ST/*ST 股票却无人知晓，会直接污染 IC/ICIR
等评估指标。现在缺列必须显式告警（不抛错——会打断合法数据集）。
"""

from __future__ import annotations

from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
_REPO = _BACKEND.parent
LOADING_PY = _REPO / "docker" / "training" / "data" / "loading.py"


def test_st_filter_warns_when_column_missing() -> None:
    """is_st 缺失时必须显式告警，不能静默跳过过滤。"""
    text = LOADING_PY.read_text(encoding="utf-8")
    start = text.index('if "is_st" in df.columns:')
    block = text[start : start + 1200]
    assert "else:" in block, "is_st 分支缺少 else，缺失时会静默跳过"
    assert "logger.warning" in block, "is_st 缺失时未告警"
    assert "ST" in block
