"""P0-3 复审 HIGH-1 测试：取消/关停时训练资源的清理纪律。

背景（复审 2026-10-08）：`REGISTRY.cancel(run_id)` 先 pop 索引再 cancel task，
`is_active` 立刻转 False → 单飞释放回调在 CancelledError 尚未触达轮询循环前
就把锁放了；而三个编排器的轮询循环从未捕获 CancelledError → 容器/子进程
继续训练、状态永远 running、下一次提交双跑（宿主 44G 内存最恶性失败模式）。

修复纪律（本文件覆盖）：
- CancelledError 处理器里**仅在用户取消标记已置**时杀资源（用户取消）；
- 无标记（API 进程正常关停）不动资源，留给重启后判尸重挂/收尸；
- `supervised_launch` → `cleanup_cancelled_provisioning` 覆盖 provisioning 窗口；
- 远端 process 模式的取消此前是空操作（落进 docker 分支）→ 已补 pid 杀。
"""

from __future__ import annotations

import asyncio
import types

import pytest

import backend.shared.database_manager_v2 as dbm
from backend.services.engine.training.local_docker_orchestrator import (
    LocalDockerOrchestrator,
)
from backend.services.engine.training.local_process_orchestrator import (
    LocalProcessOrchestrator,
)
from backend.services.engine.training.remote_ssh_orchestrator import (
    RemoteSSHOrchestrator,
)


@pytest.fixture(autouse=True)
def _reset_orchestrator_marks(monkeypatch):
    """编排器模块级标记集跨用例共享：每个用例前隔离，防相互污染。"""
    import backend.services.engine.training.orchestrator_base as ob

    monkeypatch.setattr(ob, "_GRACEFUL_STOPS", set())
    monkeypatch.setattr(ob, "_USER_CANCEL_KILLS", set())


class FakeStream:
    """可配置取消标记的日志流假件（记录 append_log / clear_cancel）。"""

    def __init__(self, cancel: bool = False):
        self.cancel = cancel
        self.appended: list[dict] = []
        self.cleared: list[str] = []

    def is_cancel_requested(self, run_id: str) -> bool:
        return self.cancel

    def append_log(self, **kw):
        self.appended.append(kw)

    def clear_cancel(self, run_id: str):
        self.cleared.append(run_id)


class _TxSession:
    """最小 DB session：get(record) + commit + 异步上下文。"""

    def __init__(self, record=None):
        self._record = record
        self.commits = 0
        self.get_kwargs: list[dict] = []

    async def get(self, model, pk, **kwargs):
        self.get_kwargs.append(kwargs)
        return self._record

    async def commit(self):
        self.commits += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_session(monkeypatch, record=None):
    session = _TxSession(record)
    monkeypatch.setattr(dbm, "get_session", lambda *a, **k: session)
    return session


# ── docker 编排器 ───────────────────────────────────────────────────────────


def _bare_docker_orch(stream: FakeStream):
    orch = object.__new__(LocalDockerOrchestrator)
    orch.log_stream = stream
    return orch


def _stub_poll_raising(orch, events):
    async def _poll(
        run_id,
        container_id,
        *,
        tenant_id,
        user_id,
        work_dir=None,
        max_time_minutes=120,
    ):
        events.append("poll")
        raise asyncio.CancelledError()

    orch._poll_container = _poll


def _stub_cancel_resume(orch, events, cancel_error=False):
    async def _cancel(run_id, container_id, tenant_id, user_id):
        events.append("cancel")
        if cancel_error:
            raise RuntimeError("docker hiccup")

    async def _resume(work_dir, *, run_id, container_id, tenant_id, user_id):
        events.append("resume")

    orch._cancel_container = _cancel
    orch._resume_paused_others = _resume


def test_poll_supervised_on_cancel_with_flag_stops_container():
    events: list[str] = []
    orch = _bare_docker_orch(FakeStream(cancel=True))
    _stub_poll_raising(orch, events)
    _stub_cancel_resume(orch, events)

    async def _run():
        with pytest.raises(asyncio.CancelledError):
            await orch._poll_container_supervised(
                "train_x", "c" * 64, tenant_id="t", user_id="u"
            )

    asyncio.run(_run())
    assert events == ["poll", "cancel", "resume"]  # 清理后 CancelledError 继续上抛


def test_poll_supervised_on_shutdown_without_flag_leaves_resources(monkeypatch):
    """API 进程正常关停（无用户取消标记）：绝不动容器——那是重挂要救回的作业。"""
    import backend.services.engine.training.orchestrator_base as ob

    marks: set = set()
    monkeypatch.setattr(ob, "_GRACEFUL_STOPS", marks)
    events: list[str] = []
    orch = _bare_docker_orch(FakeStream(cancel=False))
    _stub_poll_raising(orch, events)
    _stub_cancel_resume(orch, events)

    async def _run():
        with pytest.raises(asyncio.CancelledError):
            await orch._poll_container_supervised(
                "train_x", "c" * 64, tenant_id="t", user_id="u"
            )

    asyncio.run(_run())
    assert events == ["poll"]  # 无 cancel/resume
    assert marks == {"train_x"}  # 且置「关停保锁」标记（HIGH-4：关停不删锁）


