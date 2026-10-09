"""挖掘任务中心 × launcher 接线（机构级 P0 / T-FM-02）。

launcher 是任务的唯一生产者，本文件锁它与 ``rd_agent_mining_tasks`` 的接线：

1. **direction 全链路在手**：构造 → 内存 → 磁盘 task_state.json → 状态接口，
   每一环都不能丢（历史页与监控器「在挖什么」全靠它）；
2. **DB 写失败绝不拦挖掘**：任务表是记录层不是主链路——store 抛任何错都只
   告警（挖矿子进程照跑，最坏是历史页缺这一行）；
3. **进度节流**：3s 内存心跳不变，DB 写 ≥15s 一次（轮询写库会把库压垮）；
4. **取消语义**：cancel 落 ``cancelled``，且随后的终态收尾不得把它改写成
   ``failed``（那会让历史页把「我取消的」和「跑挂了」混为一谈）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent import launcher as launcher_module  # noqa: E402
from backend.services.engine.alpha_agent.launcher import (  # noqa: E402
    AlphaAgentLauncher,
    EvolutionTask,
    TaskStatus,
)


class _RecordingStore:
    """记录调用参数的假 store。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def create_task(self, **kwargs) -> None:
        self.calls.append(("create", kwargs))

    async def update_progress(self, task_id: str, **kwargs) -> None:
        self.calls.append(("progress", task_id, kwargs))

    async def mark_terminal(self, task_id: str, **kwargs) -> None:
        self.calls.append(("terminal", task_id, kwargs))

    async def count_factors(self, task_id: str) -> int:
        self.calls.append(("count", task_id))
        return 5


class _BoomStore:
    """任何调用都炸的假 store。"""

    async def create_task(self, **kwargs):
        raise RuntimeError("db down")

    async def update_progress(self, task_id, **kwargs):
        raise RuntimeError("db down")

    async def mark_terminal(self, task_id, **kwargs):
        raise RuntimeError("db down")

    async def count_factors(self, task_id):
        raise RuntimeError("db down")


def _launcher_with(log_dir: Path | None = None) -> AlphaAgentLauncher:
    obj = AlphaAgentLauncher.__new__(AlphaAgentLauncher)
    obj._tasks = {}
    obj._log_dir = log_dir
    return obj


def _task(**overrides) -> EvolutionTask:
    base: dict = {
        "task_id": "t-1",
        "user_id": "u-1",
        "status": TaskStatus.RUNNING,
        "direction": "动量 × 波动率",
    }
    base.update(overrides)
    return EvolutionTask(**base)


# ── direction 全链路 ─────────────────────────────────────────────────


def test_direction_survives_disk_roundtrip(tmp_path: Path) -> None:
    """磁盘 task_state.json 也要带 direction——否则重启后历史只剩一个空方向。"""
    writer = _launcher_with(tmp_path)
    writer._persist_task(_task())

    reader = _launcher_with(tmp_path)
    reader._load_tasks()

    assert reader._tasks["t-1"].direction == "动量 × 波动率"


@pytest.mark.asyncio
async def test_status_payload_carries_direction() -> None:
    """监控器/history 的「在挖什么」从这里取；老任务没有该字段时给空串。"""
    launcher = _launcher_with()
    launcher._tasks["t-1"] = _task()
    launcher._tasks["t-2"] = EvolutionTask(task_id="t-2", user_id="u-1")

    s1 = await launcher.get_task_status("t-1")
    s2 = await launcher.get_task_status("t-2")
    assert s1["direction"] == "动量 × 波动率"
    assert s2["direction"] == ""


# ── DB 接线 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db_create_forwards_identity_and_source(monkeypatch) -> None:
    store = _RecordingStore()
    monkeypatch.setattr(launcher_module, "_task_store", lambda: store)
    launcher = _launcher_with()

    task = _task(
        task_id="t-d2",
        user_id="u-9",
        market="crypto",
        universe="csi300",
        data_source="parquet",
    )
    await launcher._db_create(task, source="doc", doc_id="doc-1")

    kind, kwargs = store.calls[0]
    assert kind == "create"
    assert kwargs["task_id"] == "t-d2" and kwargs["user_id"] == "u-9"
    assert kwargs["direction"] == "动量 × 波动率"
    assert kwargs["source"] == "doc" and kwargs["doc_id"] == "doc-1"
    assert kwargs["market"] == "crypto" and kwargs["loop_n"] == task.loop_n


