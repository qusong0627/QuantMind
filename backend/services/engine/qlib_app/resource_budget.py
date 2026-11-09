"""Backtest/optimization budget shared by HTTP and Celery entry points."""
# OSS port: 语义与企业版一致（POOL="backtest" 独立池，不与 alpha_mining 混用；
# 配额默认值沿用企业版：每用户 1 / 全局 4，与 worker --concurrency=4 对齐，
# 均可经 QLIB_* 环境变量覆盖）。唯一 OSS 适配：本模块不依赖计费，
# publish/consume 两端的 billing hook 由调用方按“无计费”分支跳过。

import os
import logging
from functools import cache
from uuid import uuid4

from fastapi import HTTPException

from backend.shared.resource_execution import ResourceExecution
from backend.shared.resource_lease import LeaseUnavailable, ResourceLeasePool
from backend.shared.resource_lease_store import DurableLeaseStore

POOL = "backtest"
logger = logging.getLogger(__name__)


def get_budget():
    return _build_budget(os.getpid())


@cache
def _build_budget(process_id):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from backend.shared.database import SessionLocal
    from backend.services.engine.qlib_app.tasks import _get_redis_client

    projection = ResourceLeasePool(
        _get_redis_client(),
        POOL,
        per_user=int(os.getenv("QLIB_BACKTEST_PER_USER_LIMIT", "1")),
        capacity=int(os.getenv("QLIB_BACKTEST_SLOTS", "4")),
        ttl_ms=int(os.getenv("QLIB_EXECUTION_LEASE_TTL_MS", "60000")),
    )
    # Created lazily in the executing process, never reuse a prefork connection.
    # The lease store is synchronous while production DATABASE_URL carries the
    # asyncpg driver, so reusing that URL verbatim fails outside greenlet context.
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
    return ResourceExecution(DurableLeaseStore(sessionmaker(engine)), projection, POOL)


def submit_budget(task_id, tenant_id, user_id):
    """Reserve bounded pending capacity before any broker or cloud side effect."""
    try:
        budget = get_budget()
        if not budget.projection.client.get(budget.projection.ready_key):
            raise LeaseUnavailable("Pool recovery barrier is closed")
        outcome = budget.store.enqueue(
            budget.pool,
            task_id,
            tenant_id,
            user_id,
            user_budget=int(os.getenv("QLIB_PENDING_PER_USER", "3")),
            total_budget=int(os.getenv("QLIB_PENDING_GLOBAL", "64")),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="任务名额服务不可用，请稍后重试"
        ) from exc
    if outcome in {"user_full", "global_full"}:
        raise HTTPException(status_code=429, detail="待执行任务已达到排队上限")
    if outcome != "queued":
        raise HTTPException(status_code=409, detail="任务已存在或归属不一致")
    return budget


def cancel_pending(task_id, tenant_id, user_id):
    budget = get_budget()
    return budget.store.cancel_queued(budget.pool, task_id, tenant_id, user_id)


def sync_business_terminal_states():
    """Repair terminal history after a failed cache write or worker termination."""
    from sqlalchemy import text

    budget = get_budget()
    with budget.store.session_factory() as session, session.begin():
        # Stock-symbol normalization triggers run on every row update, including
        # status-only repairs. Large historical result JSON can exceed the
        # lease store's normal 10s statement timeout, leaving terminal state
        # mismatches to be retried forever by the monitor.
        session.execute(text("SET LOCAL statement_timeout = '60000ms'"))
        for table in (
            "qlib_backtest_runs",
            "qlib_optimization_runs",
            "strategy_loop_tasks",
        ):
            if (
                session.execute(
                    text("SELECT to_regclass(:table)"), {"table": table}
                ).scalar()
                is None
            ):
                continue
            completed_at = (
                ", completed_at=NOW()" if table != "strategy_loop_tasks" else ""
            )
            session.execute(
                text(
                    f"UPDATE {table} AS business SET status=lease.state{completed_at} "
                    "FROM resource_task_leases AS lease WHERE lease.pool=:pool "
                    "AND lease.task_id=business.task_id AND lease.tenant_id=business.tenant_id "
                    "AND lease.user_id=business.user_id AND lease.state IN ('failed','cancelled') "
                    "AND business.status NOT IN ('completed','failed','cancelled')"
                ),
                {"pool": budget.pool},
            )
        if (
            session.execute(
                text("SELECT to_regclass(:table)"), {"table": "pipeline_runs"}
            ).scalar()
            is not None
        ):
            session.execute(
                text(
                    "UPDATE pipeline_runs AS business SET status='failed', stage='failed', "
                    "error_message=COALESCE(business.error_message, 'Execution stopped before terminal state') "
                    "FROM resource_task_leases AS lease WHERE lease.pool=:pool "
                    "AND lease.task_id=business.run_id AND lease.tenant_id=business.tenant_id "
                    "AND lease.user_id=business.user_id AND lease.state IN ('failed','cancelled') "
                    "AND business.status IN ('queued','running')"
                ),
                {"pool": budget.pool},
            )


