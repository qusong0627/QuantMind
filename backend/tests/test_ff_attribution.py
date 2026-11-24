"""FF5 归因的纯函数单测：HAC 回归、因子排序方向、涨跌停/月复利。

不做数据集成（真实数据口径由脚本的运行报告自证）；这里只锁纯计算。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.scripts.eval.ff_attribution import (
    hac_ols,
    limit_pct_for_symbol,
    monthly_compound,
)
from backend.scripts.eval.ff_factors import (
    apply_academic_sorts,
    compute_factors_from_panel,
    orthogonalize_inv,
)


def _naive_ols_se(y: np.ndarray, X: np.ndarray) -> np.ndarray:
    n = len(y)
    Xc = np.column_stack([np.ones(n), X])
    xtx_inv = np.linalg.pinv(Xc.T @ Xc)
    beta = xtx_inv @ Xc.T @ y
    resid = y - Xc @ beta
    sigma2 = float(resid @ resid) / max(1, n - Xc.shape[1])
    return np.sqrt(np.diag(sigma2 * xtx_inv))


def test_hac_ols_recovers_known_alpha_and_loadings():
    rng = np.random.default_rng(7)
    n = 180
    x = rng.normal(size=n)
    # AR(1) 误差（正自相关）——HAC 必须比朴素 OLS 标准误更大
    e = np.zeros(n)
    for i in range(1, n):
        e[i] = 0.6 * e[i - 1] + rng.normal(scale=0.02)
    y = 0.01 + 0.5 * x + e

    res = hac_ols(y, x)
    assert abs(res["beta"][0] - 0.01) < 0.006
    assert abs(res["beta"][1] - 0.5) < 0.12
    assert res["se"][0] > _naive_ols_se(y, x)[0]
    assert res["n"] == n and res["lags"] >= 1
    assert 0.0 <= res["r2"] <= 1.0


def test_hac_ols_alpha_zero_when_fully_explained():
    rng = np.random.default_rng(11)
    n = 120
    x = rng.normal(size=n)
    y = 0.8 * x + rng.normal(scale=0.01, size=n)
    res = hac_ols(y, x)
    assert abs(res["beta"][0]) < 0.004


def test_compute_factors_signs_on_synthetic_panel():
    """小市值/高 B/M/高盈利/低投资/高换手分别给正超额 → 各因子方向必须为正。"""
    months = ["2024-01", "2024-02"]
    rows = []
    for month in months:
        for i in range(400):
            small = i < 200
            high_char = i % 2 == 0
            base = 0.0
            ret = (
                base
                + (0.02 if small else 0.0)  # SMB +
                + (0.03 if high_char else 0.0)  # 高 B/M/OP/换手 → 正；低 Inv → 正
            )
            rows.append(
                {
                    "month": month,
                    "symbol": f"S{i:04d}",
                    "size": 1e9 if small else 1e11,
                    "bm": 2.0 if high_char else 0.5,
                    # 用同一 high_char 标记：盈利高、投资低（inv 取负）、换手高
                    "op": 0.20 if high_char else 0.05,
                    "inv": -0.10 if high_char else 0.30,
                    "turn": 1.0 if high_char else 0.1,
                    "ret": ret,
                }
            )
    panel = pd.DataFrame(rows)
    factors, diag = compute_factors_from_panel(panel, rf_annual=0.0)
    assert len(factors) == len(months)
    f = factors.iloc[0]
    assert f["SMB"] > 0.015
    assert f["HML"] > 0.02
    assert f["RMW"] > 0.02
    assert f["CMA"] > 0.02
    assert f["TURN"] > 0.02
    assert f["n_universe"] == 400
    assert (diag["bm_SL_n"] > 0).all()


def test_limit_pct_by_board():
    assert limit_pct_for_symbol("600036.SH") == 0.10
    assert limit_pct_for_symbol("000001.SZ") == 0.10
    assert limit_pct_for_symbol("300750.SZ") == 0.20
    assert limit_pct_for_symbol("688981.SH") == 0.20
    assert limit_pct_for_symbol("830799.BJ") == 0.30
    assert limit_pct_for_symbol("430047.BJ") == 0.30


def test_monthly_compound_matches_manual_product():
    idx = pd.to_datetime(["2024-01-02", "2024-01-31", "2024-02-01", "2024-02-29"])
    daily = pd.Series([0.01, -0.02, 0.03, 0.01], index=idx)
    monthly = monthly_compound(daily)
    assert abs(monthly["2024-01"] - ((1.01 * 0.98) - 1.0)) < 1e-12
    assert abs(monthly["2024-02"] - ((1.03 * 1.01) - 1.0)) < 1e-12


def _panel_rows(months: list[str], size: float = 1.0) -> list[dict]:
    rows = []
    for month in months:
        rows.append(
            {
                "month": month,
                "char_month": month,
                "symbol": "A",
                "size": size,
                "bm": 1.0,
                "op": 0.1,
                "inv": 0.05,
                "turn": 0.2,
                "ret": 0.0,
                "close_fwd": 1.0,
            }
        )
    return rows


def test_apply_academic_sorts_uses_june_snapshot():
    months = ["2024-06", "2024-07", "2024-12", "2025-01", "2025-06", "2025-07"]
    rows = []
    for i, month in enumerate(months):
        row = _panel_rows([month])[0]
        row["size"] = float(i + 1)  # 每期不同，便于断言取的是哪一期
        rows.append(row)
    panel = pd.DataFrame(rows)
    # factor_month=month；char_month 同月，方便手工核对
    out = apply_academic_sorts(panel).set_index("month")
    # 2024-07 ~ 2025-06 学术年 → 用 2024-06 的 size=1
    for m in ["2024-07", "2024-12", "2025-01", "2025-06"]:
        assert out.loc[m, "size"] == 1.0
    # 2025-07 → 用 2025-06 的 size=5
    assert out.loc["2025-07", "size"] == 5.0


def test_orthogonalize_inv_removes_op_exposure():
    rng = np.random.default_rng(3)
    rows = []
    for month in ["2024-01", "2024-02"]:
        for i in range(200):
            op = float(rng.normal())
            inv = 2.0 * op + float(rng.normal(scale=0.1))
            row = _panel_rows([month])[0]
            row.update({"symbol": f"S{i}", "op": op, "inv": inv})
            rows.append(row)
    panel = pd.DataFrame(rows)
    out = orthogonalize_inv(panel)
    for _, frame in out.groupby("month"):
        corr = np.corrcoef(frame["inv"], frame["op"])[0, 1]
        assert abs(corr) < 0.05
