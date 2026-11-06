"""Reconcile Alpha mining stop requests after a Celery task child disappears."""

from __future__ import annotations

import logging
import os
import signal
from datetime import datetime, timezone
from typing import Any

from backend.shared.persistent_event_loop import run as run_on_shared_loop
from backend.shared.process_witness import capture_process, process_has_stopped
from backend.services.engine.alpha_agent.launcher import AlphaAgentLauncher
from backend.services.engine.qlib_app.services.rd_agent_persistence import (
    AlphaMiningTaskPersistence,
)

logger = logging.getLogger(__name__)


def _settle_mining_billing(
    task_id: str, tenant_id: str, user_id: str, state: str
) -> bool:
    # OSS-ADAPT: OSS 无计费(side billing)，删除 billing hold/capture 调用。
    return False


def _same_process_namespace(witness: dict[str, Any]) -> bool:
    try:
        local = capture_process()
        return all(
            witness.get(key) == local[key]
            for key in ("host", "boot_id", "pid_namespace")
        )
    except (OSError, ValueError, IndexError, KeyError, TypeError):
        return False


def reconcile_stopping_tasks(store=None, *, now: datetime | None = None) -> dict[str, int]:
    """Signal known child groups and release status only after group exit is observed.

    This runs in the Celery worker parent, which shares the task child's PID
    namespace. Records from another host/namespace or without a witness stay in
    ``stopping`` and continue to consume admission capacity.
    """
    task_store = store or AlphaMiningTaskPersistence()
    current_time = now or datetime.now(timezone.utc)
    counts = {"observed": 0, "signalled": 0, "confirmed": 0, "unknown": 0}
    rows = run_on_shared_loop(task_store.list_stopping())
    for row in rows:
        counts["observed"] += 1
        executor = row.get("executor_json") or {}
        pgid = executor.get("process_group")
        leader = executor.get("leader")
        if not pgid or not isinstance(leader, dict) or not _same_process_namespace(leader):
            counts["unknown"] += 1
            continue
        try:
            pgid = int(pgid)
            if pgid <= 0:
                raise ValueError("invalid process group")
            if not AlphaAgentLauncher._process_group_has_live_process(pgid):
                if run_on_shared_loop(
                    task_store.confirm_cancelled(
                        str(row["task_id"]),
                        tenant_id=str(row["tenant_id"]),
                        user_id=str(row["user_id"]),
                    )
                ):
                    from backend.services.engine.alpha_agent.resource_budget import (
                        finish_execution,
                    )

                    released = finish_execution(
                        str(row["task_id"]),
                        str(row["tenant_id"]),
                        str(row["user_id"]),
                        "cancelled",
                    )
                    if released:
                        _settle_mining_billing(
                            str(row["task_id"]),
                            str(row["tenant_id"]),
                            str(row["user_id"]),
                            "cancelled",
                        )
                    counts["confirmed"] += 1
                continue
            updated_at = row.get("updated_at")
            if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
                counts["unknown"] += 1
                continue
            age_seconds = max(0.0, (current_time - updated_at).total_seconds())
            os.killpg(pgid, signal.SIGKILL if age_seconds >= 60 else signal.SIGTERM)
            counts["signalled"] += 1
        except (OSError, ValueError, KeyError, TypeError):
            counts["unknown"] += 1
    return counts


def reconcile_orphaned_workers(
    store=None, *, now: datetime | None = None
) -> dict[str, int]:
    """Recover Celery executions only after both worker and RD child are observed.

    Missing or foreign process witnesses stay occupied. Local engine executions
    are deliberately excluded because this Celery parent cannot observe their
    process namespace.
    """
    from backend.services.engine.alpha_agent.resource_budget import (
        POOL,
        finish_execution,
        get_budget,
    )

    task_store = store or AlphaMiningTaskPersistence()
    budget = get_budget()
    current_time = now or datetime.now(timezone.utc)
    counts = {"observed": 0, "signalled": 0, "confirmed": 0, "unknown": 0}
    for lease in budget.store.active_executions(POOL):
        executor = lease.get("executor") or {}
        if executor.get("kind") != "alpha-celery":
            continue
        counts["observed"] += 1
        worker = executor.get("worker_process")
        pgid = executor.get("rd_process_group")
        leader = executor.get("rd_leader")
        if (
            not isinstance(worker, dict)
            or not _same_process_namespace(worker)
            or not process_has_stopped(worker)
            or not pgid
            or not isinstance(leader, dict)
            or not _same_process_namespace(leader)
        ):
            counts["unknown"] += 1
            continue
        try:
            pgid = int(pgid)
            if pgid <= 0:
                raise ValueError("invalid process group")
            if AlphaAgentLauncher._process_group_has_live_process(pgid):
                updated_at = lease.get("updated_at")
                if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
                    counts["unknown"] += 1
                    continue
                age_seconds = max(0.0, (current_time - updated_at).total_seconds())
                os.killpg(pgid, signal.SIGKILL if age_seconds >= 60 else signal.SIGTERM)
                counts["signalled"] += 1
                continue

            task_id = str(lease["task_id"])
            tenant_id = str(lease["tenant_id"])
            user_id = str(lease["user_id"])
            business = run_on_shared_loop(
                task_store.get(task_id, tenant_id=tenant_id, user_id=user_id)
            )
            if not business:
                counts["unknown"] += 1
                continue
            state = str(business.get("status") or "")
            if state == "stopping":
                confirmed = run_on_shared_loop(
                    task_store.confirm_cancelled(
                        task_id, tenant_id=tenant_id, user_id=user_id
                    )
                )
                if not confirmed:
                    counts["unknown"] += 1
                    continue
                state = "cancelled"
            elif state not in {"completed", "failed", "cancelled"}:
                updated = run_on_shared_loop(
                    task_store.update(
                        task_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        expected_status=("pending", "queued", "running"),
                        status="failed",
                        error_message="Celery worker exited; RD-Agent process group confirmed stopped",
                    )
                )
                if not updated:
                    latest = run_on_shared_loop(
                        task_store.get(task_id, tenant_id=tenant_id, user_id=user_id)
                    )
                    state = str((latest or {}).get("status") or "")
                    if state not in {"completed", "failed", "cancelled"}:
                        counts["unknown"] += 1
                        continue
                else:
                    state = "failed"
            if finish_execution(task_id, tenant_id, user_id, state):
                _settle_mining_billing(task_id, tenant_id, user_id, state)
                counts["confirmed"] += 1
            else:
                counts["unknown"] += 1
        except (OSError, ValueError, KeyError, TypeError):
            counts["unknown"] += 1
    return counts
