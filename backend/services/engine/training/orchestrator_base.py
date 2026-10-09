"""训练编排器抽象基类 + 工厂。

LocalDockerOrchestrator（本地 Docker-in-Docker）与 RemoteSSHOrchestrator
（AutoDL 远程 GPU）实现同一接口，调用方通过 get_orchestrator(node_id) 获取，
本地/远端可无缝切换。
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any
from collections.abc import Awaitable

logger = logging.getLogger(__name__)


class TrainingOrchestrator(ABC):
    """训练编排器基类。子类必须实现单周期训练。"""

    @abstractmethod
    async def launch_training_job(self, run_id: str, payload: dict | None = None) -> None:
        """编排单周期训练任务（推送数据 → 训练 → 注册模型）。"""


def get_orchestrator(node_id: str | None = None) -> TrainingOrchestrator:
    """根据 node_id 返回对应训练编排器。

    - node_id 为空 / "local" → 本地编排器（按运行时自动选择）：
      - Docker daemon 可达 → LocalDockerOrchestrator（容器训练，服务器部署默认）
      - 便携包等免 Docker 环境 → LocalProcessOrchestrator（同运行时 python 直跑）
      TRAINING_EXECUTOR=docker|process 可显式覆盖自动选择。
    - node_id 以 "autodl" 开头 → RemoteSSHOrchestrator（AutoDL 远程 GPU）
      按 node_id 从节点配置表（config/training_nodes.yaml）取 SSH 参数，
      支持多台 AutoDL 各自独立配置。
    """
    if node_id and node_id.startswith("autodl"):
        from backend.services.engine.training.node_manager import get_node_config
        from backend.services.engine.training.remote_ssh_orchestrator import RemoteSSHOrchestrator

        node_config = get_node_config(node_id)
        return RemoteSSHOrchestrator(node_id=node_id, node_config=node_config)

    from backend.shared.training_runtime import resolve_training_executor

    if resolve_training_executor().get("executor") == "process":
        from backend.services.engine.training.local_process_orchestrator import LocalProcessOrchestrator

        return LocalProcessOrchestrator()
    from backend.services.engine.training.local_docker_orchestrator import LocalDockerOrchestrator

    return LocalDockerOrchestrator()


# 便于类型标注 / 前端感知
LOCAL_NODE_ID = "local"


# ============================================================================
# P0-2: 进程级强引用 task registry，防止 asyncio.create_task 被 GC 吞
# ============================================================================

class TrainingTaskRegistry:
    """进程级强引用容器：保存所有未完成的训练编排 task。

    asyncio.create_task 返回的 task 只有在持有强引用时才会被事件循环调度。
    请求 handler 返回时局部变量被回收，task 也会被 GC。本 registry 持有强引用，
    并通过 done_callback 在 task 完成后自动清理，避免内存泄漏。
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._by_run_id: dict[str, set[asyncio.Task[Any]]] = {}

    def register(
        self, coro_or_task: Any, *, run_id: str | None = None
    ) -> asyncio.Task[Any]:
        """注册一个协程或已创建的 task 到 registry。

        - 传入 coroutine：asyncio.create_task + 注册
        - 传入 task：直接加入 set
        - run_id 非空时额外按 run_id 建索引，供 cancel(run_id) 快速定位并中断
        """
        if isinstance(coro_or_task, asyncio.Task):
            task = coro_or_task
        else:
            task = asyncio.create_task(coro_or_task)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if run_id:
            self._by_run_id.setdefault(str(run_id), set()).add(task)
            task.add_done_callback(
                lambda t: self._by_run_id.get(str(run_id), set()).discard(t)
            )
        return task

    def cancel(self, run_id: str) -> bool:
        """请求取消该 run 已注册的全部编排 task（best-effort 中断长等待）。

        返回是否有 task 被实际取消。真正的资源清理（docker stop / ssh kill）
        由编排器轮询循环在读到取消标记后执行。
        """
        tasks = self._by_run_id.pop(str(run_id), None)
        cancelled = False
        if tasks:
            for t in list(tasks):
                if not t.done():
                    t.cancel()
                    cancelled = True
        return cancelled

    def discard(self, task: asyncio.Task[Any]) -> None:
        """手动从 registry 移除（done_callback 失败时兜底）。"""
        self._tasks.discard(task)

    @property
    def size(self) -> int:
        """当前活跃 task 数（用于监控/调试）。"""
        return len(self._tasks)

    def is_active(self, run_id: str) -> bool:
        """该 run_id 是否仍有未完成的编排 task（launch 协程或其轮询循环）。

        P0-3（job_reaper 判尸）依据：在管 = 活作业，一律不碰；同时用于训练
        单飞锁的释放判定（run 不再受管 → 释放锁）。
        """
        tasks = self._by_run_id.get(str(run_id), set())
        return any(not t.done() for t in tasks)

    async def recover_pending_runs(
        self,
        *,
        get_session: Any,
        launch_fn: Any,
    ) -> int:
        """[已废弃，勿在新代码调用] 启动时批量重投孤儿任务。

        旧设计「重启即重投」有双跑风险（旧容器其实还活着时），且无生产调用方。
        P0-3 起由 ``job_reaper.reconcile_training_jobs`` 取代：先判尸
        （容器/PID/在管三探针），可验证存活的容器**重挂监管**而非重投，
        判死才标 failed 且默认不自动重投。保留本方法仅为兼容历史测试。

        Parameters
        ----------
        get_session : callable
            接受 (read_only: bool) 返回 async session context manager
        launch_fn : callable
            编排器的 launch_training_job 方法，接受 (run_id, payload) 返回 awaitable

        Returns
        -------
        int : 恢复的 run 数
        """
        from sqlalchemy import text

        n = 0
        try:
            async with get_session(read_only=True) as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT id, request_payload FROM admin_training_jobs "
                            "WHERE status IN ('pending','provisioning','running') "
                            "ORDER BY created_at ASC"
                        )
                    )
                ).mappings().all()
            for r in rows:
                run_id = str(r["id"])
                payload = (
                    r["request_payload"]
                    if isinstance(r["request_payload"], dict)
                    else {}
                )
                try:
                    self.register(
                        launch_fn(run_id=run_id, payload=payload), run_id=run_id
                    )
                    n += 1
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "[%s] recover launch failed: %s", run_id, exc
                    )
        except Exception as exc:  # noqa: BLE001
            logger.error("recover_pending_runs failed: %s", exc)
        logger.info("Recovered %d pending training runs on startup", n)
        return n


