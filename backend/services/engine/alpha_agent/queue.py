"""Engine-owned durable dispatcher with owner-aware remote node placement."""

import asyncio
import logging
import socket
from contextlib import closing

from backend.services.engine.alpha_agent.resource_budget import (
    POOL,
    finish_execution,
    get_budget,
    pool_provisioned,
    recover_budget,
)
from backend.services.engine.qlib_app.services.rd_agent_persistence import (
    AlphaMiningTaskPersistence,
)
from backend.shared.container_witness import container_namespace_has_stopped
from backend.shared.process_witness import capture_process, process_has_stopped

logger = logging.getLogger(__name__)


async def dispatch_assigned_nodes(store, last_dispatch):
    # OSS-ADAPT: OSS 单容器不支持远端挖掘节点，远端分发不可用。
    raise RuntimeError("OSS 单容器不支持远端挖掘节点")


def capture_controller():
    import docker

    with closing(docker.from_env(timeout=5)) as client:
        container = client.containers.get(socket.gethostname())
        return {
            "controller": container.id,
            "daemon": client.info()["ID"],
            "process": capture_process(),
        }


def _ended(client, executor):
    from docker.errors import NotFound
    from backend.services.engine.alpha_agent.launcher import AlphaAgentLauncher

    if executor.get("kind") == "alpha-celery":
        # OSS-ADAPT: OSS 单容器不支持远端挖掘节点(celery-worker-factor-remote)。
        raise RuntimeError("OSS 单容器不支持远端挖掘节点")

    process = executor.get("process")
    if not executor.get("controller") or executor.get("daemon") != client.info()["ID"]:
        return False
    try:
        client.containers.get(executor["controller"])
    except NotFound:
        # Exact full container ID on the same daemon; its entire PID namespace
        # was removed. Redis TTL or a parent PID disappearing is not enough.
        return True
    if container_namespace_has_stopped(client, process):
        return True
    leader = executor.get("rd_leader")
    pgid = executor.get("rd_process_group")
    return bool(
        process_has_stopped(process)
        and leader
        and pgid
        and leader.get("pid_namespace") == capture_process()["pid_namespace"]
        and not AlphaAgentLauncher._process_group_has_live_process(int(pgid))
    )


async def recover_executions():
    """Keep asyncpg on engine's loop; Docker and sync lease I/O use threads."""
    import docker

    budget = get_budget()
    store = AlphaMiningTaskPersistence()
    executions = await asyncio.to_thread(budget.store.active_executions, POOL)
    if not executions:
        return
    client = await asyncio.to_thread(docker.from_env, timeout=5)
    try:
        for execution in executions:
            executor = execution.get("executor") or {}
            if executor.get("kind") not in {"engine-local", "alpha-celery"}:
                continue
            if not await asyncio.to_thread(_ended, client, executor):
                continue
            owner = {
                "tenant_id": execution["tenant_id"],
                "user_id": execution["user_id"],
            }
            task_id = execution["task_id"]
            if executor.get("rdagent_child"):
                # A remote parent exiting does not prove its separately hosted
                # sandbox ended. Stop the child before releasing the parent slot.
                await asyncio.to_thread(
                    budget.store.request_stop,
                    POOL,
                    task_id,
                    owner["tenant_id"],
                    owner["user_id"],
                )
                await store.update(
                    task_id,
                    **owner,
                    expected_status=("pending", "queued", "running"),
                    status="stopping",
                )
                continue
            row = await store.get(task_id, **owner)
            if not row:
                continue
            state = row["status"]
            if state == "stopping":
                await store.confirm_cancelled(task_id, **owner)
            elif state not in {"failed", "completed", "cancelled"}:
                await store.update(
                    task_id,
                    **owner,
                    expected_status=("pending", "queued", "running"),
                    status="failed",
                    error_message="Execution container stopped before task completion",
                )
            latest = await store.get(task_id, **owner)
            if latest and latest["status"] in {"completed", "failed", "cancelled"}:
                finished = await asyncio.to_thread(
                    finish_execution,
                    task_id,
                    owner["tenant_id"],
                    owner["user_id"],
                    latest["status"],
                )
                if finished:
                    logger.warning(
                        "Recovered stopped factor mining execution %s (%s)",
                        task_id,
                        latest["status"],
                    )
    finally:
        await asyncio.to_thread(client.close)


