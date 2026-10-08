"""mining_plugins RRE 评估器金样测试（TDD 先红后绿）。

金样：``backend/tests/fixtures/miningMetricsGolden.json``（前端描述符测试共读）。
口径 = AlphaEval ``modeltester.py:315-327``：probs = 当日排名份额（ranks/Σranks）；
KL_t = Σ p·ln((p+ε)/(p_prev+ε))，ε=1e-8；RRE = mean(1/(1+KL))。
首日 probs_prev 全 NaN → pandas sum(skipna) 给 0.0 → 该日计 1.0（与源实现同行为，
刻意保留，金样钉住）；某标的缺值日其 KL 项跳过。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GOLDEN = json.loads(
    (
        Path(__file__).resolve().parent / "fixtures" / "miningMetricsGolden.json"
    ).read_text(encoding="utf-8")
)

try:  # pragma: no cover - 环境相关
    from backend.services.engine.mining_plugins.evaluators.reliability import (
        EPSILON,
        compute_rre,
    )
except Exception as _exc:  # noqa: BLE001
    compute_rre = None
    EPSILON = None
    _IMPORT_ERR = _exc

pytestmark = pytest.mark.skipif(compute_rre is None, reason="mining_plugins 不可用")


def _paired(case: dict) -> pd.DataFrame:
    """把 fixture 的 values 行式面板摊成 paired 长表（None 行剔除）。"""
    dates = pd.date_range("2024-01-01", periods=len(case["values"]))
    rows = [
        (d, sym, float(v), 0.0)
        for d, row in zip(dates, case["values"], strict=True)
        for sym, v in zip(case["symbols"], row, strict=True)
        if v is not None
    ]
    return pd.DataFrame(rows, columns=["datetime", "symbol", "factor", "ret"])


@pytest.mark.parametrize(
    "case", GOLDEN["rre_cases"], ids=[c["name"] for c in GOLDEN["rre_cases"]]
)
def test_rre_matches_golden(case: dict) -> None:
    got = compute_rre(_paired(case))
    assert got == pytest.approx(case["expected_rre"], rel=1e-6)


def test_epsilon_contract_matches_golden() -> None:
    """ε 是金样数值的一部分，改 ε 必须同步重算金样。"""
    assert EPSILON == GOLDEN["meta"]["epsilon"]


def test_stable_ranking_is_exactly_one() -> None:
    """排序天天不变 → 每日分布相同 → KL=0 → RRE 恰为 1（含首日 1.0 惯例）。"""
    case = next(c for c in GOLDEN["rre_cases"] if c["name"] == "stable_rank_is_one")
    assert compute_rre(_paired(case)) == pytest.approx(1.0, abs=1e-12)


def test_single_day_is_none() -> None:
    """单日面板没有「相邻日」可言，不给 1.0 这种伪值。"""
    paired = pd.DataFrame(
        [("2024-01-01", "A", 1.0, 0.0), ("2024-01-01", "B", 2.0, 0.0)],
        columns=["datetime", "symbol", "factor", "ret"],
    )
    paired["datetime"] = pd.to_datetime(paired["datetime"])
    assert compute_rre(paired) is None


def test_empty_is_none() -> None:
    empty = pd.DataFrame(columns=["datetime", "symbol", "factor", "ret"])
    assert compute_rre(empty) is None


def test_shuffle_beats_stable_lower() -> None:
    """方向性 sanity：逐日乱序的 RRE 必须低于稳定排序。"""
    stable = _paired(
        next(c for c in GOLDEN["rre_cases"] if c["name"] == "stable_rank_is_one")
    )
    swap = _paired(
        next(c for c in GOLDEN["rre_cases"] if c["name"] == "swap_two_names")
    )
    assert compute_rre(swap) < compute_rre(stable)
