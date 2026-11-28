"""regime_universe 纯函数单元测试（宿主即可运行，不需要 fastapi）。"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

from backend.services.api.market_analysis.regime_universe import _roll_pct


def test_roll_pct_matches_rolling_apply_with_nans():
    """向量化分位必须与原 rolling.apply 逐值一致（含 NaN 窗口）。"""
    rng = np.random.default_rng(0)
    s = pd.Series(rng.normal(size=800))
    s.iloc[100:120] = np.nan
    expected = s.rolling(300, min_periods=200).apply(
        lambda x: float((x.iloc[-1] >= x).mean()), raw=False
    )
    got = _roll_pct(s, window=300, min_periods=200)
    assert np.allclose(expected, got, equal_nan=True)


def test_roll_pct_requires_min_periods():
    s = pd.Series([1.0, 2.0, 3.0])
    out = _roll_pct(s, window=3, min_periods=2)
    assert np.isnan(out.iloc[0])
    assert out.iloc[1] == 1.0
    assert out.iloc[2] == 1.0


def test_valuation_stamp_changes_when_panel_rebuilt(
    tmp_path: Path, monkeypatch
) -> None:
    """估值聚合文件重建后，市场阶段 API 的缓存键必须变化（PB 停更回归）。"""
    from backend.services.api.market_analysis import regime_universe as ru

    panel = tmp_path / "valuation_daily.parquet"
    panel.write_bytes(b"x")
    val_dir = tmp_path / "valuation"
    val_dir.mkdir()
    monkeypatch.setattr(ru, "VAL", panel)
    monkeypatch.setattr(ru, "VAL_DIR", val_dir)

    first = ru.valuation_stamp()
    os.utime(panel, ns=(first + 10_000, first + 10_000))

    assert ru.valuation_stamp() > first
