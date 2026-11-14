"""行情 canonical 去重：同一 (symbol, time) 只保留一个 release。

口径：``paid_hk``（付费历史，原始不复权）优先于 ``akshare``（补充/新数据）；
同一 release 内按 ``published_at`` 最新优先。2024-09~2026-05 的重叠段若让
akshare 胜出，会把 0755.HK 等标的的 1/100 价格、0 价格写进 canonical 序列。
"""

from __future__ import annotations

import pandas as pd

from backend.shared.kline_canonical import RELEASE_PRIORITY, canonicalize


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_paid_wins_over_akshare_for_same_symbol_day():
    # Arrange
    df = _frame(
        [
            {
                "symbol": "0755.HK",
                "time": "2025-03-10",
                "close": 0.011,
                "release_id": "akshare",
                "published_at": "2026-08-24T22:00:00",
            },
            {
                "symbol": "0755.HK",
                "time": "2025-03-10",
                "close": 1.1,
                "release_id": "paid_hk",
                "published_at": "2026-08-09T13:00:00",
            },
        ]
    )

    # Act
    out = canonicalize(df)

    # Assert
    assert len(out) == 1
    assert out.iloc[0]["close"] == 1.1
    assert out.iloc[0]["release_id"] == "paid_hk"


def test_akshare_is_used_when_paid_missing():
    # Arrange
    df = _frame(
        [
            {
                "symbol": "0001.HK",
                "time": "2026-11-13",
                "close": 60.0,
                "release_id": "akshare",
                "published_at": "2026-11-13T20:00:00",
            },
        ]
    )

    # Act
    out = canonicalize(df)

    # Assert
    assert len(out) == 1
    assert out.iloc[0]["release_id"] == "akshare"


def test_same_release_keeps_latest_published_at():
    # Arrange
    df = _frame(
        [
            {
                "symbol": "0005.HK",
                "time": "2026-11-13",
                "close": 60.0,
                "release_id": "akshare",
                "published_at": "2026-11-13T19:00:00",
            },
            {
                "symbol": "0005.HK",
                "time": "2026-11-13",
                "close": 61.0,
                "release_id": "akshare",
                "published_at": "2026-11-13T21:00:00",
            },
        ]
    )

    # Act
    out = canonicalize(df)

    # Assert
    assert len(out) == 1
    assert out.iloc[0]["close"] == 61.0


def test_idempotent_and_preserves_rows_without_duplicates():
    # Arrange
    df = _frame(
        [
            {
                "symbol": "0001.HK",
                "time": "2026-11-12",
                "close": 60.0,
                "release_id": "akshare",
                "published_at": "2026-11-12T20:00:00",
            },
            {
                "symbol": "0001.HK",
                "time": "2026-11-13",
                "close": 61.0,
                "release_id": "akshare",
                "published_at": "2026-11-13T20:00:00",
            },
        ]
    )

    # Act
    once = canonicalize(df)
    twice = canonicalize(once)

    # Assert
    assert once.equals(twice)
    assert len(once) == 2


def test_unknown_release_is_lowest_priority():
    # Arrange
    df = _frame(
        [
            {
                "symbol": "0001.HK",
                "time": "2026-11-13",
                "close": 1.0,
                "release_id": "other",
                "published_at": "2026-11-14T00:00:00",
            },
            {
                "symbol": "0001.HK",
                "time": "2026-11-13",
                "close": 2.0,
                "release_id": "akshare",
                "published_at": "2026-11-13T20:00:00",
            },
        ]
    )

    # Act
    out = canonicalize(df)

    # Assert
    assert len(out) == 1
    assert out.iloc[0]["close"] == 2.0
    assert RELEASE_PRIORITY["paid_hk"] < RELEASE_PRIORITY["akshare"]


def test_missing_published_at_does_not_drop_rows():
    # Arrange
    df = _frame(
        [
            {
                "symbol": "0001.HK",
                "time": "2026-11-13",
                "close": 1.0,
                "release_id": "akshare",
            },
        ]
    )

    # Act
    out = canonicalize(df)

    # Assert
    assert len(out) == 1
    assert out.iloc[0]["close"] == 1.0