def recover_budget(*, initialize=False, legacy_drained=False):
    """Recover an existing pool; initial creation is an explicit maintenance step."""
    budget = get_budget()
    if initialize:
        budget.store.provision_pool(budget.pool, legacy_drained=legacy_drained)
    budget.store.restore_projection(budget.pool, budget.projection)
    budget.store.expire_queued(
        budget.pool,
        max_age_seconds=int(os.getenv("QLIB_PENDING_MAX_AGE_SECONDS", "21600")),
    )
    sync_business_terminal_states()


async def publish_owned_task(
    task,
    payload,
    tenant_id,
    user_id,
    persist,
    *,
    task_id=None,
    task_args=None,
    task_options=None,
    before_publish=None,
    on_publish_failure=None,
):
    """Persist ownership before broker delivery; use a server-generated task ID."""
    task_id = task_id or uuid4().hex
    budget = submit_budget(task_id, tenant_id, user_id)
    saved = False
    billing_started = False
    try:
        if before_publish is not None:
            billing_started = True
            prepared = before_publish(task_id)
            if hasattr(prepared, "__await__"):
                await prepared
        await persist(task_id, "pending")
        saved = True
        args = task_args(task_id) if task_args else [payload]
        task.apply_async(args=args, task_id=task_id, **(task_options or {}))
    except Exception as exc:
        safe_to_release = not saved
        try:
            cancelled = budget.store.cancel_queued(
                budget.pool, task_id, tenant_id, user_id
            )
            safe_to_release = safe_to_release or bool(cancelled)
            if saved and cancelled:
                await persist(task_id, "failed")
        except Exception:
            logger.warning("Submission cleanup requires reconciliation: %s", task_id)
        if billing_started and safe_to_release and on_publish_failure is not None:
            try:
                cleanup = on_publish_failure(task_id)
                if hasattr(cleanup, "__await__"):
                    await cleanup
            except Exception:
                logger.warning(
                    "Billing hold cleanup requires reconciliation: %s", task_id
                )
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=503, detail="任务提交失败，请稍后重试") from exc
    return task_id


def begin_execution(task_id, tenant_id, user_id):
    """Enter a queued worker execution; duplicate/nonqueued delivery is rejected."""
    from backend.shared.process_witness import capture_process

    from backend.shared.resource_execution import ExecutionCapacityFull

    budget = get_budget()
    guard = budget.running(
        task_id,
        tenant_id,
        user_id,
        executor={
            "kind": "celery",
            "process": capture_process(),
        },
    )
    try:
        guard.__enter__()
    except ExecutionCapacityFull as exc:
        state = budget.store.task_state(budget.pool, task_id, tenant_id, user_id)
        if state == "queued":
            return None
        if state is not None:
            from celery.exceptions import Ignore

            raise Ignore() from exc
        raise LeaseUnavailable("Task missing or foreign") from exc
    return guard


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Maintain durable backtest resource leases"
    )
    parser.add_argument("action", choices=["initialize", "recover"])
    parser.add_argument(
        "--legacy-drained",
        action="store_true",
        help="Only after legacy producers, workers and queues were verified drained",
    )
    args = parser.parse_args()
    recover_budget(
        initialize=args.action == "initialize", legacy_drained=args.legacy_drained
    )
