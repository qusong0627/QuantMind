"""新闻标签契约的纯函数测试（重点：Huntly 发布时间的时区换算）。

`parse_huntly_time` 是这条链上**唯一一处错了会静默偏移整体窗口**的地方：
`connected_at` 是上海墙钟字符串，若当成 UTC 用，窗口会整体错 8 小时；
实测口径判别法——按错口径会有 20.06% 的文章「发布前 8 小时就被 enrich」
（136,705 行聚在 −8h），物理上不可能。

跨日边界是这条最容易漏的：上海 `2026-09-20 03:00` 换算后是 **UTC 09-19**，
按 UTC 日期分桶会把它算成前一天。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from backend.shared.news_tag_contract import parse_huntly_time, window_start


def test_converts_shanghai_wallclock_to_utc() -> None:
    """上海墙钟 − 8h = UTC。"""
    # Act
    got = parse_huntly_time("2026-09-20 15:44:05.000")

    # Assert
    assert got == datetime(2026, 9, 20, 7, 44, 5, tzinfo=timezone.utc)


def test_crosses_utc_day_boundary_correctly() -> None:
    """上海凌晨属于**前一个 UTC 日**——按 UTC 日分桶时最容易错的一格。"""
    # Act
    got = parse_huntly_time("2026-09-20 03:00:00.000")

    # Assert
    assert got == datetime(2026, 9, 19, 19, 0, 0, tzinfo=timezone.utc)
    assert got.date() == date(2026, 9, 19)


def test_result_is_timezone_aware() -> None:
    """必须 aware——本仓瞬时列口径禁止 naive datetime 混用。"""
    # Act
    got = parse_huntly_time("2026-09-20 15:44:05.000")

    # Assert
    assert got is not None
    assert got.tzinfo is not None
    assert got.utcoffset() is not None


def test_accepts_format_without_milliseconds() -> None:
    """秒精度（无毫秒）的形态也要吃下。"""
    # Act / Assert
    assert parse_huntly_time("2026-09-20 15:44:05") == datetime(
        2026, 9, 20, 7, 44, 5, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("0001-12-30 08:00:00.000", "实测哨兵值（min，1 行）"),
        ("1000-01-01 00:00:00.000", "年份早于下限"),
        ("1989-12-31 23:59:59.000", "恰在阈值之前"),
        ("", "空串"),
        (None, "None"),
        ("not a date", "不可解析"),
        ("2026-09-20T15:44:05", "ISO 带 T（不是 Huntly 的形态）"),
        ("1758328005", "epoch 数字串（曾据此写错过）"),
    ],
)
def test_rejects_sentinels_and_garbage(raw: object, why: str) -> None:
    """哨兵/垃圾值返回 None，调用方必须丢弃该行——**不得**退回 enrich 时间兜底。"""
    # Act / Assert
    assert parse_huntly_time(raw) is None, why


def test_threshold_year_is_inclusive() -> None:
    """阈值当年本身是合法的（边界取闭区间，避免把真数据误杀）。"""
    # Act
    got = parse_huntly_time("1990-01-01 00:00:00.000")

    # Assert
    assert got is not None
    assert got.year == 1989  # 上海 1990-01-01 00:00 → UTC 1989-12-31 16:00


# ---------------------------------------------------------------- 窗口


def test_window_start_subtracts_days() -> None:
    """窗口起点 = now - days（aware UTC）。"""
    # Arrange
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

    # Act
    got = window_start(20, now=now)

    # Assert
    assert got == datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def test_window_start_defaults_to_now_utc() -> None:
    """不给 now 时取当前 UTC 时刻，且为 aware。"""
    # Act
    got = window_start(20)

    # Assert
    assert got.tzinfo is not None
    assert (datetime.now(tz=timezone.utc) - got).days in (19, 20)
