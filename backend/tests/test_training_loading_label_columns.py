"""``execution_price`` 标签列缺失：必须在 groupby 之前明确报错。

2026-11-08 任务 ``train_20261109123002_56a909dc``（catboost，
``execution_price=open``）在 ``loading.py`` 的标签路径抛
``KeyError: 'Column not found: open'``——错误发生在
``narrow.groupby("symbol")[_entry_col]``，日志没有列清单，看不出是
「因子源没选出 open」还是「数据源没有 open」。

本组用例用假 reader 构造小帧，钉住：缺 ``open``/``close`` 时抛带列清单的
``RuntimeError``，而不是 pandas ``KeyError``。全部在内存中完成，不读真实
QuantDB 目录、不连 DB。
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


def _run_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    frame: pd.DataFrame,
    *,
    execution_price: str,
):
    class _FakeReader:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def describe(self, source: str):
            return SimpleNamespace(min_date=None, max_date=None)

        def read_range(self, *args, **kwargs) -> pd.DataFrame:
            return frame.copy()

    monkeypatch.setattr(
        "backend.services.engine.data_platform.quantdb_factor_reader.QuantDBFactorReader",
        _FakeReader,
    )
    return loading.load_data(
        "2026-01-05",
        "2026-01-09",
        ["f1"],
        target_horizon_days=1,
        execution_price=execution_price,
        local_dir=str(tmp_path),
        factor_source="l1_factors",
        quantdb_dir=str(tmp_path),
    )


def _base_frame(*, drop: tuple[str, ...] = ()) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "symbol": ["SH600036"] * 5,
            "trade_date": pd.date_range("2026-01-05", periods=5, freq="D"),
            "open": np.arange(10.0, 15.0),
            "close": np.arange(10.5, 15.5),
            "volume": [1e6] * 5,
            "mom_ret_1d": [0.0, 0.02, 0.02, 0.02, 0.02],
            "f1": [1.0, 2.0, 3.0, 4.0, 5.0],
        }
    )
    return frame.drop(columns=[c for c in drop if c in frame.columns])


@pytest.mark.unit
def test_execution_price_open_without_open_column_fails_before_groupby(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    frame = _base_frame(drop=("open",))  # 只有 close / volume / mom_ret_1d，没有 open

    with pytest.raises(RuntimeError, match=r"缺少必要列.*open"):
        _run_load(monkeypatch, tmp_path, frame, execution_price="open")


@pytest.mark.unit
def test_execution_price_close_without_close_column_fails_before_groupby(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    frame = _base_frame(drop=("close",))  # 有 open、无 close

    with pytest.raises(RuntimeError, match=r"缺少必要列.*close"):
        _run_load(monkeypatch, tmp_path, frame, execution_price="close")


@pytest.mark.unit
def test_execution_price_open_with_both_columns_still_builds_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """有 open/close 时不得被新闸误杀（正路径不回归）。"""
    frame = _base_frame()  # open + close 都在

    df, valid_features = _run_load(
        monkeypatch, tmp_path, frame, execution_price="open"
    )

    assert len(df) > 0
    assert "label" in df.columns
    assert df["label"].notna().all()
    # 返回帧只保留 symbol/trade_date/label+features（open/close 是标签输入，
    # 不回传给训练器）——这里只钉「新闸不误杀正路径」。
    assert list(df.columns[:3]) == ["symbol", "trade_date", "label"]
    assert valid_features == ["f1"]
