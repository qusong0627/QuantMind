from __future__ import annotations

from dataclasses import dataclass, field

from backend.services.trade.sandbox.manager import SandboxPlatformManager


@dataclass
class _FakeProcess:
    pid: int
    alive: bool = True
    joined: bool = False

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        self.joined = True
        self.alive = False

    def terminate(self) -> None:
        self.alive = False


@dataclass
class _FakeQueue:
    items: list[object] = field(default_factory=list)
    closed: bool = False

    def put(self, item: object) -> None:
        self.items.append(item)

    def close(self) -> None:
        self.closed = True


def _build_manager() -> SandboxPlatformManager:
    SandboxPlatformManager._instance = None
    return SandboxPlatformManager(pool_size=2)


def test_submit_strategy_prefers_idle_worker() -> None:
    manager = _build_manager()
    manager._workers = {11: _FakeProcess(pid=11), 12: _FakeProcess(pid=12)}  # type: ignore[assignment]
    manager._task_queues = {11: _FakeQueue(), 12: _FakeQueue()}  # type: ignore[assignment]
    manager._active_runs = {"default_u1_s1": 11}
    manager._ensure_pool_capacity = lambda: None  # type: ignore[method-assign]

    run_id = manager.submit_strategy(
        tenant_id="default",
        user_id="u2",
        strategy_id="s2",
        code_str="print('ok')",
        exec_config={},
    )

    assert run_id
    assert manager._active_runs["default_u2_s2"] == 12
    queue = manager._task_queues[12]
    assert isinstance(queue, _FakeQueue)
    assert len(queue.items) == 1


def test_is_strategy_running_cleans_stale_mapping() -> None:
    manager = _build_manager()
    manager._workers = {21: _FakeProcess(pid=21, alive=False)}  # type: ignore[assignment]
    manager._task_queues = {21: _FakeQueue()}  # type: ignore[assignment]
    manager._active_runs = {"default_u1_s1": 21}

    running = manager.is_strategy_running("default", "u1", "s1")
    assert running is False
    assert "default_u1_s1" not in manager._active_runs


def test_stop_strategy_refuses_to_kill_shared_worker(monkeypatch) -> None:
    manager = _build_manager()
    manager._workers = {31: _FakeProcess(pid=31)}  # type: ignore[assignment]
    manager._task_queues = {31: _FakeQueue()}  # type: ignore[assignment]
    manager._active_runs = {"default_u1_s1": 31, "default_u2_s2": 31}

    killed: list[int] = []
    monkeypatch.setattr("backend.services.trade.sandbox.manager.os.kill", lambda pid, sig: killed.append(pid))

    stopped = manager.stop_strategy("default", "u1", "s1")
    assert stopped is False
    assert killed == []
    assert "default_u1_s1" not in manager._active_runs
    assert manager._active_runs["default_u2_s2"] == 31
