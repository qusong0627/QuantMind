"""Durable lease budget for owner-scoped factor research child jobs."""

from __future__ import annotations

import argparse
import logging
import os
from functools import cache

from backend.shared.resource_execution import ResourceExecution
from backend.shared.resource_lease import LeaseUnavailable, ResourceLeasePool
from backend.shared.resource_lease_store import DurableLeaseStore

POOL = "alpha_factor_jobs"
logger = logging.getLogger(__name__)


def pending_limits() -> tuple[int, int]:
    per_user = int(os.getenv("FACTOR_JOBS_PENDING_PER_USER", "20"))
    global_limit = int(os.getenv("FACTOR_JOBS_PENDING_GLOBAL", "100"))
    if min(per_user, global_limit) <= 0:
        raise ValueError("Factor job pending limits must be positive")
    return per_user, global_limit


def get_budget() -> ResourceExecution:
    return _build_budget(os.getpid())


@cache
def _build_budget(process_id: int) -> ResourceExecution:
    del process_id
    # OSS-ADAPT: OSS 单容器不支持远端挖掘节点，全局容量默认 1，可经 FACTOR_JOBS_GLOBAL 覆盖。
    return build_execution_budget(
        POOL,
        per_user=int(os.getenv("FACTOR_JOBS_PER_USER", "1")),
        capacity=int(os.getenv("FACTOR_JOBS_GLOBAL", "1")),
    )


def build_execution_budget(
    pool: str, *, per_user: int, capacity: int
) -> ResourceExecution:
    """Common PG/Redis plumbing; each caller retains its own named pool policy."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.shared.database import SessionLocal
    import redis

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
        pool,
        per_user=per_user,
        capacity=capacity,
        ttl_ms=int(os.getenv("QLIB_EXECUTION_LEASE_TTL_MS", "60000")),
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
        pool,
        # Only factor placement replaces the shared FIFO reservation policy.
        fair_reservation=pool != POOL or capacity == 1,
    )


def require_ready_budget() -> ResourceExecution:
    budget = get_budget()
    try:
        if not budget.projection.client.get(budget.projection.ready_key):
            raise LeaseUnavailable("Factor job pool awaits reconciliation")
    except Exception as exc:
        if isinstance(exc, LeaseUnavailable):
            raise
        raise LeaseUnavailable("Factor job budget is unavailable") from exc
    return budget


def recover_budget(*, initialize: bool = False, legacy_drained: bool = False) -> None:
    from sqlalchemy import text

    budget = get_budget()
    if initialize:
        budget.store.provision_pool(POOL, legacy_drained=legacy_drained)
    budget.store.restore_projection(POOL, budget.projection)
    with budget.store.session_factory() as session, session.begin():
        session.execute(
            text(
                "UPDATE alpha_factor_jobs AS job SET status=lease.state, "
                "updated_at=now(), completed_at=COALESCE(job.completed_at,now()) "
                "FROM resource_task_leases AS lease WHERE lease.pool=:pool "
                "AND lease.task_id=job.job_id AND lease.tenant_id=job.tenant_id "
                "AND lease.user_id=job.user_id AND lease.state IN "
                "('completed','failed','cancelled') "
                "AND job.status IN ('queued','running','stopping')"
            ),
            {"pool": POOL},
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Maintain factor research job leases")
    parser.add_argument("action", choices=("initialize", "recover"))
    parser.add_argument("--legacy-drained", action="store_true")
    args = parser.parse_args()
    if args.action == "initialize" and not args.legacy_drained:
        parser.error(
            "initialize requires --legacy-drained after verified executor drain"
        )
    recover_budget(
        initialize=args.action == "initialize", legacy_drained=args.legacy_drained
    )
