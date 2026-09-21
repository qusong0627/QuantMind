"""``/research/predict-stock`` 响应里的 ``rank_pct``（对外展示的中性刻度）。

这个字段是前端「研究评分」的唯一输入，取错值的后果不是报错而是**显示成一个
看起来很正常的分数**，所以边界要逐条钉住：分位必须落在 0–1，越界宁可返回
``None``（界面显示「—」）也不能夹断成 1.0。
"""

from __future__ import annotations

import pytest

from backend.services.api.routers.research_service import _rank_pct_of


def test_dict_取值() -> None:
    assert _rank_pct_of({"rank_pct": 0.871}) == pytest.approx(0.871)


def test_行对象取值走_mapping() -> None:
    """SQLAlchemy Row 的 ``in`` 判的是值不是键，必须走 ``_mapping``。"""

    class _FakeRow:
        def __init__(self, mapping: dict) -> None:
            self._mapping = mapping

    assert _rank_pct_of(_FakeRow({"rank_pct": 0.5})) == pytest.approx(0.5)
    assert _rank_pct_of(_FakeRow({"fusion_score": 1.2})) is None


def test_键不存在返回_None() -> None:
    assert _rank_pct_of({}) is None
    assert _rank_pct_of({"fusion_score": 0.9}) is None


def test_行是_None_返回_None() -> None:
    assert _rank_pct_of(None) is None


@pytest.mark.parametrize("raw", [None, "0.5", True, False])
def test_非数值返回_None(raw: object) -> None:
    assert _rank_pct_of({"rank_pct": raw}) is None


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf")])
def test_非有限值返回_None(raw: float) -> None:
    assert _rank_pct_of({"rank_pct": raw}) is None


@pytest.mark.parametrize("raw", [1.5, -0.01, 87.1])
def test_越界返回_None_而不是夹断(raw: float) -> None:
    """87.1 这种「百分数被当分位传进来」的情形必须显形，不能渲染成 100 分。"""
    assert _rank_pct_of({"rank_pct": raw}) is None


@pytest.mark.parametrize("raw", [0.0, 1.0])
def test_两端闭区间是合法分位(raw: float) -> None:
    assert _rank_pct_of({"rank_pct": raw}) == raw
