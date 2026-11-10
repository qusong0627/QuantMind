"""因子研究时间窗 / 方向冻结 / 年分区 —— 口径单源回归。

钉住三件事（错了不会报错，只会静默给出「看起来能回测」的错数）：
1. 年分区只读与请求区间相交的年份，且 end=YYYY-MM 取当月最后一天；
2. sign 只认校准窗（2016–2019 评估时不得偷看 2020+ 的 IC）；
3. 已冻结方向优先于本块重算，绝不因换窗口翻符号。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.services.engine.factor_research import windows as W


def test_write_read_year_partitions_只取相交年份(tmp_path):
    df = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(
                ["2019-12-31", "2020-01-23", "2020-12-31", "2021-01-29"]
            ),
            "factor_code": ["f"] * 4,
            "score": [1.0, 2.0, 3.0, 4.0],
        }
    )
    W.write_year_partitions(df, tmp_path, "factor_panel")
    files = W.year_files(tmp_path / "factor_panel")
    assert [p.name for p in files] == [
        "year=2019.parquet",
        "year=2020.parquet",
        "year=2021.parquet",
    ]

    got = W.read_year_partitions(
        tmp_path / "factor_panel", start="2020-01", end="2020-12"
    )
    assert list(got["score"]) == [2.0, 3.0]  # 2019/2021 不得混入

    # end=YYYY-MM 必须含当月最后一天（不是 12-01）
    got2 = W.read_year_partitions(
        tmp_path / "factor_panel", start="2019-01", end="2020-01"
    )
    assert list(got2["score"]) == [1.0, 2.0]


def test_year_files_区间裁剪():
    assert W.selected_years(None, None) is None
    assert W.selected_years("2016-07-01", None) == (2016, 2999)
    assert W.selected_years(None, "2019-12-31") == (1900, 2019)


def _ic_matrix() -> tuple[np.ndarray, list[pd.Timestamp]]:
    dates = [pd.Timestamp(f"{y}-06-30") for y in (2016, 2020, 2024, 2025)]
    # F0: 校准窗正、评估窗负（若按全样本算会被翻成 -1）
    # F1: 校准窗负、评估窗正
    # F2: 校准窗全 NaN、评估窗正（必须 fallback 到全样本）
    ic = np.array(
        [
            [0.10, 0.05, 0.04, -0.30],
            [-0.10, -0.05, -0.04, 0.30],
            [np.nan, np.nan, np.nan, 0.20],
        ]
    )
    return ic, dates


def test_calibrated_signs_只认校准窗不偷看评估窗():
    ic, dates = _ic_matrix()
    signs, entries = W.calibrated_signs(
        ic,
        dates,
        ["F0", "F1", "F2"],
        cal_start="2020-01-01",
        cal_end="2024-12-31",
    )
    assert list(signs) == [1.0, -1.0, 1.0]
    assert entries[0]["source"] == "calibration"
    assert entries[2]["source"] == "fallback"
    assert entries[2]["fallback"] is True


def test_calibrated_signs_校准窗不在构建窗口内_标记fallback():
    """只重建 2016–2019 时校准窗（2020–2024）不在本次 dates 里。

    此时 sign 只能用本窗 IC 兜底，但**必须在审计条目里标 fallback**，不能
    冒充「按校准窗算的」——否则增量块的方向来源无法对账。
    """
    ic = np.array([[0.10, -0.20]])
    dates = [pd.Timestamp("2016-06-30"), pd.Timestamp("2017-06-30")]
    signs, entries = W.calibrated_signs(
        ic,
        dates,
        ["F"],
        cal_start="2020-01-01",
        cal_end="2024-12-31",
    )
    assert list(signs) == [-1.0]  # 本窗 IC 均值为负
    assert entries[0]["source"] == "fallback"
    assert entries[0]["fallback"] is True


def test_calibrated_signs_冻结方向优先():
    ic, dates = _ic_matrix()
    signs, entries = W.calibrated_signs(
        ic,
        dates,
        ["F0", "F1"],
        cal_start="2020-01-01",
        cal_end="2024-12-31",
        frozen={"F0": {"direction": -1, "source": "frozen", "calibration": ["2020", "2024"]}},
    )
    assert list(signs) == [-1.0, -1.0]
    assert entries[0]["source"] == "frozen"


def test_merge_direction_entries_不覆盖已冻结方向():
    base = {"A": {"direction": -1, "source": "calibration"}}
    merged = W.merge_direction_entries(
        base, [{"direction": 1, "source": "calibration"}], ["A"]
    )
    assert merged["A"]["direction"] == -1  # 冻结值优先


def test_merge_direction_entries_补缺失因子():
    base = {"A": {"direction": -1}}
    merged = W.merge_direction_entries(
        base, [{"direction": 1, "source": "calibration"}], ["B"]
    )
    assert merged["B"]["direction"] == 1
    assert merged["A"]["direction"] == -1


def test_direction_文件往返(tmp_path):
    p = tmp_path / "direction.json"
    W.save_direction(
        p,
        {"A": {"direction": -1, "source": "calibration", "ic_mean": -0.02}},
        calibration=("2020-01-01", "2024-12-31"),
    )
    got = W.load_direction(p)
    assert got["A"]["direction"] == -1
    assert W.load_direction(tmp_path / "missing.json") == {}


def test_defaults_CN_2016_非CN_保持旧窗口():
    assert W.defaults_for_market("CN") == ("2016-07-01", "2016-01-04")
    assert W.defaults_for_market("HK") == ("2020-01-01", "2018-06-01")
    with pytest.raises(ValueError):
        W.parse_start("not-a-date", "2016-07-01")
