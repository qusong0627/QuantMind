"""Celery parent-process exit observations, never a revoke acknowledgement."""
# OSS port: 与企业版逐字一致（引用路径本就指向 qlib_app.resource_budget，
# 随本批次补齐后直接可用）。由 celery_config 按 QLIB_LEASE_MONITOR_ENABLED
# 接入 worker bootstep（默认关闭，见 celery_config 末尾）。

import logging
import os

from celery.worker.request import Request
from celery import bootsteps

from backend.shared.process_witness import capture_process

logger = logging.getLogger(__name__)


class LeaseRequest(Request):
    def on_accepted(self, pid, time_accepted):
        try:
            self._process_witness = capture_process(pid)
        except (OSError, ValueError, IndexError):
            self._process_witness = None
        return super().on_accepted(pid, time_accepted)

    def on_failure(self, exc_info, *args, **kwargs):
        try:
            # The pool callback can precede actual process exit. The store checks
            # /proc identity and only releases after confirmed death/PID reuse.
            witness = getattr(self, "_process_witness", None)
            if witness:
                from backend.services.engine.qlib_app.resource_budget import get_budget

                budget = get_budget()
                lease = budget.store.confirm_process_stopped(
                    budget.pool, self.id, witness
                )
                if lease:
                    try:
                        budget.projection.release(lease)
                    except Exception:
                        logger.warning(
                            "Stopped task projection awaits recovery: %s", self.id
                        )
        except Exception:
            logger.warning("Worker exit awaits durable reconciliation: %s", self.id)
        return super().on_failure(exc_info, *args, **kwargs)


class LeaseExitMonitor(bootsteps.StartStopStep):
    """Parent timer observes deaths missed by callbacks; no queue consumer added."""

    requires = {"celery.worker.components:Pool", "celery.worker.components:Timer"}

    def start(self, worker):
        self._timer = worker.timer.call_repeatedly(30, self.reconcile)

    def stop(self, worker):
        if getattr(self, "_timer", None):
            self._timer.cancel()

    def reconcile(self):
        try:
            from backend.services.engine.alpha_agent.resource_budget import (
                get_budget as get_alpha_budget,
                pool_provisioned as alpha_pool_provisioned,
                recover_budget as recover_alpha_budget,
            )

            alpha_budget = get_alpha_budget()
            if alpha_pool_provisioned() and not alpha_budget.projection.client.get(
                alpha_budget.projection.ready_key
            ):
                # This restores an initialized pool after Redis loss. Initial
                # provisioning remains an explicit drained-state operation.
                recover_alpha_budget()
        except Exception:
            logger.warning("Alpha lease recovery deferred; admission remains closed")
        try:
            from backend.services.engine.alpha_agent.stop_reconciler import (
                reconcile_orphaned_workers,
                reconcile_stopping_tasks,
            )

            reconcile_stopping_tasks()
            reconcile_orphaned_workers()
        except Exception:
            logger.warning("Alpha stop reconciliation deferred; occupancy retained")
        try:
            from backend.services.engine.qlib_app.resource_budget import get_budget

            budget = get_budget()
            if not budget.projection.client.get(budget.projection.ready_key):
                from backend.services.engine.qlib_app.resource_budget import (
                    recover_budget,
                )

                # Existing durable pools recover; this never initializes a new
                # pool or assumes unknown legacy workers have stopped.
                recover_budget()
            for task_id, witness in budget.store.execution_witnesses(budget.pool):
                if not witness:
                    continue
                lease = budget.store.confirm_process_stopped(
                    budget.pool, task_id, witness
                )
                if lease:
                    try:
                        budget.projection.release(lease)
                    except Exception:
                        logger.warning(
                            "Stopped execution awaits Redis recovery: %s", task_id
                        )
            budget.store.expire_queued(
                budget.pool,
                max_age_seconds=int(os.getenv("QLIB_PENDING_MAX_AGE_SECONDS", "21600")),
            )
            from backend.services.engine.qlib_app.resource_budget import (
                sync_business_terminal_states,
            )

            sync_business_terminal_states()
        except Exception:
            logger.warning("Lease exit monitor deferred; occupancy retained")
