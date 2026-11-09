"""Gateway recovery uses its Docker capability; workers never gain the socket."""
# OSS port: 与企业版逐字一致。各节均为 try/except 包裹：
# ai_ide 段在 OSS 缺对应预算模块时会降级跳过；factor/research 段依赖在 OSS 均存在。
# 由 engine/main.py lifespan 以后台任务启动（30s 一轮，失败只记 warning）。

import asyncio
import logging

logger = logging.getLogger(__name__)


def reconcile_local_containers():
    import docker
    from backend.services.engine.qlib_app.resource_budget import (
        get_budget,
        recover_budget,
        sync_business_terminal_states,
    )

    budget = get_budget()
    if not budget.projection.client.get(budget.projection.ready_key):
        recover_budget()
    client = docker.from_env(timeout=5)
    try:
        try:
            from backend.services.engine.routers.ai_ide.resource_budget import (
                get_budget as get_ai_ide_budget,
                reconcile_containers as reconcile_ai_ide_containers,
            )

            reconcile_ai_ide_containers(client, budget=get_ai_ide_budget())
            reconcile_ai_ide_containers(client, budget=budget)
        except Exception:
            logger.warning("AI-IDE lease recovery deferred; occupancy retained")
        try:
            from backend.services.engine.alpha_agent.factor_job_reconciler import (
                reconcile_orphaned_factor_jobs,
            )

            recovered = reconcile_orphaned_factor_jobs(client)
            if recovered:
                logger.warning("Recovered %d orphaned factor job lease(s)", recovered)
        except Exception:
            logger.warning("Factor-job lease recovery deferred; occupancy retained")
        for task_id, witness in budget.store.execution_witnesses(budget.pool):
            if not witness:
                continue
            lease = budget.store.confirm_container_stopped(
                budget.pool, task_id, witness, client
            )
            if lease:
                try:
                    budget.projection.release(lease)
                except Exception:
                    logger.warning(
                        "Stopped container lease awaits Redis recovery: %s", task_id
                    )
        sync_business_terminal_states()
        try:
            from backend.services.engine.alpha_agent.resource_budget import (
                reconcile_terminal_leases,
            )

            released = reconcile_terminal_leases()
            if released:
                logger.warning(
                    "Recovered %d stale Alpha mining lease(s)", released
                )
        except Exception:
            logger.warning("Alpha mining lease recovery deferred; occupancy retained")
    finally:
        client.close()


async def run_gateway_reconciler():
    while True:
        try:
            from backend.services.engine.alpha_agent.research_requests import reconcile

            await asyncio.to_thread(reconcile)
        except Exception:
            logger.warning(
                "Short research request reconciliation deferred; occupancy retained"
            )
        try:
            await asyncio.to_thread(reconcile_local_containers)
        except Exception:
            logger.warning("Gateway resource reconciliation deferred")
        await asyncio.sleep(30)
