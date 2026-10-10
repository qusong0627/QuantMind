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

import json
import os
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
async def test_db_create_forwards_direction_mode(monkeypatch) -> None:
    """方向模式（T-MV-02）：类别选择路径要随之落档，历史页能看到「随机/选定」。"""
    store = _RecordingStore()
    monkeypatch.setattr(launcher_module, "_task_store", lambda: store)
    launcher = _launcher_with()

    await launcher._db_create(
        _task(direction_mode="random", direction_meta='{"seed": 7, "picked": "方向A"}')
    )

    kind, kwargs = store.calls[0]
    assert kind == "create"
    assert kwargs["direction_mode"] == "random"
    assert kwargs["direction_meta"] == '{"seed": 7, "picked": "方向A"}', (
        "抽样证据（T-MV-03）随任务落档——审计「方向怎么抽出来的」"
    )


@pytest.mark.asyncio
async def test_db_create_blank_direction_mode_is_null(monkeypatch) -> None:
    """模式没参与（自由文本/卡片派发）→ NULL，不伪记成 selected；抽样证据同理。"""
    store = _RecordingStore()
    monkeypatch.setattr(launcher_module, "_task_store", lambda: store)
    launcher = _launcher_with()

    await launcher._db_create(_task())

    assert store.calls[0][1]["direction_mode"] is None
    assert store.calls[0][1]["direction_meta"] is None


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
        "u-1",
        direction="动量 × 波动率",
        source="doc",
        doc_id="doc-9",
        direction_mode="random",
        direction_meta='{"seed": 9, "picked": "动量 × 波动率"}',
    )

    task = launcher._tasks[task_id]
    assert task.direction == "动量 × 波动率", "内存任务也要带着方向（监控器直接读它）"
    assert task.direction_mode == "random"
    assert task.direction_meta == '{"seed": 9, "picked": "动量 × 波动率"}'

    kind, kwargs = store.calls[0]
    assert kind == "create" and kwargs["task_id"] == task_id
    assert kwargs["direction"] == "动量 × 波动率"
    assert kwargs["direction_mode"] == "random"
    assert kwargs["direction_meta"] == '{"seed": 9, "picked": "动量 × 波动率"}'
    assert kwargs["source"] == "doc" and kwargs["doc_id"] == "doc-9"


@pytest.mark.asyncio
async def test_start_or_queue_forwards_direction_mode(monkeypatch) -> None:
    """批量派发路径同样把模式带上（子进程与心跳不打真）。"""
    store = _RecordingStore()
    monkeypatch.setattr(launcher_module, "_task_store", lambda: store)

    from backend.services.engine.alpha_agent import hw_lock

    monkeypatch.setattr(hw_lock, "assert_factor_mining_hardware", lambda: None)

    launcher = _launcher_with()
    monkeypatch.setattr(launcher, "_launch", lambda *a, **k: None)

    receipt = await launcher.start_or_queue(
        "u-1",
        direction="方向A",
        direction_mode="selected",
        direction_meta='{"seed": 3, "picked": "方向A"}',
    )

    task = launcher._tasks[receipt.task_id]
    assert task.direction_mode == "selected"
    assert store.calls[0][1]["direction_mode"] == "selected"
    assert store.calls[0][1]["direction_meta"] == '{"seed": 3, "picked": "方向A"}'


# ── 任务日志根与留存 GC（T-FM-20）────────────────────────────────────
#
# 日志根默认 /data（容器重建不丢）；启动期 GC 只清「状态文件可解析 + 终态 +
# 超龄」目录——认不出的东西（框架 __session__、写坏的 json、非终态）一律不动。
# 真删错的代价是排障日志没了，所以闸门全部按最保守方向设计，本组测试逐条钉死。


def _mk_task_dir(root: Path, name: str, *, status: str | None, age_days: float) -> Path:
    """造一个任务目录：state 文件 + 把 mtime 拨老 age_days 天。

    status=None 表示不写 task_state.json（模拟非任务目录）。
    """
    task_dir = root / name
    task_dir.mkdir()
    if status is not None:
        state_file = task_dir / "task_state.json"
        state_file.write_text(
            json.dumps({"task_id": name, "status": status}), encoding="utf-8"
        )
        old = time.time() - age_days * 86400
        os.utime(state_file, (old, old))
    return task_dir


def test_gc_task_logs_prunes_only_aged_terminal_dirs(tmp_path: Path) -> None:
    _mk_task_dir(tmp_path, "t-completed-old", status="completed", age_days=100)
    _mk_task_dir(tmp_path, "t-failed-old", status="failed", age_days=100)
    _mk_task_dir(tmp_path, "t-completed-fresh", status="completed", age_days=1)
    _mk_task_dir(tmp_path, "t-running-old", status="running", age_days=100)
    _mk_task_dir(tmp_path, "t-pending-old", status="pending", age_days=100)
    _mk_task_dir(tmp_path, "t-future-status", status="reborn", age_days=100)
    _mk_task_dir(tmp_path, "t-no-state", status=None, age_days=100)
    garbage = tmp_path / "t-garbage"
    garbage.mkdir()
    (garbage / "task_state.json").write_text("{not json", encoding="utf-8")

    # 显式留存线：不赌环境里 LOG_TRACE_RETENTION_DAYS 的默认值（环境无关）
    out = launcher_module.gc_task_logs(tmp_path, retention_days=90)

    assert out == {"scanned": 8, "pruned": 2}
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "t-completed-fresh",
        "t-future-status",
        "t-garbage",
        "t-no-state",
        "t-pending-old",
        "t-running-old",
    ]


def test_gc_task_logs_missing_root_is_zero(tmp_path: Path) -> None:
    assert launcher_module.gc_task_logs(tmp_path / "nope") == {
        "scanned": 0,
        "pruned": 0,
    }


def test_gc_task_logs_retention_env_and_param(tmp_path: Path, monkeypatch) -> None:
    _mk_task_dir(tmp_path, "t-40d", status="completed", age_days=40)

    # env 留存 30 天：40 天前的该清
    monkeypatch.setenv("LOG_TRACE_RETENTION_DAYS", "30")
    assert launcher_module.gc_task_logs(tmp_path)["pruned"] == 1

    # 显式参数优先于 env：留存 60 天 → 40 天前的保住
    _mk_task_dir(tmp_path, "t-40d-b", status="completed", age_days=40)
    assert launcher_module.gc_task_logs(tmp_path, retention_days=60)["pruned"] == 0

    # env 非整数回落默认 90：40 天前的保住（不因配置写错而提前删）
    monkeypatch.setenv("LOG_TRACE_RETENTION_DAYS", "abc")
    _mk_task_dir(tmp_path, "t-40d-c", status="completed", age_days=40)
    assert launcher_module.gc_task_logs(tmp_path)["pruned"] == 0


def test_resolve_log_dir_defaults_to_data_and_env_wins(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("LOG_TRACE_PATH", raising=False)
    assert launcher_module._resolve_log_dir() == Path("/data/alpha_agent_logs")
    monkeypatch.setenv("LOG_TRACE_PATH", str(tmp_path / "logs"))
    assert launcher_module._resolve_log_dir() == tmp_path / "logs"


def test_launcher_init_uses_resolved_log_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOG_TRACE_PATH", str(tmp_path / "logs"))

    launcher = AlphaAgentLauncher()

    assert launcher._log_dir == tmp_path / "logs"
    assert launcher._log_dir.is_dir()
    assert launcher._tasks == {}