def test_poll_supervised_cleanup_failure_does_not_mask_cancel():
    events: list[str] = []
    orch = _bare_docker_orch(FakeStream(cancel=True))
    _stub_poll_raising(orch, events)
    _stub_cancel_resume(orch, events, cancel_error=True)

    async def _run():
        with pytest.raises(asyncio.CancelledError):  # 不是 RuntimeError
            await orch._poll_container_supervised(
                "train_x", "c" * 64, tenant_id="t", user_id="u"
            )

    asyncio.run(_run())
    assert events == ["poll", "cancel"]  # 停容器失败不再做 resume，但取消照抛


def _docker_with_probe(probe_state, not_found=False):
    calls: list[str] = []

    def _get(name):
        calls.append(name)
        if not_found:
            # 用生产模块自己的 docker 引用（复审指正：裸 import docker 在没有
            # SDK / 被仓库 docker/ 目录遮蔽的环境会 AttributeError，落进泛
            # except 后 NotFound 分支根本验证不到）
            import backend.services.engine.training.local_docker_orchestrator as ldo

            raise ldo.docker.errors.NotFound("nf")
        probe = types.SimpleNamespace(
            id="cid123", attrs={"State": {"Status": probe_state}}
        )
        probe.reload = lambda: None
        return probe

    docker_fake = types.SimpleNamespace(containers=types.SimpleNamespace(get=_get))
    return docker_fake, calls


def test_cleanup_cancelled_provisioning_without_flag_is_noop():
    orch = _bare_docker_orch(FakeStream(cancel=False))
    docker_fake, calls = _docker_with_probe("running")
    orch.docker = docker_fake
    asyncio.run(orch.cleanup_cancelled_provisioning("train_p"))
    assert calls == []  # 无标记：连探针都不打


def test_cleanup_cancelled_provisioning_stops_running_container():
    orch = _bare_docker_orch(FakeStream(cancel=True))
    docker_fake, calls = _docker_with_probe("running")
    orch.docker = docker_fake
    killed: list[tuple] = []

    async def _cancel(run_id, container_id, tenant_id, user_id):
        killed.append((run_id, container_id, tenant_id, user_id))

    orch._cancel_container = _cancel
    resumed: list[tuple] = []
    orch._resume_others = lambda work_dir, run_id: (
        resumed.append((work_dir, run_id)) or []
    )

    asyncio.run(
        orch.cleanup_cancelled_provisioning("train_p", tenant_id="t", user_id="u")
    )
    assert calls == ["qm-train-train_p"]
    assert killed == [("train_p", "cid123", "t", "u")]
    assert resumed and resumed[0][1] == "train_p"  # 恢复被暂停的其它容器


def test_cleanup_cancelled_provisioning_container_gone_is_safe():
    orch = _bare_docker_orch(FakeStream(cancel=True))
    docker_fake, calls = _docker_with_probe("running", not_found=True)
    orch.docker = docker_fake
    killed: list[tuple] = []

    async def _cancel(*a):
        killed.append(a)

    orch._cancel_container = _cancel
    orch._resume_others = lambda work_dir, run_id: []

    asyncio.run(orch.cleanup_cancelled_provisioning("train_p"))  # 不抛
    assert killed == []  # NotFound → 无容器可杀
    assert calls == ["qm-train-train_p"]  # 先探测再决定

    # exited 状态的容器不视为「需停止」（已完成训练，等回调）
    orch2 = _bare_docker_orch(FakeStream(cancel=True))
    docker_fake2, _ = _docker_with_probe("exited")
    orch2.docker = docker_fake2
    orch2._cancel_container = _cancel
    orch2._resume_others = lambda work_dir, run_id: []
    asyncio.run(orch2.cleanup_cancelled_provisioning("train_p"))
    assert killed == []


def test_kill_container_when_created_with_flag_kills_and_resumes():
    """创建在途取消（HIGH-1 残窗）：containers.run 线程打不断 → 句柄浮现后补杀。"""
    events: list[str] = []
    orch = _bare_docker_orch(FakeStream(cancel=True))

    async def _cancel(run_id, container_id, tenant_id, user_id):
        events.append(f"cancel:{container_id}")

    orch._cancel_container = _cancel
    orch._resume_others = lambda work_dir, run_id: (
        events.append(f"resume:{run_id}") or []
    )

    async def _run():
        async def _create():
            await asyncio.sleep(0.01)  # 创建仍在途时取消先到
            return types.SimpleNamespace(id="cid-late")

        create_task = asyncio.ensure_future(_create())
        await orch._kill_container_when_created(
            "train_k", create_task, tenant_id="t", user_id="u", work_dir=None
        )

    asyncio.run(_run())
    assert events == ["cancel:cid-late", "resume:train_k"]


def test_kill_container_when_created_without_flag_leaves_it():
    """无标记（进程关停）：补杀不得动手——容器浮现后判尸按名可见、重挂接回。"""
    events: list[str] = []
    orch = _bare_docker_orch(FakeStream(cancel=False))

    async def _cancel(*a):
        events.append("cancel")

    orch._cancel_container = _cancel
    orch._resume_others = lambda work_dir, run_id: events.append("resume") or []

    async def _run():
        async def _create():
            return types.SimpleNamespace(id="cid-late")

        await orch._kill_container_when_created(
            "train_k", asyncio.ensure_future(_create()), tenant_id="t", user_id="u"
        )

    asyncio.run(_run())
    assert events == []


def test_kill_container_when_created_create_failed_is_silent():
    """创建最终失败：无资源可杀，补杀静默返回（不抛、不动容器接口）。"""
    orch = _bare_docker_orch(FakeStream(cancel=True))

    async def _cancel(*a):
        raise AssertionError("创建失败不应触发补杀")

    orch._cancel_container = _cancel

    async def _run():
        async def _create():
            raise RuntimeError("daemon down")

        await orch._kill_container_when_created(
            "train_k", asyncio.ensure_future(_create()), tenant_id="t", user_id="u"
        )  # 不抛

    asyncio.run(_run())


