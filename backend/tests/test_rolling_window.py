"""滚动训练窗口计算器 golden 测试（P1，设计文档 §4.1/§4.8-④）。

纪律：全部按**交易日历**计算，禁止自然日近似。本测试用 fixture 日历
（`backend/tests/fixtures/rollingWindowGolden.json`）锁定：
1. 小日历手工推演的段边界（人可复核）；
2. 长日历上按文档默认 policy（756/126/63 + purge=horizon+lag）的完整 plan；
3. CLI `rolling_train.py --dry-run --calendar-file` 输出与同一 fixture 逐字一致。

改口径先改 fixture 并说明理由 —— 这是防「窗口悄悄漂移」的 golden 锁。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from backend.shared.training.rolling_window import (
    DEFAULT_TEST_DAYS,
    DEFAULT_TRAIN_DAYS,
    DEFAULT_VALID_DAYS,
    WindowCalculationError,
    WindowPolicy,
    compute_window,
    month_index,
    resolve_anchor,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "rollingWindowGolden.json"


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_small_calendar_window_endpoints_exact(golden):
    """小日历（30 个工作日）手工推演段边界 —— 逐位锁定。"""
    case = golden["small_calendar"]
    days = [date.fromisoformat(d) for d in case["trading_days"]]
    policy = WindowPolicy.from_dict(case["policy"])
    window = compute_window(
        case["anchor_date"], days, policy, horizon_days=case["horizon_days"]
    )
    assert window.to_plan() == case["expected_plan"]


@pytest.mark.unit
def test_default_policy_long_calendar_golden(golden):
    """文档默认 policy（756/126/63，purge=horizon+lag）在长日历上的完整 plan。"""
    case = golden["default_policy"]
    days = [date.fromisoformat(d) for d in case["trading_days"]]
    policy = WindowPolicy.from_dict(case["policy"])
    window = compute_window(
        case["anchor_date"], days, policy, horizon_days=case["horizon_days"]
    )
    assert window.to_plan() == case["expected_plan"]


@pytest.mark.unit
def test_purge_override_wins_over_horizon(golden):
    """显式 purge_days 覆盖 horizon+lag 推导。"""
    case = golden["purge_override"]
    days = [date.fromisoformat(d) for d in golden["small_calendar"]["trading_days"]]
    policy = WindowPolicy.from_dict(case["policy"])
    window = compute_window(
        case["anchor_date"], days, policy, horizon_days=case["horizon_days"]
    )
    assert window.to_plan() == case["expected_plan"]


@pytest.mark.unit
def test_window_segments_gap_equals_purge(golden):
    """段间实际交易日间隔 == purge（train_end/valid_start、valid_end/test_start）。"""
    case = golden["small_calendar"]
    days = [date.fromisoformat(d) for d in case["trading_days"]]
    policy = WindowPolicy.from_dict(case["policy"])
    window = compute_window(
        case["anchor_date"], days, policy, horizon_days=case["horizon_days"]
    )
    idx = {d: i for i, d in enumerate(days)}
    purge = window.purge_days
    assert idx[window.valid_start] - idx[window.train_end] == purge + 1
    assert idx[window.test_start] - idx[window.valid_end] == purge + 1
    # test 段长度 == test_days（含端点）
    assert idx[window.test_end] - idx[window.test_start] + 1 == policy.test_days


@pytest.mark.unit
def test_expanding_mode_pins_train_start_to_calendar_head(golden):
    case = golden["small_calendar"]
    days = [date.fromisoformat(d) for d in case["trading_days"]]
    policy = WindowPolicy.from_dict({**case["policy"], "mode": "expanding"})
    window = compute_window(
        case["anchor_date"], days, policy, horizon_days=case["horizon_days"]
    )
    assert window.train_start == date.fromisoformat(case["trading_days"][0])


@pytest.mark.unit
def test_anchor_not_on_calendar_floors_to_previous_trading_day(golden):
    """anchor 非交易日（如周末）→ 向下取整到最近交易日，不抛。"""
    case = golden["small_calendar"]
    days = [date.fromisoformat(d) for d in case["trading_days"]]
    policy = WindowPolicy.from_dict(case["policy"])
    # 小日历 30 天结尾为 2026-04-10（周五），04-11/12 是周末
    window = compute_window(
        date(2026, 4, 12), days, policy, horizon_days=case["horizon_days"]
    )
    assert window.anchor_date == date.fromisoformat(case["expected_plan"]["anchor_date"])


@pytest.mark.unit
def test_insufficient_calendar_raises_with_budget_hint(golden):
    case = golden["small_calendar"]
    days = [date.fromisoformat(d) for d in case["trading_days"]]
    policy = WindowPolicy.from_dict(
        {**case["policy"], "train_days": 100, "valid_days": 50, "test_days": 30}
    )
    with pytest.raises(WindowCalculationError) as ei:
        compute_window(
            case["anchor_date"], days, policy, horizon_days=case["horizon_days"]
        )
    assert "交易日" in str(ei.value)


@pytest.mark.unit
def test_empty_calendar_raises():
    with pytest.raises(WindowCalculationError):
        compute_window(
            date(2026, 4, 10), [], WindowPolicy(), horizon_days=5
        )


@pytest.mark.unit
def test_resolve_anchor_walks_back_label_span():
    """anchor = 最后一批【标签完整】的交易日（分区末尾回退 horizon+lag）。

    标签在 T 日收盘生成、T+1 执行、持有 horizon 日 → T 的标签需要
    T+horizon+lag 的收盘价；分区最后 (horizon+lag) 天的标签不完整，
    anchor 必须回退到标签完整的那一天（设计 §4.1 数据滞后守卫）。
    """
    days = [date(2026, 4, d) for d in (1, 2, 3, 7, 8, 9, 10, 13, 14, 15)]
    assert resolve_anchor(days, horizon_days=5, execution_lag_days=1) == date(2026, 4, 7)
    assert resolve_anchor(days, horizon_days=2, execution_lag_days=1) == date(2026, 4, 10)
    # 数据不够一个标签跨度 → 无法定锚
    assert resolve_anchor(days[:3], horizon_days=5, execution_lag_days=1) is None
    assert resolve_anchor([], horizon_days=1) is None


@pytest.mark.unit
def test_resolve_anchor_accepts_iso_strings():
    days = ["2026-04-01", "2026-04-02", "2026-04-03", "2026-04-07"]
    assert resolve_anchor(days, horizon_days=2, execution_lag_days=1) == date(2026, 4, 1)


@pytest.mark.unit
def test_month_index_is_absolute_and_monotonic():
    assert month_index(date(2026, 1, 5)) == 312
    assert month_index(date(2026, 10, 8)) == 321
    assert month_index(date(2026, 11, 2)) - month_index(date(2026, 10, 8)) == 1


@pytest.mark.unit
def test_policy_from_dict_clamps_and_defaults():
    policy = WindowPolicy.from_dict(
        {"train_days": "800", "valid_days": -5, "test_days": 0, "mode": "weird", "purge_days": "3"}
    )
    assert policy.train_days == 800
    assert policy.valid_days == DEFAULT_VALID_DAYS  # 非法值回落默认
    assert policy.test_days == DEFAULT_TEST_DAYS
    assert policy.mode == "sliding"
    assert policy.purge_days == 3
    assert WindowPolicy.from_dict(None) == WindowPolicy()
    assert WindowPolicy().train_days == DEFAULT_TRAIN_DAYS


@pytest.mark.unit
def test_cli_dry_run_matches_golden_fixture(golden, tmp_path):
    """验收 ④：CLI --dry-run 输出的 plan 与 fixture golden 逐字一致。"""
    case = golden["small_calendar"]
    calendar_file = tmp_path / "calendar.json"
    calendar_file.write_text(
        json.dumps({"trading_days": case["trading_days"]}), encoding="utf-8"
    )
    policy = case["policy"]
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", "/app:/app/backend")
    proc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "rolling_train.py"),
            "--dry-run",
            "--market", "CN",
            "--calendar-file", str(calendar_file),
            "--anchor", case["anchor_date"],
            "--horizon-days", str(case["horizon_days"]),
            "--train-days", str(policy["train_days"]),
            "--valid-days", str(policy["valid_days"]),
            "--test-days", str(policy["test_days"]),
            "--mode", policy["mode"],
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["plan"] == case["expected_plan"]