async def _execute(launcher, row):
    """Settle only this process's lease, after its child group has ended."""
    owner = {"tenant_id": row["tenant_id"], "user_id": row["user_id"]}
    store = AlphaMiningTaskPersistence()
    task_id = row["task_id"]
    interrupted = True
    try:
        await launcher.run_queued(row)
        interrupted = False
    finally:
        budget = get_budget()
        executions = await asyncio.to_thread(budget.store.active_executions, POOL)
        owned = next(
            (
                item
                for item in executions
                if item["task_id"] == task_id
                and item["tenant_id"] == row["tenant_id"]
                and item["user_id"] == row["user_id"]
                and (item.get("executor") or {}).get("process") == capture_process()
            ),
            None,
        )
        if owned:
            task = launcher._tasks.get(task_id)
            stopped = task is None or task.process is None
            if not stopped:
                stopped = await launcher._terminate_process_group(task.process.pid)
            if stopped:
                latest = await store.get(task_id, **owner)
                if latest and latest["status"] == "stopping":
                    await store.confirm_cancelled(task_id, **owner)
                elif latest and latest["status"] not in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    state = task.status.value if task and not interrupted else "failed"
                    if state not in {"completed", "failed", "cancelled"}:
                        state = "failed"
                    await store.update(
                        task_id,
                        **owner,
                        expected_status=("pending", "queued", "running"),
                        status=state,
                        error_message=task.error_message
                        if task
                        else "Executor interrupted",
                    )
                latest = await store.get(task_id, **owner)
                if latest and latest["status"] in {"completed", "failed", "cancelled"}:
                    await asyncio.to_thread(
                        finish_execution,
                        task_id,
                        row["tenant_id"],
                        row["user_id"],
                        latest["status"],
                    )


async def run_queue():
    from backend.services.engine.alpha_agent.launcher import get_launcher

    launcher = get_launcher()
    store = AlphaMiningTaskPersistence()
    active = None
    try:
        while True:
            try:
                if active and active.done():
                    try:
                        active.result()
                    except Exception:
                        logger.exception("Factor mining execution failed")
                    active = None
                budget = await asyncio.to_thread(get_budget)
                if not await asyncio.to_thread(pool_provisioned):
                    await asyncio.sleep(5)
                    continue
                ready = await asyncio.to_thread(
                    budget.projection.client.get, budget.projection.ready_key
                )
                if not ready:
                    await asyncio.to_thread(recover_budget)
                await recover_executions()
                # OSS-ADAPT: OSS 单容器不支持远端挖掘节点，只走本地执行。
                if active is None:
                    queued = await asyncio.to_thread(
                        budget.store.queued_tasks,
                        POOL,
                        min_age_seconds=0,
                        limit=1,
                    )
                    if queued:
                        item = queued[0]
                        row = await store.get(
                            item["task_id"],
                            tenant_id=item["tenant_id"],
                            user_id=item["user_id"],
                        )
                        if row and row["status"] in {"pending", "queued"}:
                            # OSS-ADAPT: OSS 单容器不支持远端挖掘节点
                            # (celery-worker-factor-remote)，固定本地执行。
                            active = asyncio.create_task(_execute(launcher, row))
                        elif row and row["status"] in {
                            "completed",
                            "failed",
                            "cancelled",
                        }:
                            await asyncio.to_thread(
                                budget.store.cancel_queued,
                                POOL,
                                item["task_id"],
                                item["tenant_id"],
                                item["user_id"],
                            )
            except Exception:
                logger.exception(
                    "Factor mining queue deferred; durable occupancy retained"
                )
            await asyncio.sleep(2)
    finally:
        if active:
            active.cancel()
            try:
                await active
            except (asyncio.CancelledError, Exception):
                pass