def test_cancel_container_db_row_locked(monkeypatch):
    """NEW-7：取消写 cancelled 走行锁（防与回调终态并发交错）。"""
    orch = _bare_docker_orch(FakeStream(cancel=True))
    removed: list = []
    probe = types.SimpleNamespace(
        attrs={"State": {"Status": "exited"}},
        reload=lambda: None,
        remove=lambda *a, **k: removed.append(a),
    )
    orch.docker = types.SimpleNamespace(
        containers=types.SimpleNamespace(get=lambda cid: probe)
    )
    record = types.SimpleNamespace(status="running", logs="", progress=5)
    session = _patch_session(monkeypatch, record)
    asyncio.run(orch._cancel_container("train_c", "c" * 64, "t", "u"))
    assert record.status == "cancelled"
    assert removed  # 容器照删（exited 不走 stop 分支）
    assert session.get_kwargs == [{"with_for_update": True}]
    assert orch.log_stream.cleared == ["train_c"]
    import backend.services.engine.training.orchestrator_base as ob

    assert ob.is_user_cancel_confirmed("train_c")  # NEW-B：杀灭即记录


def test_reattach_supervise_false_is_probe_only(monkeypatch):
    """CLI 重挂：只探活不建监管（注册的 task 会随 CLI 退出被取消→误删锁）。"""
    import backend.shared.redis_sentinel_client as rsc
    import backend.shared.training_singleflight as sf
    import backend.services.engine.training.local_docker_orchestrator as ldo

    orch = _bare_docker_orch(FakeStream(cancel=False))
    session = _patch_session(monkeypatch, record=None)
    registered: list = []
    monkeypatch.setattr(
        ldo,
        "REGISTRY",
        types.SimpleNamespace(
            register=lambda coro, run_id=None: (
                registered.append(run_id) or (coro.close() or types.SimpleNamespace())
            )
        ),
    )
    monkeypatch.setattr(ldo, "attach_singleflight_release", lambda t, r: None)
    # 不碰真 Redis：supervise=True 路径会做重挂补锁（get_holder → try_acquire），
    # 返回「他人持有」使补锁短路（本测试只关心注册/续租行为）
    monkeypatch.setattr(rsc, "get_redis_sentinel_client", lambda: object())
    monkeypatch.setattr(sf, "get_holder", lambda _r: "train_other")
    monkeypatch.setattr(sf, "try_acquire", lambda *a: pytest.fail("不应抢占他人锁"))
    renewals: list = []
    monkeypatch.setattr(
        sf, "start_renewal", lambda rid, ttl: renewals.append((rid, ttl))
    )

    asyncio.run(
        orch.reattach_training_job(
            run_id="train_re",
            payload={},
            container_id="c" * 64,
            supervise=False,
        )
    )
    assert registered == []  # 不注册轮询监管
    assert renewals == []  # 不启续租
    assert session.commits == 0  # 无行 → 无状态校准写入
    assert "probe-only" in orch.log_stream.appended[0]["line"]

    asyncio.run(
        orch.reattach_training_job(
            run_id="train_re",
            payload={},
            container_id="c" * 64,
            supervise=True,
        )
    )
    assert registered == ["train_re"]  # 默认路径建立监管
    assert renewals and renewals[0][0] == "train_re"  # 重挂即重续租（HIGH-4）


def _reattach_with_lock_stubs(monkeypatch, holder):
    """重挂 + 补锁观测装置：返回 (orch, events)。events 记录 holder/acquire/renew。"""
    import backend.shared.redis_sentinel_client as rsc
    import backend.shared.training_singleflight as sf
    import backend.services.engine.training.local_docker_orchestrator as ldo

    orch = _bare_docker_orch(FakeStream(cancel=False))
    _patch_session(monkeypatch, record=None)
    monkeypatch.setattr(
        ldo,
        "REGISTRY",
        types.SimpleNamespace(
            register=lambda coro, run_id=None: coro.close() or types.SimpleNamespace()
        ),
    )
    monkeypatch.setattr(ldo, "attach_singleflight_release", lambda t, r: None)
    events: list[tuple] = []

    def _get_holder(_redis):
        events.append(("holder",))
        return holder

    def _acquire(_redis, run_id, ttl):
        events.append(("acquire", run_id, ttl))
        return True

    def _renew(run_id, ttl):
        events.append(("renew", run_id, ttl))

    monkeypatch.setattr(rsc, "get_redis_sentinel_client", lambda: object())
    monkeypatch.setattr(sf, "get_holder", _get_holder)
    monkeypatch.setattr(sf, "try_acquire", _acquire)
    monkeypatch.setattr(sf, "start_renewal", _renew)
    return orch, events, sf


def test_reattach_reacquires_idle_lock_before_renewal(monkeypatch):
    """HIGH-4 残窗：停机期间锁被删/过期 → 重挂须先补锁再续租（续租救不回已删键）。"""
    orch, events, sf = _reattach_with_lock_stubs(monkeypatch, holder=None)
    asyncio.run(
        orch.reattach_training_job(
            run_id="train_lock", payload={}, container_id="c" * 64
        )
    )
    assert [e[0] for e in events] == ["holder", "acquire", "renew"]  # 补锁在续租前
    assert events[1][1] == "train_lock"
    assert events[1][2] == sf.lock_ttl_seconds(120)  # TTL 与预算同源
    assert events[2][1] == "train_lock"


