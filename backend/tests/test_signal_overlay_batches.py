"""Signal overlay must distinguish generic script versions from real models."""

from contextlib import asynccontextmanager
from datetime import date

import pytest

from backend.services.api.routers import stock_terminal as router


@pytest.mark.asyncio
async def test_overlay_keeps_model_and_batch_identity(monkeypatch):
    rows = [
        (
            date(2026, 9, 28),
            -0.1194,
            "HOLD",
            "inference_script",
            "old_1",
            None,
            None,
            None,
        ),
        (
            date(2026, 9, 28),
            0.0002,
            "HOLD",
            "inference_script",
            "old_2",
            None,
            None,
            None,
        ),
        (
            date(2026, 10, 8),
            -0.1071,
            "HOLD",
            "inference_script",
            "new_1",
            "model_a",
            date(2026, 9, 30),
            date(2026, 10, 8),
        ),
        (
            date(2026, 10, 8),
            -0.0991,
            "HOLD",
            "inference_script",
            "new_2",
            "model_b",
            date(2026, 9, 30),
            date(2026, 10, 8),
        ),
    ]

    class Result:
        def fetchall(self):
            return rows

    class Session:
        async def execute(self, statement, params):
            sql = str(statement)
            assert "r.tenant_id = s.tenant_id" in sql
            assert "r.user_id = s.user_id" in sql
            assert "s.created_at, s.id" in sql
            return Result()

    @asynccontextmanager
    async def session():
        yield Session()

    monkeypatch.setattr(router, "get_session", session)
    result = await router.stock_signal_overlay(
        "002614.SZ", 180, {"tenant_id": "default"}
    )
    series = result["data"]["series"]
    assert set(series) == {"old_1", "old_2", "model_a", "model_b"}
    assert series["old_2"][0]["fusion"] == 0.0002
    assert series["old_1"][0]["model_id"] is None
    assert series["model_a"][0]["data_trade_date"] == "2026-09-30"
    assert series["model_a"][0]["date"] == "2026-10-08"
