"""策略长序列载荷（`scripts/eval/strategy_series.py`，设计 §1.6）。

盯三件事：

1. **回撤曲线有两种键名**：实测 24 份在盘结果里，15 份写 ``{date, drawdown}``、
   9 份写 ``{date, drawdown, value}``。只认 ``value`` 会把前 15 份读成一条 0 线
   （正是「没证据」被当成「证据」的经典静默失败）；
2. **缺测不是 0**：值为 None / 非数 / 非有限数的点丢弃并记数，绝不补 0；
3. **截断要留痕**：超长只留最近的点，截断本身写进 notes。
"""

from __future__ import annotations

import math

import pytest

from backend.scripts.eval.strategy_series import (
    MAX_SERIES_POINTS,
    parse_drawdown_entries,
    strategy_series_payload,
)


# ── 回撤键名兼容 ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_parse_drawdown_entries_reads_drawdown_key():
    """老结果只写 `drawdown` 键 —— 这是实测里的多数派，读不到就等于回撤全 0。"""
    # Arrange
    raw = [
        {"date": "2026-01-02", "drawdown": 0.0},
        {"date": "2026-01-05", "drawdown": -0.0156},
    ]

    # Act
    points, dropped = parse_drawdown_entries(raw)

    # Assert
    assert points == [("2026-01-02", 0.0), ("2026-01-05", -0.0156)]
    assert dropped == 0


@pytest.mark.unit
def test_parse_drawdown_entries_reads_value_key():
    """新结果同时写 `drawdown` 与 `value`，两者一致时取 `drawdown`。"""
    # Arrange
    raw = [{"date": "2026-01-02", "drawdown": -0.02, "value": -0.02}]

    # Act
    points, dropped = parse_drawdown_entries(raw)

    # Assert
    assert points == [("2026-01-02", -0.02)]
    assert dropped == 0


@pytest.mark.unit
def test_parse_drawdown_entries_accepts_bare_numbers():
    """纯数字列表（更老的写法）按下标对齐，日期留空由调用方补。"""
    # Arrange
    raw = [0.0, -0.01, -0.02]

    # Act
    points, dropped = parse_drawdown_entries(raw)

    # Assert
    assert points == [("", 0.0), ("", -0.01), ("", -0.02)]
    assert dropped == 0


@pytest.mark.unit
def test_parse_drawdown_entries_drops_missing_values_without_zero_fill():
    """值为 None / 非数 / NaN / inf 的点丢弃并计数 —— 补 0 等于宣称「当天没回撤」。"""
    # Arrange
    raw = [
        {"date": "2026-01-02", "drawdown": None},
        {"date": "2026-01-05", "drawdown": "abc"},
        {"date": "2026-01-06", "drawdown": float("nan")},
        {"date": "2026-01-07", "drawdown": float("inf")},
        {"date": "2026-01-08", "drawdown": -0.03},
    ]

    # Act
    points, dropped = parse_drawdown_entries(raw)

    # Assert
    assert points == [("2026-01-08", -0.03)]
    assert dropped == 4


@pytest.mark.unit
def test_parse_drawdown_entries_handles_empty_and_none():
    """空 / None 输入返回空序列，不抛异常也不编点。"""
    assert parse_drawdown_entries(None) == ([], 0)
    assert parse_drawdown_entries([]) == ([], 0)


# ── 载荷组装 ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_strategy_series_payload_emits_equity_and_drawdown_points():
    """净值与回撤按日期对齐成点列（回撤只保留净值曲线覆盖的那些日期）。"""
    # Arrange
    dates = ["2026-01-02", "2026-01-05", "2026-01-06"]
    equity = [1000000.0, 1010000.0, 990000.0]
    drawdown = [("2026-01-02", 0.0), ("2026-01-06", -0.0198), ("2026-02-01", -0.5)]

    # Act
    payload = strategy_series_payload(
        dates=dates, equity=equity, drawdown_entries=drawdown
    )

    # Assert
    assert payload["series"]["equity"] == [
        {"date": "2026-01-02", "value": 1000000.0},
        {"date": "2026-01-05", "value": 1010000.0},
        {"date": "2026-01-06", "value": 990000.0},
    ]
    # 2026-02-01 不在净值曲线上 → 不进序列，否则两条曲线 x 轴不可比
    assert payload["series"]["drawdown"] == [
        {"date": "2026-01-02", "value": 0.0},
        {"date": "2026-01-06", "value": -0.0198},
    ]


@pytest.mark.unit
def test_strategy_series_payload_notes_missing_drawdown_instead_of_flat_line():
    """没有回撤曲线 → 空序列 + notes 说明；绝不下发一条 0 线。"""
    # Act
    payload = strategy_series_payload(
        dates=["2026-01-02", "2026-01-05"],
        equity=[1000000.0, 1010000.0],
        drawdown_entries=None,
    )

    # Assert
    assert payload["series"]["drawdown"] == []
    assert "回撤" in payload["notes"]["drawdown"]
    assert payload["scalars"]["max_drawdown"] is None


