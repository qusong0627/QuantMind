"""mining_plugins 换手/扣成本评估器金样测试（TDD 先红后绿）。

金样：``backend/tests/fixtures/miningMetricsGolden.json``。
口径：多头集合 S_t = 当日 rank(pct)≥0.7；to_t = |S_t∖S_{t-1}|/|S_t|（首日无定义被排除，
与 AlphaEval 一致，首日也不计成本）；r_net = r − to×cost_rate（研究口径 0.2% 双边，
来源 factor_research.analysis.COST_RATE 单一出处）；净指标与毛指标同款「>1 个点」门。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
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
    from backend.services.engine.mining_plugins.config import get_cost_rate
    from backend.services.engine.mining_plugins.evaluators.turnover_cost import (
        compute_turnover_cost,
    )
except Exception as _exc:  # noqa: BLE001
    compute_turnover_cost = None
    get_cost_rate = None
    _IMPORT_ERR = _exc

pytestmark = pytest.mark.skipif(
    compute_turnover_cost is None, reason="mining_plugins 不可用"
)

METRIC_KEYS = (
    "turnover_daily",
    "ann_turnover",
    "ann_return_net",
    "sharpe_net",
    "max_drawdown_net",
)


def _paired_from_rows(symbols, factors, returns) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(factors))
    rows = [
        (d, sym, float(f), float(r))
        for d, frow, rrow in zip(dates, factors, returns, strict=True)
        for sym, f, r in zip(symbols, frow, rrow, strict=True)
    ]
    return pd.DataFrame(rows, columns=["datetime", "symbol", "factor", "ret"])


def _case_paired(case: dict) -> pd.DataFrame:
    return _paired_from_rows(case["symbols"], case["factors"], case["returns"])


@pytest.mark.parametrize(
    "case",
    GOLDEN["turnover_cases"],
    ids=[c["name"] for c in GOLDEN["turnover_cases"]],
)
def test_turnover_cost_matches_golden(case: dict) -> None:
    got = compute_turnover_cost(_case_paired(case), case["cost_rate"])
    assert set(METRIC_KEYS) <= set(got)
    for key in METRIC_KEYS:
        expected = case["expected"][key]
        value = got[key]
        if expected is None:
            assert value is None, f"{case['name']}.{key} 应为 None，实得 {value!r}"
        else:
            assert value == pytest.approx(expected, rel=1e-6), f"{case['name']}.{key}"


def test_first_day_excluded_from_turnover() -> None:
    """首日没有「昨日名单」：不产生换手样本，也不计成本。"""
    case = next(
        c for c in GOLDEN["turnover_cases"] if c["name"] == "three_day_rotation"
    )
    paired = _case_paired(case)
    # 只留第一天 → 全部指标无定义
    day1 = paired[paired["datetime"] == paired["datetime"].min()]
    got = compute_turnover_cost(day1, case["cost_rate"])
    assert all(got[k] is None for k in METRIC_KEYS)


def test_zero_cost_equals_gross_return_after_first_day() -> None:
    """cost_rate=0 时扣费收益必须等于「首日除外的毛收益」——不能偷偷少扣或多扣。"""
    case = next(
        c for c in GOLDEN["turnover_cases"] if c["name"] == "three_day_rotation"
    )
    got = compute_turnover_cost(_case_paired(case), 0.0)
    # returns 第 2..4 天 = 0.01 / 0.02 / -0.01
    expected_ann = (0.01 + 0.02 - 0.01) / 3 * 252
    assert got["ann_return_net"] == pytest.approx(expected_ann, rel=1e-9)


def test_two_day_panel_turnover_defined_but_net_gated() -> None:
    """两日面板：换手有定义（1 个样本），但净指标只有 1 个点 → 与毛指标同款 >1 门。"""
    symbols = [chr(ord("A") + i) for i in range(10)]
    frow = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    paired = _paired_from_rows(symbols, [frow, frow], [[0.0] * 10, [0.03] * 10])
    got = compute_turnover_cost(paired, 0.002)
    assert got["turnover_daily"] == pytest.approx(0.0)
    assert got["ann_turnover"] == pytest.approx(0.0)
    assert got["ann_return_net"] is None
    assert got["sharpe_net"] is None


def test_default_cost_rate_is_single_source() -> None:
    """默认成本 = factor_research.analysis.COST_RATE（0.002 双边），三处不许漂移。"""
    from backend.services.engine.factor_research.analysis import COST_RATE

    assert get_cost_rate() == pytest.approx(float(COST_RATE))
    assert float(COST_RATE) == pytest.approx(GOLDEN["meta"]["cost_rate"])


def test_env_override_changes_cost_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QM_MINING_COST_RATE", "0.005")
    assert get_cost_rate() == pytest.approx(0.005)


def test_nan_factor_rows_are_ignored_in_membership() -> None:
    """缺值标的当日在名单外——排名分位与换手分母都只数有效标的。"""
    symbols = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
    day1 = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    # 第二天 D 缺值、A 掉到最低：有效 9 只 → rank(pct)>=0.7 取 rank 7..9 = {J,C,B}
    day2 = [1, 9, 8, None, 6, 5, 4, 3, 2, 7]
    paired = pd.DataFrame(
        [("2024-01-01", s, float(f), 0.01) for s, f in zip(symbols, day1, strict=True)]
        + [
            ("2024-01-02", s, float(f), 0.02)
            for s, f in zip(symbols, day2, strict=True)
            if f is not None
        ],
        columns=["datetime", "symbol", "factor", "ret"],
    )
    paired["datetime"] = pd.to_datetime(paired["datetime"])
    got = compute_turnover_cost(paired, 0.002)
    # day1 10 只 → 4 只 {A,B,C,D}；day2 3 只 {B,C,J}；进场 {J} → to=1/3
    # （若分母错用 10 而不是有效 3，会得到 1/10）
    assert got["turnover_daily"] == pytest.approx(1.0 / 3.0)


def test_sharpe_gate_matches_gross_convention() -> None:
    """净夏普用与毛夏普相同的 +1e-8 防零除写法（数值影响 <1e-6，仅口径对齐）。"""
    case = next(
        c for c in GOLDEN["turnover_cases"] if c["name"] == "three_day_rotation"
    )
    got = compute_turnover_cost(_case_paired(case), case["cost_rate"])
    rets = np.array([0.0095, 0.02, -0.011])
    expected = rets.mean() / (rets.std(ddof=1) + 1e-8) * np.sqrt(252)
    assert got["sharpe_net"] == pytest.approx(float(expected), rel=1e-8)
