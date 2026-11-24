"""截面排序报告核心指标单测（容器内 python3 直跑，无 pytest 依赖）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.scripts.eval.model_signal_report import (
    _residualize,
    _zscore,
    daily_cross_section_ic,
    fama_macbeth,
    quantile_report,
)


def _synthetic_panel(
    n_dates: int = 12, n_stocks: int = 300, seed: int = 3
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    dates = pd.date_range("2024-01-31", periods=n_dates, freq="ME").strftime("%Y%m%d")
    industries = np.array(["银行", "电子", "医药", "汽车"])
    for d in dates:
        size = rng.normal(22.0, 1.0, n_stocks)
        pred = rng.normal(0, 0.1, n_stocks)
        ret = 0.02 * pred / 0.1 + rng.normal(0, 0.02, n_stocks)
        rows.append(
            pd.DataFrame(
                {
                    "date_key": d,
                    "sym_suffix": [f"{i:06d}.SZ" for i in range(n_stocks)],
                    "pred": pred,
                    "label_return": ret,
                    "log_size": size,
                    "industry": industries[rng.integers(0, len(industries), n_stocks)],
                }
            )
        )
    return pd.concat(rows, ignore_index=True)


def test_residualize_removes_size_exposure():
    rng = np.random.default_rng(11)
    size = rng.normal(0, 1, 500)
    signal = 2.0 * size + rng.normal(0, 0.1, 500)
    resid = _residualize(signal, _zscore(size)[:, None])
    ok = np.isfinite(resid)
    corr = np.corrcoef(resid[ok], size[ok])[0, 1]
    assert abs(corr) < 0.05, corr


def test_daily_ic_positive_when_signal_predicts_return():
    df = _synthetic_panel()
    ic = daily_cross_section_ic(df)
    assert (ic["ic_raw"].dropna() > 0).mean() >= 0.9
    assert ic["ic_raw"].mean() > 0.3


def test_fama_macbeth_detects_significant_slope():
    df = _synthetic_panel()
    res = fama_macbeth(df, with_controls=False)
    assert res["n"] >= 10
    assert res["mean_slope"] > 0
    assert res["t"] > 3, res


def test_quantiles_monotonic_with_predictive_signal():
    quant = quantile_report(_synthetic_panel(), n_q=10)
    assert quant["monotonicity"] > 0.8, quant["monotonicity"]
    assert quant["spread_mean"] > 0
    assert quant["spread_pos_rate"] >= 0.9
