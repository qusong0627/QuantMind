"""P0-3 测试：训练僵尸作业判尸与回收（reconcile_training_jobs）。

背景（docs/滚动训练与模型生命周期_设计方案.md §4.4）：
`recover_pending_runs` 无生产调用方（仅测试引用）→ API 重启后 running 行永挂。
判尸纪律：**不动活作业**（在管 task / 容器存活 / 进程存活 → 不碰）；
判死 → 标 failed(reason=orphaned…) + 释放单飞锁 + 告警；默认不自动重投。

分类核心 `classify_job` 是纯函数（全矩阵直测）；编排壳 `reconcile_training_jobs`
用注入的 provider/动作函数测（apply=False 时必须零副作用）。
"""

from __future__ import annotations

import asyncio
import os
import types

import backend.services.engine.training.job_reaper as jr
from backend.services.engine.training.job_reaper import (
    JobView,
    classify_job,
    reconcile_training_jobs,
)
from backend.shared.training_singleflight import LOCK_KEY

# ── 分类矩阵（纯函数）──────────────────────────────────────────────────────


def _view(**overrides) -> JobView:
    base = {
        "run_id": "train_20261008_abc",
        "status": "running",
        "tenant_id": "default",
        "user_id": "00000001",
        "instance_id": "abc123def456",
        "age_seconds": 120.0,
        "max_time_minutes": 120,
        "mode": "docker",
        "probe": "running",
        "in_registry": False,
    }
    base.update(overrides)
    return JobView(**base)


def test_terminal_and_in_registry_and_remote_never_touched():
    assert classify_job(_view(status="completed")).action == "skip"
    assert classify_job(_view(status="failed")).reason == "terminal"
    assert classify_job(_view(status="cancelled")).reason == "terminal"
    v = classify_job(_view(in_registry=True))
    assert (v.action, v.reason) == ("skip", "in_registry")
    v = classify_job(_view(mode="remote", probe="unavailable", age_seconds=10**6))
    assert (v.action, v.reason) == ("skip", "remote_unverifiable")


def test_docker_container_present_reattach():
    for probe in ("running", "created", "paused", "exited"):
        v = classify_job(_view(probe=probe))
        assert (v.action, v.reason) == ("reattach", "container_present"), probe
    # waiting_callback 阶段容器已退出（exit0 等回调）也要重挂
    v = classify_job(_view(status="waiting_callback", probe="exited"))
    assert v.action == "reattach"


def test_docker_container_missing():
    # running 行 + 容器不在 → 孤儿（无需宽限：状态说明容器曾经启动过）
    v = classify_job(_view(status="running", probe="missing"))
    assert (v.action, v.reason) == ("mark_failed", "container_missing")
    # pending 年轻（可能正在建容器/写盘）→ 宽限内不碰
    v = classify_job(_view(status="pending", probe="missing", age_seconds=100))
    assert (v.action, v.reason) == ("skip", "young_active")
    # provisioning 年轻（docker run 尚在途中，容器还未建完）→ 同样宽限
    v = classify_job(_view(status="provisioning", probe="missing", age_seconds=100))
    assert (v.action, v.reason) == ("skip", "young_active")
    # 超过宽限仍无容器 → 判死
    v = classify_job(_view(status="pending", probe="missing", age_seconds=700))
    assert (v.action, v.reason) == ("mark_failed", "container_missing")
    # provisioning 同理（宽限用 pending_grace）
    v = classify_job(_view(status="provisioning", probe="missing", age_seconds=700))
    assert v.action == "mark_failed"
    # 宽限只认「年轻 + 启动期状态」：running 行无宽限（上行已测），
    # 年轻但状态已是 running（容器曾存在过）也无宽限
    v = classify_job(_view(status="running", probe="missing", age_seconds=30))
    assert (v.action, v.reason) == ("mark_failed", "container_missing")


