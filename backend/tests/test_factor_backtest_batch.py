"""T-FB-08/09 单测：批量引擎（mini-app + 直调排水/重建）。

钉的契约：
- 派发：payload 去重、归属不符/代码空/已有活跃 run 三种跳过理由都进
  skipped 清单（绝不静默）；全跳过 = 空批次响应（不落库）；
- launch：spec 单元序（因子主序）、kinds/skipped 入 spec、先占位后落库、
  落库失败回滚内存态；
- 排水：单元串行、行终态回读定成败、连败 N 熔断（降级终态重置计数）、
  取消自退、被占因子等待唤醒后重试；
- 重建：孤儿 running 行收口 engine_restarted_mid_run、恰一次重试、
  终态批次拒绝重建、resume_interrupted 批量扫描；
- 取消：杀当前单元 + 台账收口 + 批次收口幂等；
- 状态：最近一次尝试滚动计数、attempts 计数、指标回落台账专列、
  failures 只含最新尝试为 failed 的单元。
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

fb = pytest.importorskip("backend.services.engine.factor_backtest.router")
bt = pytest.importorskip("backend.services.engine.factor_backtest.batch")
aa = pytest.importorskip("backend.services.engine.routers.alpha_agent")

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except Exception:  # noqa: BLE001
    FastAPI = None

pytestmark = pytest.mark.unit

_PREFIX = "/api/v1/factor-backtest"
_CODE = 'def calculate_factor(df):\n    return df["$close"]\n'


@pytest.fixture(autouse=True)
def _clean_shared_state():
    """批次内存态 + 共享去重/取消注册表是进程内的——用例结束必须清干净。"""
    yield
    bt._STATES.clear()
    bt._QUEUE = None
    bt._SCHEDULER = None
    fb._running_backtests.clear()
    fb._running_backtest_runs.clear()
    fb._backtest_cancelled.clear()


@pytest.fixture()
def client():
    app = FastAPI()

    @app.middleware("http")
    async def _inject_identity(request, call_next):
        request.state.user = {"user_id": "u-1", "tenant_id": "default"}
        return await call_next(request)

    app.include_router(fb.router)
    return TestClient(app)


def _factor(fid: str, code: str = _CODE) -> dict:
    return {
        "factor_id": fid,
        "factor_name": f"N-{fid}",
        "factor_code": code,
        "user_id": "u-1",
    }


# ── 派发端点 ─────────────────────────────────────────────────────────


def test_batch_dispatch_dedupes_and_records_skip_reasons(client, monkeypatch):
    async def _owned(factor_id, request, *, for_write=False):
        if factor_id == "f-3":
            raise fb.HTTPException(status_code=404, detail="nope")
        return _factor(factor_id)

    async def _pairs(ids):
        return [{"factor_id": "f-2", "market": "us_stock"}]

    captured: dict = {}

    async def _launch(**kwargs):
        captured.update(kwargs)
        return {
            "batch_id": "fbb-1",
            "total": 4,
            "queued": 4,
            "skipped": kwargs["skipped"],
        }

    monkeypatch.setattr(fb, "_require_owned_factor", _owned)
    monkeypatch.setattr(fb.store, "running_factor_pairs", _pairs)
    monkeypatch.setattr(fb.batch, "launch", _launch)

    r = client.post(
        f"{_PREFIX}/batch",
        json={
            "factor_ids": ["f-1", "f-1", "f-2", "f-3", "f-4"],
            "markets": ["a_share", "us_stock"],
        },
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["batch_id"] == "fbb-1"
    assert set(captured["factors"]) == {"f-1", "f-4"}  # 去重 + 跳过
    assert captured["markets"] == ["a_share", "us_stock"]
    assert captured["kinds"]["f-1"] == "functional"  # AST 预检进 kinds
    assert captured["start"] is None and captured["cost_bps"] is None
    reasons = {(s["factor_id"], s["reason"]) for s in captured["skipped"]}
    assert ("f-2", "已有进行中的回测") in reasons
    assert ("f-3", "因子不可见或不存在") in reasons


def test_batch_dispatch_skips_empty_code_factor(client, monkeypatch):
    async def _owned(factor_id, request, *, for_write=False):
        return _factor(factor_id, code="" if factor_id == "f-2" else _CODE)

    async def _pairs(ids):
        return []

    captured: dict = {}

    async def _launch(**kwargs):
        captured.update(kwargs)
        return {"batch_id": "fbb-1", "total": 1, "queued": 1, "skipped": []}

    monkeypatch.setattr(fb, "_require_owned_factor", _owned)
    monkeypatch.setattr(fb.store, "running_factor_pairs", _pairs)
    monkeypatch.setattr(fb.batch, "launch", _launch)

    r = client.post(
        f"{_PREFIX}/batch", json={"factor_ids": ["f-1", "f-2"], "markets": ["a_share"]}
    )
    assert r.status_code == 200
    assert set(captured["factors"]) == {"f-1"}
    assert captured["skipped"][0]["factor_id"] == "f-2"
    assert captured["skipped"][0]["reason"] == "因子代码为空"


def test_batch_dispatch_all_skipped_returns_empty_without_launch(client, monkeypatch):
    async def _owned(factor_id, request, *, for_write=False):
        return _factor(factor_id)

    async def _pairs(ids):
        return [{"factor_id": fid, "market": "a_share"} for fid in ids]

    async def _launch(**kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("全跳过时不得启动批次")

    monkeypatch.setattr(fb, "_require_owned_factor", _owned)
    monkeypatch.setattr(fb.store, "running_factor_pairs", _pairs)
    monkeypatch.setattr(fb.batch, "launch", _launch)

    r = client.post(
        f"{_PREFIX}/batch", json={"factor_ids": ["f-1"], "markets": ["a_share"]}
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["batch_id"] is None
    assert data["status"] == "empty"
    assert data["total"] == 0
    assert data["skipped"][0]["reason"] == "已有进行中的回测"


def test_batch_dispatch_rejects_unknown_market(client, monkeypatch):
    monkeypatch.setattr(fb, "get_authenticated_identity", lambda request: ("u-1", "d"))
    r = client.post(
        f"{_PREFIX}/batch", json={"factor_ids": ["f-1"], "markets": ["mars"]}
    )
    assert r.status_code == 400


def test_batches_list_passes_user_filter(client, monkeypatch):
    seen: dict = {}

    async def _list(*, user_id=None, limit=20):
        seen.update(user_id=user_id, limit=limit)
        return [{"batch_id": "fbb-1", "status": "running"}]

    monkeypatch.setattr(fb, "get_authenticated_identity", lambda request: ("u-1", "d"))
    monkeypatch.setattr(fb.store, "list_batches", _list)
    r = client.get(f"{_PREFIX}/batches", params={"limit": 5})
    assert r.status_code == 200
    assert seen == {"user_id": "u-1", "limit": 5}
    assert r.json()["data"]["batches"][0]["batch_id"] == "fbb-1"


def test_batch_status_404_and_ownership(client, monkeypatch):
    async def _get_batch(batch_id):
        if batch_id == "missing":
            return None
        return {
            "batch_id": batch_id,
            "user_id": "u-1",
            "status": "completed",
            "error": None,
            "spec": {"units": []},
            "created_at": dt.datetime(2026, 1, 1),
            "finished_at": dt.datetime(2026, 1, 1, 1),
        }

    monkeypatch.setattr(fb.store, "get_batch", _get_batch)
    assert (
        client.get(
            f"{_PREFIX}/batch/status", params={"batch_id": "missing"}
        ).status_code
        == 404
    )
    monkeypatch.setattr(fb, "get_authenticated_identity", lambda request: ("u-2", "d"))
    assert (
        client.get(f"{_PREFIX}/batch/status", params={"batch_id": "fbb-1"}).status_code
        == 404
    )
    # 本人可见：透传装配结果
    monkeypatch.setattr(fb, "get_authenticated_identity", lambda request: ("u-1", "d"))

    async def _assemble(batch_id):
        return {"batch": {"batch_id": batch_id, "status": "completed"}}

    monkeypatch.setattr(fb.batch, "get_batch_status", _assemble)
    r = client.get(f"{_PREFIX}/batch/status", params={"batch_id": "fbb-1"})
    assert r.status_code == 200
    assert r.json()["data"]["batch"]["status"] == "completed"


# ── launch ───────────────────────────────────────────────────────────


def test_launch_builds_spec_enqueues_and_spawns(monkeypatch):
    created: dict = {}

    async def _create(batch_id, *, user_id, spec):
        created.update(batch_id=batch_id, user_id=user_id, spec=spec)

    spawned: list = []

    def _spawn(coro):
        spawned.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(bt.store, "create_batch", _create)
    monkeypatch.setattr(bt, "_spawn", _spawn)

    async def runner():
        return await bt.launch(
            factors={"f-1": _factor("f-1"), "f-2": _factor("f-2")},
            kinds={"f-1": "functional", "f-2": "functional"},
            markets=["a_share", "us_stock"],
            start="2023-10-08",
            end=None,
            cost_bps=20,
            user_id="u-1",
            skipped=[{"factor_id": "f-9", "market": None, "reason": "x"}],
        )

    out = asyncio.run(runner())
    assert out["total"] == 4 == out["queued"]
    units = created["spec"]["units"]
    assert [(u["factor_id"], u["market"]) for u in units] == [
        ("f-1", "a_share"),
        ("f-1", "us_stock"),
        ("f-2", "a_share"),
        ("f-2", "us_stock"),
    ]
    assert created["spec"]["start"] == "2023-10-08"
    assert created["spec"]["cost_bps"] == 20
    assert created["spec"]["kinds"]["f-2"] == "functional"
    assert created["spec"]["skipped"][0]["factor_id"] == "f-9"
    assert created["user_id"] == "u-1"
    assert bt._STATES[out["batch_id"]].pending == units
    assert bt._QUEUE.get_nowait() == out["batch_id"]
    assert spawned  # 调度器被拉起（桩内 close，不真跑）


def test_launch_rolls_back_state_when_ledger_unavailable(monkeypatch):
    async def _create(batch_id, *, user_id, spec):
        raise RuntimeError("db down")

    monkeypatch.setattr(bt.store, "create_batch", _create)

    async def runner():
        with pytest.raises(RuntimeError):
            await bt.launch(
                factors={"f-1": _factor("f-1")},
                kinds={"f-1": "functional"},
                markets=["a_share"],
                start=None,
                end=None,
                cost_bps=None,
                user_id="u-1",
            )

    asyncio.run(runner())
    assert bt._STATES == {}


# ── 排水 ─────────────────────────────────────────────────────────────


def _state(units: list[dict[str, str]], batch_id: str = "fbb-1") -> bt.BatchState:
    spec = {
        "factor_ids": sorted({u["factor_id"] for u in units}),
        "markets": sorted({u["market"] for u in units}),
        "start": None,
        "end": None,
        "cost_bps": 20,
        "kinds": {u["factor_id"]: "functional" for u in units},
        "units": units,
        "skipped": [],
    }
    return bt.BatchState(batch_id=batch_id, spec=spec, pending=[dict(u) for u in units])


def _stub_finish_batch(monkeypatch) -> list:
    calls: list = []

    async def _finish_batch(batch_id, status, *, error=None):
        calls.append((batch_id, status, error))
        return True

    monkeypatch.setattr(bt.store, "finish_batch", _finish_batch)
    return calls


def test_drain_serial_completes_and_finalizes(monkeypatch):
    executed: list = []

    async def _execute(state, unit):
        executed.append((unit["factor_id"], unit["market"]))
        # 真 _execute_unit 的占位释放由 _run_single finally 做——桩须同契约，
        # 否则同因子第二单元永远等不到释放（eligibility 堵塞）。
        fb._running_backtests.discard(unit["factor_id"])
        return "completed"

    monkeypatch.setattr(bt, "_execute_unit", _execute)
    calls = _stub_finish_batch(monkeypatch)

    units = [
        {"factor_id": "f-1", "market": "a_share"},
        {"factor_id": "f-1", "market": "us_stock"},
    ]
    state = _state(units)
    bt._STATES[state.batch_id] = state
    asyncio.run(bt._drain_batch(state))

    # 默认并发=1：同因子两单元严格按序执行（子进程登记互斥的地基）
    assert executed == [("f-1", "a_share"), ("f-1", "us_stock")]
    assert calls == [("fbb-1", "completed", None)]
    assert state.batch_id not in bt._STATES


def test_drain_circuit_breaker_stops_after_n_consecutive_failures(monkeypatch):
    executed: list = []

    async def _execute(state, unit):
        executed.append(unit["factor_id"])
        return "failed"

    monkeypatch.setattr(bt, "_execute_unit", _execute)
    monkeypatch.setattr(bt, "max_consec_fails", lambda: 2)
    calls = _stub_finish_batch(monkeypatch)

    units = [
        {"factor_id": "f-1", "market": "a_share"},
        {"factor_id": "f-2", "market": "a_share"},
        {"factor_id": "f-3", "market": "a_share"},
    ]
    state = _state(units)
    bt._STATES[state.batch_id] = state
    asyncio.run(bt._drain_batch(state))

    assert executed == ["f-1", "f-2"]  # 第 3 单元未启动（熔断）
    assert len(state.pending) == 1
    assert calls[0][1] == "aborted"
    assert "circuit_breaker" in calls[0][2]


def test_drain_degraded_statuses_reset_consec_fails(monkeypatch):
    outcomes = iter(["failed", "data_unsupported", "failed", "unavailable"])
    executed: list = []

    async def _execute(state, unit):
        executed.append(unit["factor_id"])
        return next(outcomes)

    monkeypatch.setattr(bt, "_execute_unit", _execute)
    monkeypatch.setattr(bt, "max_consec_fails", lambda: 2)
    calls = _stub_finish_batch(monkeypatch)

    units = [{"factor_id": f"f-{i}", "market": "a_share"} for i in range(1, 5)]
    state = _state(units)
    bt._STATES[state.batch_id] = state
    asyncio.run(bt._drain_batch(state))

    # 降级终态是适配结论不是故障：连败计数被重置，四单元全部跑完
    assert executed == ["f-1", "f-2", "f-3", "f-4"]
    assert calls == [("fbb-1", "completed", None)]


def test_drain_cancelled_state_exits_without_running_units(monkeypatch):
    executed: list = []

    async def _execute(state, unit):  # pragma: no cover - 不应被调用
        executed.append(unit)
        return "completed"

    monkeypatch.setattr(bt, "_execute_unit", _execute)
    calls = _stub_finish_batch(monkeypatch)

    units = [{"factor_id": "f-1", "market": "a_share"}]
    state = _state(units)
    state.cancelled = True
    bt._STATES[state.batch_id] = state
    asyncio.run(bt._drain_batch(state))

    assert executed == []
    assert calls == [("fbb-1", "cancelled", "cancelled_by_user")]


def test_drain_waits_for_busy_factor_then_runs(monkeypatch):
    executed: list = []

    async def _execute(state, unit):
        executed.append(unit["factor_id"])
        return "completed"

    monkeypatch.setattr(bt, "_execute_unit", _execute)
    monkeypatch.setattr(bt, "_ELIGIBILITY_POLL_S", 0.01)
    calls = _stub_finish_batch(monkeypatch)

    units = [{"factor_id": "f-1", "market": "a_share"}]
    state = _state(units)
    bt._STATES[state.batch_id] = state
    fb._running_backtests.add("f-1")  # 单发正在跑：单元必须等待

    async def runner():
        async def _release():
            await asyncio.sleep(0.05)
            fb._running_backtests.discard("f-1")

        await asyncio.gather(bt._drain_batch(state), _release())

    asyncio.run(runner())
    assert executed == ["f-1"]
    assert calls == [("fbb-1", "completed", None)]


# ── 重建 / 续跑 ──────────────────────────────────────────────────────


def test_pending_units_retries_restart_orphans_exactly_once():
    spec = {
        "units": [
            {"factor_id": "f-1", "market": "us"},  # 无行 → 待跑
            {"factor_id": "f-2", "market": "us"},  # 一次重启中断 → 重试一次
            {"factor_id": "f-3", "market": "us"},  # 两次重启中断 → 不再重试
            {"factor_id": "f-4", "market": "us"},  # 真失败 → 不重试
            {"factor_id": "f-5", "market": "us"},  # completed → 完结
        ]
    }
    now = dt.datetime(2026, 1, 1)

    def _row(fid, status, error=None, ts=None):
        return {
            "factor_id": fid,
            "market": "us",
            "status": status,
            "error": error,
            "created_at": ts or now,
        }

    rows = [
        _row("f-2", "failed", "engine_restarted_mid_run"),
        _row("f-3", "failed", "engine_restarted_mid_run", now),
        _row("f-3", "failed", "engine_restarted_mid_run", now + dt.timedelta(1)),
        _row("f-4", "failed", "boom"),
        _row("f-5", "completed"),
    ]
    pending = bt._pending_units(spec, rows)
    assert [u["factor_id"] for u in pending] == ["f-1", "f-2"]


def test_resume_batch_settles_orphans_and_requeues(monkeypatch):
    spec = {
        "factor_ids": ["f-1", "f-2"],
        "markets": ["us_stock"],
        "start": None,
        "end": None,
        "cost_bps": 20,
        "kinds": {"f-1": "functional", "f-2": "functional"},
        "units": [
            {"factor_id": "f-1", "market": "us_stock"},
            {"factor_id": "f-2", "market": "us_stock"},
        ],
        "skipped": [],
    }
    settle_calls: list = []

    async def _get_batch(batch_id):
        return {
            "batch_id": batch_id,
            "user_id": "u-1",
            "status": "running",
            "error": None,
            "spec": spec,
            "created_at": dt.datetime(2026, 1, 1),
            "finished_at": None,
        }

    async def _settle(batch_id, *, error):
        settle_calls.append((batch_id, error))
        return 1

    async def _runs(batch_id):
        return [
            {
                "run_id": "fb-x",
                "factor_id": "f-1",
                "market": "us_stock",
                "status": "failed",
                "error": "engine_restarted_mid_run",
                "created_at": dt.datetime(2026, 1, 1),
            }
        ]

    spawned: list = []

    def _spawn(coro):
        spawned.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(bt.store, "get_batch", _get_batch)
    monkeypatch.setattr(bt.store, "settle_orphan_running", _settle)
    monkeypatch.setattr(bt.store, "batch_runs", _runs)
    monkeypatch.setattr(bt, "_spawn", _spawn)

    async def runner():
        first = await bt.resume_batch("fbb-1")
        second = await bt.resume_batch("fbb-1")  # 已在内存 → 拒绝重复重建
        return first, second

    first, second = asyncio.run(runner())
    assert first is True and second is False
    assert settle_calls == [("fbb-1", "engine_restarted_mid_run")]
    state = bt._STATES["fbb-1"]
    assert [u["factor_id"] for u in state.pending] == ["f-1", "f-2"]
    assert bt._QUEUE.get_nowait() == "fbb-1"
    assert spawned


def test_resume_batch_skips_terminal_batches(monkeypatch):
    async def _get_batch(batch_id):
        return {
            "batch_id": batch_id,
            "user_id": "u-1",
            "status": "completed",
            "error": None,
            "spec": {"units": [{"factor_id": "f-1", "market": "us_stock"}]},
            "created_at": dt.datetime(2026, 1, 1),
            "finished_at": dt.datetime(2026, 1, 1),
        }

    monkeypatch.setattr(bt.store, "get_batch", _get_batch)
    assert asyncio.run(bt.resume_batch("fbb-1")) is False
    assert bt._STATES == {}


def test_resume_interrupted_scans_and_resumes(monkeypatch):
    async def _running(limit=20):
        return [
            {"batch_id": "fbb-1", "spec": {"units": []}},
            {"batch_id": "fbb-2", "spec": {"units": []}},
        ]

    async def _get_batch(batch_id):
        return {
            "batch_id": batch_id,
            "user_id": "u-1",
            "status": "running",
            "error": None,
            "spec": {"units": []},
            "created_at": dt.datetime(2026, 1, 1),
            "finished_at": None,
        }

    def _spawn(coro):
        coro.close()
        return None

    monkeypatch.setattr(bt.store, "list_running_batches", _running)
    monkeypatch.setattr(bt.store, "get_batch", _get_batch)
    monkeypatch.setattr(bt, "_spawn", _spawn)

    assert asyncio.run(bt.resume_interrupted()) == 2
    assert set(bt._STATES) == {"fbb-1", "fbb-2"}


# ── 取消 ─────────────────────────────────────────────────────────────


def test_cancel_batch_kills_running_unit_and_closes(monkeypatch):
    finished: list = []
    kill_calls: list = []

    async def _finish_run(run_id, status, **kwargs):
        finished.append((run_id, status, kwargs))
        return True

    calls = _stub_finish_batch(monkeypatch)
    monkeypatch.setattr(bt.store, "finish_run", _finish_run)
    monkeypatch.setattr(fb, "_kill_backtest_process", kill_calls.append)

    state = bt.BatchState(batch_id="fbb-1", spec={"units": []}, pending=[])
    state.running_units[("f-1", "us_stock")] = "fb-run-1"
    bt._STATES["fbb-1"] = state

    out = asyncio.run(bt.cancel_batch("fbb-1"))
    assert kill_calls == ["f-1"]
    assert "f-1" in fb._backtest_cancelled  # 标记留给任务 finally 清理
    assert finished == [("fb-run-1", "cancelled", {"error": "cancelled_by_user"})]
    assert calls == [("fbb-1", "cancelled", "cancelled_by_user")]
    assert state.cancelled is True
    assert out["killed"] == [
        {"factor_id": "f-1", "market": "us_stock", "run_id": "fb-run-1"}
    ]


def test_cancel_batch_without_memory_state_still_closes(monkeypatch):
    calls = _stub_finish_batch(monkeypatch)
    out = asyncio.run(bt.cancel_batch("fbb-gone"))
    assert calls == [("fbb-gone", "cancelled", "cancelled_by_user")]
    assert out["killed"] == []


# ── 状态装配 ─────────────────────────────────────────────────────────


def test_get_batch_status_rolls_up_latest_rows(monkeypatch):
    spec = {
        "factor_ids": ["f-1"],
        "markets": ["us_stock", "hong_kong", "a_share"],
        "start": "2023-10-08",
        "end": None,
        "cost_bps": 20,
        "kinds": {"f-1": "functional"},
        "units": [
            {"factor_id": "f-1", "market": "us_stock"},
            {"factor_id": "f-1", "market": "hong_kong"},
            {"factor_id": "f-1", "market": "a_share"},
        ],
        "skipped": [{"factor_id": "f-9", "market": None, "reason": "x"}],
    }

    async def _get_batch(batch_id):
        return {
            "batch_id": batch_id,
            "user_id": "u-1",
            "status": "running",
            "error": None,
            "spec": spec,
            "created_at": dt.datetime(2026, 1, 1),
            "finished_at": None,
        }

    def _row(run_id, market, status, created, error=None, metrics=None, **cols):
        return {
            "run_id": run_id,
            "factor_id": "f-1",
            "market": market,
            "status": status,
            "error": error,
            "metrics": metrics or {},
            "created_at": created,
            "finished_at": None,
            **cols,
        }

    async def _runs(batch_id):
        return [
            _row(
                "fb-a",
                "us_stock",
                "completed",
                dt.datetime(2026, 1, 1, 10),
                metrics={"ic": 0.05, "n_days": 254},
            ),
            # hong_kong：先失败（重启中断），又重跑（running）——取最近一次
            _row(
                "fb-b",
                "hong_kong",
                "failed",
                dt.datetime(2026, 1, 1, 10),
                error="engine_restarted_mid_run",
            ),
            _row("fb-c", "hong_kong", "running", dt.datetime(2026, 1, 1, 12)),
        ]

    monkeypatch.setattr(bt.store, "get_batch", _get_batch)
    monkeypatch.setattr(bt.store, "batch_runs", _runs)

    data = asyncio.run(bt.get_batch_status("fbb-1"))
    prog = data["progress"]
    assert prog["total"] == 3
    assert prog["completed"] == 1
    assert prog["running"] == 1
    assert prog["pending"] == 1
    assert prog["done"] == 1
    assert prog["draining"] is False  # 内存无状态（重启后重建前）
    assert data["failures"] == []  # 最近一次尝试为 running → 不算失败
    by_market = {u["market"]: u for u in data["units"]}
    assert by_market["us_stock"]["ic"] == 0.05
    assert by_market["us_stock"]["n_days"] == 254
    assert by_market["hong_kong"]["status"] == "running"
    assert by_market["hong_kong"]["attempts"] == 2
    assert by_market["a_share"]["status"] == "pending"
    assert by_market["a_share"]["attempts"] == 0
    assert data["spec"]["skipped"][0]["reason"] == "x"
    assert data["batch"]["status"] == "running"


def test_get_batch_status_failures_lists_latest_failed_units(monkeypatch):
    spec = {
        "factor_ids": ["f-1"],
        "markets": ["us_stock"],
        "start": None,
        "end": None,
        "cost_bps": None,
        "kinds": {"f-1": "functional"},
        "units": [{"factor_id": "f-1", "market": "us_stock"}],
        "skipped": [],
    }

    async def _get_batch(batch_id):
        return {
            "batch_id": batch_id,
            "user_id": "u-1",
            "status": "aborted",
            "error": "circuit_breaker: 5 consecutive failures",
            "spec": spec,
            "created_at": dt.datetime(2026, 1, 1),
            "finished_at": dt.datetime(2026, 1, 1, 1),
        }

    async def _runs(batch_id):
        return [
            {
                "run_id": "fb-a",
                "factor_id": "f-1",
                "market": "us_stock",
                "status": "failed",
                "error": "boom",
                "metrics": {},
                "ic_value": 0.01,
                "created_at": dt.datetime(2026, 1, 1, 10),
                "finished_at": dt.datetime(2026, 1, 1, 10, 1),
            }
        ]

    monkeypatch.setattr(bt.store, "get_batch", _get_batch)
    monkeypatch.setattr(bt.store, "batch_runs", _runs)

    data = asyncio.run(bt.get_batch_status("fbb-1"))
    assert data["progress"]["failed"] == 1
    assert len(data["failures"]) == 1
    failure = data["failures"][0]
    assert failure["run_id"] == "fb-a"
    assert failure["error"] == "boom"
    assert failure["ic"] == 0.01  # 指标回落台账专列
    assert data["batch"]["error"].startswith("circuit_breaker")