def test_reattach_does_not_steal_foreign_lock(monkeypatch):
    """他人持有锁 → 不抢占（异常双跑让提交侧 409 语义收敛），只重启续租。"""
    orch, events, _ = _reattach_with_lock_stubs(monkeypatch, holder="train_other")
    asyncio.run(
        orch.reattach_training_job(
            run_id="train_lock", payload={}, container_id="c" * 64
        )
    )
    assert [e[0] for e in events] == ["holder", "renew"]  # 无 acquire


# ── process 编排器 ──────────────────────────────────────────────────────────


def _bare_process_orch(stream: FakeStream):
    orch = object.__new__(LocalProcessOrchestrator)
    orch.log_stream = stream
    return orch


def test_monitor_supervised_on_cancel_with_flag_kills_process():
    events: list[str] = []
    orch = _bare_process_orch(FakeStream(cancel=True))

    async def _monitor(
        run_id, proc, *, tenant_id, user_id, work_dir, max_time_minutes=120
    ):
        events.append("monitor")
        raise asyncio.CancelledError()

    async def _kill(run_id, proc, tenant_id, user_id):
        events.append("kill")

    orch._monitor_process = _monitor
    orch._cancel_process = _kill

    async def _run():
        with pytest.raises(asyncio.CancelledError):
            await orch._monitor_process_supervised(
                "train_p",
                object(),
                tenant_id="t",
                user_id="u",
                work_dir=None,
            )

    asyncio.run(_run())
    assert events == ["monitor", "kill"]


def test_monitor_supervised_on_shutdown_without_flag_keeps_process(monkeypatch):
    import backend.services.engine.training.orchestrator_base as ob

    marks: set = set()
    monkeypatch.setattr(ob, "_GRACEFUL_STOPS", marks)
    events: list[str] = []
    orch = _bare_process_orch(FakeStream(cancel=False))

    async def _monitor(
        run_id, proc, *, tenant_id, user_id, work_dir, max_time_minutes=120
    ):
        events.append("monitor")
        raise asyncio.CancelledError()

    async def _kill(*a):
        events.append("kill")

    orch._monitor_process = _monitor
    orch._cancel_process = _kill

    async def _run():
        with pytest.raises(asyncio.CancelledError):
            await orch._monitor_process_supervised(
                "train_p", object(), tenant_id="t", user_id="u", work_dir=None
            )

    asyncio.run(_run())
    assert events == ["monitor"]
    assert marks == {"train_p"}  # 关停保锁标记（HIGH-4）


def test_monitor_supervised_cleanup_failure_does_not_mask_cancel():
    """NEW-5：取消处理器里的清理异常不得顶替 CancelledError（吞掉取消）。"""
    events: list[str] = []
    orch = _bare_process_orch(FakeStream(cancel=True))

    async def _monitor(
        run_id, proc, *, tenant_id, user_id, work_dir, max_time_minutes=120
    ):
        raise asyncio.CancelledError()

    async def _kill(run_id, proc, tenant_id, user_id):
        events.append("kill")
        raise RuntimeError("db hiccup")

    orch._monitor_process = _monitor
    orch._cancel_process = _kill

    async def _run():
        with pytest.raises(asyncio.CancelledError):  # 不是 RuntimeError
            await orch._monitor_process_supervised(
                "train_p", object(), tenant_id="t", user_id="u", work_dir=None
            )

    asyncio.run(_run())
    assert events == ["kill"]


def test_kill_proc_when_spawned_with_flag_cancels(monkeypatch):
    """spawn 在途取消（HIGH-1 残窗）：句柄浮现后补 _cancel_process（杀+落态+清标记）。"""
    events: list[str] = []
    orch = _bare_process_orch(FakeStream(cancel=True))

    class _Proc:
        returncode = None

        def kill(self):
            events.append("kill")
            self.returncode = -9

        async def wait(self):
            return -9

    record = types.SimpleNamespace(status="running", logs="", progress=0)
    session = _patch_session(monkeypatch, record)

    async def _run():
        async def _spawn():
            await asyncio.sleep(0.01)  # spawn 在途时取消先到
            return _Proc()

        await orch._kill_proc_when_spawned(
            "train_k", asyncio.ensure_future(_spawn()), tenant_id="t", user_id="u"
        )

    asyncio.run(_run())
    assert events == ["kill"]
    assert record.status == "cancelled"
    assert session.get_kwargs[-1].get("with_for_update") is True  # NEW-7
    assert orch.log_stream.cleared == ["train_k"]


def test_kill_proc_when_spawned_without_flag_leaves_proc():
    events: list[str] = []
    orch = _bare_process_orch(FakeStream(cancel=False))

    async def _kill(*a):
        events.append("kill")

    orch._cancel_process = _kill

    class _Proc:
        pass

    async def _run():
        async def _spawn():
            return _Proc()

        await orch._kill_proc_when_spawned(
            "train_k", asyncio.ensure_future(_spawn()), tenant_id="t", user_id="u"
        )

    asyncio.run(_run())
    assert events == []  # 无标记：留给重启后判尸/回调收敛


