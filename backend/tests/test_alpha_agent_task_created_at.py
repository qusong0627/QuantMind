"""任务状态接口必须带上 `created_at`，且**缺失时不能伪造**。

**回归背景（2026-10-07）**

前端要加一个全局的挖掘进度面板（刷新/切页都不丢），数据源就是
`GET /api/v1/alpha-agent/tasks`。它的第一句话是「这个任务是刚起的，还是我离开
前就挂着的那一个」——而 `get_task_status()` 的返回里根本没有 `created_at`：
该字段只存在于磁盘上的 `task_state.json`（`_persist_task` 写了它），状态接口
不往外吐。前端 `normalizeAgentTask` 于是拿到 `undefined`，回落到
`new Date().toISOString()`，把一个几小时前的僵尸任务显示成「刚刚创建」。

所以本文件锁三件事：

1. **字段在**，且是 ISO-8601 UTC（带 `Z`）——前端直接 `new Date(s)` 就能用；
2. **取不到就是 `None`**，不回落到 `now()`。宁可让面板显示「时间未知」，
   也不能显示一个假的时间：那正是这个面板唯一要回答的问题；
3. **时区口径**：`created_at` 是 `time.time()` 的 epoch 秒，
   `datetime.fromtimestamp(ts, UTC)` 与 `datetime.fromtimestamp(ts)`（本机时区）
   在这台机器上差 9 小时（host=JST）。差 9 小时的时间戳在面板上看起来完全合理，
   不会报错，只会让人把任务的时间线理解错——典型的静默错误。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent.launcher import (  # noqa: E402
    AlphaAgentLauncher,
    EvolutionTask,
    TaskStatus,
)

# 2025-10-07 07:58:33.5 UTC。本机（JST）的 local-time 读法会得到 16:58:33.5，
# 差 9 小时——够大到能一眼看出用了错的时区。
EPOCH = 1759823913.5
EPOCH_ISO = "2025-10-07T07:58:33.500000Z"


def _launcher_with(task: EvolutionTask) -> AlphaAgentLauncher:
    """只挂一个任务、不碰磁盘的 launcher（`__init__` 会扫日志目录，这里跳过）。"""
    obj = AlphaAgentLauncher.__new__(AlphaAgentLauncher)
    obj._tasks = {task.task_id: task}
    return obj


def _task(**overrides) -> EvolutionTask:
    base: dict = {
        "task_id": "t-1",
        "user_id": "1",
        "status": TaskStatus.RUNNING,
        "created_at": EPOCH,
    }
    base.update(overrides)
    return EvolutionTask(**base)


@pytest.mark.asyncio
async def test_status_payload_carries_created_at_as_utc_iso() -> None:
    """epoch 秒 → 带 Z 的 ISO-8601，且是 UTC 那一刻（不是本机时区那一刻）。"""
    launcher = _launcher_with(_task())

    status = await launcher.get_task_status("t-1")

    assert status is not None
    assert status["created_at"] == EPOCH_ISO


@pytest.mark.asyncio
async def test_created_at_is_none_when_unavailable_not_fabricated() -> None:
    """取不到创建时间就老实给 None——绝不能回落成「现在」。"""
    launcher = _launcher_with(_task(created_at=None))  # type: ignore[arg-type]

    status = await launcher.get_task_status("t-1")

    assert status is not None
    assert status["created_at"] is None, "伪造的 now() 会把僵尸任务显示成刚提交的"


@pytest.mark.asyncio
async def test_created_at_is_none_for_garbage_instead_of_raising() -> None:
    """磁盘上被写坏的值不能让整个状态接口 500——一个任务坏不该拖垮任务列表。"""
    launcher = _launcher_with(_task(created_at="not-a-timestamp"))  # type: ignore[arg-type]

    status = await launcher.get_task_status("t-1")

    assert status is not None
    assert status["created_at"] is None


@pytest.mark.asyncio
async def test_every_task_in_the_list_carries_created_at() -> None:
    """列表接口逐条复用状态接口——多条任务时不能只有第一条带时间。"""
    launcher = _launcher_with(_task(task_id="a", created_at=EPOCH))
    launcher._tasks["b"] = _task(
        task_id="b", created_at=EPOCH + 60, status=TaskStatus.FAILED
    )

    tasks = await launcher.list_tasks(user_id="1")

    by_id = {t["task_id"]: t for t in tasks}
    assert by_id["a"]["created_at"] == EPOCH_ISO
    assert by_id["b"]["created_at"] == "2025-10-07T07:59:33.500000Z"


@pytest.mark.asyncio
async def test_failure_reason_still_travels_with_the_task() -> None:
    """面板「失败 + 原因」这一行依赖 error_message，加字段时别碰掉它。"""
    launcher = _launcher_with(
        _task(
            status=TaskStatus.FAILED,
            error_message="Server restarted while task was running",
        )
    )

    status = await launcher.get_task_status("t-1")

    assert status is not None
    assert status["status"] == "failed"
    assert status["error_message"] == "Server restarted while task was running"
