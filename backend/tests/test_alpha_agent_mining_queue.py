"""因子挖掘排队底座（自动拆解 → 批量派发 → 有序排队）的 launcher 契约。

用户原话：「我给了 10 个方向、一批一批的去挖掘……几批一起挖，自动拆解，效率更高」。
拆解出的子方向要能一次全部提交、服务端**有序排队**，而不是让前端收一串 429。

本文件锁住的边（每条都是极易踩的坑）：

1. **容量参数唯一读取点**：running 上限沿用 ``ALPHA_AGENT_MAX_RUNNING_*``（路由
   429 判定与排队判断同一来源，禁止两处各读各的）；排队深度上限
   ``ALPHA_AGENT_MAX_QUEUED_*``（默认 20/50）。坏值回落默认——绝不因配置写错
   把挖掘入口整个锁死。
2. **排队是持久状态**：queued 行落 task_state.json + DB；重启 ``_load_tasks``
   保留 queued（只把 running 翻 failed），DB 对账 reconcile_orphans 同样不碰。
3. **排空永不双重启动**：槽位在第一个 await 之前同步预留（QUEUED→PENDING），
   并发 drain 由 ``_draining`` 重入闸挡住；每轮按 running 上限重算名额。
4. **排空绝不读持久化密钥**：提交时的密钥只活在内存里；排空时用注册的解析器
   按 (user_id, tenant_id) 重解析。解析器返回 None（或抛异常，profile 网关挂
   同义）→ 任务显式失败并给可操作报错——绝不静默换供应商/换 Key。解析器
   未注册（主机直跑/单测）→ 按容器 env 继续（overrides=None）。
5. **evolve 不排队**：``start_evolution`` 永远立即启动——429 背压契约在路由层
   （前端「原文上屏」），排队只属于 ``start_or_queue`` 的批量派发路径。
6. **排队任务可取消**：不碰进程（还没有进程）；取消运行中的任务腾出名额后
   排队任务立即补位。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent import launcher as launcher_module  # noqa: E402
from backend.services.engine.alpha_agent.launcher import (  # noqa: E402
    AlphaAgentLauncher,
    EvolutionTask,
    QueueFullError,
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
        return 0


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """模块级注册位与硬件闸门按用例隔离（防跨用例串味/依赖真机资源）。"""
    from backend.services.engine.alpha_agent import hw_lock

    monkeypatch.setattr(launcher_module, "_llm_override_resolver", None)
    monkeypatch.setattr(hw_lock, "assert_factor_mining_hardware", lambda: None)


def _launcher(log_dir: Path | None = None) -> AlphaAgentLauncher:
    """``__new__`` 工厂：绕开 __init__（不碰真实日志根/不加载历史任务）。

    未给 log_dir 时也落一个临时目录——排队/取消路径**必然**写
    ``task_state.json``（排队即持久状态），磁盘面必须真实参与，不能靠
    None 短路把持久化契约绕过去。
    """
    obj = AlphaAgentLauncher.__new__(AlphaAgentLauncher)
    obj._tasks = {}
    obj._log_dir = (
        Path(log_dir)
        if log_dir is not None
        else Path(tempfile.mkdtemp(prefix="qm-mining-queue-"))
    )
    obj._draining = False
    return obj


def _install_store(monkeypatch) -> _RecordingStore:
    store = _RecordingStore()
    monkeypatch.setattr(launcher_module, "_task_store", lambda: store)
    return store


def _seed(
    launcher: AlphaAgentLauncher,
    task_id: str,
    user_id: str,
    status: TaskStatus,
    **overrides,
) -> EvolutionTask:
    task = EvolutionTask(task_id=task_id, user_id=user_id, status=status, **overrides)
    launcher._tasks[task_id] = task
    return task


class _LaunchSpy:
    """假 ``_launch``：记录调用 + 把任务翻 running（模拟子进程已起）。"""

    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.calls: list[dict] = []
        self._fail_for = fail_for or set()

    def __call__(
        self,
        task: EvolutionTask,
        *,
        loop_n: int,
        seed: str | None = None,
        provider_uri: str | None = None,
        direction: str = "",
        llm_overrides: dict[str, str] | None = None,
    ) -> None:
        self.calls.append(
            {
                "task_id": task.task_id,
                "loop_n": loop_n,
                "direction": direction,
                "llm_overrides": llm_overrides,
            }
        )
        if task.task_id in self._fail_for:
            raise RuntimeError("qlib 缓存构建失败")
        task.status = TaskStatus.RUNNING


# ── 容量参数唯一读取点 ───────────────────────────────────────────────


def test_running_capacity_env_and_defaults(monkeypatch) -> None:
    monkeypatch.delenv("ALPHA_AGENT_MAX_RUNNING_PER_USER", raising=False)
    monkeypatch.delenv("ALPHA_AGENT_MAX_RUNNING_GLOBAL", raising=False)
    launcher = _launcher()
    assert launcher.running_capacity() == (2, 4)

    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_PER_USER", "5")
    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_GLOBAL", "9")
    assert launcher.running_capacity() == (5, 9)

    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_PER_USER", "abc")
    assert launcher.running_capacity() == (2, 9), "坏值回落默认，绝不因配置写错锁死入口"


def test_queue_capacity_env_and_defaults(monkeypatch) -> None:
    monkeypatch.delenv("ALPHA_AGENT_MAX_QUEUED_PER_USER", raising=False)
    monkeypatch.delenv("ALPHA_AGENT_MAX_QUEUED_GLOBAL", raising=False)
    launcher = _launcher()
    assert launcher.queue_capacity() == (20, 50)

    monkeypatch.setenv("ALPHA_AGENT_MAX_QUEUED_PER_USER", "3")
    monkeypatch.setenv("ALPHA_AGENT_MAX_QUEUED_GLOBAL", "7")
    assert launcher.queue_capacity() == (3, 7)

    monkeypatch.setenv("ALPHA_AGENT_MAX_QUEUED_GLOBAL", "  ")
    assert launcher.queue_capacity() == (3, 50), "空白=未配置"


# ── start_or_queue：提交侧 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_or_queue_launches_immediately_when_slot_free(monkeypatch) -> None:
    store = _install_store(monkeypatch)
    launcher = _launcher()
    spy = _LaunchSpy()
    monkeypatch.setattr(launcher, "_launch", spy)

    receipt = await launcher.start_or_queue("u-1", direction="动量 × 波动率")

    assert receipt.status == "running" and receipt.queue_position is None
    kind, kwargs = store.calls[0]
    assert kind == "create" and kwargs["status"] == "pending"
    assert spy.calls[0]["task_id"] == receipt.task_id
    assert spy.calls[0]["direction"] == "动量 × 波动率"
    assert launcher._tasks[receipt.task_id].status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_start_or_queue_queues_when_at_capacity(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_PER_USER", "1")
    store = _install_store(monkeypatch)
    launcher = _launcher(tmp_path)
    spy = _LaunchSpy()
    monkeypatch.setattr(launcher, "_launch", spy)
    _seed(launcher, "t-run", "u-1", TaskStatus.RUNNING)

    receipt = await launcher.start_or_queue(
        "u-1", direction="第二个方向", tenant_id="tenant-9"
    )

    assert receipt.status == "queued" and receipt.queue_position == 1
    task = launcher._tasks[receipt.task_id]
    assert task.status == TaskStatus.QUEUED and task.tenant_id == "tenant-9"
    assert spy.calls == [], "排队的任务绝不先起子进程"
    kind, kwargs = store.calls[0]
    assert kind == "create" and kwargs["status"] == "queued"

    # 排队状态必须落盘（重启后 _load_tasks 原样读回；tenant 供排空重解析）
    state = json.loads((tmp_path / receipt.task_id / "task_state.json").read_text())
    assert state["status"] == "queued" and state["tenant_id"] == "tenant-9"


@pytest.mark.asyncio
async def test_start_or_queue_queues_second_with_position_2(monkeypatch) -> None:
    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_PER_USER", "1")
    _install_store(monkeypatch)
    launcher = _launcher()
    monkeypatch.setattr(launcher, "_launch", _LaunchSpy())
    _seed(launcher, "t-run", "u-1", TaskStatus.RUNNING)

    first = await launcher.start_or_queue("u-1", direction="方向A")
    second = await launcher.start_or_queue("u-1", direction="方向B")

    assert first.queue_position == 1 and second.queue_position == 2


@pytest.mark.asyncio
async def test_start_or_queue_queue_full_per_user(monkeypatch) -> None:
    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_PER_USER", "1")
    monkeypatch.setenv("ALPHA_AGENT_MAX_QUEUED_PER_USER", "1")
    store = _install_store(monkeypatch)
    launcher = _launcher()
    monkeypatch.setattr(launcher, "_launch", _LaunchSpy())
    _seed(launcher, "t-run", "u-1", TaskStatus.RUNNING)

    first = await launcher.start_or_queue("u-1", direction="方向A")
    assert first.status == "queued"

    before = len(store.calls)
    with pytest.raises(QueueFullError) as err:
        await launcher.start_or_queue("u-1", direction="方向B")
    assert "排队已满" in str(err.value)
    assert len(store.calls) == before, "被拒的提交不留任务行"


@pytest.mark.asyncio
async def test_start_or_queue_queue_full_global(monkeypatch) -> None:
    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_PER_USER", "1")
    # 全局 running 也要占满——否则 u-2 自己还有运行名额，会正常启动而不是排队
    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_GLOBAL", "1")
    monkeypatch.setenv("ALPHA_AGENT_MAX_QUEUED_PER_USER", "20")
    monkeypatch.setenv("ALPHA_AGENT_MAX_QUEUED_GLOBAL", "1")
    _install_store(monkeypatch)
    launcher = _launcher()
    monkeypatch.setattr(launcher, "_launch", _LaunchSpy())
    _seed(launcher, "t-run", "u-1", TaskStatus.RUNNING)

    await launcher.start_or_queue("u-1", direction="占掉唯一的排队位")

    with pytest.raises(QueueFullError) as err:
        await launcher.start_or_queue("u-2", direction="别人的方向——全局已满")
    assert "排队已满" in str(err.value)


@pytest.mark.asyncio
async def test_start_evolution_never_queues_even_at_capacity(monkeypatch) -> None:
    """429 背压契约在路由层（前端「原文上屏」）；start_evolution 绝不悄悄排队。"""
    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_PER_USER", "1")
    _install_store(monkeypatch)
    launcher = _launcher()
    spy = _LaunchSpy()
    monkeypatch.setattr(launcher, "_launch", spy)
    _seed(launcher, "t-run", "u-1", TaskStatus.RUNNING)

    task_id = await launcher.start_evolution("u-1", direction="直达方向")

    assert launcher._tasks[task_id].status == TaskStatus.RUNNING
    assert len(spy.calls) == 1


# ── drain_queue：排空侧 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drain_launches_oldest_first_within_caps(monkeypatch) -> None:
    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_PER_USER", "2")
    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_GLOBAL", "2")
    _install_store(monkeypatch)
    launcher = _launcher()
    spy = _LaunchSpy()
    monkeypatch.setattr(launcher, "_launch", spy)
    running = _seed(launcher, "t-run", "u-1", TaskStatus.RUNNING)
    old = _seed(launcher, "t-old", "u-1", TaskStatus.QUEUED, created_at=1000.0)
    new = _seed(launcher, "t-new", "u-1", TaskStatus.QUEUED, created_at=2000.0)

    started = await launcher.drain_queue()

    assert started == 1
    assert [c["task_id"] for c in spy.calls] == ["t-old"], "最旧优先"
    assert old.status == TaskStatus.RUNNING
    assert new.status == TaskStatus.QUEUED, "全局名额 2/2，后一个继续排队"

    running.status = TaskStatus.COMPLETED
    assert await launcher.drain_queue() == 1
    assert new.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_drain_skips_users_at_their_cap_but_serves_others(monkeypatch) -> None:
    """同用户满额不阻塞别人：跳过 u-1 的最旧任务，先起 u-2 的。"""
    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_PER_USER", "1")
    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_GLOBAL", "2")
    _install_store(monkeypatch)
    launcher = _launcher()
    spy = _LaunchSpy()
    monkeypatch.setattr(launcher, "_launch", spy)
    _seed(launcher, "t-u1-run", "u-1", TaskStatus.RUNNING)
    _seed(launcher, "t-u1-q", "u-1", TaskStatus.QUEUED, created_at=1000.0)
    _seed(launcher, "t-u2-q", "u-2", TaskStatus.QUEUED, created_at=2000.0)

    started = await launcher.drain_queue()

    assert started == 1
    assert [c["task_id"] for c in spy.calls] == ["t-u2-q"]
    assert launcher._tasks["t-u1-q"].status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_drain_reserves_slot_before_await_and_blocks_double_start(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_PER_USER", "2")
    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_GLOBAL", "1")
    _install_store(monkeypatch)
    launcher = _launcher()
    spy = _LaunchSpy()
    monkeypatch.setattr(launcher, "_launch", spy)
    _seed(launcher, "t-a", "u-1", TaskStatus.QUEUED, created_at=1000.0)
    _seed(launcher, "t-b", "u-2", TaskStatus.QUEUED, created_at=2000.0)

    resolver_calls: list[str] = []

    async def resolver(user_id: str, tenant_id: str):
        resolver_calls.append(user_id)
        await asyncio.sleep(0)  # 让出事件循环：并发 drain 在此期间进来
        return {"OPENAI_API_KEY": "k"}

    monkeypatch.setattr(launcher_module, "_llm_override_resolver", resolver)

    results = await asyncio.gather(launcher.drain_queue(), launcher.drain_queue())

    assert sorted(results) == [0, 1], "重入闸：第二个 drain 立即空转返回"
    assert [c["task_id"] for c in spy.calls] == ["t-a"]
    assert resolver_calls == ["u-1"], "名额只有 1：第二个任务连解析都不该发起"


@pytest.mark.asyncio
async def test_submit_during_drain_sees_reserved_slot(monkeypatch) -> None:
    """排空在 await 期间已预留名额：窗口期的新提交必须转排队，不得超额启动。"""
    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_PER_USER", "2")
    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_GLOBAL", "1")
    _install_store(monkeypatch)
    launcher = _launcher()
    spy = _LaunchSpy()
    monkeypatch.setattr(launcher, "_launch", spy)
    _seed(launcher, "t-a", "u-1", TaskStatus.QUEUED, created_at=1000.0)

    gate = asyncio.Event()

    async def resolver(user_id: str, tenant_id: str):
        await gate.wait()  # 卡住排水，暴露「已预留但还没起子进程」的窗口
        return {"OPENAI_API_KEY": "k"}

    monkeypatch.setattr(launcher_module, "_llm_override_resolver", resolver)

    drain_task = asyncio.create_task(launcher.drain_queue())
    await asyncio.sleep(0)
    await asyncio.sleep(0)  # 让 drain 走到 resolver 的 gate.wait()

    receipt = await launcher.start_or_queue("u-3", direction="窗口期的新提交")
    assert receipt.status == "queued", "已被排空预留的名额不能二次分配"

    gate.set()
    assert await drain_task == 1
    assert [c["task_id"] for c in spy.calls] == ["t-a"]


@pytest.mark.asyncio
async def test_drain_fails_task_when_resolver_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_GLOBAL", "2")
    store = _install_store(monkeypatch)
    launcher = _launcher()
    spy = _LaunchSpy()
    monkeypatch.setattr(launcher, "_launch", spy)
    _seed(launcher, "t-u1", "u-1", TaskStatus.QUEUED, created_at=1000.0)
    _seed(launcher, "t-u2", "u-2", TaskStatus.QUEUED, created_at=2000.0)

    async def resolver(user_id: str, tenant_id: str):
        return None if user_id == "u-1" else {"OPENAI_API_KEY": "k"}

    monkeypatch.setattr(launcher_module, "_llm_override_resolver", resolver)

    started = await launcher.drain_queue()

    assert started == 1
    failed = launcher._tasks["t-u1"]
    assert failed.status == TaskStatus.FAILED
    assert "LLM API Key" in (failed.error_message or ""), (
        "报错必须可操作（指路个人中心/env）"
    )
    assert [c["task_id"] for c in spy.calls] == ["t-u2"], (
        "一个任务解析失败不占名额，后面的照常排水"
    )
    terminal = [c for c in store.calls if c[0] == "terminal"]
    assert (
        terminal and terminal[0][1] == "t-u1" and terminal[0][2]["status"] == "failed"
    )


@pytest.mark.asyncio
async def test_drain_resolver_exception_fails_actionably(monkeypatch) -> None:
    """profile 网关连不上 ≠ 没有配置：同样显式失败，不许静默回落容器 env。"""
    _install_store(monkeypatch)
    launcher = _launcher()
    spy = _LaunchSpy()
    monkeypatch.setattr(launcher, "_launch", spy)
    _seed(launcher, "t-a", "u-1", TaskStatus.QUEUED)

    async def resolver(user_id: str, tenant_id: str):
        raise RuntimeError("profile gate 挂了")

    monkeypatch.setattr(launcher_module, "_llm_override_resolver", resolver)

    started = await launcher.drain_queue()

    assert started == 0 and spy.calls == []
    task = launcher._tasks["t-a"]
    assert task.status == TaskStatus.FAILED
    assert "LLM API Key" in (task.error_message or "")


@pytest.mark.asyncio
async def test_drain_without_resolver_uses_container_env(monkeypatch) -> None:
    """解析器未注册（主机直跑/单测）→ None 覆盖，容器 env 兜底照旧。"""
    _install_store(monkeypatch)
    launcher = _launcher()
    spy = _LaunchSpy()
    monkeypatch.setattr(launcher, "_launch", spy)
    _seed(launcher, "t-a", "u-1", TaskStatus.QUEUED)

    assert await launcher.drain_queue() == 1
    assert spy.calls[0]["llm_overrides"] is None


@pytest.mark.asyncio
async def test_drain_launch_failure_frees_slot_for_next(monkeypatch) -> None:
    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_GLOBAL", "2")
    _install_store(monkeypatch)
    launcher = _launcher()
    spy = _LaunchSpy(fail_for={"t-bad"})
    monkeypatch.setattr(launcher, "_launch", spy)
    _seed(launcher, "t-bad", "u-1", TaskStatus.QUEUED, created_at=1000.0)
    _seed(launcher, "t-ok", "u-2", TaskStatus.QUEUED, created_at=2000.0)

    started = await launcher.drain_queue()

    assert started == 1
    bad = launcher._tasks["t-bad"]
    assert bad.status == TaskStatus.FAILED
    assert "启动失败" in (bad.error_message or "")
    assert launcher._tasks["t-ok"].status == TaskStatus.RUNNING


# ── 取消与重启 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_queued_task_marks_cancelled_without_process(monkeypatch) -> None:
    store = _install_store(monkeypatch)
    launcher = _launcher()
    monkeypatch.setattr(launcher, "_launch", _LaunchSpy())
    _seed(launcher, "t-q", "u-1", TaskStatus.QUEUED)

    assert await launcher.cancel_task("t-q") is True

    task = launcher._tasks["t-q"]
    assert task.status == TaskStatus.FAILED
    assert task.error_message == "Cancelled by user"
    terminal = [c for c in store.calls if c[0] == "terminal"]
    assert terminal and terminal[0][2]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_running_task_lets_queued_start(monkeypatch) -> None:
    monkeypatch.setenv("ALPHA_AGENT_MAX_RUNNING_PER_USER", "1")
    _install_store(monkeypatch)
    launcher = _launcher()
    spy = _LaunchSpy()
    monkeypatch.setattr(launcher, "_launch", spy)
    _seed(launcher, "t-run", "u-1", TaskStatus.RUNNING)  # 无 process：模拟已被回收
    queued = _seed(launcher, "t-q", "u-1", TaskStatus.QUEUED, created_at=2000.0)

    assert await launcher.cancel_task("t-run") is True

    assert queued.status == TaskStatus.RUNNING, "取消释放名额后排队任务立即补位"
    assert [c["task_id"] for c in spy.calls] == ["t-q"]


def test_load_tasks_keeps_queued_and_tenant_running_becomes_failed(tmp_path) -> None:
    writer = _launcher(tmp_path)
    writer._persist_task(
        EvolutionTask(
            task_id="t-q",
            user_id="u-1",
            status=TaskStatus.QUEUED,
            tenant_id="tenant-9",
        )
    )
    writer._persist_task(
        EvolutionTask(task_id="t-r", user_id="u-1", status=TaskStatus.RUNNING)
    )

    reader = _launcher(tmp_path)
    reader._load_tasks()

    assert reader._tasks["t-q"].status == TaskStatus.QUEUED, "排队任务重启后原样保留"
    assert reader._tasks["t-q"].tenant_id == "tenant-9"
    assert reader._tasks["t-r"].status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_status_payload_exposes_queue_position_per_user(monkeypatch) -> None:
    launcher = _launcher()
    _seed(launcher, "t-r", "u-1", TaskStatus.RUNNING)
    _seed(launcher, "t-q1", "u-1", TaskStatus.QUEUED, created_at=1000.0)
    _seed(launcher, "t-q2", "u-1", TaskStatus.QUEUED, created_at=2000.0)
    _seed(launcher, "t-q-other", "u-2", TaskStatus.QUEUED, created_at=1500.0)

    s_r = await launcher.get_task_status("t-r")
    s_q1 = await launcher.get_task_status("t-q1")
    s_q2 = await launcher.get_task_status("t-q2")
    s_o = await launcher.get_task_status("t-q-other")

    assert s_r["queue_position"] is None
    assert s_q1["queue_position"] == 1 and s_q2["queue_position"] == 2
    assert s_o["queue_position"] == 1, "位次是本用户队列内的位次（不泄露别人的队列）"
