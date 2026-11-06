"""Fail-closed recovery for factor-job sandboxes abandoned by engine crashes."""

from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy import text

from backend.services.engine.alpha_agent.factor_job_budget import POOL, get_budget
from backend.shared.process_witness import process_has_stopped
from backend.shared.process_witness import capture_process
from backend.shared.container_witness import container_namespace_has_stopped

logger = logging.getLogger(__name__)


def _engine_has_stopped(client, witness):
    return process_has_stopped(witness) or container_namespace_has_stopped(
        client, witness
    )


def _node_rebooted(executor):
    node = os.getenv("FACTOR_MINING_NODE_ID", "")
    witness = executor.get("engine_process") or {}
    return bool(
        node
        and executor.get("execution_node_id") == node
        and witness.get("boot_id")
        and witness["boot_id"] != capture_process()["boot_id"]
    )


def _get_container(client: Any, name: str):
    try:
        container = client.containers.get(name)
    except Exception as exc:  # Docker SDK NotFound is the only absence proof.
        from docker.errors import NotFound

        if isinstance(exc, NotFound):
            return None
        raise
    if str(getattr(container, "name", "")) != name:
        raise RuntimeError("Docker returned a container with a different name")
    return container


def _container_is_owned(container: Any, name: str, expected: dict[str, Any]) -> bool:
    if container is None:
        return True
    container.reload()
    attrs = getattr(container, "attrs", {}) or {}
    config = attrs.get("Config") or {}
    labels = config.get("Labels") or {}
    if str(getattr(container, "name", "")) != name:
        return False
    if not expected or any(labels.get(key) != value for key, value in expected.items()):
        return False
    return True


def _stop_owned_container(
    client: Any, name: str, labels: dict[str, Any], container_id=None
) -> bool:
    container = None
    if container_id:
        try:
            container = client.containers.get(container_id)
        except Exception as exc:
            from docker.errors import NotFound

            if not isinstance(exc, NotFound):
                raise
    if container is None:
        container = _get_container(client, name)
    if not _container_is_owned(container, name, labels):
        return False
    if container is None:
        return True
    attrs = getattr(container, "attrs", {}) or {}
    state = attrs.get("State") or {}
    if state.get("Status") not in {"exited", "dead"}:
        container.stop(timeout=10)
        container.reload()
        attrs = getattr(container, "attrs", {}) or {}
        state = attrs.get("State") or {}
    if state.get("Status") not in {"exited", "dead"} or state.get("Running"):
        return False
    try:
        container.remove(force=False)
    except Exception:
        logger.warning("Stopped factor sandbox awaits Docker object cleanup: %s", name)
    return True


def _stop_and_remove_artifacts(
    client: Any, candidate: dict[str, Any], daemon_id: str
) -> bool:
    executor = candidate.get("executor") or {}
    if executor.get("daemon_id") != daemon_id:
        return False
    task_id = str(candidate.get("task_id") or "")
    tenant_id = str(candidate.get("tenant_id") or "")
    user_id = str(candidate.get("user_id") or "")
    token = str(candidate.get("token") or "")
    if not all((task_id, tenant_id, user_id, token)):
        return False
    required = {
        "quantmind.job_id": task_id,
        "quantmind.tenant_id": tenant_id,
        "quantmind.user_id": user_id,
        "quantmind.lease_token": token,
        "quantmind.resource_pool": POOL,
    }
    sandbox_name = executor.get("container_name")
    sandbox_labels = executor.get("container_labels")
    engine_process = executor.get("engine_process")
    generation = executor.get("execution_generation")
    dispatch_token = executor.get("dispatch_token")
    if (
        not isinstance(sandbox_name, str)
        or not sandbox_name
        or not isinstance(sandbox_labels, dict)
        or any(sandbox_labels.get(key) != value for key, value in required.items())
        or sandbox_labels.get("quantmind.role") != "sandbox"
        or not isinstance(engine_process, dict)
        or not dispatch_token
        or type(generation) is not int
        or generation <= 0
        or not (_engine_has_stopped(client, engine_process) or _node_rebooted(executor))
    ):
        return False

    if not _stop_owned_container(
        client,
        sandbox_name,
        sandbox_labels,
        container_id=executor.get("container_id"),
    ):
        return False
    related = executor.get("related_containers")
    if not isinstance(related, list):
        return False
    for item in related:
        if not isinstance(item, dict):
            return False
        name, labels = item.get("name"), item.get("labels")
        if (
            not isinstance(name, str)
            or not isinstance(labels, dict)
            or any(labels.get(key) != value for key, value in required.items())
            or not _stop_owned_container(client, name, labels)
        ):
            return False

    volume_name = executor.get("volume_name")
    volume_labels = executor.get("volume_labels")
    if not isinstance(volume_name, str) or not isinstance(volume_labels, dict):
        return False
    try:
        volume = client.volumes.get(volume_name)
    except Exception as exc:
        from docker.errors import NotFound

        if isinstance(exc, NotFound):
            volume = None
        else:
            raise
    if volume is not None:
        attrs = getattr(volume, "attrs", {}) or {}
        labels = attrs.get("Labels") or {}
        if str(getattr(volume, "name", "")) != volume_name or any(
            labels.get(key) != value for key, value in volume_labels.items()
        ):
            return False
        try:
            volume.remove(force=False)
        except Exception:
            logger.warning(
                "Stopped factor output volume awaits cleanup: %s", volume_name
            )
    return _engine_has_stopped(client, engine_process) or _node_rebooted(executor)


