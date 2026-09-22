"""风格产物取数层：财务序列 → TTM → PIT 对齐的已知答案测试。

这两步错了不会报错，只会让 rmw/cma 变成两个「看起来像风格」的噪声列 ——
故用合成序列把 单季/累计 两种语义、缺季、公告滞后 全部钉死。
"""

from __future__ import annotations

import numpy as np
import pytest

from backend.scripts.build_style_factors import pit_align, ttm_from_reports

Q = (331, 630, 930, 1231)  # 报告期月日


def _quarters(per_year: list[tuple[int, list[float]]]):
    """把 [(年, [四季值])] 铺成 (timetags, values)。"""
    tags, vals = [], []
    for y, vs in per_year:
        assert len(vs) == 4
        for q, v in zip(Q, vs):
            tags.append(y * 10000 + q)
            vals.append(v)
    return np.array(tags, dtype=np.int64), np.array(vals, dtype=float)


def test_单季序列_TTM_等于滚动四季和_前三个报告期无定义():
    tags, vals = _quarters([(2023, [1, 2, 3, 4]), (2024, [10, 20, 30, 40])])
    out = ttm_from_reports(tags, vals, cumulative=False)
    assert np.isnan(out[:3]).all(), "不足四季必须 NaN，不能用「有多少算多少」"
    assert out[3] == pytest.approx(10.0)              # 2023Q4：1+2+3+4
    assert out[4] == pytest.approx(2 + 3 + 4 + 10)    # 2024Q1：跨年滚动
    assert out[7] == pytest.approx(100.0)             # 2024FY 视角：10+20+30+40


def test_累计序列_先差分还原单季再滚动():
    cum = np.cumsum([1, 2, 3, 4])                     # 单季 = [1,2,3,4]
    cum24 = np.cumsum([10, 20, 30, 40])               # 单季 = [10,20,30,40]
    tags, _ = _quarters([(2023, [0] * 4), (2024, [0] * 4)])
    vals = np.concatenate([cum, cum24])
    out = ttm_from_reports(tags, vals, cumulative=True)
    assert np.isnan(out[:3]).all()
    assert out[3] == pytest.approx(10.0)              # 2023 全年累计
    assert out[4] == pytest.approx(2 + 3 + 4 + 10)    # 跨年：Q2'23+Q3'23+Q4'23+Q1'24
    assert out[7] == pytest.approx(100.0)             # 2024 全年累计


def test_累计序列_跨年同季差分正确():
    """Q1'25 的累计值 = 单季 Q1'25（年初重置），不是 Q1'25 − Q4'24。"""
    tags, _ = _quarters([(2024, [0] * 4), (2025, [0] * 4)])
    cum = np.concatenate([np.cumsum([1, 2, 3, 4]), np.cumsum([7, 8, 9, 10])])
    out = ttm_from_reports(tags, cum, cumulative=True)
    assert out[4] == pytest.approx(2 + 3 + 4 + 7)
    assert out[7] == pytest.approx(7 + 8 + 9 + 10)


def test_缺一期则整个窗口作废():
    tags = np.array([20230331, 20230630, 20231231, 20240331, 20240630], dtype=np.int64)
    vals = np.array([1.0, 2.0, 4.0, 8.0, 16.0])       # 缺 2023Q3
    out = ttm_from_reports(tags, vals, cumulative=False)
    assert np.isnan(out).all(), "季度不连续时任何 TTM 都不能出数"


def test_值缺失按缺季处理():
    tags, _ = _quarters([(2023, [0] * 4), (2024, [0] * 4)])
    vals = np.array([1.0, 2.0, np.nan, 4.0, 10.0, 20.0, 30.0, 40.0])
    out = ttm_from_reports(tags, vals, cumulative=False)
    assert np.isnan(out[3]) and np.isnan(out[4]), "被污染的两窗必须作废"
    assert out[7] == pytest.approx(100.0), "污染窗口移出后必须恢复出数"


def test_pit对齐_公告日之前看不到_当日可见():
    ann = np.array([20240429, 20240829, 20241030])     # Q1/H1/Q3 公告
    val = np.array([11.0, 22.0, 33.0])
    dates = np.array([20240401, 20240429, 20240828, 20240829, 20250101])
    out = pit_align(ann, val, dates)
    assert np.isnan(out[0]), "公告前不可见（前视偏差防线）"
    assert out[1] == 11.0, "公告当日即可见"
    assert out[2] == 11.0 and out[3] == 22.0, "H1 公告前后各取其值"
    assert out[4] == 33.0
