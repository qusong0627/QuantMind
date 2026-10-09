"""因子质量分档（后端侧）——与前端 ``classifyQuality`` 共用金样。

金样只有**一份**（``backend/tests/fixtures/factorQualityGolden.json``），前端
``electron/src/features/alpha-research/services-v2/__tests__/classifyQuality.golden.test.ts``
反向读取本文件（与 ``researchScoreGolden.json`` 同一套纪律）：两边各存一份迟早
漂移，而两处阈值一旦漂移，界面瓦片（全量口径）与列表徽章（前端口径）会对不上，
用户看到「中等 47」却筛出 24 条——正是本轮修复要消灭的那类假象。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

try:  # pragma: no cover - 环境相关
    from backend.services.engine.qlib_app.services.rd_agent_persistence import (
        QUALITY_HIGH_MIN_ABS_IC,
        QUALITY_MEDIUM_MIN_ABS_IC,
        classify_quality,
    )
except Exception:  # noqa: BLE001
    classify_quality = None

pytestmark = pytest.mark.skipif(
    classify_quality is None, reason="依赖不可用（需容器环境）"
)

GOLDEN_PATH = Path(__file__).resolve().parent / "fixtures" / "factorQualityGolden.json"


@pytest.fixture(scope="module")
def golden() -> dict:
    assert GOLDEN_PATH.exists(), f"金样文件缺失：{GOLDEN_PATH}"
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_thresholds_match_golden(golden: dict) -> None:
    """阈值唯一出处：实现常量 == 金样，前端测试反向钉同一对数字。"""
    assert QUALITY_HIGH_MIN_ABS_IC == golden["highMinAbsIc"]
    assert QUALITY_MEDIUM_MIN_ABS_IC == golden["mediumMinAbsIc"]


@pytest.mark.unit
@pytest.mark.parametrize("case", json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["cases"])
def test_classify_cases_match_golden(case: dict) -> None:
    assert classify_quality(case["ic"]) == case["quality"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (float("nan"), "unknown"),
        (float("inf"), "unknown"),
        (float("-inf"), "unknown"),
        ("not-a-number", "unknown"),
    ],
)
def test_missing_and_non_finite_are_unknown(value, expected: str) -> None:
    """缺失/非有限值一律 unknown——绝不落 low（「没算出来」≠「算出来是差」）。"""
    assert classify_quality(value) == expected