def _is_local_cleanup(
    budget, candidate: dict[str, Any], *, finished_only=False
) -> bool:
    """Identify trusted local work without an external executor or sandbox."""
    executor = candidate.get("executor") or {}
    if any(
        executor.get(key) is not None
        for key in (
            "container_name",
            "container_id",
            "container_labels",
            "volume_name",
            "volume_labels",
            "related_containers",
            "rdagent_child",
        )
    ):
        return False
    with budget.store.session_factory() as session:
        return session.execute(
            text(
                "SELECT kind FROM alpha_factor_jobs WHERE tenant_id=:tenant "
                "AND user_id=:user AND job_id=:job AND execution_generation=:generation "
                "AND status IN ('running','stopping')"
            ),
            {
                "tenant": candidate["tenant_id"],
                "user": candidate["user_id"],
                "job": candidate["task_id"],
                "generation": executor.get("execution_generation"),
            },
        ).scalar_one_or_none() in (
            {"pool_research", "purge", "purge_task"}
            if finished_only
            else {
                "cleanup",
                "recovery",
                "pool_research",
                "purge",
                "purge_task",
                "materialize",
                "serve_features",
            }
        )


def reconcile_orphaned_factor_jobs(client: Any) -> int:
    """Stop exact orphaned sandboxes and release only leases with positive proof."""
    budget = get_budget()
    if not budget.projection.client.get(budget.projection.ready_key):
        from backend.services.engine.alpha_agent.factor_job_budget import recover_budget

        recover_budget()
    daemon_id = str(client.info().get("ID") or "")
    if not daemon_id:
        raise RuntimeError("Docker daemon identity is unavailable")
    recovered = 0
    for candidate in budget.store.engine_execution_candidates(POOL):
        executor = candidate.get("executor") or {}
        if executor.get("execution_node_id") and executor[
            "execution_node_id"
        ] != os.getenv("FACTOR_MINING_NODE_ID"):
            continue
        engine_process = executor.get("engine_process")
        if not isinstance(engine_process, dict):
            continue
        try:
            local_cleanup = False
            if not (
                _engine_has_stopped(client, engine_process) or _node_rebooted(executor)
            ):
                if executor.get("engine_request_state") != "finished":
                    continue
                local_cleanup = _is_local_cleanup(budget, candidate, finished_only=True)
                if not local_cleanup:
                    continue
            local_cleanup = local_cleanup or _is_local_cleanup(budget, candidate)
            if not local_cleanup and not _stop_and_remove_artifacts(
                client, candidate, daemon_id
            ):
                continue

            def persist(
                session,
                status,
                witness,
                *,
                job_id=candidate["task_id"],
                tenant_id=candidate["tenant_id"],
                user_id=candidate["user_id"],
                cleanup=local_cleanup,
            ):
                generation = witness.get("execution_generation")
                result = session.execute(
                    text(
                        "UPDATE alpha_factor_jobs SET status=:status,updated_at=now(),"
                        "completed_at=COALESCE(completed_at,now()),"
                        "error_code=CASE WHEN :cleanup AND kind='cleanup' THEN 'ArtifactCleanupInterrupted' "
                        "WHEN :cleanup AND kind='pool_research' THEN 'PoolResearchInterrupted' "
                        "WHEN :cleanup AND kind='purge' THEN 'OwnerPurgeInterrupted' "
                        "WHEN :cleanup AND kind='purge_task' THEN 'OwnerTaskPurgeInterrupted' "
                        "WHEN :cleanup AND kind='materialize' THEN 'FactorMaterializationInterrupted' "
                        "WHEN :cleanup AND kind='serve_features' THEN 'FactorServingInterrupted' "
                        "WHEN :cleanup THEN 'FactorRecoveryInterrupted' ELSE error_code END,"
                        "error_message=CASE WHEN :cleanup AND kind='cleanup' THEN '清理进程已退出；可重新预览剩余文件后提交清理' "
                        "WHEN :cleanup AND kind='pool_research' THEN '原池研究执行已结束或进程退出；不会自动重新计算，可按需重新提交' "
                        "WHEN :cleanup AND kind='purge' THEN '清空挖掘数据进程已退出；可重新预览后提交清理' "
                        "WHEN :cleanup AND kind='purge_task' THEN '单任务清理进程已退出；可重新预览后提交清理' "
                        "WHEN :cleanup AND kind='materialize' THEN '因子物化进程已退出；该 generation 未发布，可重新提交物化' "
                        "WHEN :cleanup AND kind='serve_features' THEN '因子供数进程已退出；训练 generation 保持不变' "
                        "WHEN :cleanup THEN '恢复进程已退出；已保存的代码版本保留，请核对历史后重新提交' ELSE error_message END "
                        "WHERE job_id=:job "
                        "AND tenant_id=:tenant AND user_id=:user "
                        "AND execution_generation=:generation "
                        "AND status IN ('running','stopping')"
                        " AND (NOT :cleanup OR kind IN ('cleanup','recovery','pool_research','purge','purge_task','materialize','serve_features'))"
                    ),
                    {
                        "status": status,
                        "job": job_id,
                        "tenant": tenant_id,
                        "user": user_id,
                        "generation": generation,
                        "cleanup": cleanup,
                    },
                )
                if result.rowcount != 1:
                    raise RuntimeError("Factor job changed during orphan recovery")
                if cleanup:
                    building = (
                        session.execute(
                            text(
                                "SELECT generation_id FROM alpha_user_factor_generations "
                                "WHERE tenant_id=:tenant AND user_id=:user AND job_id=:job "
                                "AND dataset_key='rd_mined' AND status='building'"
                            ),
                            {"tenant": tenant_id, "user": user_id, "job": job_id},
                        )
                        .scalars()
                        .all()
                    )
                    if building:
                        from backend.services.engine.alpha_agent.materialization_store import (
                            discard_generation_files,
                        )

                        discard_generation_files(
                            tenant_id, user_id, [str(item) for item in building]
                        )
                    session.execute(
                        text(
                            "UPDATE alpha_user_factor_generations SET status='failed' "
                            "WHERE tenant_id=:tenant AND user_id=:user AND job_id=:job "
                            "AND dataset_key='rd_mined' AND status='building'"
                        ),
                        {"tenant": tenant_id, "user": user_id, "job": job_id},
                    )

            lease = budget.store.finalize_orphaned_engine_execution(
                POOL,
                candidate,
                terminal_state="failed",
                on_transition=persist,
            )
            if lease is None:
                continue
            try:
                budget.projection.release(lease)
            except Exception:
                logger.warning("Recovered factor lease awaits Redis projection repair")
            recovered += 1
        except Exception:
            logger.exception(
                "Factor job orphan recovery deferred: %s", candidate.get("task_id")
            )
    return recovered
