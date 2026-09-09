"""账户快照新鲜度告警单测（``qmt_account_sync_task``）。

覆盖：成功/失败/空账户记账、告警只在交易时段触发、阈值与冷却节流、
告警自身失败不冒泡。全部无真机依赖（notify 与时段判定均注入假体）。
"""

from __future__ import annotations

import time
from unittest.mock import patch

from backend.services.live_trading.services import qmt_account_sync_task as t


def _health(
    *, age_seconds: float = 0.0, failures: int = 0, error: str = ""
) -> t._SyncHealth:
    health = t._SyncHealth()
    if age_seconds:
        health.last_write_at = time.monotonic() - age_seconds
        health.started_at = time.monotonic() - age_seconds
    health.consecutive_failures = failures
    health.last_error = error
    return health


class TestSyncHealth:
    def test_written_snapshot_refreshes_and_clears_failures(self) -> None:
        health = _health(failures=3, error="boom")
        health.record({"success": True, "account_id": "qmt-1"})
        assert health.consecutive_failures == 0
        assert health.last_error == ""
        assert health.last_write_at is not None
        assert health.stale_seconds < 1

    def test_skipped_empty_account_keeps_write_anchor(self) -> None:
        # 通道可用但账户为空：不算失败，但也不算一次成功落库
        health = _health(failures=2)
        health.record({"success": True, "skipped": True, "reason": "empty_account"})
        assert health.consecutive_failures == 0
        assert health.last_write_at is None

    def test_failure_counts_and_keeps_last_error(self) -> None:
        health = t._SyncHealth()
        health.record({"success": False, "error": "NOT_CONNECTED"})
        health.record({"success": False, "code": "TIMEOUT"})
        assert health.consecutive_failures == 2
        assert health.last_error == "TIMEOUT"


class TestStaleAlert:
    def _run(self, health: t._SyncHealth, *, trading: bool = True) -> list[dict]:
        calls: list[dict] = []

        def fake_notify(**kwargs: object) -> None:
            calls.append(dict(kwargs))

        with (
            patch.object(t, "is_trading_time", return_value=trading),
            patch(
                "backend.services.live_trading.services.real_mirror_service.notify",
                side_effect=fake_notify,
            ),
        ):
            t._maybe_alert_stale(
                health,
                threshold_seconds=300,
                cooldown_seconds=3600,
                account_id="qmt-1",
            )
        return calls

    def test_alerts_when_stale_during_trading_hours(self) -> None:
        calls = self._run(_health(age_seconds=600, failures=2, error="NOT_CONNECTED"))
        assert len(calls) == 1
        assert calls[0]["level"] == "error"
        assert "连续 2 次拉取失败" in calls[0]["content"]
        assert "NOT_CONNECTED" in calls[0]["content"]

    def test_quiet_outside_trading_hours(self) -> None:
        assert self._run(_health(age_seconds=600), trading=False) == []

    def test_quiet_before_threshold(self) -> None:
        assert self._run(_health(age_seconds=100)) == []

    def test_cooldown_blocks_repeat_alert(self) -> None:
        health = _health(age_seconds=600)
        assert len(self._run(health)) == 1
        assert self._run(health) == []

    def test_empty_account_detail_when_no_failures(self) -> None:
        calls = self._run(_health(age_seconds=600))
        assert "账户为空" in calls[0]["content"]

    def test_notify_failure_does_not_raise(self) -> None:
        with (
            patch.object(t, "is_trading_time", return_value=True),
            patch(
                "backend.services.live_trading.services.real_mirror_service.notify",
                side_effect=RuntimeError("boom"),
            ),
        ):
            t._maybe_alert_stale(
                _health(age_seconds=600),
                threshold_seconds=300,
                cooldown_seconds=3600,
                account_id="qmt-1",
            )