def test_docker_probe_unavailable():
    budget_age = 120 * 60 * 1.5 + 900  # 1.5×max_time + 回调宽限
    # 预算内不可验证 → 不碰（可能只是 docker daemon 抖动）
    v = classify_job(_view(probe="unavailable", age_seconds=budget_age - 100))
    assert (v.action, v.reason) == ("skip", "probe_unavailable")
    # 超过 1.5×预算仍不可验证 → 判死（合法运行不可能这么久）
    v = classify_job(_view(probe="unavailable", age_seconds=budget_age + 1))
    assert (v.action, v.reason) == ("mark_failed", "exceeded_budget_unverifiable")


def test_process_mode():
    v = classify_job(_view(mode="process", probe="alive", instance_id="12345"))
    assert (v.action, v.reason) == ("skip", "process_alive")
    v = classify_job(_view(mode="process", probe="dead", instance_id="12345"))
    assert (v.action, v.reason) == ("mark_failed", "process_gone")
    # 已记录过 pid 且已死 = 可验证死亡：无论行多年轻、状态多早，立即判死
    v = classify_job(
        _view(
            mode="process",
            probe="dead",
            instance_id="12345",
            status="provisioning",
            age_seconds=30,
        )
    )
    assert (v.action, v.reason) == ("mark_failed", "process_gone")
    # pid 从未记录（None）+ 行年轻 = 启动窗口（spawn 前/pid 回写前）→ 宽限
    v = classify_job(
        _view(
            mode="process",
            probe="dead",
            instance_id=None,
            status="pending",
            age_seconds=30,
        )
    )
    assert (v.action, v.reason) == ("skip", "young_active")
    v = classify_job(
        _view(
            mode="process",
            probe="dead",
            instance_id=None,
            status="provisioning",
            age_seconds=30,
        )
    )
    assert (v.action, v.reason) == ("skip", "young_active")
    # pid 从未记录 + 超宽限（但预算内）：probe=dead 不再适用宽限 → 判死
    v = classify_job(
        _view(
            mode="process",
            probe="dead",
            instance_id=None,
            status="pending",
            age_seconds=700,
        )
    )
    assert (v.action, v.reason) == ("mark_failed", "process_gone")
    # pid 不可读/未记录 + 超预算 → 判死
    budget_age = 120 * 60 * 1.5 + 900
    v = classify_job(
        _view(mode="process", probe="unavailable", age_seconds=budget_age + 1)
    )
    assert v.action == "mark_failed"
    v = classify_job(_view(mode="process", probe="unavailable", age_seconds=60))
    assert v.action == "skip"


def test_classify_ignores_non_reapable_statuses():
    for status in ("completed", "failed", "cancelled"):
        assert classify_job(_view(status=status, probe="missing")).action == "skip"


# ── 编排壳（注入假件）──────────────────────────────────────────────────────


def _plan():
    views = [
        _view(run_id="train_reattach", probe="running"),
        _view(run_id="train_missing", status="running", probe="missing"),
        _view(run_id="train_inflight", probe="missing", in_registry=True),
        _view(run_id="train_remote", mode="remote", probe="unavailable"),
        _view(run_id="train_young", status="pending", probe="missing", age_seconds=30),
    ]
    return [(v, {"node_id": "local"}) for v in views]


async def _run_reconcile(*, apply: bool):
    reattached: list[str] = []
    failed: list[tuple[str, str]] = []

    async def _provider():
        return _plan()

    async def _reattach(view, payload):
        reattached.append(view.run_id)
        return True

    async def _fail(view, verdict):
        failed.append((view.run_id, verdict.reason))
        return True

    summary = await reconcile_training_jobs(
        apply=apply,
        job_views_provider=_provider,
        reattach_fn=_reattach,
        mark_failed_fn=_fail,
        write_heartbeat=False,
    )
    return summary, reattached, failed


def test_reconcile_apply_dispatches_actions():
    summary, reattached, failed = asyncio.run(_run_reconcile(apply=True))
    assert summary["scanned"] == 5
    assert reattached == ["train_reattach"]
    assert failed == [("train_missing", "container_missing")]
    assert summary["reattached"] == 1
    assert summary["failed"] == 1
    assert summary["skipped"].get("in_registry") == 1
    assert summary["skipped"].get("remote_unverifiable") == 1
    assert summary["skipped"].get("young_active") == 1
    assert summary["dry_run"] is False