def test_kill_proc_when_spawned_spawn_failed_is_silent():
    orch = _bare_process_orch(FakeStream(cancel=True))

    async def _kill(*a):
        raise AssertionError("spawn 失败不应触发补杀")

    orch._cancel_process = _kill

    async def _run():
        async def _spawn():
            raise FileNotFoundError("python gone")

        await orch._kill_proc_when_spawned(
            "train_k", asyncio.ensure_future(_spawn()), tenant_id="t", user_id="u"
        )  # 不抛

    asyncio.run(_run())


def test_cancel_process_kills_sets_status_and_clears_flag(monkeypatch):
    orch = _bare_process_orch(FakeStream(cancel=True))
    killed: list[int] = []

    class _Proc:
        returncode = None

        def kill(self):
            killed.append(1)
            self.returncode = -9

        async def wait(self):
            return -9

    record = types.SimpleNamespace(status="running", logs="", progress=7)
    session = _patch_session(monkeypatch, record)
    asyncio.run(orch._cancel_process("train_p", _Proc(), "t", "u"))
    assert killed == [1]
    assert record.status == "cancelled"
    assert "训练已被用户取消" in record.logs
    assert session.commits == 1
    assert session.get_kwargs == [{"with_for_update": True}]  # NEW-7 行锁
    assert orch.log_stream.cleared == ["train_p"]
    assert orch.log_stream.appended[0]["status"] == "cancelled"
    import backend.services.engine.training.orchestrator_base as ob

    assert ob.is_user_cancel_confirmed("train_p")  # NEW-B：杀灭即记录


def test_cancel_process_terminal_row_not_flipped(monkeypatch):
    """竞态：进程退出后回调已落 completed → 取消不得把终态翻回 cancelled。"""
    orch = _bare_process_orch(FakeStream(cancel=True))

    class _Proc:
        returncode = 0

        def kill(self):
            raise AssertionError("已退出的进程不应被 kill")

        async def wait(self):
            return 0

    record = types.SimpleNamespace(status="completed", logs="done", progress=100)
    session = _patch_session(monkeypatch, record)
    asyncio.run(orch._cancel_process("train_p", _Proc(), "t", "u"))
    assert record.status == "completed"
    assert session.commits == 0
    assert orch.log_stream.cleared == ["train_p"]  # 标记仍要清


def test_process_cleanup_cancelled_provisioning_is_noop():
    orch = _bare_process_orch(FakeStream(cancel=False))
    assert (
        asyncio.run(orch.cleanup_cancelled_provisioning("train_p", tenant_id="t"))
        is None
    )


# ── remote 编排器 ───────────────────────────────────────────────────────────


def _bare_remote_orch(stream: FakeStream, *, executor="docker", exec_mode="ssh_docker"):
    orch = object.__new__(RemoteSSHOrchestrator)
    orch.log_stream = stream
    orch.executor = executor
    orch.exec_mode = exec_mode
    orch.work_dir = "/workspace"
    orch._log = lambda *a, **k: None
    return orch


def test_remote_guarded_cancel_only_with_flag(monkeypatch):
    import backend.services.engine.training.orchestrator_base as ob

    marks: set = set()
    monkeypatch.setattr(ob, "_GRACEFUL_STOPS", marks)
    calls: list[tuple] = []
    orch = _bare_remote_orch(FakeStream(cancel=False))

    async def _cancel(run_id, run_key):
        calls.append((run_id, run_key))

    orch._cancel_remote = _cancel
    asyncio.run(orch._cancel_remote_guarded("train_r", "qm-train-train_r"))
    assert calls == []  # 无标记（进程关停）→ 不动远端资源
    assert marks == {"train_r"}  # 且置关停保锁标记（HIGH-4）

    orch.log_stream.cancel = True
    asyncio.run(orch._cancel_remote_guarded("train_r", "qm-train-train_r"))
    assert calls == [("train_r", "qm-train-train_r")]


def test_remote_guarded_cancel_swallows_errors():
    orch = _bare_remote_orch(FakeStream(cancel=True))

    async def _cancel(run_id, run_key):
        raise RuntimeError("ssh down")

    orch._cancel_remote = _cancel
    asyncio.run(orch._cancel_remote_guarded("train_r", "k"))  # 不抛


def test_remote_cancel_process_mode_kills_pid(monkeypatch):
    """executor=process（节点包 run_one.sh）：按 train.pid 杀——旧实现落进
    docker 分支空杀（docker stop 对不存在容器是 no-op），远端训练永不停止。"""
    orch = _bare_remote_orch(FakeStream(cancel=True), executor="process")
    cmds: list[str] = []

    async def _ssh_exec(cmd, timeout=120):
        cmds.append(cmd)
        return 0, "", ""

    orch._ssh_exec = _ssh_exec
    session = _patch_session(monkeypatch, record=None)
    asyncio.run(orch._cancel_remote("train_r", "qm-train-train_r"))
    assert len(cmds) == 1
    assert "train.pid" in cmds[0]
    assert "kill -9" in cmds[0]
    assert "docker stop" not in cmds[0]  # 不是 docker 分支
    assert ".qm_active_run" in cmds[0]  # NEW-A：共享 pid 文件走归属守卫
    assert orch.log_stream.cleared == ["train_r"]
    assert session.get_kwargs == [{"with_for_update": True}]  # NEW-7 行锁
    import backend.services.engine.training.orchestrator_base as ob

    assert ob.is_user_cancel_confirmed("train_r")  # NEW-B：杀灭即记录


