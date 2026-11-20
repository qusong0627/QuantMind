"""训练四道闸与波动率目标模型的单元测试（2026-11-16）。

被测模块在训练镜像侧 ``docker/training/model_trainers/``；按既有测试模式
把 ``docker/training`` 加进 sys.path 后直接 import。全部合成数据，不读真实
QuantDB、不连 DB、不启动训练容器。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "docker" / "training"))

from model_trainers.gates import (  # noqa: E402
    compute_gates,
    horizon_curve,
    permutation_p,
    random_batch_report,
    residual_ic,
)
from model_trainers.vol_model import train_vol_model  # noqa: E402


def _panel(
    *, n_symbols: int = 120, n_days: int = 12, seed: int = 0, signal: bool = True
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(n_days):
        for s in range(n_symbols):
            x = float(rng.normal())
            ret = 0.02 * x + float(rng.normal(0, 0.02))
            vol = 0.01 + 0.005 * abs(x)
            rows.append(
                dict(
                    trade_date=pd.Timestamp("2026-01-01") + pd.Timedelta(days=d),
                    symbol=f"S{s:03d}",
                    x=x,
                    ret=ret,
                    vol=vol,
                )
            )
    df = pd.DataFrame(rows)
    df["pred"] = df["x"] if signal else rng.normal(size=len(df))
    df["label_return"] = df["ret"]
    df["label_return_h2"] = df["ret"] * 1.5
    df["label_vol"] = df["vol"]
    df["total_cap"] = rng.lognormal(10.0, 1.0, len(df))
    return df


@pytest.mark.unit
def test_permutation_p_detects_signal_and_rejects_noise() -> None:
    signal = permutation_p(_panel(seed=1), n_perm=50, seed=7)
    noise = permutation_p(_panel(seed=2, signal=False), n_perm=50, seed=7)

    assert signal["p_value"] < 0.05
    assert signal["actual_mean_ic"] > 0.3
    assert noise["p_value"] > 0.05


@pytest.mark.unit
def test_residual_ic_keeps_incremental_signal() -> None:
    report = residual_ic(_panel(seed=3), controls=("total_cap",))

    assert report["controls"] == ["total_cap"]
    assert report["resid_mean"] > 0.3
    assert report["resid_t_stat"] is not None and report["resid_t_stat"] > 2.0


@pytest.mark.unit
def test_random_batch_report_is_positive_across_batches() -> None:
    report = random_batch_report(_panel(seed=4), n_batches=4, seed=7)

    assert report["n_batches"] == 4
    assert report["rank_ic_mean"] > 0.3
    assert report["rank_ic_positive_frac"] == 1.0
    assert len(report["batches"]) == 4


@pytest.mark.unit
def test_horizon_curve_reads_horizon_columns() -> None:
    report = horizon_curve(_panel(seed=5), horizons=(1, 2, 3, 5, 10))

    assert "h2" in report
    assert report["h2"]["rank_ic"]["mean"] > 0.3
    assert "h1" not in report  # 没造 label_return_h1


@pytest.mark.unit
def test_compute_gates_degrades_when_symbol_missing() -> None:
    df = _panel(seed=6).drop(columns=["symbol"])
    report = compute_gates(df, n_perm=20, n_batches=3, seed=7)

    assert report["permutation"]["p_value"] < 0.05
    assert report["random_batches"]["skipped"] is True
    assert report["residual_ic"]["resid_mean"] > 0.3


@pytest.mark.unit
def test_vol_model_trains_and_returns_pred_vol(tmp_path: Path) -> None:
    df = _panel(seed=8)
    train = df[df["trade_date"] < pd.Timestamp("2026-01-08")]
    valid = df[
        (df["trade_date"] >= pd.Timestamp("2026-01-08"))
        & (df["trade_date"] < pd.Timestamp("2026-01-10"))
    ]
    test = df[df["trade_date"] >= pd.Timestamp("2026-01-10")]

    result = train_vol_model(
        train,
        valid,
        test,
        df,
        ["x", "vol"],
        tmp_path,
        n_estimators=20,
    )

    assert result["enabled"] is True
    assert Path(result["model_path"]).is_file()
    assert len(result["pred_df"]) == len(df)
    assert result["pred_df"]["pred_vol"].notna().all()
    assert result["metrics"]["valid"]["rank_ic"] > 0.3


@pytest.mark.unit
def test_vol_model_degrades_without_label(tmp_path: Path) -> None:
    df = _panel(seed=9).drop(columns=["label_vol"])

    result = train_vol_model(
        df, df, df, df, ["x", "vol"], tmp_path, n_estimators=10
    )

    assert result["enabled"] is False
    assert "label_vol" in result["reason"]
