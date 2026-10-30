"""按账户快照补账的**纯计划解析**（不碰库）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.scripts.repair_agent_ledger_recovered_fills import (  # noqa: E402
    load_plan,
)

PLAN = {
    "tenant_id": "default",
    "user_id": "10000001",
    "agent": "deepseek-v4-flash",
    "quota": 200000,
    "code": "601838.SH",
    "fills": [
        {
            "code": "601838.SH",
            "side": "sell",
            "volume": 200,
            "price": 19.56415,
            "trade_date": "2026-10-29",
            "filled_at": "2026-10-29T10:06:29+08:00",
            "order_id": "116409",
            "internal_order_id": "19e27af7-be60-4fa1-b946-41f4728c4d87",
            "fill_key": "cum-20261029-116409-200",
            "exit_reason": "fill_recovered",
            "note": "snapshot cash delta",
        }
    ],
}


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_plan_parses_dates_and_fills(tmp_path: Path) -> None:
    plan = load_plan(_write(tmp_path, PLAN))

    assert plan.agent == "deepseek-v4-flash"
    assert plan.quota == 200000
    assert len(plan.fills) == 1
    fill = plan.fills[0]
    assert fill.trade_date.isoformat() == "2026-10-29"
    assert fill.filled_at.isoformat().startswith("2026-10-29T02:06:29")


def test_load_plan_rejects_duplicate_fill_key(tmp_path: Path) -> None:
    payload = {**PLAN, "fills": [PLAN["fills"][0], PLAN["fills"][0]]}
    with pytest.raises(ValueError, match="fill_key"):
        load_plan(_write(tmp_path, payload))


def test_load_plan_rejects_bad_uuid_and_date(tmp_path: Path) -> None:
    bad_uuid = {**PLAN, "fills": [{**PLAN["fills"][0], "internal_order_id": "x"}]}
    with pytest.raises(ValueError):
        load_plan(_write(tmp_path, bad_uuid))

    bad_date = {**PLAN, "fills": [{**PLAN["fills"][0], "trade_date": "2026-13-99"}]}
    with pytest.raises(ValueError):
        load_plan(_write(tmp_path, bad_date))
