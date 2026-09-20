"""账户长序列载荷（`scripts/eval/account_series.py`，设计 §1.6）。

账户是三类里**最薄**的一条序列：实测模拟盘净值快照每个账户只有 5~10 天
（`10000001:CN` 5 天、`999:ALL` 10 天）。所以这个模块的重点不是画图，而是
**如实标注样本量**——5 个点连成的线不是趋势，页面上必须说出来。

三条纪律：缺测不补 0（`today_pnl` 为空就跳过并计数）；非 CN 市场不误用 CN 序列；
样本不足写进 notes 且带上起止日期。
"""

from __future__ import annotations

import pytest

from backend.scripts.eval.account_series import (
    MIN_TREND_POINTS,
    account_series_payload,
)


def _row(day: str, total: float | None, pnl: float | None = None) -> dict:
    return {"date": day, "total_asset": total, "today_pnl": pnl}


# ── 净值与盈亏点列 ───────────────────────────────────────────────────


@pytest.mark.unit
def test_account_series_payload_emits_equity_points_in_order():
    """净值点列按日期升序，值原样带出（不重算、不复权）。"""
    # Arrange
    rows = [
        _row("2026-09-16", 1000000.0, 0.0),
        _row("2026-09-17", 1001200.0, 1200.0),
        _row("2026-09-18", 999800.0, -1400.0),
    ]

    # Act
    payload = account_series_payload(rows)

    # Assert
    assert payload["series"]["equity"] == [
        {"date": "2026-09-16", "value": 1000000.0},
        {"date": "2026-09-17", "value": 1001200.0},
        {"date": "2026-09-18", "value": 999800.0},
    ]
    assert payload["scalars"]["n_days"] == 3


@pytest.mark.unit
def test_account_series_payload_skips_null_pnl_instead_of_zero_filling():
    """当日盈亏为空的日子跳过并计数 —— 补 0 等于宣称「那天不赚不亏」。"""
    # Arrange
    rows = [
        _row("2026-09-16", 1000000.0, None),
        _row("2026-09-17", 1001200.0, 1200.0),
        _row("2026-09-18", 999800.0, None),
    ]

    # Act
    payload = account_series_payload(rows)

    # Assert
    assert payload["series"]["daily_pnl"] == [{"date": "2026-09-17", "value": 1200.0}]
    assert "2" in payload["notes"]["daily_pnl"]
    assert "0" in payload["notes"]["daily_pnl"]


@pytest.mark.unit
def test_account_series_payload_all_pnl_null_says_not_recorded():
    """全空的当日盈亏 → 空序列 + 「快照没记这一列」，不是一排 0 柱。"""
    # Arrange
    rows = [_row("2026-09-16", 1000000.0), _row("2026-09-17", 1001200.0)]

    # Act
    payload = account_series_payload(rows)

    # Assert
    assert payload["series"]["daily_pnl"] == []
    assert payload["notes"]["daily_pnl"]


@pytest.mark.unit
def test_account_series_payload_drops_null_total_asset_with_count():
    """净值为空的行丢掉并计数（缺净值的那天不进曲线，也不补前值）。"""
    # Arrange
    rows = [
        _row("2026-09-16", 1000000.0, 0.0),
        _row("2026-09-17", None, 100.0),
        _row("2026-09-18", 999800.0, -1400.0),
    ]

    # Act
    payload = account_series_payload(rows)

    # Assert
    assert [p["date"] for p in payload["series"]["equity"]] == [
        "2026-09-16",
        "2026-09-18",
    ]
    assert payload["scalars"]["n_days"] == 3
    assert "1" in payload["notes"]["equity"]


# ── 样本量如实标注 ───────────────────────────────────────────────────


@pytest.mark.unit
def test_account_series_payload_flags_short_sample_with_window():
    """样本不足 20 天 → notes 写明「只有 N 个交易日」并带起止日期。"""
    # Arrange
    rows = [_row(f"2026-09-{d:02d}", 1000000.0 + d) for d in range(11, 16)]

    # Act
    payload = account_series_payload(rows)

    # Assert
    note = payload["notes"]["equity"]
    assert "5" in note
    assert "2026-09-11" in note and "2026-09-15" in note
    assert payload["scalars"]["sample_days"] == 5
    assert payload["scalars"]["sample_sufficient"] is False


