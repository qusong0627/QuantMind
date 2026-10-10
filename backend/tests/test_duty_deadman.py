"""P2-5 值班死手检查（``backend/services/engine/tasks/duty_deadman.py``）规格测试。

死手回答的问题：「**该响没响**」——收盘报表、值班摘要、决策轮停滞检查、实时信号
四项回执，到点后哪些不在。它住在 celery（跨进程树）的原因只有一个：trade 整体
死亡时，它还得能响。语义：

- 缺失集**只收缩**：集合不变不重复响（半小时一轮 × 16 轮不刷屏）；集合变化即告警；
  补全后发恢复并当日封账（done 键）。
- 读失败**本身要报**：Redis/PG 故障映射为对应「不可读」项，绝不静默算过。
- 非交易日秒退（周末没有回执是对的，不是缺回执）。

不碰真外部：Redis/PG 走 Fake、通知走 monkeypatch、交易日历 monkeypatch。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import date, datetime, time, timezone

from backend.shared import duty_receipts as dr
from backend.services.engine.tasks import duty_deadman as dd

DAY = date(2026, 10, 10)


def run(coro):
    return asyncio.run(coro)


# ── Fake 面 ─────────────────────────────────────────────────────────


class FakeR:
    """redis 原生客户端最小面（exists/get/set/delete/hgetall/close）。"""

    def __init__(self, keys=(), kv=None, hgetall=None, fail=False):
        self.keys = set(keys)
        self.kv = dict(kv or {})
        self.h = dict(hgetall or {})
        self.fail = fail
        self.exists_calls: list[str] = []
        self.closed = False

    def exists(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        self.exists_calls.append(key)
        return 1 if key in self.keys else 0

    def get(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        return self.kv.get(key)

    def set(self, key, value, ex=None):
        if self.fail:
            raise RuntimeError("redis down")
        self.kv[key] = value
        self.keys.add(key)
        return True

    def delete(self, key):
        self.kv.pop(key, None)
        self.keys.discard(key)
        return 1

    def hgetall(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        return dict(self.h)

    def close(self):
        self.closed = True


class FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeSession:
    def __init__(self, signal_row=(0, None), fail=False):
        self.signal_row = signal_row
        self.fail = fail

    async def execute(self, stmt, params=None):
        if self.fail:
            raise RuntimeError("pg down")
        return FakeResult(self.signal_row)


def _enabled_status():
    return {"config": json.dumps({"enabled": True})}


def _patch_notify(monkeypatch):
    calls = []

    async def fake_notify(*, title, content, level, qq_alert):
        calls.append((title, level, qq_alert))
        return 2

    monkeypatch.setattr(dd, "_notify", fake_notify)

    async def trading(day):
        return True

    monkeypatch.setattr(dd, "_is_trading_day", trading)
    return calls


# ── evaluate_receipts（纯函数） ─────────────────────────────────────


class TestEvaluateReceipts:
    def test_all_ok(self):
        facts = {
            "pnl": "sent",
            "summary": "done",
            "stall": "present",
            "realtime": "ok",
        }
        assert dd.evaluate_receipts(facts) == []

    def test_nodata_and_disabled_and_unknown_are_ok(self):
        facts = {
            "pnl": "nodata",
            "summary": "done",
            "stall": "present",
            "realtime": "disabled",
        }
        assert dd.evaluate_receipts(facts) == []
        facts["realtime"] = "unknown"
        assert dd.evaluate_receipts(facts) == []

    def test_each_missing_code(self):
        base = {"pnl": "sent", "summary": "done", "stall": "present", "realtime": "ok"}
        assert dd.evaluate_receipts({**base, "pnl": "unsent"}) == ["pnl_report_unsent"]
        assert dd.evaluate_receipts({**base, "pnl": "missing"}) == [
            "pnl_report_missing"
        ]
        assert dd.evaluate_receipts({**base, "summary": "missing"}) == [
            "duty_summary_missing"
        ]
        assert dd.evaluate_receipts({**base, "stall": "missing"}) == [
            "stall_check_missing"
        ]
        assert dd.evaluate_receipts({**base, "realtime": "missing"}) == [
            "realtime_signals_missing"
        ]
        assert dd.evaluate_receipts({**base, "realtime": "stale"}) == [
            "realtime_signals_stale"
        ]

    def test_trade_redis_unreadable_collapses_to_one_code(self):
        facts = {
            "pnl": "unreadable",
            "summary": "unreadable",
            "stall": "unreadable",
            "realtime": "ok",
        }
        assert dd.evaluate_receipts(facts) == ["trade_receipts_unreadable"]

    def test_realtime_unreadable_is_reported(self):
        facts = {
            "pnl": "sent",
            "summary": "done",
            "stall": "present",
            "realtime": "unreadable",
        }
        assert dd.evaluate_receipts(facts) == ["realtime_signals_unreadable"]

    def test_sorted_deterministic(self):
        facts = {
            "pnl": "missing",
            "summary": "missing",
            "stall": "missing",
            "realtime": "stale",
        }
        assert dd.evaluate_receipts(facts) == sorted(dd.evaluate_receipts(facts))


# ── decide_alert（纯函数） ──────────────────────────────────────────


class TestDecideAlert:
    def test_first_missing_alerts(self):
        assert dd.decide_alert(["a"], []) == "alert"

    def test_same_set_is_silent(self):
        assert dd.decide_alert(["a", "b"], ["b", "a"]) == "silent"

    def test_changed_set_alerts_again(self):
        assert dd.decide_alert(["a"], ["a", "b"]) == "alert"
        assert dd.decide_alert(["a", "b"], ["a"]) == "alert"

    def test_empty_states(self):
        assert dd.decide_alert([], []) == "silent"
        assert dd.decide_alert([], ["a"]) == "recover"


# ── 渲染 ────────────────────────────────────────────────────────────


class TestRender:
    def test_alert_content_names_each_item(self):
        facts = {
            "pnl": "missing",
            "summary": "missing",
            "stall": "present",
            "realtime": "ok",
        }
        out = dd.render_alert_content(DAY, dd.evaluate_receipts(facts), facts)
        assert "2026-10-10" in out
        assert "收盘收益报表" in out
        assert "值班摘要" in out
        assert "16:00~23:30" in out
        assert "恢复" in out

    def test_recovery_content(self):
        out = dd.render_recovery_content(DAY)
        assert "2026-10-10" in out
        assert "补齐" in out


# ── run_duty_deadman_check（编排） ──────────────────────────────────


class TestRunCheck:
    def test_all_clear_writes_done_and_no_alert(self, monkeypatch):
        calls = _patch_notify(monkeypatch)
        trade = FakeR(
            keys={
                dr.pnl_report_done_key(DAY),
                dr.duty_summary_done_key(DAY),
                dr.stall_check_key(DAY),
            }
        )
        sched = FakeR(hgetall=_enabled_status())
        out = run(
            dd.run_duty_deadman_check(
                day=DAY,
                client_trade=trade,
                client_sched=sched,
                session=FakeSession(
                    signal_row=(5, datetime(2026, 10, 10, 7, 5, 0, tzinfo=timezone.utc))
                ),
            )
        )
        assert out["status"] == "ok"
        assert out["missing"] == []
        assert calls == []
        assert trade.kv.get(dr.deadman_done_key(DAY)) == "1"

    def test_missing_summary_alerts_and_records_set(self, monkeypatch):
        calls = _patch_notify(monkeypatch)
        trade = FakeR(keys={dr.pnl_report_done_key(DAY), dr.stall_check_key(DAY)})
        sched = FakeR(hgetall=_enabled_status())
        out = run(
            dd.run_duty_deadman_check(
                day=DAY,
                client_trade=trade,
                client_sched=sched,
                session=FakeSession(
                    signal_row=(5, datetime(2026, 10, 10, 7, 5, 0, tzinfo=timezone.utc))
                ),
            )
        )
        assert out["status"] == "alerted"
        assert out["missing"] == ["duty_summary_missing"]
        assert len(calls) == 1
        title, level, qq_alert = calls[0]
        assert "该响没响" in title and level == "error" and qq_alert is True
        alerted = json.loads(trade.kv[dr.deadman_alerted_key(DAY)])
        assert alerted == ["duty_summary_missing"]
        assert dr.deadman_done_key(DAY) not in trade.keys  # 没齐不封账

    def test_same_missing_set_does_not_repeat(self, monkeypatch):
        calls = _patch_notify(monkeypatch)
        trade = FakeR(
            keys={dr.pnl_report_done_key(DAY), dr.stall_check_key(DAY)},
            kv={dr.deadman_alerted_key(DAY): json.dumps(["duty_summary_missing"])},
        )
        sched = FakeR(hgetall=_enabled_status())
        out = run(
            dd.run_duty_deadman_check(
                day=DAY,
                client_trade=trade,
                client_sched=sched,
                session=FakeSession(
                    signal_row=(5, datetime(2026, 10, 10, 7, 5, 0, tzinfo=timezone.utc))
                ),
            )
        )
        assert out["status"] == "pending"
        assert calls == []

    def test_shrunk_set_alerts_again_with_new_list(self, monkeypatch):
        calls = _patch_notify(monkeypatch)
        trade = FakeR(
            keys={dr.pnl_report_done_key(DAY), dr.stall_check_key(DAY)},
            kv={
                dr.deadman_alerted_key(DAY): json.dumps(
                    ["duty_summary_missing", "realtime_signals_stale"]
                )
            },
        )
        sched = FakeR(hgetall=_enabled_status())
        out = run(
            dd.run_duty_deadman_check(
                day=DAY,
                client_trade=trade,
                client_sched=sched,
                session=FakeSession(
                    signal_row=(5, datetime(2026, 10, 10, 7, 5, 0, tzinfo=timezone.utc))
                ),
            )
        )
        assert out["action"] == "alert"
        assert json.loads(trade.kv[dr.deadman_alerted_key(DAY)]) == [
            "duty_summary_missing"
        ]
        assert len(calls) == 1  # 集合收缩 ⇒ 再响一次

    def test_recovery_path(self, monkeypatch):
        calls = _patch_notify(monkeypatch)
        trade = FakeR(
            keys={
                dr.pnl_report_done_key(DAY),
                dr.duty_summary_done_key(DAY),
                dr.stall_check_key(DAY),
            },
            kv={dr.deadman_alerted_key(DAY): json.dumps(["duty_summary_missing"])},
        )
        sched = FakeR(hgetall=_enabled_status())
        out = run(
            dd.run_duty_deadman_check(
                day=DAY,
                client_trade=trade,
                client_sched=sched,
                session=FakeSession(
                    signal_row=(5, datetime(2026, 10, 10, 7, 5, 0, tzinfo=timezone.utc))
                ),
            )
        )
        assert out["recovered"] is True
        assert len(calls) == 1
        assert calls[0][1] == "success"  # 恢复是好消息
        assert dr.deadman_alerted_key(DAY) not in trade.kv  # 清台账，允许再报
        assert trade.kv.get(dr.deadman_done_key(DAY)) == "1"

    def test_done_key_short_circuits(self, monkeypatch):
        calls = _patch_notify(monkeypatch)
        trade = FakeR(keys={dr.deadman_done_key(DAY)})
        out = run(
            dd.run_duty_deadman_check(
                day=DAY, client_trade=trade, client_sched=FakeR(), session=FakeSession()
            )
        )
        assert out["status"] == "done"
        assert calls == []
        assert trade.exists_calls == [dr.deadman_done_key(DAY)]  # 没读其余键

    def test_force_bypasses_done_key(self, monkeypatch):
        calls = _patch_notify(monkeypatch)
        trade = FakeR(
            keys={
                dr.deadman_done_key(DAY),
                dr.pnl_report_done_key(DAY),
                dr.duty_summary_done_key(DAY),
                dr.stall_check_key(DAY),
            }
        )
        out = run(
            dd.run_duty_deadman_check(
                day=DAY,
                force=True,
                client_trade=trade,
                client_sched=FakeR(hgetall=_enabled_status()),
                session=FakeSession(
                    signal_row=(5, datetime(2026, 10, 10, 7, 5, 0, tzinfo=timezone.utc))
                ),
            )
        )
        assert out["status"] == "ok"
        assert calls == []  # 全齐且无先前告警 ⇒ 静默（force 也不该制造通知）

    def test_non_trading_day_skips_unless_forced(self, monkeypatch):
        calls = _patch_notify(monkeypatch)

        async def weekend(day):
            return False

        monkeypatch.setattr(dd, "_is_trading_day", weekend)
        out = run(
            dd.run_duty_deadman_check(
                day=DAY,
                client_trade=FakeR(),
                client_sched=FakeR(),
                session=FakeSession(),
            )
        )
        assert out["status"] == "skipped"
        assert out["reason"] == "non_trading_day"
        assert calls == []  # 非交易日静默（跳过不是故障）
        out = run(
            dd.run_duty_deadman_check(
                day=DAY,
                force=True,
                client_trade=FakeR(
                    keys={
                        dr.pnl_report_done_key(DAY),
                        dr.duty_summary_done_key(DAY),
                        dr.stall_check_key(DAY),
                    }
                ),
                client_sched=FakeR(hgetall=_enabled_status()),
                session=FakeSession(
                    signal_row=(5, datetime(2026, 10, 10, 7, 5, 0, tzinfo=timezone.utc))
                ),
            )
        )
        assert out["status"] == "ok"

    def test_trade_redis_unreadable_alerts(self, monkeypatch):
        calls = _patch_notify(monkeypatch)
        out = run(
            dd.run_duty_deadman_check(
                day=DAY,
                client_trade=FakeR(fail=True),
                client_sched=FakeR(hgetall=_enabled_status()),
                session=FakeSession(
                    signal_row=(5, datetime(2026, 10, 10, 7, 5, 0, tzinfo=timezone.utc))
                ),
            )
        )
        assert out["missing"] == ["trade_receipts_unreadable"]
        assert len(calls) == 1

    def test_alert_not_recorded_when_delivery_fails(self, monkeypatch):
        # 投递失败 ≠ 已通知：不落 alerted 台账，下轮集合未变也会再试
        async def failing_notify(*, title, content, level, qq_alert):
            return 0

        async def trading(day):
            return True

        monkeypatch.setattr(dd, "_notify", failing_notify)
        monkeypatch.setattr(dd, "_is_trading_day", trading)
        trade = FakeR(keys={dr.pnl_report_done_key(DAY), dr.stall_check_key(DAY)})
        out = run(
            dd.run_duty_deadman_check(
                day=DAY,
                client_trade=trade,
                client_sched=FakeR(hgetall=_enabled_status()),
                session=FakeSession(
                    signal_row=(5, datetime(2026, 10, 10, 7, 5, 0, tzinfo=timezone.utc))
                ),
            )
        )
        assert out["status"] == "alerted"
        assert dr.deadman_alerted_key(DAY) not in trade.kv

    def test_realtime_disabled_skips_signal_check(self, monkeypatch):
        calls = _patch_notify(monkeypatch)
        trade = FakeR(
            keys={
                dr.pnl_report_done_key(DAY),
                dr.duty_summary_done_key(DAY),
                dr.stall_check_key(DAY),
            }
        )
        sched = FakeR(hgetall={"config": json.dumps({"enabled": False})})
        # PG 不可用也不影响：服务关着就不判
        out = run(
            dd.run_duty_deadman_check(
                day=DAY,
                client_trade=trade,
                client_sched=sched,
                session=FakeSession(fail=True),
            )
        )
        assert out["status"] == "ok"
        assert calls == []

    def test_realtime_status_absent_is_unknown_not_alarm(self, monkeypatch):
        _patch_notify(monkeypatch)
        trade = FakeR(
            keys={
                dr.pnl_report_done_key(DAY),
                dr.duty_summary_done_key(DAY),
                dr.stall_check_key(DAY),
            }
        )
        out = run(
            dd.run_duty_deadman_check(
                day=DAY,
                client_trade=trade,
                client_sched=FakeR(hgetall={}),
                session=FakeSession(fail=True),
            )
        )
        assert out["status"] == "ok"

    def test_realtime_zero_rows_alerts(self, monkeypatch):
        _patch_notify(monkeypatch)
        trade = FakeR(
            keys={
                dr.pnl_report_done_key(DAY),
                dr.duty_summary_done_key(DAY),
                dr.stall_check_key(DAY),
            }
        )
        out = run(
            dd.run_duty_deadman_check(
                day=DAY,
                client_trade=trade,
                client_sched=FakeR(hgetall=_enabled_status()),
                session=FakeSession(signal_row=(0, None)),
            )
        )
        assert out["missing"] == ["realtime_signals_missing"]

    def test_realtime_before_close_is_stale(self, monkeypatch):
        _patch_notify(monkeypatch)
        trade = FakeR(
            keys={
                dr.pnl_report_done_key(DAY),
                dr.duty_summary_done_key(DAY),
                dr.stall_check_key(DAY),
            }
        )
        out = run(
            dd.run_duty_deadman_check(
                day=DAY,
                client_trade=trade,
                client_sched=FakeR(hgetall=_enabled_status()),
                session=FakeSession(
                    signal_row=(
                        5,
                        datetime(2026, 10, 10, 6, 20, 0, tzinfo=timezone.utc),
                    )
                ),
            )
        )
        assert out["missing"] == ["realtime_signals_stale"]

    def test_realtime_pg_unreadable_alerts(self, monkeypatch):
        _patch_notify(monkeypatch)
        trade = FakeR(
            keys={
                dr.pnl_report_done_key(DAY),
                dr.duty_summary_done_key(DAY),
                dr.stall_check_key(DAY),
            }
        )
        out = run(
            dd.run_duty_deadman_check(
                day=DAY,
                client_trade=trade,
                client_sched=FakeR(hgetall=_enabled_status()),
                session=FakeSession(fail=True),
            )
        )
        assert out["missing"] == ["realtime_signals_unreadable"]

    def test_injected_clients_are_not_closed(self, monkeypatch):
        _patch_notify(monkeypatch)
        trade = FakeR(
            keys={
                dr.pnl_report_done_key(DAY),
                dr.duty_summary_done_key(DAY),
                dr.stall_check_key(DAY),
            }
        )
        sched = FakeR(hgetall=_enabled_status())
        run(
            dd.run_duty_deadman_check(
                day=DAY,
                client_trade=trade,
                client_sched=sched,
                session=FakeSession(
                    signal_row=(5, datetime(2026, 10, 10, 7, 5, 0, tzinfo=timezone.utc))
                ),
            )
        )
        assert trade.closed is False and sched.closed is False

    def test_self_opened_session_resets_pool_around_check(self, monkeypatch):
        _patch_notify(monkeypatch)
        closes: list[int] = []

        async def fake_close_database():
            closes.append(len(closes))

        import contextlib

        @contextlib.asynccontextmanager
        async def fake_get_session(read_only=False):
            assert read_only is True
            yield FakeSession(
                signal_row=(5, datetime(2026, 10, 10, 7, 5, 0, tzinfo=timezone.utc))
            )

        monkeypatch.setattr(dd, "close_database", fake_close_database)
        monkeypatch.setattr(dd, "get_session", fake_get_session)

        trade = FakeR(
            keys={
                dr.pnl_report_done_key(DAY),
                dr.duty_summary_done_key(DAY),
                dr.stall_check_key(DAY),
            }
        )
        out = run(
            dd.run_duty_deadman_check(
                day=DAY,
                client_trade=trade,
                client_sched=FakeR(hgetall=_enabled_status()),
            )
        )
        assert out["status"] == "ok"
        assert closes == [0, 1]  # 开跑前 + 收尾各清一次池（跨 tick 不互相投毒）

    def test_injected_session_skips_pool_reset(self, monkeypatch):
        _patch_notify(monkeypatch)
        closes: list[int] = []

        async def fake_close_database():
            closes.append(1)

        monkeypatch.setattr(dd, "close_database", fake_close_database)
        trade = FakeR(
            keys={
                dr.pnl_report_done_key(DAY),
                dr.duty_summary_done_key(DAY),
                dr.stall_check_key(DAY),
            }
        )
        out = run(
            dd.run_duty_deadman_check(
                day=DAY,
                client_trade=trade,
                client_sched=FakeR(hgetall=_enabled_status()),
                session=FakeSession(
                    signal_row=(5, datetime(2026, 10, 10, 7, 5, 0, tzinfo=timezone.utc))
                ),
            )
        )
        assert out["status"] == "ok"
        assert closes == []  # 注入会话 = 调用方所有，不碰全局池


# ── 接线守卫 ────────────────────────────────────────────────────────


class TestWiring:
    def test_heartbeat_and_receipts_keys_wired(self):
        src = inspect.getsource(dd)
        assert '_sched_heartbeat("duty_deadman")' in src
        assert "deadman_done_key" in src
        assert "deadman_alerted_key" in src
        assert "trade:duty-deadman" not in src  # 键字面量只许住在 duty_receipts

    def test_close_threshold_matches_summary(self):
        from backend.services.trade.services.duty_summary import (
            SIGNAL_CLOSE_THRESHOLD as summary_threshold,
        )

        assert dd.SIGNAL_CLOSE_THRESHOLD == summary_threshold

    def test_status_key_matches_realtime_service(self):
        from backend.services.engine.inference.realtime_service import (
            STATUS_KEY as service_key,
        )

        assert dd._status_key() == service_key

    def test_celery_task_and_beat_registered(self):
        from backend.services.engine.qlib_app import celery_config
        from backend.services.engine.tasks import celery_tasks

        assert 'name="engine.tasks.duty_deadman_check"' in inspect.getsource(
            celery_tasks
        )
        src = inspect.getsource(celery_config)
        assert '"engine.tasks.duty_deadman_check"' in src
        assert 'crontab(minute="0,30", hour="16-23")' in src
        assert "DUTY_DEADMAN_ENABLED" in src
        assert "duty-deadman-check" in celery_config.beat_schedule

    def test_schedule_ctl_can_rerun_both_jobs(self):
        from backend.scripts import schedule_ctl

        assert "duty_summary" in schedule_ctl._RERUN_DISPATCH
        assert "duty_deadman" in schedule_ctl._RERUN_DISPATCH

    def test_registry_specs_exist(self):
        from backend.shared.scheduler_registry import JOBS_BY_KEY

        summary = JOBS_BY_KEY["duty_summary"]
        assert summary.kind == "worker" and summary.owner == "trade"
        assert summary.switch_env == "QM_DUTY_SUMMARY_ENABLED"
        assert summary.switch_default_on is True
        deadman = JOBS_BY_KEY["duty_deadman"]
        assert deadman.kind == "celery_beat" and deadman.owner == "celery"
        assert deadman.switch_env == "DUTY_DEADMAN_ENABLED"
        assert deadman.switch_default_on is True