def test_reconcile_dry_run_has_no_side_effects():
    summary, reattached, failed = asyncio.run(_run_reconcile(apply=False))
    assert reattached == [] and failed == []
    assert summary["dry_run"] is True
    # 计划仍被完整分类（供演练输出）
    assert summary["planned_reattach"] == ["train_reattach"]
    assert summary["planned_failed"] == ["train_missing"]


def test_reconcile_collect_error_does_not_raise():
    async def _broken_provider():
        raise RuntimeError("db down")

    summary = asyncio.run(
        reconcile_training_jobs(
            apply=True, job_views_provider=_broken_provider, write_heartbeat=False
        )
    )
    assert summary["scanned"] == 0
    assert summary["errors"] == ["collect: db down"]


def test_reconcile_action_errors_collected_not_raised():
    """单个作业的重挂/判死失败 → 记 errors 并继续处理其余作业。"""

    async def _provider():
        return [
            (_view(run_id="train_boom_re", probe="running"), {}),
            (_view(run_id="train_boom_fail", probe="missing"), {}),
            (_view(run_id="train_ok", probe="running"), {}),
        ]

    async def _reattach(view, payload):
        if view.run_id == "train_boom_re":
            raise RuntimeError("docker hiccup")
        return True

    async def _fail(view, verdict):
        raise RuntimeError("row locked")

    summary = asyncio.run(
        reconcile_training_jobs(
            apply=True,
            job_views_provider=_provider,
            reattach_fn=_reattach,
            mark_failed_fn=_fail,
            write_heartbeat=False,
        )
    )
    assert summary["reattached"] == 1  # train_ok 不受前两处异常影响
    assert summary["failed"] == 0
    assert summary["errors"] == [
        "reattach train_boom_re: docker hiccup",
        "mark_failed train_boom_fail: row locked",
    ]


# ── IO 层（探针 / 收集器 / 重挂 / 判死 / 循环）─────────────────────────────


def test_probe_process(monkeypatch):
    assert jr._probe_process(None) == "dead"
    assert jr._probe_process("not-a-pid") == "dead"
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    assert jr._probe_process("12345") == "alive"
    monkeypatch.setattr(
        os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError())
    )
    assert jr._probe_process("12345") == "dead"
    monkeypatch.setattr(
        os, "kill", lambda pid, sig: (_ for _ in ()).throw(PermissionError())
    )
    assert jr._probe_process("12345") == "alive"  # 存在但无权限 → 视为存活


def _fake_docker_client(get_impl):
    containers = types.SimpleNamespace(get=get_impl)
    return types.SimpleNamespace(containers=containers)


def test_probe_docker_states(monkeypatch):
    import docker as docker_pkg

    c = types.SimpleNamespace(id="cid123", attrs={"State": {"Status": "running"}})
    c.reload = lambda: None
    monkeypatch.setattr(jr, "_docker_client", lambda: _fake_docker_client(lambda n: c))
    assert jr._probe_docker("train_x") == ("running", "cid123")

    monkeypatch.setattr(
        jr,
        "_docker_client",
        lambda: _fake_docker_client(
            lambda n: (_ for _ in ()).throw(docker_pkg.errors.NotFound("nf"))
        ),
    )
    assert jr._probe_docker("train_x") == ("missing", None)  # 确定不存在

    monkeypatch.setattr(
        jr,
        "_docker_client",
        lambda: _fake_docker_client(
            lambda n: (_ for _ in ()).throw(RuntimeError("daemon hiccup"))
        ),
    )
    assert jr._probe_docker("train_x") == ("unavailable", None)  # 不可验证

    monkeypatch.setattr(jr, "_docker_client", lambda: None)
    assert jr._probe_docker("train_x") == ("unavailable", None)