@pytest.mark.unit
def test_account_series_payload_long_sample_has_no_short_sample_warning():
    """样本够长（≥20 天）不再贴「样本少」的提示，避免狼来了。"""
    # Arrange
    rows = [_row(f"2026-08-{(d % 28) + 1:02d}", 1000000.0 + d) for d in range(25)]

    # Act
    payload = account_series_payload(rows)

    # Assert
    assert payload["notes"]["equity"] == ""
    assert payload["scalars"]["sample_sufficient"] is True
    assert payload["scalars"]["sample_days"] == MIN_TREND_POINTS + 5


# ── 空 / 非 CN ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_account_series_payload_empty_rows_explains_absence():
    """一行快照都没有 → 空序列 + 原因（不是「净值是一条平线」）。"""
    # Act
    payload = account_series_payload([])

    # Assert
    assert payload["series"]["equity"] == []
    assert payload["notes"]["equity"]
    assert payload["scalars"]["n_days"] == 0


@pytest.mark.unit
def test_account_series_payload_refuses_cn_series_for_other_market():
    """非 CN 且一行都没有：如实说「该市场没有快照行」，绝不借 CN 的行顶上。"""
    # Act
    payload = account_series_payload([], market="US")

    # Assert
    assert payload["series"]["equity"] == []
    assert "US" in payload["notes"]["equity"]
    assert "不误用 CN 序列" in payload["notes"]["equity"]


@pytest.mark.unit
def test_account_series_payload_plots_other_market_rows_when_present():
    """FUTURES/ALL 市场现在各自有快照行（表带 market 列）——有行就照画。"""
    # Arrange
    rows = [_row("2026-09-16", 1000000.0, 0.0), _row("2026-09-17", 1000000.0, 0.0)]

    # Act
    payload = account_series_payload(rows, market="FUTURES")

    # Assert
    assert len(payload["series"]["equity"]) == 2
    assert payload["notes"]["equity"]  # 仍提示样本只有 2 天
    assert "不误用 CN 序列" not in payload["notes"]["equity"]


@pytest.mark.unit
def test_account_series_payload_says_when_pnl_is_all_zero():
    """当日盈亏全 0 是实测值（窗口内无盈亏）不是缺测：照画，但说一句。"""
    # Arrange
    rows = [_row("2026-09-16", 1000000.0, 0.0), _row("2026-09-17", 1000000.0, 0.0)]

    # Act
    payload = account_series_payload(rows)

    # Assert
    assert len(payload["series"]["daily_pnl"]) == 2
    assert "全为 0" in payload["notes"]["daily_pnl"]
    assert payload["scalars"]["pnl_all_zero"] is True


@pytest.mark.unit
def test_account_series_payload_no_zero_note_when_pnl_has_real_values():
    """有真盈亏时不贴「全为 0」的提示（避免狼来了）。"""
    # Arrange
    rows = [_row("2026-09-16", 1000000.0, 0.0), _row("2026-09-17", 1001200.0, 1200.0)]

    # Act
    payload = account_series_payload(rows)

    # Assert
    assert payload["notes"]["daily_pnl"] == ""
    assert payload["scalars"]["pnl_all_zero"] is False


@pytest.mark.unit
def test_account_series_payload_carries_window_return_as_scalar():
    """窗口收益 = 末值/首值 − 1（首值 ≤ 0 时不给数，避免除零编出天文数字）。"""
    # Arrange
    rows = [_row("2026-09-16", 1000000.0, 0.0), _row("2026-09-17", 1010000.0, 0.0)]

    # Act
    payload = account_series_payload(rows)

    # Assert
    assert payload["scalars"]["window_return"] == pytest.approx(0.01, abs=1e-9)
    assert payload["scalars"]["latest_total_asset"] == 1010000.0


@pytest.mark.unit
def test_account_series_payload_zero_first_value_gives_no_return():
    """首值为 0（账户刚建）→ window_return 为 None，不是 inf。"""
    # Arrange
    rows = [_row("2026-09-16", 0.0, 0.0), _row("2026-09-17", 1000.0, 0.0)]

    # Act
    payload = account_series_payload(rows)

    # Assert
    assert payload["scalars"]["window_return"] is None
