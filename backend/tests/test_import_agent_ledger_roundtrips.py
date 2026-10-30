"""历史回合台账迁移的**纯规划**（不碰库）。"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.scripts.import_agent_ledger_roundtrips import (  # noqa: E402
    ImportPlan,
    parse_row,
    plan_import,
    row_key,
)

ROW = {
    "market": "A",
    "agent": "deepseek-v4-flash",
    "code": "603213.SH",
    "volume": 400,
    "cost_price": 13.6,
    "sell_price": 13.625,
    "buy_ts": "2026-09-11T09:37:26.903941+08:00",
    "sell_ts": "2026-09-14T10:23:02.478733+08:00",
    "holding_days": 3.032,
    "realized_pnl": 10.0,
    "pnl_pct": 0.184,
    "closed": False,
    "exit_reason": None,
}


def test_parse_row_normalizes_market_and_timestamps() -> None:
    row = parse_row(ROW)

    assert row["market"] == "CN"  # 文件 "A" → 账本契约 "CN"
    assert row["agent"] == "deepseek-v4-flash"
    assert row["volume"] == 400.0
    assert row["buy_ts"].tzinfo is not None
    assert row["buy_ts"].astimezone(timezone.utc) == datetime(
        2026, 9, 11, 1, 37, 26, 903941, tzinfo=timezone.utc
    )
    assert row["closed"] is False
    assert row["exit_reason"] == ""
    assert row["pnl_pct"] == 0.184


def test_plan_skips_duplicates_in_db_and_in_file() -> None:
    existing = {row_key(parse_row(ROW))}
    second = dict(ROW, sell_price=13.7, sell_ts="2026-09-15T10:00:00+08:00")

    plan = plan_import([ROW, second, second], existing)

    assert isinstance(plan, ImportPlan)
    assert len(plan.rows) == 1  # 第一条已在库；第三条与第二条文件内重复
    assert plan.rows[0]["sell_price"] == 13.7
    assert plan.duplicates == 2
    assert plan.invalid == ()


def test_plan_collects_invalid_rows_without_dropping_them_silently() -> None:
    bad_agent = dict(ROW, agent="")
    bad_volume = dict(ROW, volume=0)
    bad_order = dict(ROW, sell_ts="2026-09-01T10:00:00+08:00")

    plan = plan_import([ROW, bad_agent, bad_volume, bad_order, None], set())

    assert len(plan.rows) == 1
    assert [line for line, _ in plan.invalid] == [2, 3, 4, 5]
    assert "agent" in plan.invalid[0][1]
    assert "volume" in plan.invalid[1][1]
    assert "sell_ts" in plan.invalid[2][1]


def test_row_key_includes_volume_and_price() -> None:
    base = parse_row(ROW)
    other_volume = parse_row(dict(ROW, volume=500))
    other_price = parse_row(dict(ROW, sell_price=13.7))

    assert row_key(base) != row_key(other_volume)
    assert row_key(base) != row_key(other_price)


@pytest.mark.parametrize(
    "override, field",
    [
        ({"buy_ts": "not-a-time"}, "buy_ts"),
        ({"cost_price": "x"}, "cost_price"),
        ({"sell_price": -1}, "sell_price"),
        ({"realized_pnl": float("nan")}, "realized_pnl"),
    ],
)
def test_parse_row_rejects_dirty_values(override: dict, field: str) -> None:
    with pytest.raises(ValueError, match=field):
        parse_row({**ROW, **override})
