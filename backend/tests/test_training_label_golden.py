"""Legacy 标签构造金样（W0 护栏）。

目的不是重复 `test_training_label_preprocessing.py` 的变换单测，而是钉住
`load_data` 的**远期收益构造**本身：T+lag 入场、T+lag+horizon 出场、按 symbol
分组 shift、以及 rank/classification 在有效标签截面上的口径。

任何改动导致标签数值漂移（哪怕 1e-13）都会红；这是后续 Target Contract
“旧请求零差异”的地基。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "docker" / "training"))

from data import loading  # noqa: E402


def _panel(symbols: int = 4, days: int = 20) -> pd.DataFrame:
    rows = []
    dates = pd.date_range("2026-01-05", periods=days, freq="D")
    for k in range(symbols):
        for i, day in enumerate(dates):
            base = 10.0 + k * 3.0 + i * 0.07
            rows.append(
                {
                    "symbol": f"SH60000{k}",
                    "trade_date": day,
                    "open": base,
                    "close": base + 0.5 + i * 0.013,
                    "volume": 1e6,
                    "mom_ret_1d": 0.01,
                    "mom_ret_5d": 0.05,
                    "f1": float(i),
                }
            )
    return pd.DataFrame(rows)


def _run_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    frame: pd.DataFrame,
    *,
    horizon: int,
    execution_price: str,
    label_transform: str,
    target_mode: str = "return",
):
    class _FakeReader:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def describe(self, source: str):
            return SimpleNamespace(min_date=None, max_date=None)

        def read_range(self, *args, **kwargs) -> pd.DataFrame:
            return frame.copy()

        def source_path(self, source: str) -> str:
            return str(tmp_path / "fake")

    monkeypatch.setattr(
        "backend.services.engine.data_platform.quantdb_factor_reader.QuantDBFactorReader",
        _FakeReader,
    )
    return loading.load_data(
        "2026-01-05",
        "2026-01-20",
        ["f1"],
        target_horizon_days=horizon,
        target_mode=target_mode,
        execution_price=execution_price,
        label_transform=label_transform,
        local_dir=str(tmp_path),
        factor_source="l1_factors",
        quantdb_dir=str(tmp_path),
    )


def _expected_raw(frame: pd.DataFrame, *, horizon: int, execution_price: str) -> pd.DataFrame:
    out = frame.sort_values(["symbol", "trade_date"]).copy()
    entry_col = "open" if execution_price == "open" else "close"
    entry = out.groupby("symbol")[entry_col].shift(-1)
    future = out.groupby("symbol")["close"].shift(-(1 + horizon))
    out["expected_raw"] = future / entry - 1.0
    return out[["symbol", "trade_date", "expected_raw"]]


def _merge(df: pd.DataFrame, expected: pd.DataFrame) -> pd.DataFrame:
    merged = df.merge(expected, on=["symbol", "trade_date"], how="inner")
    assert len(merged) == len(df), "输出行必须都能在期望帧中找到"
    return merged


@pytest.mark.unit
@pytest.mark.parametrize("execution_price", ["open", "close"])
@pytest.mark.parametrize("horizon", [1, 5])
def test_forward_return_matches_pit_shift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    horizon: int,
    execution_price: str,
) -> None:
    frame = _panel()
    df, _ = _run_load(
        monkeypatch,
        tmp_path,
        frame,
        horizon=horizon,
        execution_price=execution_price,
        label_transform="raw",
    )
    merged = _merge(df, _expected_raw(frame, horizon=horizon, execution_price=execution_price))
    np.testing.assert_allclose(
        merged["label"].to_numpy(dtype=float),
        merged["expected_raw"].to_numpy(dtype=float),
        rtol=0,
        atol=1e-6,
    )
    assert merged["label"].notna().all()


@pytest.mark.unit
def test_rank_label_is_cross_sectional_pct_rank(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    frame = _panel()
    df, _ = _run_load(
        monkeypatch,
        tmp_path,
        frame,
        horizon=5,
        execution_price="open",
        label_transform="rank",
    )
    expected = _expected_raw(frame, horizon=5, execution_price="open")
    # 只对输出中实际存在的 (date, symbol) 截面计算 rank；load_data 先丢 NaN 标签
    keys = set(zip(df["symbol"], df["trade_date"]))
    expected = expected[
        expected.apply(lambda r: (r["symbol"], r["trade_date"]) in keys, axis=1)
    ].copy()
    expected["expected_rank"] = (
        expected.groupby("trade_date")["expected_raw"].rank(pct=True) - 0.5
    )
    merged = _merge(df, expected[["symbol", "trade_date", "expected_rank"]])
    np.testing.assert_allclose(
        merged["label"].to_numpy(dtype=float),
        merged["expected_rank"].to_numpy(dtype=float),
        rtol=0,
        atol=1e-6,
    )


@pytest.mark.unit
def test_classification_label_is_binarized_raw_return(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    frame = _panel()
    df, _ = _run_load(
        monkeypatch,
        tmp_path,
        frame,
        horizon=5,
        execution_price="open",
        label_transform="rank",  # 分类模式应忽略变换
        target_mode="classification",
    )
    merged = _merge(
        df, _expected_raw(frame, horizon=5, execution_price="open")
    )
    expected = (merged["expected_raw"] > 0).astype(float).to_numpy()
    np.testing.assert_array_equal(merged["label"].to_numpy(dtype=float), expected)


def test_label_path_has_no_backtest_period_coupling() -> None:
    """源码守卫：标签构造不得复用回测的 deal_price / 回测周期字段。"""
    text = (ROOT / "docker" / "training" / "data" / "loading.py").read_text(
        encoding="utf-8"
    )
    assert "deal_price" not in text
    assert 'context.get("target_horizon_days")' not in text
