"""P2-5 值班摘要（``backend/services/trade/services/duty_summary.py``）规格测试。

摘要回答的问题：「今天这条链上**发生过什么**」——收盘报表送没送到、池出了几行、
轮次跑了几轮/提了几腿、被精确否决了什么、镜像跳了几笔、哪些同步被跳发、实时信号
写到几点。它是一份**如实**的记录：读不到的段渲染「不可读」、没有的段渲染「无记录」、
旧格式条目「不判也不编 0」——绝不把「读不到」渲染成 0，也绝不把 0 编成正常。

不碰真外部：QQ 走 monkeypatch、Redis/PG 走 Fake。发送结果三键（全文/done/登记）
的语义与 ``daily_pnl_report_task`` 同构（sent → done；每（日,结果）至多一条登记）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import date, datetime, timezone

import pytest

from backend.shared import duty_receipts as dr
from backend.services.trade.services import duty_summary as ds

DAY = date(2026, 10, 10)


def run(coro):
    return asyncio.run(coro)


# ── Fake 面 ─────────────────────────────────────────────────────────


class FakeNative:
    """redis-py 原生客户端的最小面：lrange/exists/set/delete。"""

    def __init__(self, log=None, exist_keys=(), fail=False):
        self.log = list(log or [])
        self.exist_keys = set(exist_keys)
        self.store: dict[str, str] = {}
        self.expires: dict[str, int] = {}
        self.fail = fail

    def lrange(self, key, start, end):
        if self.fail:
            raise RuntimeError("redis 读不下来")
        return list(self.log)

    def exists(self, key):
        if self.fail:
            raise RuntimeError("redis 读不下来")
        return 1 if key in self.exist_keys else 0

    def set(self, key, value, nx=False, ex=None):
        if self.fail:
            raise RuntimeError("redis 写不下来")
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.expires[key] = ex
        return True

    def delete(self, key):
        self.store.pop(key, None)
        return 1


class FakeWrapper:
    def __init__(self, native):
        self.client = native


class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """按 SQL 文本分派结果的假会话（只认本模块会发的两条查询）。"""

    def __init__(self, veto_rows=(), signal_row=(0, None), fail=False):
        self.veto_rows = list(veto_rows)
        self.signal_row = signal_row
        self.fail = fail
        self.calls: list[str] = []

    async def execute(self, stmt, params=None):
        text = str(stmt)
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("pg 读不下来")
        if "qm_decision_ledger" in text:
            return FakeResult(self.veto_rows)
        if "engine_signal_scores" in text:
            return FakeResult([self.signal_row])
        raise AssertionError(f"未预期的 SQL: {text[:100]}")


class FakeHash:
    def __init__(self, data=None, fail=False):
        self.data = dict(data or {})
        self.fail = fail

    def hgetall(self, key):
        if self.fail:
            raise RuntimeError("redis 读不下来")
        return dict(self.data)


def _entry(**over):
    base = {
        "ts": "2026-10-10T14:45:00+08:00",
        "day": "2026-10-10",
        "slot": "1445",
        "status": "ok",
        "note": "提交完成",
        "agent": "",
        "decisions": 3,
        "legs": 3,
        "submitted": 2,
        "failed": 1,
        "watch_armed": 1,
        "audit_rows": 3,
    }
    base.update(over)
    return json.dumps(base, ensure_ascii=False)


# ── aggregate_rounds（纯函数） ───────────────────────────────────────


class TestAggregateRounds:
    def test_sums_and_last_from_newest(self):
        # LPUSH 写入 ⇒ index 0 最新
        raw = [
            _entry(slot="1445", submitted=2, failed=1),
            _entry(slot="0935", submitted=5, failed=0, note=""),
            _entry(day="2026-10-09", slot="1445", submitted=9),
        ]
        agg = ds.aggregate_rounds(raw, DAY)
        assert agg["rounds"] == 2
        assert agg["decisions"] == 6
        assert agg["legs"] == 6
        assert agg["submitted"] == 7
        assert agg["failed"] == 1
        assert agg["audit_rows"] == 6
        assert agg["legacy"] == 0
        assert agg["slots"] == ["1445", "0935"]
        assert agg["statuses"] == {"ok": 2}
        assert agg["last_slot"] == "1445"
        assert agg["last_status"] == "ok"
        assert agg["last_note"] == "提交完成"
        assert agg["last_ts"].startswith("2026-10-10T14:45")

    def test_legacy_entry_counted_but_not_judged(self):
        # T2-1 前的旧条目没有计数字段：计「轮数」，计数字段不判、不编 0
        raw = [
            json.dumps({"day": "2026-10-10", "status": "llm_failed"}),
            _entry(slot="1000"),
        ]
        agg = ds.aggregate_rounds(raw, DAY)
        assert agg["rounds"] == 2
        assert agg["legacy"] == 1
        assert agg["legs"] == 3  # 只有新条目那 3 腿，旧条目没编 0 也没编别的
        assert agg["last_note"] == ""  # 最新一条（legacy）没有 note

    def test_last_pool_captured_when_present(self):
        raw = [
            _entry(pool={"rows": 30, "shown": 20, "dropped": 10, "direction": "做多"}),
            _entry(slot="0935", pool={"file": "/x", "shown": 30, "dropped": 0}),
        ]
        agg = ds.aggregate_rounds(raw, DAY)
        assert agg["last_pool"] == {
            "rows": 30,
            "shown": 20,
            "dropped": 10,
            "direction": "做多",
        }

    def test_pool_without_rows_not_captured(self):
        agg = ds.aggregate_rounds([_entry(pool={"file": "/x", "shown": 1})], DAY)
        assert agg["last_pool"] is None

    def test_garbage_and_other_days_skipped(self):
        raw = ["not json", None, 123, json.dumps(["list"]), _entry(day="2026-10-08")]
        agg = ds.aggregate_rounds(raw, DAY)
        assert agg["rounds"] == 0
        assert agg["last_ts"] == ""
        assert agg["slots"] == []
        assert agg["statuses"] == {}

    def test_empty(self):
        agg = ds.aggregate_rounds([], DAY)
        assert agg["rounds"] == 0
        assert agg["legs"] == 0
        assert agg["last_pool"] is None


# ── render_summary（纯函数） ─────────────────────────────────────────


def _materials(**over):
    base = {
        "report": "sent",
        "pool": {
            "rows": 30,
            "direction": "做多",
            "source": "/data/reports/stock_picks/20261010_picks.json",
            "missing_columns": (),
        },
        "rounds": {
            "available": True,
            **ds.aggregate_rounds([_entry()], DAY),
        },
        "vetoes": {"available": True, "counts": {"l2.pool_not_member": 5, "vcash": 2}},
        "mirror": {"available": True, "stats": {"skipped_total": 3, "failed_total": 1}},
        "skips": {"available": True, "entries": {"market_sync:CN": "连续非交易日"}},
        "signal": {
            "available": True,
            "rows": 12,
            "last": datetime(2026, 10, 10, 7, 7, 23, tzinfo=timezone.utc),
            "wrote_to_close": True,
        },
    }
    base.update(over)
    return base


class TestRenderSummary:
    def test_full_render_has_all_sections(self):
        out = ds.render_summary(DAY, _materials())
        lines = out.splitlines()
        assert "收盘报表：已送达" in lines
        assert "池：30 只 · 方向：做多 · 20261010_picks.json" in lines
        assert "轮次：2 轮 · 决策 3 · 腿 3 · 提交 2 · 失败 1 · 审计行 3" not in out
        assert "轮次：1 轮 · 决策 3 · 腿 3 · 提交 2 · 失败 1 · 审计行 3" in lines
        assert "  末轮 1445（ok）：提交完成" in lines
        assert "精确否决：l2.pool_not_member 5 · vcash 2" in lines
        assert "镜像：跳过 3 · 失败 1" in lines
        assert "跳发：市场同步 CN（连续非交易日）" in lines
        assert "信号：实时信号 12 条，写到 15:07:23（覆盖收盘段）" in lines

    def test_pool_missing_columns_shown(self):
        mats = _materials(
            pool={
                "rows": 30,
                "direction": "—",
                "source": "/x/20261010_agent_picks.json",
                "missing_columns": ("industry", "rank"),
            }
        )
        out = ds.render_summary(DAY, mats)
        assert "缺列：industry, rank" in out

    def test_pool_missing_file(self):
        out = ds.render_summary(DAY, _materials(pool=None))
        assert "池：未生成（文件不在）" in out

    def test_degraded_render_is_honest(self):
        mats = _materials(
            report="unreadable",
            pool=None,
            rounds={"available": False},
            vetoes={"available": False},
            mirror={"available": False},
            skips={"available": False},
            signal={"available": False},
        )
        out = ds.render_summary(DAY, mats)
        assert "收盘报表：回执不可读（Redis 故障）" in out
        assert "池：未生成（文件不在）" in out
        assert "轮次：日志不可读（Redis 故障）" in out
        assert "精确否决：台账不可读（PG 故障）" in out
        assert "镜像：台账不可读（Redis 故障）" in out
        assert "跳发：台账不可读（Redis 故障）" in out
        assert "信号：不可读（PG 故障）" in out

    def test_zero_rounds_and_no_events_are_not_errors(self):
        mats = _materials(
            rounds={"available": True, **ds.aggregate_rounds([], DAY)},
            vetoes={"available": True, "counts": {}},
            mirror={"available": True, "stats": None},
            skips={"available": True, "entries": {}},
            signal={
                "available": True,
                "rows": 0,
                "last": None,
                "wrote_to_close": False,
            },
        )
        out = ds.render_summary(DAY, mats)
        assert "轮次：0 轮（今日无轮次记录）" in out
        assert "精确否决：无" in out
        assert "镜像：无跳过/失败记录" in out
        assert "跳发：无" in out
        assert "信号：今日无实时信号写入" in out

    def test_signal_not_covering_close_is_called_out(self):
        mats = _materials(
            signal={
                "available": True,
                "rows": 4,
                "last": datetime(2026, 10, 10, 6, 20, 0, tzinfo=timezone.utc),
                "wrote_to_close": False,
            }
        )
        out = ds.render_summary(DAY, mats)
        assert "只写到 14:20:00（未覆盖 14:50 后）" in out

    def test_report_statuses(self):
        for status, expect in (
            ("nodata", "收盘报表：无数据"),
            ("unsent", "收盘报表：未送达"),
            ("none", "收盘报表：无记录"),
        ):
            out = ds.render_summary(DAY, _materials(report=status))
            assert expect in out, status


# ── collect_report_status ───────────────────────────────────────────


class TestCollectReportStatus:
    def _status(self, native):
        return ds.collect_report_status(native, DAY)

    def test_sent_wins_over_unsent(self):
        native = FakeNative(
            exist_keys={
                dr.pnl_report_done_key(DAY),
                dr.pnl_report_registered_key(DAY, "unsent"),
            }
        )
        assert self._status(native) == "sent"

    def test_nodata_and_unsent_recognized(self):
        assert (
            self._status(
                FakeNative(exist_keys={dr.pnl_report_registered_key(DAY, "nodata")})
            )
            == "nodata"
        )
        assert (
            self._status(
                FakeNative(exist_keys={dr.pnl_report_registered_key(DAY, "unsent")})
            )
            == "unsent"
        )

    def test_none_when_no_keys(self):
        assert self._status(FakeNative()) == "none"

    def test_unreadable_on_redis_failure(self):
        assert self._status(FakeNative(fail=True)) == "unreadable"
        assert ds.collect_report_status(None, DAY) == "unreadable"


# ── collect_rounds / collect_vetoes / collect_signal / collect_skips ─


class TestCollectors:
    def test_collect_rounds_unavailable_when_native_none(self):
        out = ds.collect_rounds(None, DAY)
        assert out["available"] is False

    def test_collect_rounds_reads_and_aggregates(self):
        native = FakeNative(log=[_entry()])
        out = ds.collect_rounds(native, DAY)
        assert out["available"] is True
        assert out["rounds"] == 1

    def test_collect_rounds_read_failure(self):
        out = ds.collect_rounds(FakeNative(fail=True), DAY)
        assert out["available"] is False

    def test_collect_vetoes_groups(self):
        session = FakeSession(veto_rows=[("l2.pool_not_member", 5), ("vcash", 2)])
        out = run(ds.collect_vetoes(session, DAY))
        assert out == {
            "available": True,
            "counts": {"l2.pool_not_member": 5, "vcash": 2},
        }
        assert "qm_decision_ledger" in session.calls[0]
        assert "reject_reason" in session.calls[0]

    def test_collect_vetoes_read_failure(self):
        out = run(ds.collect_vetoes(FakeSession(fail=True), DAY))
        assert out["available"] is False

    def test_collect_signal_wrote_to_close(self):
        # 15:07 上海 = 07:07 UTC
        session = FakeSession(
            signal_row=(12, datetime(2026, 10, 10, 7, 7, 23, tzinfo=timezone.utc))
        )
        out = run(ds.collect_signal(session, DAY))
        assert out["available"] is True
        assert out["rows"] == 12
        assert out["wrote_to_close"] is True
        assert "source = 'realtime'" in session.calls[0]

    def test_collect_signal_before_close(self):
        session = FakeSession(
            signal_row=(4, datetime(2026, 10, 10, 6, 20, 0, tzinfo=timezone.utc))
        )
        out = run(ds.collect_signal(session, DAY))
        assert out["wrote_to_close"] is False

    def test_collect_signal_naive_treated_as_utc(self):
        session = FakeSession(signal_row=(1, datetime(2026, 10, 10, 7, 0, 0)))
        out = run(ds.collect_signal(session, DAY))
        assert out["wrote_to_close"] is True

    def test_collect_signal_no_rows(self):
        out = run(ds.collect_signal(FakeSession(signal_row=(0, None)), DAY))
        assert out["rows"] == 0
        assert out["last"] is None
        assert out["wrote_to_close"] is False

    def test_collect_signal_read_failure(self):
        out = run(ds.collect_signal(FakeSession(fail=True), DAY))
        assert out["available"] is False

    def test_collect_skips_ok_and_failure(self):
        out = ds.collect_skips(DAY, client=FakeHash({"market_sync:CN": "连续非交易日"}))
        assert out == {
            "available": True,
            "entries": {"market_sync:CN": "连续非交易日"},
        }
        out = ds.collect_skips(DAY, client=FakeHash(fail=True))
        assert out["available"] is False


# ── collect_all（接线） ─────────────────────────────────────────────


class TestCollectAllWiring:
    def test_collect_all_feeds_all_sections(self, monkeypatch):
        monkeypatch.setattr(ds, "collect_pool", lambda day: None)
        monkeypatch.setattr(
            ds, "collect_rounds", lambda native, day: {"available": True, "rounds": 0}
        )
        monkeypatch.setattr(
            ds, "collect_mirror", lambda redis, day: {"available": True, "stats": None}
        )
        monkeypatch.setattr(
            ds,
            "collect_skips",
            lambda day, client=None: {"available": True, "entries": {}},
        )
        session = FakeSession(veto_rows=[], signal_row=(0, None))
        out = run(ds.collect_all(FakeWrapper(FakeNative()), DAY, session=session))
        assert set(out) == {
            "report",
            "pool",
            "rounds",
            "vetoes",
            "mirror",
            "skips",
            "signal",
        }
        assert out["report"] == "none"
        assert out["vetoes"] == {"available": True, "counts": {}}
        assert out["signal"]["rows"] == 0
        assert len(session.calls) == 2  # 两条 PG 查询各一次

    def test_collect_all_without_session_marks_pg_unreadable(self, monkeypatch):
        monkeypatch.setattr(ds, "collect_pool", lambda day: None)
        monkeypatch.setattr(
            ds, "collect_rounds", lambda native, day: {"available": False}
        )
        monkeypatch.setattr(
            ds, "collect_mirror", lambda redis, day: {"available": False}
        )
        monkeypatch.setattr(
            ds,
            "collect_skips",
            lambda day, client=None: {"available": False},
        )
        out = run(ds.collect_all(FakeWrapper(FakeNative()), DAY, session=None))
        assert out["vetoes"]["available"] is False
        assert out["signal"]["available"] is False


# ── run_duty_summary（编排） ─────────────────────────────────────────


class TestRunDutySummary:
    def _patch_ok(self, monkeypatch, materials=None, qq=True, delivered=2):
        mats = materials if materials is not None else _materials()
        seen = {}

        async def fake_collect(redis, day, *, session=None):
            seen["session"] = session
            return mats

        async def fake_publish(title, content, *, level, qq_alert):
            seen.setdefault("publish", []).append((title, level, qq_alert))
            return delivered

        sent_calls = []

        def fake_notify(title, content):
            sent_calls.append((title, content))
            return qq

        monkeypatch.setattr(ds, "collect_all", fake_collect)
        monkeypatch.setattr(ds, "_publish_admin", fake_publish)
        monkeypatch.setattr(ds.qq_notify, "notify", fake_notify)
        return seen, sent_calls

    def test_sent_path_writes_full_key_and_registers_sent(self, monkeypatch):
        seen, sent_calls = self._patch_ok(monkeypatch)
        native = FakeNative()
        result = run(ds.run_duty_summary(FakeWrapper(native), today=DAY))

        assert result["sent"] is True
        assert len(sent_calls) == 1
        assert sent_calls[0][0] == "值班摘要 · 2026-10-10"
        payload = json.loads(native.store[dr.duty_summary_key(DAY)])
        assert payload["title"] == "值班摘要 · 2026-10-10"
        assert "收盘报表：已送达" in payload["content"]
        assert native.expires[dr.duty_summary_key(DAY)] == dr.DUTY_SUMMARY_TTL_SECONDS
        # 登记：sent → success，且不去 QQ 二次告警
        assert seen["publish"] == [("值班摘要 · 2026-10-10", "success", False)]
        assert dr.duty_summary_registered_key(DAY, "sent") in native.store
        # done 键由常驻循环写（sent 才落），编排层不写
        assert dr.duty_summary_done_key(DAY) not in native.store

    def test_unsent_registers_error_with_qq_bypass(self, monkeypatch):
        seen, _ = self._patch_ok(monkeypatch, qq=False)
        native = FakeNative()
        result = run(ds.run_duty_summary(FakeWrapper(native), today=DAY))
        assert result["sent"] is False
        assert seen["publish"] == [("值班摘要 · 2026-10-10", "error", True)]
        assert dr.duty_summary_registered_key(DAY, "unsent") in native.store

    def test_registration_deduped_per_day_outcome(self, monkeypatch):
        seen, _ = self._patch_ok(monkeypatch)
        native = FakeNative()
        wrapper = FakeWrapper(native)
        run(ds.run_duty_summary(wrapper, today=DAY))
        run(ds.run_duty_summary(wrapper, today=DAY))
        assert len(seen["publish"]) == 1  # 第二次撞登记占位键

    def test_collect_explosion_reports_error_and_skips_qq(self, monkeypatch):
        sent_calls = []

        async def boom(redis, day, *, session=None):
            raise RuntimeError("explode")

        monkeypatch.setattr(ds, "collect_all", boom)
        monkeypatch.setattr(
            ds.qq_notify, "notify", lambda t, c: sent_calls.append((t, c)) or True
        )
        result = run(ds.run_duty_summary(FakeWrapper(FakeNative()), today=DAY))
        assert result["sent"] is False
        assert "error" in result
        assert sent_calls == []

    def test_pg_session_failure_still_sends(self, monkeypatch):
        from backend.shared import database_manager_v2 as dbm

        seen = {}

        async def fake_collect(redis, day, *, session=None):
            seen["session"] = session
            mats = _materials(vetoes={"available": False}, signal={"available": False})
            return mats

        async def fake_publish(title, content, *, level, qq_alert):
            return 2

        class BoomCM:
            def __call__(self, *a, **kw):
                raise RuntimeError("pg 连不上")

        monkeypatch.setattr(ds, "collect_all", fake_collect)
        monkeypatch.setattr(ds, "_publish_admin", fake_publish)
        monkeypatch.setattr(ds.qq_notify, "notify", lambda t, c: True)
        monkeypatch.setattr(dbm, "get_session", BoomCM())
        result = run(ds.run_duty_summary(FakeWrapper(FakeNative()), today=DAY))
        assert result["sent"] is True
        assert seen["session"] is None  # PG 段按「不可读」渲染

    def test_release_registration_when_nobody_delivered(self, monkeypatch):
        seen, _ = self._patch_ok(monkeypatch, delivered=0)
        native = FakeNative()
        run(ds.run_duty_summary(FakeWrapper(native), today=DAY))
        # delivered=0 → 释放占位，下周期可补登记
        assert dr.duty_summary_registered_key(DAY, "sent") not in native.store


# ── 常驻循环与接线 ──────────────────────────────────────────────────


class TestTaskLoopAndWiring:
    def test_config_defaults(self, monkeypatch):
        monkeypatch.delenv("QM_DUTY_SUMMARY_ENABLED", raising=False)
        monkeypatch.delenv("QM_DUTY_SUMMARY_TIME", raising=False)
        cfg = ds._config()
        assert cfg["enabled"] is True
        assert cfg["time"] == "15:40"
        assert cfg["interval"] >= 10

    def test_disabled_task_returns_immediately(self, monkeypatch):
        monkeypatch.setenv("QM_DUTY_SUMMARY_ENABLED", "0")
        run(ds.run_duty_summary_task())  # 不进入循环

    def test_time_parse_fallback(self):
        assert ds.parse_report_time("15:40") == (15, 40)
        assert ds.parse_report_time("garbage")[0] == 15  # 复用收盘报表的回落

    def test_task_loop_gated_by_trading_day_and_done_key(self):
        src = inspect.getsource(ds.run_duty_summary_task)
        assert "_is_trading_day" in src
        assert "duty_summary_done_key" in src
        assert "DUTY_SUMMARY_DONE_TTL_SECONDS" in src

    def test_trade_main_wires_task(self):
        import backend.services.trade.main as main_mod

        src = inspect.getsource(main_mod)
        assert "run_duty_summary_task" in src
        # 创建与取消都要在（lifespan 起停成对）
        assert src.count("run_duty_summary_task") >= 2

    def test_keys_come_from_receipts_module(self):
        src = inspect.getsource(ds)
        assert "duty_summary_key" in src
        assert "trade:duty-summary" not in src  # 键字面量只许住在 duty_receipts