def test_job_mode(monkeypatch):
    import backend.shared.training_runtime as tr

    assert jr._job_mode({"node_id": "autodl-gpu1"}) == "remote"
    monkeypatch.setattr(
        tr, "resolve_training_executor", lambda: {"executor": "process"}
    )
    assert jr._job_mode({}) == "process"
    monkeypatch.setattr(tr, "resolve_training_executor", lambda: {"executor": "docker"})
    assert jr._job_mode({}) == "docker"
    monkeypatch.setattr(
        tr,
        "resolve_training_executor",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert jr._job_mode({}) == "docker"  # 探测失败按默认 docker


def test_max_time_minutes():
    assert jr._max_time_minutes({"max_time_minutes": 240}) == 240
    assert (
        jr._max_time_minutes({"max_time_minutes": 5}) == 10
    )  # 下限 10（与编排器同口径）
    assert jr._max_time_minutes({"max_time_minutes": "abc"}) == 120
    assert jr._max_time_minutes({}) == 120


def test_reaper_enabled_env_matrix(monkeypatch):
    monkeypatch.setenv("TRAINING_JOB_REAPER_ENABLED", "off")
    assert jr.reaper_enabled() is False
    monkeypatch.setenv("TRAINING_JOB_REAPER_ENABLED", "1")
    assert jr.reaper_enabled() is True
    monkeypatch.delenv("TRAINING_JOB_REAPER_ENABLED", raising=False)
    assert jr.reaper_enabled() is True  # 注册表默认开
    # 注册表不可用 → 回落同名环境变量
    import backend.shared.scheduler_registry as sr

    monkeypatch.setattr(sr, "JOBS_BY_KEY", {})
    monkeypatch.setenv("TRAINING_JOB_REAPER_ENABLED", "no")
    assert jr.reaper_enabled() is False


def test_reaper_interval_seconds(monkeypatch):
    monkeypatch.delenv("TRAINING_JOB_REAPER_INTERVAL_SECONDS", raising=False)
    assert jr.reaper_interval_seconds() == 3600
    monkeypatch.setenv("TRAINING_JOB_REAPER_INTERVAL_SECONDS", "120")
    assert jr.reaper_interval_seconds() == 120
    monkeypatch.setenv("TRAINING_JOB_REAPER_INTERVAL_SECONDS", "5")
    assert jr.reaper_interval_seconds() == 60  # 下限 60
    monkeypatch.setenv("TRAINING_JOB_REAPER_INTERVAL_SECONDS", "abc")
    assert jr.reaper_interval_seconds() == 3600


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows=None, record=None):
        self._rows = rows or []
        self._record = record
        self.commits = 0

    async def execute(self, stmt):
        return _FakeResult(self._rows)

    async def get(self, model, pk):
        return self._record

    async def commit(self):
        self.commits += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_collect_job_views(monkeypatch):
    from datetime import datetime, timedelta, timezone

    import backend.shared.database_manager_v2 as dbm

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = [
        {
            "id": "train_old",
            "tenant_id": "t1",
            "user_id": "u1",
            "status": "running",
            "instance_id": "cid",
            "request_payload": {"node_id": "local", "max_time_minutes": 60},
            "created_at": now_naive - timedelta(hours=2),
            "updated_at": now_naive - timedelta(minutes=5),
        },
        {
            "id": "train_no_payload",
            "tenant_id": None,
            "user_id": None,
            "status": "pending",
            "instance_id": None,
            "request_payload": "not-a-dict",
            "created_at": now_naive - timedelta(seconds=30),
            "updated_at": None,
        },
    ]
    monkeypatch.setattr(dbm, "get_session", lambda read_only=False: _FakeSession(rows))
    monkeypatch.setattr(jr, "_job_mode", lambda payload: "docker")
    monkeypatch.setattr(jr, "_probe_docker", lambda run_id: ("running", "cid"))

    views = asyncio.run(jr._collect_job_views())
    assert [v.run_id for v, _ in views] == ["train_old", "train_no_payload"]
    v1, p1 = views[0]
    assert v1.status == "running" and v1.mode == "docker" and v1.probe == "running"
    assert v1.tenant_id == "t1" and v1.user_id == "u1"
    assert v1.max_time_minutes == 60
    assert 290 <= v1.age_seconds <= 310  # updated_at 5 分钟前优先于 created_at
    assert v1.in_registry is False
    v2, p2 = views[1]
    assert v2.tenant_id == "default" and v2.user_id == "unknown"
    assert p2 == {} and v2.max_time_minutes == 120  # 坏 payload 回落默认
    assert v2.age_seconds <= 60  # 无 updated_at → created_at 兜底