@pytest.mark.unit
def test_strategy_series_payload_flags_unaligned_drawdown_mismatch():
    """回撤与净值点数不一致时如实写进 notes（不假装对齐）。"""
    # Arrange
    dates = ["2026-01-02", "2026-01-05", "2026-01-06"]
    equity = [1000000.0, 1010000.0, 990000.0]
    # 回撤只有前两点有日期、第三点无日期 → 无法按日期对齐
    drawdown = [("2026-01-02", 0.0), ("2026-01-05", -0.01), ("", -0.02)]

    # Act
    payload = strategy_series_payload(
        dates=dates, equity=equity, drawdown_entries=drawdown
    )

    # Assert
    assert len(payload["series"]["drawdown"]) == 2
    assert "日期" in payload["notes"]["drawdown"]


@pytest.mark.unit
def test_strategy_series_payload_reports_dropped_points():
    """值缺失被剔除的点数写进 notes —— 「缺了 3 天」和「这 3 天没回撤」不是一回事。"""
    # Act
    payload = strategy_series_payload(
        dates=["2026-01-02"],
        equity=[1000000.0],
        drawdown_entries=[("2026-01-02", None), ("2026-01-05", None)],
    )

    # Assert
    assert "2" in payload["notes"]["drawdown"]
    assert "0" in payload["notes"]["drawdown"]


@pytest.mark.unit
def test_strategy_series_payload_truncates_long_series_and_says_so():
    """超长序列只留最近的点，截断本身必须写进 notes。"""
    # Arrange
    n = MAX_SERIES_POINTS + 50
    dates = [f"2026-01-{i % 28 + 1:02d}" for i in range(n)]
    equity = [1000000.0 + i for i in range(n)]

    # Act
    payload = strategy_series_payload(
        dates=dates, equity=equity, drawdown_entries=None
    )

    # Assert
    assert len(payload["series"]["equity"]) == MAX_SERIES_POINTS
    assert payload["scalars"]["n_days"] == n
    assert str(MAX_SERIES_POINTS) in payload["notes"]["equity"]


@pytest.mark.unit
def test_strategy_series_payload_carries_card_scalars_verbatim():
    """卡上算出来的标量原样带出（图上数与卡上分同源，前端不重算）。"""
    # Arrange
    card_scalars = {"annual_return": 0.1234, "max_drawdown": -0.0876}

    # Act
    payload = strategy_series_payload(
        dates=["2026-01-02", "2026-01-05"],
        equity=[1000000.0, 1010000.0],
        drawdown_entries=[("2026-01-02", 0.0), ("2026-01-05", -0.0876)],
        scalars=card_scalars,
    )

    # Assert
    assert payload["scalars"]["annual_return"] == 0.1234
    assert payload["scalars"]["max_drawdown"] == -0.0876
    assert payload["scalars"]["window_return"] == pytest.approx(0.01, abs=1e-9)


@pytest.mark.unit
def test_strategy_series_payload_empty_curve_gives_notes_not_silence():
    """净值曲线为空（历史结果被清理）→ 空序列 + 原因，不留空白。"""
    # Act
    payload = strategy_series_payload(dates=[], equity=[], drawdown_entries=None)

    # Assert
    assert payload["series"]["equity"] == []
    assert payload["notes"]["equity"]
    assert payload["scalars"]["n_days"] == 0


@pytest.mark.unit
def test_strategy_series_payload_drops_non_finite_equity_values():
    """净值里的 NaN/inf 不进点列（画出来是一条断线或 1e308 的尖刺）。"""
    # Arrange
    dates = ["2026-01-02", "2026-01-05", "2026-01-06"]
    equity = [1000000.0, float("nan"), 990000.0]

    # Act
    payload = strategy_series_payload(
        dates=dates, equity=equity, drawdown_entries=None
    )

    # Assert
    assert [p["date"] for p in payload["series"]["equity"]] == [
        "2026-01-02",
        "2026-01-06",
    ]
    assert math.isfinite(payload["scalars"]["window_return"])


@pytest.mark.unit
def test_strategy_series_payload_monthly_returns_are_bars_in_order():
    """月度收益按时间升序成柱（口径来自策略卡同一函数）。"""
    # Arrange
    monthly = [("2025-09", -0.01), ("2025-10", 0.03)]

    # Act
    payload = strategy_series_payload(
        dates=["2025-09-01", "2025-10-31"],
        equity=[1000000.0, 1020000.0],
        drawdown_entries=None,
        monthly=monthly,
    )

    # Assert
    assert payload["series"]["monthly_return"] == [
        {"label": "2025-09", "value": -0.01},
        {"label": "2025-10", "value": 0.03},
    ]
    assert payload["notes"]["monthly_return"] == ""


@pytest.mark.unit
def test_strategy_series_payload_notes_absent_monthly():
    """月度收益算不出来 → 空序列 + 原因。"""
    # Act
    payload = strategy_series_payload(
        dates=["2025-09-01"], equity=[1000000.0], drawdown_entries=None
    )

    # Assert
    assert payload["series"]["monthly_return"] == []
    assert payload["notes"]["monthly_return"]
