# -*- coding: utf-8 -*-
"""minibt 模拟盘桥：当日 K 线合成单测（pandas 纯函数）。"""

from __future__ import annotations

import time
from datetime import date, timedelta

import pandas as pd
import pytest

from backend.shared.minibt_sim_data import (
    LOTS_TO_SHARES,
    LiveBarUnavailable,
    append_live_bar,
    build_live_bar,
    snapshot_age_seconds,
)

TODAY = date(2026, 10, 26)


def _snapshot(**overrides):
    now = time.time()
    data = {
        "Now": "10.50",
        "PreClose": "10.10",
        "Open": "10.20",
        "High": "10.60",
        "Low": "10.05",
        "Volume": "1200",  # 手
        "Amount": "1234567.89",  # 元
        "timestamp": str(now - 5),
        "source": "tdx_bridge",
    }
    data.update(overrides)
    return data


def _history(days: int = 3) -> pd.DataFrame:
    rows = []
    for offset in range(days, 0, -1):
        rows.append(
            {
                "datetime": pd.Timestamp(TODAY - timedelta(days=offset)),
                "open": 10.0,
                "high": 10.2,
                "low": 9.9,
                "close": 10.1,
                "volume": 100000.0,
                "amount": 1010000.0,
            }
        )
    frame = pd.DataFrame(rows)
    frame.attrs["symbol"] = "600036.SH"
    return frame


def test_快照合成当日K线_成交量手转股() -> None:
    bar = build_live_bar(_snapshot(), symbol="600036.SH", trade_date=TODAY)
    assert bar["datetime"] == pd.Timestamp(TODAY)
    assert bar["open"] == 10.20
    assert bar["close"] == 10.50
    assert bar["high"] == 10.60
    assert bar["low"] == 10.05
    assert bar["volume"] == 1200 * LOTS_TO_SHARES
    assert bar["amount"] == 1234567.89


def test_快照缺开高低时按现价与前收补齐() -> None:
    snapshot = _snapshot()
    snapshot.pop("Open")
    snapshot.pop("High")
    snapshot.pop("Low")
    bar = build_live_bar(snapshot, symbol="600036.SH", trade_date=TODAY)
    assert bar["open"] == 10.10  # 前收
    assert bar["high"] >= max(bar["open"], bar["close"])
    assert bar["low"] <= min(bar["open"], bar["close"])


def test_快照超龄_缺时间戳_现价非法一律拒绝() -> None:
    stale = _snapshot(timestamp=str(time.time() - 999))
    with pytest.raises(LiveBarUnavailable, match="snapshot_stale"):
        build_live_bar(stale, symbol="600036.SH", trade_date=TODAY)

    no_ts = _snapshot()
    no_ts.pop("timestamp")
    with pytest.raises(LiveBarUnavailable, match="timestamp_missing"):
        build_live_bar(no_ts, symbol="600036.SH", trade_date=TODAY)

    bad_price = _snapshot(Now="0")
    with pytest.raises(LiveBarUnavailable, match="price_invalid"):
        build_live_bar(bad_price, symbol="600036.SH", trade_date=TODAY)


def test_快照年龄无时间戳返回None() -> None:
    assert snapshot_age_seconds({"Now": "10"}) is None
    assert snapshot_age_seconds({"timestamp": str(time.time() - 5)}) is not None


def test_历史未含今日则追加一根() -> None:
    history = _history(3)
    merged, source = append_live_bar(
        history, snapshot=_snapshot(), symbol="600036", trade_date=TODAY
    )
    assert source == "live_bar_appended"
    assert len(merged) == 4
    assert merged["datetime"].iloc[-1] == pd.Timestamp(TODAY)
    assert merged["close"].iloc[-1] == 10.50
    assert merged.attrs["symbol"] == "600036.SH"
    # 原 DataFrame 不被修改（pandas 3 CoW）
    assert len(history) == 3


def test_历史已含今日则不追加() -> None:
    history = _history(0)  # 空
    with pytest.raises(LiveBarUnavailable, match="history_empty"):
        append_live_bar(
            history, snapshot=_snapshot(), symbol="600036", trade_date=TODAY
        )

    today_frame = _history(1)
    today_frame.loc[0, "datetime"] = pd.Timestamp(TODAY)
    merged, source = append_live_bar(
        today_frame, snapshot=_snapshot(), symbol="600036", trade_date=TODAY
    )
    assert source == "history_has_today"
    assert len(merged) == 1


def test_缺快照时拒绝合成() -> None:
    with pytest.raises(LiveBarUnavailable, match="snapshot_missing"):
        append_live_bar(_history(2), snapshot=None, symbol="600036", trade_date=TODAY)
