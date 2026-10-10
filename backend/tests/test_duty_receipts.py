"""P2-5 值班回执：键空间单源 + 停滞检查回执（``count_rounds_for_day`` / ``write_stall_check``）。

死手检查（celery 侧）与生产者（trade 侧）住在**不同进程树**，回执键是它们之间
唯一的契约。键字符串一旦两侧各写一份就会漂移——漂移的表现是「回执明明写了、
死手却报缺失」这种最难查的假故障。``backend/shared/duty_receipts.py`` 是键的
唯一构造点；其中三把 P2-5 之前就存在的键（收盘报表 done / 登记）必须与
``daily_pnl_report_task`` 里的字面量**逐字一致**，本文件用交叉断言钉死。
"""

from __future__ import annotations

import inspect
import json
from datetime import date, datetime

from backend.shared import duty_receipts as dr

DAY = date(2026, 10, 10)


class TestKeySpace:
    def test_stall_check_key_iso(self):
        assert dr.stall_check_key(DAY) == "trade:decision-round:stall-check:2026-10-10"

    def test_duty_summary_keys(self):
        assert dr.duty_summary_key(DAY) == "trade:duty-summary:20261010"
        assert dr.duty_summary_done_key(DAY) == "trade:duty-summary:done:20261010"
        assert (
            dr.duty_summary_registered_key(DAY, "sent")
            == "trade:duty-summary:registered:20261010:sent"
        )

    def test_deadman_keys(self):
        assert dr.deadman_done_key(DAY) == "trade:duty-deadman:done:20261010"
        assert dr.deadman_alerted_key(DAY) == "trade:duty-deadman:alerted:20261010"

    def test_pnl_keys_match_producer_literals(self):
        """交叉锚定：与 ``daily_pnl_report_task`` 的 _DONE_KEY/_REGISTERED_KEY 逐字一致。"""
        from backend.services.trade.services import daily_pnl_report_task as pnl

        assert pnl._DONE_KEY.format(date="20261010") == dr.pnl_report_done_key(DAY)
        assert pnl._REGISTERED_KEY.format(
            date="20261010", outcome="unsent"
        ) == dr.pnl_report_registered_key(DAY, "unsent")


class TestCountRoundsForDay:
    def _count(self, entries, day=DAY):
        from backend.services.trade.services.decision_round_alerts import (
            count_rounds_for_day,
        )

        return count_rounds_for_day(entries, day)

    def test_counts_only_matching_day(self):
        entries = [
            json.dumps({"day": "2026-10-10", "slot": "0935"}),
            json.dumps({"day": "2026-10-09", "slot": "1445"}),
            json.dumps({"day": "2026-10-10", "slot": "1000"}),
        ]
        assert self._count(entries) == 2

    def test_tolerates_bytes_and_garbage_and_non_dict(self):
        entries = [
            b'{"day": "2026-10-10"}',
            "not json",
            None,
            123,
            json.dumps(["not", "a", "dict"]),
        ]
        assert self._count(entries) == 1

    def test_empty_is_zero(self):
        assert self._count([]) == 0


class FakeNative:
    def __init__(self, fail: bool = False):
        self.store: dict[str, str] = {}
        self.expires: dict[str, int] = {}
        self.fail = fail

    def set(self, key, value, nx=False, ex=None):
        if self.fail:
            raise RuntimeError("redis 写不下来")
        self.store[key] = value
        if ex is not None:
            self.expires[key] = ex
        return True

    def get(self, key):
        return self.store.get(key)


class TestWriteStallCheck:
    def _write(self, native, **overrides):
        from backend.services.trade.services.decision_round_io import (
            write_stall_check,
        )

        kwargs = {
            "day": DAY,
            "rounds": 3,
            "alerted": False,
            "at": datetime(2026, 10, 10, 15, 30),
            "note": "日历附注",
        }
        kwargs.update(overrides)
        return write_stall_check(native, **kwargs)

    def test_writes_json_with_ttl(self):
        native = FakeNative()
        assert self._write(native) is True
        key = dr.stall_check_key(DAY)
        payload = json.loads(native.store[key])
        assert payload["rounds"] == 3
        assert payload["alerted"] is False
        assert payload["note"] == "日历附注"
        assert payload["at"].startswith("2026-10-10T15:30")
        assert native.expires[key] == dr.STALL_CHECK_TTL_SECONDS

    def test_write_error_returns_false_never_raises(self):
        # 回执是可见性旁路：写不进去只许返回 False（调用方记日志），
        # 绝不许把停滞检查本身掀翻
        native = FakeNative(fail=True)
        assert self._write(native) is False


class TestRunnerWiring:
    """防回潮：停滞检查执行后必须落回执（死手检查的核对项）。"""

    def test_stall_watch_writes_receipt(self):
        from backend.services.trade.services import decision_round_runner as runner

        src = inspect.getsource(runner._stall_watch)
        assert "write_stall_check" in src
        assert "count_rounds_for_day" in src