def test_remote_cancel_docker_mode_stops_container(monkeypatch):
    orch = _bare_remote_orch(FakeStream(cancel=True), executor="docker")
    cmds: list[str] = []

    async def _ssh_exec(cmd, timeout=120):
        cmds.append(cmd)
        return 0, "", ""

    orch._ssh_exec = _ssh_exec
    _patch_session(monkeypatch, record=None)
    asyncio.run(orch._cancel_remote("train_r", "qm-train-train_r"))
    assert "docker stop qm-train-train_r" in cmds[0]
    assert "train.pid" not in cmds[0]


def test_remote_cleanup_provisioning_key_by_mode(monkeypatch):
    """provisioning 窗口清理：run_key 按模式选择（native vs docker/process）。"""
    import backend.services.engine.training.remote_ssh_orchestrator as rso

    spawned: list = []
    monkeypatch.setattr(
        rso, "spawn_compensation", lambda coro: (spawned.append(coro), coro.close())
    )
    calls: list[tuple] = []
    orch = _bare_remote_orch(FakeStream(cancel=True), executor="docker")

    async def _guarded(run_id, run_key):
        calls.append((run_id, run_key))

    orch._cancel_remote_guarded = _guarded
    asyncio.run(orch.cleanup_cancelled_provisioning("train_r"))
    assert calls == [("train_r", "qm-train-train_r")]
    assert len(spawned) == 1  # 用户取消 → 发射创建在途重杀（HIGH-1 残窗）

    orch2 = _bare_remote_orch(FakeStream(cancel=True), exec_mode="native_python")
    orch2._cancel_remote_guarded = _guarded
    asyncio.run(orch2.cleanup_cancelled_provisioning("train_r"))
    assert calls[-1] == ("train_r", "native-train_r")
    assert len(spawned) == 2


def test_remote_cleanup_spawns_retry_kill_on_user_cancel(monkeypatch):
    """创建在途残窗（HIGH-1）：远端启动命令不受本地取消影响，首杀可能是空枪，
    须发射延时重杀覆盖资源浮现窗口。"""
    import backend.services.engine.training.remote_ssh_orchestrator as rso

    spawned: list = []
    monkeypatch.setattr(rso, "spawn_compensation", lambda coro: spawned.append(coro))
    orch = _bare_remote_orch(FakeStream(cancel=True), executor="docker")
    orch._KILL_RETRY_ATTEMPTS = 2
    orch._KILL_RETRY_DELAY_SECONDS = 0.01

    async def _guarded(run_id, run_key):
        return None

    kills: list[tuple] = []

    async def _kill(run_id, run_key):
        kills.append((run_id, run_key))

    orch._cancel_remote_guarded = _guarded
    orch._kill_remote_resource = _kill

    async def _run():
        await orch.cleanup_cancelled_provisioning("train_r")
        assert len(spawned) == 1
        await spawned[0]  # 驱动重杀循环跑满

    asyncio.run(_run())
    assert kills == [("train_r", "qm-train-train_r")] * 2  # 幂等重发


def test_remote_cleanup_shutdown_no_retry_no_flag(monkeypatch):
    """无用户取消标记（进程关停）：不动远端资源，也不发射重杀。"""
    import backend.services.engine.training.remote_ssh_orchestrator as rso

    spawned: list = []
    monkeypatch.setattr(rso, "spawn_compensation", lambda coro: spawned.append(coro))
    orch = _bare_remote_orch(FakeStream(cancel=False), executor="docker")
    calls: list[tuple] = []

    async def _guarded(run_id, run_key):
        calls.append((run_id, run_key))

    orch._cancel_remote_guarded = _guarded
    asyncio.run(orch.cleanup_cancelled_provisioning("train_r"))
    assert calls == [("train_r", "qm-train-train_r")]  # 仍走 guarded（内部判标记）
    assert spawned == []


# ── orchestrator_base：单飞释放回调实路径 + 关停保锁（复审 HIGH-4）────────────


def _release_probe(monkeypatch):
    """真实 attach_singleflight_release 的观测装置：返回 released 列表。"""
    import backend.shared.redis_sentinel_client as rsc
    import backend.shared.training_singleflight as sf
    import backend.services.engine.training.orchestrator_base as ob

    monkeypatch.setattr(ob, "REGISTRY", ob.TrainingTaskRegistry())
    monkeypatch.setattr(ob, "_GRACEFUL_STOPS", set())
    released: list[str] = []
    monkeypatch.setattr(sf, "release", lambda _r, rid: released.append(rid))
    monkeypatch.setattr(rsc, "get_redis_sentinel_client", lambda: object())
    return released


def test_release_callback_releases_when_run_unmanaged(monkeypatch):
    """核心闸门（复审指正：此前测试 stub 掉回调，实路径无人钉）：
    run 不再受管 → 释放锁。"""
    import backend.services.engine.training.orchestrator_base as ob

    released = _release_probe(monkeypatch)

    async def _run():
        async def _noop():
            return None

        t = asyncio.get_running_loop().create_task(_noop())
        ob.attach_singleflight_release(t, "train_free")
        await t
        await asyncio.sleep(0)  # done 回调执行

    asyncio.run(_run())
    assert released == ["train_free"]


def test_release_callback_keeps_lock_while_managed(monkeypatch):
    """run 仍有受管 task（轮询在管）→ launch task 结束也不释放。"""
    import backend.services.engine.training.orchestrator_base as ob

    released = _release_probe(monkeypatch)

    async def _run():
        loop = asyncio.get_running_loop()
        blocker = loop.create_task(asyncio.sleep(1))
        ob.REGISTRY.register(blocker, run_id="train_busy")

        async def _noop():
            return None

        t = loop.create_task(_noop())
        ob.attach_singleflight_release(t, "train_busy")
        await t
        await asyncio.sleep(0)
        blocker.cancel()
        try:
            await blocker
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert released == []