# 进程级单例
REGISTRY = TrainingTaskRegistry()


# ============================================================================
# P0-3 复审增补（2026-10-08 二轮）：资源创建在途补偿 + 优雅关停保锁
# ============================================================================

_COMPENSATION_TASKS: set[asyncio.Task[Any]] = set()


def log_task_exception(task: asyncio.Task[Any]) -> None:
    """done 回调：消费后台 task 的异常（防 "exception was never retrieved"）。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("[Task] 后台任务异常: %s", exc)


def spawn_compensation(coro: Awaitable[Any]) -> asyncio.Task[Any] | None:
    """发射脱离当前取消上下文的补偿任务（复审 HIGH-1 残窗）。

    CancelledError 处理器里直接 ``await`` 长清理会被随后的关停再次取消，而
    「资源创建在途」窗口（``containers.run`` 线程 / ``create_subprocess_exec``
    尚未返回句柄）若无人接手，会留下无锁、无监管、界面已取消的孤儿训练。
    独立 task 承接补杀；模块级强引用防 GC（asyncio 仅弱引用 task），异常经
    done 回调消费。无运行 loop（同步收尾）时返回 None。
    """
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        logger.warning("[Compensation] 无运行 loop，补偿任务未发射")
        return None
    _COMPENSATION_TASKS.add(task)

    def _retire(t: asyncio.Task[Any]) -> None:
        _COMPENSATION_TASKS.discard(t)
        log_task_exception(t)

    task.add_done_callback(_retire)
    return task


_GRACEFUL_STOPS: set[str] = set()


def mark_graceful_stop(run_id: str) -> None:
    """标记该 run 的编排 task 因**进程关停**（而非用户取消）被取消。

    done 回调据此**不释放**单飞锁（复审 HIGH-4 残窗）：资源仍在跑，锁必须
    活到重启后重挂续租——优雅关停若把锁删掉，续租 CAS 救不回已删键，
    净结果 = 整个训练期无互斥。标记不消费：launch 与轮询两张 task 的
    done 回调都会查询它（进程随即退出，集合不会长期驻留）。
    """
    _GRACEFUL_STOPS.add(str(run_id))


def is_graceful_stop(run_id: str) -> bool:
    """该 run 是否被标记为优雅关停中断（只读，不消费）。"""
    return str(run_id) in _GRACEFUL_STOPS


_USER_CANCEL_KILLS: set[str] = set()


def mark_user_cancel_confirmed(run_id: str) -> None:
    """记录「本进程内已按用户取消标记杀灭资源」（复审 NEW-B，只增不删）。

    三个编排器的 ``_cancel_*`` 在动手时都会 ``clear_cancel``（清 Redis 标记），
    而 ``supervised_launch`` 的取消处理器在**内层处理器之后**才读标记——防的是
    用户取消被误判成进程关停（错置 ``mark_graceful_stop`` → 锁被无谓续持到
    下次提交的陈旧自愈）。进程内集合不受标记清理影响，run_id 一次性使用，
    集合随进程退出消亡，无需清理。
    """
    _USER_CANCEL_KILLS.add(str(run_id))


def is_user_cancel_confirmed(run_id: str) -> bool:
    """该 run 是否已在本进程内被确认为用户取消（只读，不消费）。"""
    return str(run_id) in _USER_CANCEL_KILLS


def attach_singleflight_release(task: asyncio.Task[Any], run_id: str) -> None:
    """给编排 task 挂「结束后释放训练单飞锁」回调（P0-3）。

    仅当该 run 已无任何受管 task（``REGISTRY.is_active`` 为 False）才释放：
    launch 协程正常返回时轮询循环仍在管 → 跳过；轮询循环结束时释放；
    launch 异常且从未注册轮询 → 释放。释放走 CAS（token=run_id），
    即使误触发也不会删到他人的锁。任何异常只告警不抛出。

    **优雅关停例外**（复审 HIGH-4）：若该 run 已被 ``mark_graceful_stop``
    标记（进程关停而资源仍在跑），一律**不释放**——锁留给重启后的重挂
    续租/补锁，用户取消不受影响（取消路径不置标记）。
    """

    def _done(_t: asyncio.Task[Any]) -> None:
        try:
            if is_graceful_stop(run_id):
                logger.info(
                    "[Singleflight] graceful shutdown; keeping lock: %s", run_id
                )
                return
            if REGISTRY.is_active(run_id):
                return
            from backend.shared.redis_sentinel_client import get_redis_sentinel_client
            from backend.shared.training_singleflight import release

            release(get_redis_sentinel_client(), run_id)
            logger.info("[Singleflight] released after run unmanaged: %s", run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Singleflight] release callback failed (%s): %s", run_id, exc
            )

    task.add_done_callback(_done)


async def supervised_launch(
    orchestrator: Any,
    *,
    run_id: str,
    payload: dict,
    tenant_id: str = "default",
    user_id: str = "unknown",
) -> None:
    """launch 协程的取消清理包装（复审 HIGH-1，2026-10-08）。

    REGISTRY.cancel 在 provisioning 窗口（容器/子进程已建、轮询未挂）把
    CancelledError 打进 launch 时，资源会失控（容器继续训练 / 子进程继续跑）
    而单飞锁已随 task 结束释放 → 下一次提交双跑。编排器实现
    ``cleanup_cancelled_provisioning`` 则在此补做清理；清理内部**仅在用户
    取消标记已置时**动手——API 进程正常关停（无标记）留给重启后的判尸重挂，
    并置 ``mark_graceful_stop`` 保住单飞锁（复审 HIGH-4：关停不删锁）。
    """
    try:
        await orchestrator.launch_training_job(run_id=run_id, payload=payload)
    except asyncio.CancelledError:
        flagged = False
        try:
            flagged = bool(orchestrator.log_stream.is_cancel_requested(run_id))
        except Exception:  # noqa: BLE001
            flagged = False
        if not flagged:
            # 标记可能已被内层取消处理器消费（_cancel_* 成功杀灭后会
            # clear_cancel）——用进程内「已确认用户取消」补判（复审 NEW-B），
            # 防用户取消被当成进程关停而误保锁。
            flagged = is_user_cancel_confirmed(run_id)
        if not flagged:
            # 进程关停：保住单飞锁供重启后重挂续租/补锁；用户取消（标记已置）
            # 照常走释放路径。标记判定失败按关停保守处理（保锁）。
            # 先置标记再 await 清理（复审 NEW-C）：清理期间再挨一次取消时
            # CancelledError 会直接从 await 传出，标记已在，HIGH-4 不复发。
            mark_graceful_stop(run_id)
        cleanup = getattr(orchestrator, "cleanup_cancelled_provisioning", None)
        if cleanup is not None:
            try:
                await cleanup(run_id, tenant_id=tenant_id, user_id=user_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[SupervisedLaunch] cleanup %s failed: %s", run_id, exc)
        raise