@pytest.mark.asyncio
async def test_db_progress_is_throttled(monkeypatch) -> None:
    """刚同步过就再心跳 → 不写库；间隔够了 → 写且带上进度。"""
    store = _RecordingStore()
    monkeypatch.setattr(launcher_module, "_task_store", lambda: store)
    launcher = _launcher_with()
    task = _task(progress_pct=42, current_loop=2)

    task._last_db_sync = time.time()
    await launcher._db_sync_progress_throttled(task)
    assert store.calls == [], "15s 内的心跳不该再写库"

    task._last_db_sync = time.time() - 100
    await launcher._db_sync_progress_throttled(task)
    kind, task_id, kwargs = store.calls[0]
    assert (kind, task_id) == ("progress", "t-1")
    assert kwargs["status"] == "running"
    assert kwargs["progress_pct"] == 42 and kwargs["current_loop"] == 2


@pytest.mark.asyncio
async def test_db_finish_counts_factors_and_writes_terminal(monkeypatch) -> None:
    store = _RecordingStore()
    monkeypatch.setattr(launcher_module, "_task_store", lambda: store)
    launcher = _launcher_with()
    task = _task(status=TaskStatus.COMPLETED, progress_pct=100)

    await launcher._db_finish(task)

    kinds = [c[0] for c in store.calls]
    assert kinds == ["count", "terminal"]
    _, task_id, kwargs = store.calls[1]
    assert kwargs["status"] == "completed" and kwargs["factor_count"] == 5
    assert kwargs["error"] is None


@pytest.mark.asyncio
async def test_db_finish_is_skipped_after_cancel(monkeypatch) -> None:
    """取消已落 cancelled；随后的普通收尾不得把它改写回 failed。"""
    store = _RecordingStore()
    monkeypatch.setattr(launcher_module, "_task_store", lambda: store)
    launcher = _launcher_with()
    task = _task(status=TaskStatus.FAILED, error_message="Cancelled by user")
    task._cancel_requested = True

    await launcher._db_finish(task)

    assert store.calls == []


@pytest.mark.asyncio
async def test_db_cancel_writes_cancelled_status(monkeypatch) -> None:
    store = _RecordingStore()
    monkeypatch.setattr(launcher_module, "_task_store", lambda: store)
    launcher = _launcher_with()

    await launcher._db_cancel(_task())

    kind, task_id, kwargs = store.calls[0]
    assert (kind, task_id) == ("terminal", "t-1")
    assert kwargs["status"] == "cancelled"


@pytest.mark.asyncio
async def test_db_failures_never_raise(monkeypatch) -> None:
    """记录层爆炸最多留一行 warning——绝不允许把挖掘主链路拖下水。"""
    monkeypatch.setattr(launcher_module, "_task_store", lambda: _BoomStore())
    launcher = _launcher_with()
    task = _task()

    await launcher._db_create(task)
    await launcher._db_sync_progress_throttled(task)  # 首次必写 → 触发 store 异常
    await launcher._db_finish(task)
    await launcher._db_cancel(task)


# ── start_evolution 端到端接线（子进程与后台协程均不打真） ───────────


@pytest.mark.asyncio
async def test_start_evolution_records_direction_and_source(monkeypatch) -> None:
    store = _RecordingStore()
    monkeypatch.setattr(launcher_module, "_task_store", lambda: store)

    from backend.services.engine.alpha_agent import hw_lock

    monkeypatch.setattr(hw_lock, "assert_factor_mining_hardware", lambda: None)

    def _noop_ensure_future(coro):
        coro.close()  # 关掉未等待的协程，避免 RuntimeWarning
        return None

    monkeypatch.setattr(launcher_module.asyncio, "ensure_future", _noop_ensure_future)

    launcher = _launcher_with()
    task_id = await launcher.start_evolution(
        "u-1", direction="动量 × 波动率", source="doc", doc_id="doc-9"
    )

    task = launcher._tasks[task_id]
    assert task.direction == "动量 × 波动率", "内存任务也要带着方向（监控器直接读它）"

    kind, kwargs = store.calls[0]
    assert kind == "create" and kwargs["task_id"] == task_id
    assert kwargs["direction"] == "动量 × 波动率"
    assert kwargs["source"] == "doc" and kwargs["doc_id"] == "doc-9"