def test_release_callback_keeps_lock_on_graceful_stop(monkeypatch):
    """优雅关停（无用户取消标记）→ 不释放：锁留给重启后重挂续租/补锁
    （HIGH-4 残窗：删了锁，续租 CAS 救不回已删键）。"""
    import backend.services.engine.training.orchestrator_base as ob

    released = _release_probe(monkeypatch)
    monkeypatch.setattr(ob, "_GRACEFUL_STOPS", {"train_gs"})  # 模拟关停标记

    async def _run():
        async def _noop():
            return None

        t = asyncio.get_running_loop().create_task(_noop())
        ob.attach_singleflight_release(t, "train_gs")
        await t
        await asyncio.sleep(0)

    asyncio.run(_run())
    assert released == []


def test_supervised_launch_marks_graceful_stop_only_without_flag(monkeypatch):
    """launch 窗口取消：无用户标记（进程关停）→ 置保锁标记；用户取消 → 不置。

    NEW-B：内层 ``_cancel_*`` 杀灭后清 Redis 标记，基础层须用进程内确认集合
    补判，否则用户取消被误判成关停（多保锁）。
    NEW-C：标记必须先于 cleanup await 置好——二次取消不会留下 HIGH-4 窗口。
    """
    import backend.services.engine.training.orchestrator_base as ob

    marks: set = set()
    monkeypatch.setattr(ob, "_GRACEFUL_STOPS", marks)
    cleaned: list[str] = []
    mark_at_cleanup: list[bool] = []

    class _Stream:
        def __init__(self, cancel):
            self.cancel = cancel

        def is_cancel_requested(self, run_id):
            return self.cancel

    class _Orch:
        def __init__(self, cancel):
            self.log_stream = _Stream(cancel)

        async def launch_training_job(self, *, run_id, payload):
            raise asyncio.CancelledError()

        async def cleanup_cancelled_provisioning(self, run_id, *, tenant_id, user_id):
            cleaned.append(run_id)
            mark_at_cleanup.append(ob.is_graceful_stop(run_id))

    async def _run(run_id, cancel):
        with pytest.raises(asyncio.CancelledError):
            await ob.supervised_launch(_Orch(cancel), run_id=run_id, payload={})

    asyncio.run(_run("train_sl1", cancel=False))
    assert cleaned == ["train_sl1"]  # cleanup 照走（内部自判标记）
    assert marks == {"train_sl1"}  # 关停 → 保锁标记（HIGH-4）
    assert mark_at_cleanup == [True]  # NEW-C：cleanup 时标记已在

    asyncio.run(_run("train_sl2", cancel=True))
    assert cleaned == ["train_sl1", "train_sl2"]
    assert marks == {"train_sl1"}  # 用户取消：不新增（正常走释放路径）

    # NEW-B：Redis 标记已被内层 clear（用 cancel=False 模拟读不到），但本进程
    # 已确认用户取消 → 不得置保锁标记。
    ob.mark_user_cancel_confirmed("train_sl3")
    asyncio.run(_run("train_sl3", cancel=False))
    assert marks == {"train_sl1"}
    assert mark_at_cleanup[-1] is False  # 确认取消 → 无标记可置


def test_spawn_compensation_runs_and_retires(monkeypatch):
    import backend.services.engine.training.orchestrator_base as ob

    async def _run():
        done: list[str] = []

        async def _ok():
            done.append("ok")

        t = ob.spawn_compensation(_ok())
        assert t is not None
        assert t in ob._COMPENSATION_TASKS  # 强引用防 GC
        await t
        await asyncio.sleep(0)
        assert done == ["ok"]
        assert t not in ob._COMPENSATION_TASKS  # done 回调已清账

        async def _boom():
            raise RuntimeError("boom")

        t2 = ob.spawn_compensation(_boom())
        await asyncio.sleep(0.01)  # 不 await（await 会重抛）；异常由回调消费
        assert t2.done() and not t2.cancelled()
        assert t2 not in ob._COMPENSATION_TASKS

    asyncio.run(_run())


def test_spawn_compensation_without_running_loop_returns_none():
    import backend.services.engine.training.orchestrator_base as ob

    async def _c():
        return None

    coro = _c()
    assert ob.spawn_compensation(coro) is None  # 同步收尾：告警跳过
    coro.close()  # 防 never-awaited 警告


# ── schedule_ctl 回收入口：CLI 只探测不监管（复审 MEDIUM-6/LOW-10）────────────


def test_schedule_ctl_training_reaper_is_probe_only(monkeypatch):
    """CLI 进程转瞬退出：不得建监管/挂释放回调（会误删 API 持有的单飞锁），
    也不得写 C07 心跳（掩盖 API 内循环停摆）。"""
    import backend.scripts.schedule_ctl as sc
    import backend.services.engine.training.job_reaper as jr
    import backend.shared.database_manager_v2 as dbm

    captured: dict = {}

    async def _reconcile(*, apply, **kw):
        captured["apply"] = apply
        captured.update(kw)
        return {
            "scanned": 1,
            "reattached": 0,
            "failed": 0,
            "planned_reattach": ["train_live"],
            "skipped": {},
            "errors": [],
        }

    async def _close():
        return None

    monkeypatch.setattr(jr, "reconcile_training_jobs", _reconcile)
    monkeypatch.setattr(dbm, "close_database", _close)
    rc = sc._run_training_reaper(None, False)
    assert rc == 0
    assert captured == {
        "apply": True,
        "write_heartbeat": False,
        "supervise_reattach": False,
    }


