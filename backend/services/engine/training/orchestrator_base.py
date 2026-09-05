"""训练编排器抽象基类 + 工厂。

LocalDockerOrchestrator（本地 Docker-in-Docker）与 RemoteSSHOrchestrator
（AutoDL 远程 GPU）实现同一接口，调用方通过 get_orchestrator(node_id) 获取，
本地/远端可无缝切换。
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Set

logger = logging.getLogger(__name__)


class TrainingOrchestrator(ABC):
    """训练编排器基类。子类必须实现单周期/多周期训练。"""

    @abstractmethod
    async def launch_training_job(self, run_id: str, payload: dict | None = None) -> None:
        """编排单周期训练任务（推送数据 → 训练 → 注册模型）。"""

    @abstractmethod
    async def launch_multi_horizon_job(
        self, parent_run_id: str, child_run_ids: list[str], payload: dict | None = None
    ) -> None:
        """编排多周期训练（串行跑各 child，全部成功后创建融合模型）。"""


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
        self._tasks: Set[asyncio.Task[Any]] = set()

    def register(self, coro_or_task: Any) -> asyncio.Task[Any]:
        """注册一个协程或已创建的 task 到 registry。

        - 传入 coroutine：asyncio.create_task + 注册
        - 传入 task：直接加入 set
        """
        if isinstance(coro_or_task, asyncio.Task):
            task = coro_or_task
        else:
            task = asyncio.create_task(coro_or_task)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def discard(self, task: asyncio.Task[Any]) -> None:
        """手动从 registry 移除（done_callback 失败时兜底）。"""
        self._tasks.discard(task)

    @property
    def size(self) -> int:
        """当前活跃 task 数（用于监控/调试）。"""
        return len(self._tasks)

    async def recover_pending_runs(
        self,
        *,
        get_session: Any,
        launch_fn: Any,
    ) -> int:
        """启动时从 DB 恢复 status in (pending, provisioning, running) 的孤儿任务。

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
                    self.register(launch_fn(run_id=run_id, payload=payload))
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
