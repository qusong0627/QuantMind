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
    label_transform: str = "rank",
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
        label_transform=label_transform,
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


@pytest.mark.unit
def test_label_vol_and_horizon_labels_are_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """风险/多周期标签：label_vol + label_return_h2 随主标签一起落盘。

    5 行单票 close=10.5..14.5，T+1 收盘执行：
    - label_vol(h=1) = close[T+2]/close[T+1]-1，前 3 行有值；
    - label_return_h2 = close[T+3]/close[T+1]-1，前 2 行有值。
    """
    frame = _base_frame()

    df, valid_features = _run_load(
        monkeypatch, tmp_path, frame, execution_price="close"
    )

    assert valid_features == ["f1"]
    assert "label_vol" in df.columns
    assert "label_return_h2" in df.columns
    assert df["label_vol"].notna().sum() == 3
    assert df["label_vol"].iloc[0] == pytest.approx(12.5 / 11.5 - 1.0, rel=1e-6)
    assert df["label_return_h2"].iloc[0] == pytest.approx(
        13.5 / 11.5 - 1.0, rel=1e-6
    )
    assert df["label_return_h2"].iloc[1] == pytest.approx(
        14.5 / 12.5 - 1.0, rel=1e-6
    )


@pytest.mark.unit
def test_horizon_labels_and_vol_use_qfq_basis_when_panel_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """多周期评估标签/label_vol 必须与主标签同价基（qfq）。

    2026-11-19 任务 train_79_t10_2010：主标签 rank IC 0.153，而 gates
    h10 IC 仅 0.003 —— 根因是 ``_entry`` 走 ``_qfq_close``、``_future`` /
    ``_daily`` 仍走因子源不复权 ``close``（混合口径）。这里用人为缩放的
    qfq 面板（因子 1.0→0.6 递减）钉住两者同基。
    """
    frame = _base_frame()
    scale = np.array([1.0, 0.9, 0.8, 0.7, 0.6])
    panel = pd.DataFrame(
        {
            "symbol": ["SH600036"] * 5,
            "trade_date": pd.date_range("2026-01-05", periods=5, freq="D"),
            "_qfq_open": frame["open"].to_numpy() * scale,
            "_qfq_close": frame["close"].to_numpy() * scale,
        }
    )
    monkeypatch.setattr(
        "backend.shared.qfq_label_prices.load_qfq_label_panel",
        lambda *args, **kwargs: panel.copy(),
    )

    df, _ = _run_load(
        monkeypatch, tmp_path, frame, execution_price="close", label_transform=""
    )

    qfq = panel["_qfq_close"].to_numpy()
    # 主标签走 rank 变换（单票日期 → 恒 0.5），主标签的 qfq 同基由
    # backend/tests/test_qfq_label_prices.py 钉住；这里只钉本次修复的两处：
    # h2 标签：qfq[T+3]/qfq[T+1]-1；混合口径会得到 close[T+3]/qfq[T+1]-1≈0.304
    assert df["label_return_h2"].iloc[0] == pytest.approx(
        qfq[3] / qfq[1] - 1.0, rel=1e-6
    )
    # label_vol(h=1)：qfq[T+2] 日收益的均方根
    assert df["label_vol"].iloc[0] == pytest.approx(
        abs(qfq[2] / qfq[1] - 1.0), rel=1e-6
    )