# ── 远端 process 模式归属守卫（复审 NEW-A）──────────────────────────────────


def test_remote_process_kill_guarded_by_workspace_marker(tmp_path):
    """NEW-A：process 模式 train.pid 是共享 workspace 固定名——重杀/清理仅当
    归属标记仍指向本 run（或缺失=修复前启动的旧运行）才动作，防尾巴误杀接管
    workspace 的新 run（真机「交错误杀」事故同族）。

    用真 bash 执行编排器生成的命令串，覆盖三种归属的真实行为。
    """
    import subprocess

    orch = _bare_remote_orch(FakeStream(cancel=True), executor="process")
    orch.work_dir = str(tmp_path)
    cmds: list[str] = []

    async def _ssh_exec(cmd, timeout=120):
        cmds.append(cmd)
        return 0, "", ""

    orch._ssh_exec = _ssh_exec
    asyncio.run(orch._kill_remote_resource("train_a", "qm-train-train_a"))
    assert len(cmds) == 1
    kill_cmd = cmds[0]
    assert ".qm_active_run" in kill_cmd  # 归属守卫在命令里

    pid_file = tmp_path / "train.pid"
    marker = tmp_path / ".qm_active_run"

    def _spawn_and_write_pid():
        proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
        pid_file.write_text(str(proc.pid))
        return proc

    # ① 标记=本 run → 杀进程 + 清 pid 文件
    proc_a = _spawn_and_write_pid()
    marker.write_text("train_a")
    subprocess.run(["bash", "-c", kill_cmd], check=True)
    proc_a.wait(timeout=5)
    assert proc_a.returncode is not None
    assert not pid_file.exists()

    # ② 标记=新 run B（已接管 workspace）→ B 的进程与 pid 文件都不许碰
    proc_b = _spawn_and_write_pid()
    marker.write_text("train_b")
    subprocess.run(["bash", "-c", kill_cmd], check=True)
    assert proc_b.poll() is None  # B 存活
    assert pid_file.exists()  # B 的 pid 文件保留（轮询还要读）
    proc_b.kill()
    proc_b.wait()

    # ③ 标记缺失（本次修复前启动的旧运行）→ 兼容：照杀
    proc_c = _spawn_and_write_pid()
    marker.unlink()
    subprocess.run(["bash", "-c", kill_cmd], check=True)
    proc_c.wait(timeout=5)
    assert proc_c.returncode is not None
    assert not pid_file.exists()


def test_remote_process_launch_writes_marker_before_run_one():
    """NEW-A：启动命令先写归属标记、再跑 run_one.sh——保证「run_one.sh 跑过
    ⇒ 标记=本 run」，尾巴守卫据此对已接管的新 run 天然失效。"""
    orch = _bare_remote_orch(FakeStream(), executor="process")
    orch.pack_root = "/pack"
    cmds: list[str] = []

    async def _ssh_exec(cmd, timeout=120):
        cmds.append(cmd)
        return 0, "", ""

    orch._ssh_exec = _ssh_exec
    code, out, err = asyncio.run(
        orch._launch_process_job("train_m", "qm-train-train_m", direct_source="remote")
    )
    assert (code, out, err) == (0, "", "")
    assert len(cmds) == 1
    cmd = cmds[0]
    assert ".qm_active_run" in cmd and "train_m" in cmd
    assert cmd.index(".qm_active_run") < cmd.index("run_one.sh")


# ── 取消标记失败面（复审 NEW-E）──────────────────────────────────────────────


def test_mark_cancel_requested_reports_write_result(monkeypatch):
    """NEW-E：置标记返回是否确认落库——False 时取消端点须走直杀兜底。"""
    from backend.services.engine.training.training_log_stream import (
        TrainingRunLogStream,
    )

    stream = TrainingRunLogStream()
    monkeypatch.setattr(stream, "_get_client", lambda: None)
    assert stream.mark_cancel_requested("train_e") is False  # Redis 不可用

    class _Ok:
        def setex(self, *a):
            return True

    monkeypatch.setattr(stream, "_get_client", lambda: _Ok())
    assert stream.mark_cancel_requested("train_e") is True

    class _Boom:
        def setex(self, *a):
            raise RuntimeError("redis down")

    monkeypatch.setattr(stream, "_get_client", lambda: _Boom())
    assert stream.mark_cancel_requested("train_e") is False


def test_get_client_retries_after_failure_cooldown(monkeypatch):
    """NEW-E：初始化失败不再永久拉黑（旧实现一次瞬断 → 取消标记整个进程生命
    周期静默失效）——冷却后重试，可自愈。"""
    import time as _time

    import backend.services.engine.training.training_log_stream as tls

    stream = tls.TrainingRunLogStream()
    stream.enabled = True
    attempts: list[int] = []

    class _Bad:
        def __init__(self, **kw):
            attempts.append(1)
            raise RuntimeError("redis down")

    monkeypatch.setattr(tls, "redis_lib", types.SimpleNamespace(Redis=_Bad))
    assert stream._get_client() is None
    assert len(attempts) == 1
    assert stream._get_client() is None  # 冷却期内不重试
    assert len(attempts) == 1
    stream._client_init_failed_at = _time.monotonic() - 3600  # 冷却已过期
    assert stream._get_client() is None
    assert len(attempts) == 2  # 已重试
