"""Celery task wrapper for monthly factor lifecycle evaluation."""

from __future__ import annotations

from backend.shared.persistent_event_loop import run as _run_async
from backend.services.engine.alpha_agent.factor_lifecycle_scheduler import (
    evaluate_monthly,
)
from backend.services.engine.qlib_app.celery_config import celery_app


@celery_app.task(
    bind=True,
    name="engine.tasks.evaluate_factor_lifecycle_monthly",
    max_retries=3,
    default_retry_delay=300,
    time_limit=60 * 60,
    soft_time_limit=55 * 60,
)
def evaluate_factor_lifecycle_monthly(self) -> dict[str, int]:
    """Persist each owner's latest completed lifecycle reading once per month."""
    try:
        return _run_async(evaluate_monthly())
    except Exception as exc:  # noqa: BLE001
        raise self.retry(exc=exc, countdown=300) from exc