def test_reattach_job_success_and_vanished(monkeypatch):
    recorded = {}

    class _Orch:
        async def reattach_training_job(self, **kw):
            recorded.update(kw)

    c = types.SimpleNamespace(id="c" * 64)
    monkeypatch.setattr(jr, "_docker_client", lambda: _fake_docker_client(lambda n: c))
    monkeypatch.setattr(jr, "_orchestrator", lambda: _Orch())
    view = _view(run_id="train_re", probe="running")
    assert asyncio.run(jr._reattach_job(view, {"node_id": "local"})) is True
    assert recorded["run_id"] == "train_re" and recorded["container_id"] == "c" * 64
    assert recorded["tenant_id"] == "default"
    assert recorded["supervise"] is True  # 默认建立监管（API 进程内）

    # CLI 探活模式：supervise=False 透传到编排器（不建监管）
    recorded.clear()
    assert (
        asyncio.run(jr._reattach_job(view, {"node_id": "local"}, supervise=False))
        is True
    )
    assert recorded["supervise"] is False

    # 竞态：容器在判尸与重挂之间消失 → False（下轮按 missing 处理）
    monkeypatch.setattr(
        jr,
        "_docker_client",
        lambda: _fake_docker_client(
            lambda n: (_ for _ in ()).throw(RuntimeError("gone"))
        ),
    )
    assert asyncio.run(jr._reattach_job(view, {})) is False

    monkeypatch.setattr(jr, "_docker_client", lambda: None)
    assert asyncio.run(jr._reattach_job(view, {})) is False


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, bytes] = {}

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value.encode() if isinstance(value, str) else value
        return True

    def get(self, key):
        return self.store.get(key)

    def eval(self, script, numkeys, key, token):
        tok = token.encode() if isinstance(token, str) else token
        if self.store.get(key) == tok:
            del self.store[key]
            return 1
        return 0


class _FakeLogStream:
    def __init__(self):
        self.calls: list[dict] = []

    def append_log(self, **kw):
        self.calls.append(kw)


def _wire_mark_orphaned(monkeypatch, record):
    import backend.services.engine.training.training_log_stream as tls
    import backend.shared.database_manager_v2 as dbm
    from backend.shared import redis_sentinel_client as rsc

    session = _FakeSession(record=record)
    monkeypatch.setattr(dbm, "get_session", lambda read_only=False: session)
    redis = _FakeRedis()
    monkeypatch.setattr(rsc, "get_redis_sentinel_client", lambda: redis)
    stream = _FakeLogStream()
    monkeypatch.setattr(tls, "TrainingRunLogStream", lambda: stream)
    return session, redis, stream


def test_mark_orphaned_marks_failed_logs_and_releases_lock(monkeypatch):
    record = types.SimpleNamespace(status="running", progress=10, logs="")
    session, redis, stream = _wire_mark_orphaned(monkeypatch, record)
    redis.set(LOCK_KEY, "train_dead", nx=True, ex=1800)
    view = _view(run_id="train_dead", status="running", probe="missing", mode="docker")
    verdict = classify_job(view)
    assert asyncio.run(jr._mark_orphaned(view, verdict)) is True
    assert record.status == "failed" and record.progress == 100
    assert "判死回收" in record.logs and "默认不自动重投" in record.logs
    assert session.commits == 1
    assert redis.get(LOCK_KEY) is None  # 单飞锁已 CAS 释放
    # 日志流补一条终态（前端可见）
    assert len(stream.calls) == 1
    entry = stream.calls[0]
    assert entry["run_id"] == "train_dead" and entry["status"] == "failed"
    assert entry["progress"] == 100 and "判死回收" in entry["line"]


def test_mark_orphaned_race_terminal_row_untouched(monkeypatch):
    record = types.SimpleNamespace(status="completed", progress=100, logs="done")
    session, redis, stream = _wire_mark_orphaned(monkeypatch, record)
    view = _view(run_id="train_race", status="running", probe="missing")
    assert asyncio.run(jr._mark_orphaned(view, classify_job(view))) is False
    assert record.status == "completed" and session.commits == 0
    assert stream.calls == []


