"""Durable queue and execution budget for Alpha mining tasks."""

from __future__ import annotations

import argparse
import logging
import os
from contextlib import contextmanager, nullcontext
from functools import cache

from backend.shared.resource_execution import ExecutionCapacityFull, ResourceExecution
from backend.shared.resource_lease import LeaseUnavailable, ResourceLeasePool
from backend.shared.resource_lease_store import DurableLeaseStore

POOL = "alpha_mining"
logger = logging.getLogger(__name__)


class AlphaQueueFull(RuntimeError):
    """The bounded Alpha queue has no admission slot."""


def _limits() -> tuple[int, int, int, int, int]:
    ttl_ms = int(os.getenv("QLIB_EXECUTION_LEASE_TTL_MS", "60000"))
    if ttl_ms <= 0:
        raise ValueError("Alpha mining lease lifetime must be positive")
    # OSS-ADAPT: OSS 单容器不支持远端挖掘节点，全局容量固定为 1。
    # Per-owner execution remains one. Single local slot owns FIFO.
    return 1, 1, 0, 0, ttl_ms


def get_budget():
    return _build_budget(os.getpid())


@cache
def _build_budget(process_id: int) -> ResourceExecution:
    del process_id  # Cache is process-local; never reuse a prefork DB connection.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.shared.database import SessionLocal
    import redis

    active_user, active_global, _, _, ttl_ms = _limits()
    projection = ResourceLeasePool(
        redis.Redis(
            host=os.getenv("REDIS_HOST", "quantmind-redis"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD"),
            db=int(os.getenv("REDIS_DB", os.getenv("REDIS_DB_DEFAULT", "0"))),
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
        ),
        POOL,
        per_user=active_user,
        capacity=active_global,
        ttl_ms=ttl_ms,
    )
    dsn = SessionLocal.kw["bind"].url.set(drivername="postgresql+psycopg2")
    engine = create_engine(
        dsn,
        pool_size=2,
        max_overflow=2,
        pool_pre_ping=True,
        pool_timeout=5,
        connect_args={
            "connect_timeout": 5,
            "options": "-c statement_timeout=10000 -c lock_timeout=5000",
        },
    )
    return ResourceExecution(
        DurableLeaseStore(sessionmaker(engine)),
        projection,
        POOL,
        fair_reservation=active_global == 1,
    )


def submit_budget(task_id: str, tenant_id: str, user_id: str):
    """Check readiness; persistence inserts task and queued lease atomically."""
    if not all((task_id, tenant_id, user_id)):
        raise ValueError("Both task owner dimensions are required")
    budget = get_budget()
    try:
        if not budget.projection.client.get(budget.projection.ready_key):
            raise LeaseUnavailable("Alpha mining pool awaits reconciliation")
    except Exception as exc:
        if isinstance(exc, (LeaseUnavailable, ValueError)):
            raise
        raise LeaseUnavailable("Alpha mining budget is unavailable") from exc
    return budget


def cancel_queued(task_id: str, tenant_id: str, user_id: str) -> bool:
    budget = get_budget()
    return budget.store.cancel_queued(POOL, task_id, tenant_id, user_id)


def request_stop(task_id: str, tenant_id: str, user_id: str) -> str | None:
    budget = get_budget()
    return budget.store.request_stop(POOL, task_id, tenant_id, user_id)


@contextmanager
def begin_execution(
    task_id: str,
    tenant_id: str,
    user_id: str,
    *,
    executor_kind="alpha-celery",
    witness: dict | None = None,
):
    """Reserve the durable execution slot before changing business state or forking."""
    executor = {"kind": executor_kind}
    if witness:
        executor.update(witness)
    if executor_kind == "alpha-celery":
        from backend.shared.process_witness import capture_process

        executor["worker_process"] = capture_process()
    # OSS-ADAPT: OSS 单容器不支持远端挖掘节点，跳过多节点分配与远端数据守卫。
    guard = nullcontext()
    with guard:
        with get_budget().running(
            task_id, tenant_id, user_id, executor=executor,
            terminal_resolver=_resolve_terminal_state,
        ) as lease:
            yield lease


def _resolve_terminal_state(lease, snapshot) -> str | None:
    """Use the Alpha row as the terminal authority after executor completion."""
    if not snapshot:
        return None
    if (snapshot.get("executor") or {}).get("rdagent_child"):
        return None
    from sqlalchemy import text

    budget = get_budget()
    with budget.store.session_factory() as session:
        state = session.execute(
            text(
                "SELECT status FROM alpha_mining_tasks WHERE task_id=:task "
                "AND tenant_id=:tenant AND user_id=:user"
            ),
            {
                "task": lease.task_id,
                "tenant": lease.tenant_id,
                "user": lease.user_id,
            },
        ).scalar_one_or_none()
    if state in {"completed", "failed", "cancelled"}:
        return state
    # A stop request remains occupied until a separate observer proves that
    # the RD-Agent process group has exited.
    return None


def record_process_group(
    task_id: str, tenant_id: str, user_id: str, *, pgid: int, leader: dict
) -> bool:
    budget = get_budget()
    lease = budget.store.get_lease(POOL, task_id, tenant_id, user_id)
    if lease is None:
        return False
    return budget.store.transition(
        POOL,
        lease,
        "running",
        executor={"rd_process_group": pgid, "rd_leader": leader},
    )


def finish_execution(task_id: str, tenant_id: str, user_id: str, state: str) -> bool:
    if state not in {"failed", "cancelled", "completed"}:
        raise ValueError("Alpha execution recovery requires a terminal state")
    budget = get_budget()
    lease = budget.store.get_lease(POOL, task_id, tenant_id, user_id)
    if lease is None:
        return False
    finished = budget.store.transition(POOL, lease, state)
    if finished:
        try:
            budget.projection.release(lease)
        except LeaseUnavailable:
            pass
    return finished


def pool_provisioned() -> bool:
    from sqlalchemy import text

    budget = get_budget()
    with budget.store.session_factory() as session:
        return bool(
            session.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM resource_lease_pools WHERE pool=:pool)"
                ),
                {"pool": POOL},
            ).scalar_one()
        )


