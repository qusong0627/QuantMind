"""Shared admission for synchronous HTTP computations."""
# OSS port: 唯一改动是预算来源由 qlib_app.resource_budget（企业版同名模块，
# 随本批次补齐）提供；其余逐字一致。

from contextlib import contextmanager
import asyncio
import logging
import sys
from uuid import uuid4

from fastapi import HTTPException
from backend.shared.resource_execution import ExecutionCapacityFull
from backend.shared.resource_lease import LeaseUnavailable
from backend.services.engine.qlib_app.resource_budget import submit_budget
from backend.shared.process_witness import capture_process

logger = logging.getLogger(__name__)
_http_executions = set()


async def run_http_execution(tenant_id, user_id, computation):
    """A disconnected waiter cannot cancel computation or release its budget.

    The owned task persists its result and exits the lease only after computation
    returns. Keep a strong reference and observe detached failures. Admission and
    cleanup run outside the event loop, including cancellation during admission.
    """

    async def execute():
        guard = heavy_execution_slot(tenant_id, user_id)
        execution = await asyncio.to_thread(guard.__enter__)
        try:
            if getattr(computation, "_accepts_execution", False):
                result = await computation(*execution)
            else:
                result = await computation()
        except BaseException:
            await asyncio.to_thread(guard.__exit__, *sys.exc_info())
            raise
        else:
            await asyncio.to_thread(guard.__exit__, None, None, None)
            return result

    task = asyncio.create_task(execute(), name="http-heavy-execution")
    _http_executions.add(task)

    def finished(completed):
        _http_executions.discard(completed)
        if not completed.cancelled() and completed.exception() is not None:
            logger.warning("HTTP computation ended with an error")

    task.add_done_callback(finished)
    return await asyncio.shield(task)


@contextmanager
def heavy_execution_slot(tenant_id: str, user_id: str):
    task_id = f"http-{uuid4().hex}"
    budget = submit_budget(task_id, tenant_id, user_id)
    try:
        with budget.running(
            task_id,
            tenant_id,
            user_id,
            executor={"kind": "http", "process": capture_process()},
        ) as lease:
            yield lease, budget
    except ExecutionCapacityFull as exc:
        raise HTTPException(status_code=429, detail="回测运行名额已满") from exc
    except LeaseUnavailable as exc:
        raise HTTPException(status_code=503, detail="回测名额服务不可用") from exc
    finally:
        # A synchronous HTTP request cannot wait in the Celery pending queue.
        # Only a still-queued record is cancelled; running/terminal are untouched.
        try:
            budget.store.cancel_queued(budget.pool, task_id, tenant_id, user_id)
        except Exception:
            logger.warning("Pending cleanup requires reconciliation: %s", task_id)