def test_mark_orphaned_missing_row(monkeypatch):
    session, redis, stream = _wire_mark_orphaned(monkeypatch, None)
    view = _view(run_id="train_gone", status="running", probe="missing")
    assert asyncio.run(jr._mark_orphaned(view, classify_job(view))) is False
    assert session.commits == 0
    assert stream.calls == []


def test_cli_dry_run_and_apply(monkeypatch, capsys):
    received = {}

    async def _fake_reconcile(*, apply=True, **kw):
        received["apply"] = apply
        received.update(kw)
        return {
            "scanned": 3,
            "dry_run": not apply,
            "reattached": 0,
            "failed": 0,
            "planned_reattach": ["r1"],
            "planned_failed": ["r2"],
            "skipped": {"in_registry": 1},
            "errors": [],
        }

    monkeypatch.setattr(jr, "reconcile_training_jobs", _fake_reconcile)
    assert jr.main(["--dry-run"]) == 0
    assert received["apply"] is False
    # CLI 一律不写心跳（C07 应反映 API 进程内循环）、重挂只探活不建监管（LOW-10）
    assert received["write_heartbeat"] is False
    assert received["supervise_reattach"] is False
    assert "计划判死" in capsys.readouterr().out
    assert jr.main(["--apply"]) == 0
    assert received["apply"] is True
    out = capsys.readouterr().out
    assert "不建立监管" in out  # apply 模式且计划重挂非空时给出说明


def test_reconcile_supervise_reattach_plumbing(monkeypatch):
    """reconcile(supervise_reattach=False) → 默认 _reattach_job → 编排器 supervise=False。"""
    recorded = {}

    class _Orch:
        async def reattach_training_job(self, **kw):
            recorded.update(kw)
            return True

    c = types.SimpleNamespace(id="c" * 64)
    monkeypatch.setattr(jr, "_docker_client", lambda: _fake_docker_client(lambda n: c))
    monkeypatch.setattr(jr, "_orchestrator", lambda: _Orch())

    async def _provider():
        return [(_view(run_id="train_re", probe="running"), {"node_id": "local"})]

    summary = asyncio.run(
        reconcile_training_jobs(
            apply=True,
            job_views_provider=_provider,
            write_heartbeat=False,
            supervise_reattach=False,
        )
    )
    assert summary["reattached"] == 1
    assert recorded["supervise"] is False

    # 缺省（API 常驻循环）→ supervise=True
    recorded.clear()
    summary = asyncio.run(
        reconcile_training_jobs(
            apply=True, job_views_provider=_provider, write_heartbeat=False
        )
    )
    assert summary["reattached"] == 1
    assert recorded["supervise"] is True


def test_reaper_loop_start_stop_lifecycle(monkeypatch):
    calls = []

    async def _fake_reconcile(*, apply=True, **kw):
        calls.append(apply)
        return {
            "scanned": 0,
            "dry_run": not apply,
            "reattached": 0,
            "failed": 0,
            "planned_reattach": [],
            "planned_failed": [],
            "skipped": {},
            "errors": [],
        }

    monkeypatch.setattr(jr, "reconcile_training_jobs", _fake_reconcile)
    monkeypatch.setattr(jr, "reaper_enabled", lambda: True)
    monkeypatch.setattr(jr, "reaper_interval_seconds", lambda: 60)
    monkeypatch.setattr(jr, "_REAPER_TASK", None)

    async def _run():
        assert jr.start_training_job_reaper() is True
        assert jr.start_training_job_reaper() is False  # 幂等：已在跑
        await asyncio.sleep(0.05)  # 让常驻循环跑完首轮 sweep
        await jr.stop_training_job_reaper()
        assert jr._REAPER_TASK is None
        await jr.stop_training_job_reaper()  # 未启动/已停：幂等无异常

    asyncio.run(_run())
    assert calls == [True]  # 首轮即以 apply=True 真扫


def test_reaper_start_gated_by_switch(monkeypatch):
    monkeypatch.setattr(jr, "reaper_enabled", lambda: False)
    monkeypatch.setattr(jr, "_REAPER_TASK", None)
    assert jr.start_training_job_reaper() is False
    assert jr._REAPER_TASK is None
