"""外部 FF5 交叉核对脚本的解析与对齐单测（容器内 python3 直跑，无 pytest 依赖）。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from backend.scripts.eval.ff_external_check import (
    compare,
    load_external_factors,
    parse_month,
)


def test_parse_month_accepts_common_formats():
    assert parse_month("2025-01") == "2025-01"
    assert parse_month("2025-01-31") == "2025-01"
    assert parse_month("202501") == "2025-01"
    assert parse_month("2025/2/3") == "2025-02"
    assert parse_month("not-a-date") is None


def test_load_external_factors_maps_chinese_headers():
    csv = "月份,市场,规模,价值,盈利,投资\n2025-01,0.01,0.02,0.03,0.04,-0.05\n2025-02,0.02,0.01,0.00,0.03,-0.01\n"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ext.csv"
        path.write_text(csv, encoding="utf-8")
        df = load_external_factors(path)
    assert list(df.index) == ["2025-01", "2025-02"]
    assert set(df.columns) >= {"MKT", "SMB", "HML", "RMW", "CMA"}
    assert df.loc["2025-01", "CMA"] == -0.05


def test_load_external_factors_rejects_missing_factor():
    csv = "month,MKT,SMB\n2025-01,0.01,0.02\n"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ext.csv"
        path.write_text(csv, encoding="utf-8")
        try:
            load_external_factors(path)
        except ValueError as exc:
            assert "缺少因子列" in str(exc)
        else:
            raise AssertionError("缺列应报 ValueError")


def test_compare_recovers_unit_slope_on_identical_data():
    months = [f"2024-{m:02d}" for m in range(1, 13)]
    rng = np.random.default_rng(7)
    data = {
        f: rng.normal(0.005, 0.02, size=12) for f in ("MKT", "SMB", "HML", "RMW", "CMA")
    }
    ours = pd.DataFrame(data, index=months)
    # 外部口径 = 我方 * 0.5 + 噪声；回归 ours ~ ext 的斜率应≈2
    noisy = ours * 0.5 + rng.normal(0, 0.001, size=ours.shape)
    res = compare(ours, noisy)
    assert res["n_months"] == 12
    for f in data:
        stat = res["factors"][f]
        assert 1.8 < stat["slope"] < 2.2, (f, stat["slope"])
        assert abs(stat["corr"]) > 0.8