def recover_budget(*, initialize: bool = False, legacy_drained: bool = False) -> None:
    from sqlalchemy import text

    budget = get_budget()
    if initialize:
        budget.store.provision_pool(POOL, legacy_drained=legacy_drained)
    budget.store.restore_projection(POOL, budget.projection)
    max_age = int(os.getenv("ALPHA_AGENT_PENDING_MAX_AGE_SECONDS", "0"))
    if max_age > 0:
        budget.store.expire_queued(POOL, max_age_seconds=max_age)
    with budget.store.session_factory() as session, session.begin():
        session.execute(
            text(
                "UPDATE alpha_mining_tasks AS task SET status='failed', "
                "error_message=COALESCE(task.error_message, 'Alpha lease expired before dispatch') "
                "FROM resource_task_leases AS lease WHERE lease.pool=:pool "
                "AND lease.task_id=task.task_id AND lease.tenant_id=task.tenant_id "
                "AND lease.user_id=task.user_id AND lease.state='failed' "
                "AND task.status IN ('pending','queued')"
            ),
            {"pool": POOL},
        )


def reconcile_terminal_leases() -> int:
    """Release mining leases whose business task already reached a terminal state.

    A worker exception can mark ``alpha_mining_tasks`` terminal after the
    execution context has exited, leaving the lease in ``running``/``suspect``
    and blocking the single global slot. This reconciler is deliberately
    conservative: it only acts when the business row is already
    completed/failed/cancelled, so a live executor never loses occupancy.
    """
    from sqlalchemy import text

    budget = get_budget()
    with budget.store.session_factory() as session:
        rows = (
            session.execute(
                text(
                    "SELECT lease.task_id, lease.tenant_id, lease.user_id, task.status, lease.executor "
                    "FROM resource_task_leases AS lease "
                    "JOIN alpha_mining_tasks AS task "
                    "  ON task.task_id=lease.task_id "
                    " AND task.tenant_id=lease.tenant_id "
                    " AND task.user_id=lease.user_id "
                    "WHERE lease.pool=:pool "
                    "AND lease.state IN ('reserved','running','suspect') "
                    "AND task.status IN ('completed','failed','cancelled')"
                ),
                {"pool": POOL},
            )
            .mappings()
            .all()
        )
    released = 0
    for row in rows:
        if (row.get("executor") or {}).get("rdagent_child"):
            continue
        if finish_execution(
            row["task_id"], row["tenant_id"], row["user_id"], row["status"]
        ):
            released += 1
            logger.warning(
                "Released stale Alpha lease %s (business state %s)",
                row["task_id"],
                row["status"],
            )
    return released


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Maintain Alpha mining execution budget"
    )
    parser.add_argument("action", choices=("initialize", "recover"))
    parser.add_argument("--legacy-drained", action="store_true")
    args = parser.parse_args()
    recover_budget(
        initialize=args.action == "initialize", legacy_drained=args.legacy_drained
    )
