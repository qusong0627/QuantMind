"""训练管线升级的纯函数测试：标签截面变换 / 极端标签清洗 / 稳健 Z-score。

覆盖 2026-11-09 引入的三个可选项（默认行为不变）：
- label_transform: rank（默认）/ zscore / raw
- label_drop_extreme_pct: 只对 train 段按日剔除两端 pct
- preprocessing.standardize: robust（median/IQR）

这些函数是纯函数，直接在宿主机 venv 跑，不依赖训练镜像。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "docker" / "training"))

import preprocessing  # noqa: E402


def test_label_rank_per_date() -> None:
    labels = pd.Series([1.0, 2.0, 3.0, 10.0, 20.0])
    dates = pd.Series(["d1", "d1", "d1", "d2", "d2"])
    out = preprocessing.cross_sectional_label_transform(labels, dates, "rank")
    # d1: pct rank [1/3, 2/3, 1] - 0.5；d2: [1/2, 1] - 0.5
    assert out.iloc[:3].tolist() == pytest.approx([-1 / 6, 1 / 6, 0.5])
    assert out.iloc[3:].tolist() == pytest.approx([0.0, 0.5])


def test_label_zscore_per_date() -> None:
    labels = pd.Series([1.0, 2.0, 3.0, 10.0, 20.0])
    dates = pd.Series(["d1", "d1", "d1", "d2", "d2"])
    out = preprocessing.cross_sectional_label_transform(labels, dates, "zscore")
    for d in ("d1", "d2"):
        x = out[dates == d]
        assert abs(float(x.mean())) < 1e-6
        assert abs(float(x.std(ddof=0)) - 1.0) < 1e-6


def test_label_top_tail_only_keeps_upper_tail() -> None:
    labels = pd.Series(np.arange(1, 11, dtype=float))
    dates = pd.Series(["d1"] * 10)
    out = preprocessing.cross_sectional_label_transform(
        labels, dates, "top_tail", top_pct=0.2
    )
    # 10 个值、top 20% = 最后 2 个；其余全部 0，尾部内部保持排序
    assert out.iloc[:-2].eq(0).all()
    assert out.iloc[-2] == pytest.approx(0.75)
    assert out.iloc[-1] == pytest.approx(1.0)


def test_label_raw_keeps_nan_and_invalid_raises() -> None:
    labels = pd.Series([1.0, np.nan, 3.0])
    dates = pd.Series(["d1", "d1", "d1"])
    out = preprocessing.cross_sectional_label_transform(labels, dates, "raw")
    assert out.iloc[0] == 1.0
    assert np.isnan(out.iloc[1])
    assert out.iloc[2] == 3.0
    with pytest.raises(ValueError):
        preprocessing.cross_sectional_label_transform(labels, dates, "bogus")


def test_drop_extreme_labels_per_date() -> None:
    frame = pd.DataFrame(
        {
            "trade_date": ["d1"] * 10 + ["d2"] * 10,
            "label_return": list(range(10)) + list(range(100, 110)),
        }
    )
    out = preprocessing.drop_extreme_labels(frame, 0.1)
    assert len(out) == 16  # 每日剔除最低/最高各 1 条
    d1 = out[out.trade_date == "d1"]
    assert d1.label_return.min() == 1
    assert d1.label_return.max() == 8
    # pct=0 原样返回
    assert len(preprocessing.drop_extreme_labels(frame, 0.0)) == len(frame)


def test_robust_zscore_median_zero_per_date() -> None:
    df = pd.DataFrame(
        {
            "trade_date": ["d1", "d1", "d1", "d2", "d2", "d2"],
            "f1": [1.0, 2.0, 100.0, 10.0, 20.0, 30.0],
        }
    )
    out = preprocessing.cross_sectional_robust_zscore(df.copy(), ["f1"])
    for d in ("d1", "d2"):
        m = out.trade_date == d
        assert abs(float(out.loc[m, "f1"].median())) < 1e-6


def test_label_config_and_request_validation() -> None:
    from backend.shared.training.request import TrainingRequest
    from backend.shared.training.schemas import LabelCfg

    cfg = LabelCfg(label_transform="zscore", label_drop_extreme_pct=0.05)
    assert cfg.label_transform == "zscore"
    assert cfg.label_drop_extreme_pct == 0.05

    base = {
        "model_type": "catboost",
        "train_start": "2016-01-04",
        "train_end": "2024-09-30",
        "features": ["mom_ret_1d"],
    }
    req = TrainingRequest.validate_request(
        {**base, "label_transform": "zscore", "label_drop_extreme_pct": 0.05}
    )
    assert req.label_transform == "zscore"
    assert req.label_drop_extreme_pct == 0.05

    with pytest.raises(HTTPException):
        TrainingRequest.validate_request({**base, "label_transform": "bad"})
    with pytest.raises(HTTPException):
        TrainingRequest.validate_request({**base, "label_drop_extreme_pct": 0.5})


def test_size_excess_rank_removes_bucket_level() -> None:
    labels = pd.Series([1.0, 2.0, 3.0, 10.0, 20.0, 30.0])
    dates = pd.Series(["d1"] * 6)
    sizes = pd.Series([1.0, 2.0, 3.0, 100.0, 200.0, 300.0])
    out = preprocessing.cross_sectional_label_transform(
        labels, dates, "size_excess_rank", size=sizes, n_size_buckets=3
    )
    # 桶内超额 = [0,-0.5,0.5,-10,0,10]；全局 pct rank - 0.5
    assert out.tolist() == pytest.approx([1 / 12, -1 / 6, 1 / 3, -1 / 3, 1 / 12, 0.5])


def test_size_excess_rank_invariant_to_bucket_level_shift() -> None:
    labels = pd.Series([1.0, 2.0, 3.0, 10.0, 20.0, 30.0])
    shifted = pd.Series([1.0, 2.0, 3.0, 20.0, 30.0, 40.0])
    dates = pd.Series(["d1"] * 6)
    sizes = pd.Series([1.0, 2.0, 3.0, 100.0, 200.0, 300.0])
    a = preprocessing.cross_sectional_label_transform(
        labels, dates, "size_excess_rank", size=sizes
    )
    b = preprocessing.cross_sectional_label_transform(
        shifted, dates, "size_excess_rank", size=sizes
    )
    assert a.tolist() == pytest.approx(b.tolist())


def test_size_excess_rank_requires_size() -> None:
    labels = pd.Series([1.0, 2.0, 3.0])
    dates = pd.Series(["d1"] * 3)
    with pytest.raises(ValueError):
        preprocessing.cross_sectional_label_transform(labels, dates, "size_excess_rank")
