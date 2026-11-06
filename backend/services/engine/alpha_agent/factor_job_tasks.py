"""Dedicated Celery execution for owner-scoped factor research jobs."""

from __future__ import annotations

import logging
import os

import httpx

from backend.shared.auth import get_internal_call_secret
from backend.shared.persistent_event_loop import run as _run_async
from backend.shared.secret_redaction import redact_text
from backend.services.engine.alpha_agent.factor_jobs import AlphaFactorJobPersistence
from backend.services.engine.qlib_app.celery_config import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="engine.tasks.dispatch_factor_job_outbox", max_retries=0)
def dispatch_factor_job_outbox() -> dict[str, int]:
    """Publish durable outbox rows; duplicate delivery is fenced by job claim."""
    from backend.services.engine.qlib_app.celery_config import celery_app as app

    try:
        # OSS-ADAPT: OSS celery_config 未定义因子任务队列常量时回落到默认队列；
        # OSS 单容器不支持远端挖掘节点，不再按 kind 路由到 CELERY_QUEUE_FACTOR_REMOTE。
        from backend.services.engine.qlib_app.celery_config import (
            CELERY_QUEUE_FACTOR_JOBS,
        )
    except ImportError:
        from backend.services.engine.qlib_app.celery_config import (
            CELERY_QUEUE as CELERY_QUEUE_FACTOR_JOBS,
        )

    jobs = AlphaFactorJobPersistence()
    claimed = _run_async(jobs.claim_outbox(limit=32))
    published = 0
    retried = 0
    for item in claimed:
        try:
            # OSS-ADAPT: OSS 单容器不支持远端挖掘节点，固定走本地因子任务队列。
            app.send_task(
                "engine.tasks.run_factor_job",
                args=[item["job_id"]],
                queue=CELERY_QUEUE_FACTOR_JOBS,
                task_id=item["dispatch_token"],
            )
            _run_async(jobs.acknowledge_outbox(item))
            published += 1
        except Exception as exc:  # noqa: BLE001
            attempt = int(item.get("attempts") or 1)
            delay = min(900, 2 ** min(attempt, 9))
            _run_async(
                jobs.retry_outbox(
                    item,
                    delay_seconds=delay,
                    error=redact_text(f"{type(exc).__name__}: {exc}")[:1000],
                )
            )
            retried += 1
    return {
        "claimed": len(claimed),
        "published": published,
        "retried": retried,
        "mining_materializations_claimed": 0,
        "mining_materializations_submitted": 0,
        "mining_materializations_retried": 0,
    }


@celery_app.task(
    bind=True,
    name="engine.tasks.run_factor_job",
    max_retries=None,
    time_limit=60 * 60,
    soft_time_limit=59 * 60,
)
def run_factor_job(self, job_id: str) -> dict[str, str]:
    # OSS-ADAPT: OSS 单容器不支持远端挖掘节点，删除 mining_remote 远端分发分支，
    # 固定走本地 _run_factor_job。
    return _run_factor_job(self, job_id)


def _run_factor_job(
    self,
    job_id: str,
    *,
    engine_url: str | None = None,
    execution_node_id: str | None = None,
    prepare_only: bool = False,
) -> dict[str, str]:
    """Delegate a claimed job to engine; this worker never receives Docker access."""
    jobs = AlphaFactorJobPersistence()
    dispatch_token = str(getattr(self.request, "id", "") or "").strip()
    if not dispatch_token:
        import uuid

        dispatch_token = uuid.uuid4().hex

    record = _run_async(jobs.get_for_dispatch(job_id))
    if record is None:
        record = _run_async(jobs.get_for_replay(job_id, dispatch_token=dispatch_token))
    if not record:
        return {"job_id": job_id, "status": "not_queued"}
    if record["status"] == "stopping" and record["kind"] not in {
        "doc_parse",
        "doc_organize",
    }:
        return {"job_id": job_id, "status": "stopping"}

    tenant_id, user_id = record.get("tenant_id"), record.get("user_id")
    from backend.shared.owner_scope import OwnerScope

    owner = OwnerScope(tenant_id, user_id)
    from backend.shared.process_witness import capture_process

    if record["status"] == "queued":
        try:
            generation = _run_async(
                jobs.claim(
                    job_id,
                    tenant_id=owner.tenant_id,
                    user_id=owner.user_id,
                    executor={
                        "kind": "factor_job_celery",
                        "task_id": job_id,
                        "dispatch_token": dispatch_token,
                        "worker_process": capture_process(),
                        **(
                            {"execution_node_id": execution_node_id}
                            if execution_node_id
                            else {}
                        ),
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[factor-job] durable claim failed for %s", job_id)
            raise self.retry(exc=exc, countdown=15) from exc
        if generation is None:
            return {"job_id": job_id, "status": "not_claimed"}
    else:
        generation = int(record["execution_generation"])
    if prepare_only:
        # Release the input guard before the node takes its own guard. The next
        # lookup retains the same token, without an extra broker delivery.
        return {"job_id": job_id, "status": "prepared"}
    secret = get_internal_call_secret()
    if not secret:
        logger.critical("[factor-job] internal engine secret is unavailable")
        raise self.retry(countdown=60)
    engine_url = (
        engine_url or os.getenv("ENGINE_SERVICE_URL", "http://quantmind-engine:8001")
    ).rstrip("/")
    headers = {
        "X-Internal-Call": secret,
        "X-Tenant-Id": owner.tenant_id,
        "X-User-Id": owner.user_id,
    }
    try:
        response = httpx.post(
            f"{engine_url}/api/v1/internal/alpha-factor-jobs/run",
            headers=headers,
            json={
                "job_id": job_id,
                "execution_generation": generation,
                "dispatch_token": dispatch_token,
            },
            timeout=httpx.Timeout(3300.0, connect=10.0),
            trust_env=False,
        )
        if response.status_code >= 500:
            logger.error(
                "[factor-job] engine unavailable for job %s with HTTP %s",
                job_id,
                response.status_code,
            )
            raise self.retry(countdown=15)
        if response.is_error:
            logger.error(
                "[factor-job] engine rejected job %s with HTTP %s",
                job_id,
                response.status_code,
            )
            return {"job_id": job_id, "status": "engine_reconciliation_required"}
        body = response.json()
        status = body.get("status") if isinstance(body, dict) else None
        if status not in {"completed", "failed", "cancelled", "running", "stopping"}:
            logger.error(
                "[factor-job] engine returned an invalid receipt for %s", job_id
            )
            raise self.retry(countdown=15)
        if status in {"running", "stopping"}:
            raise self.retry(countdown=15)
        return {"job_id": job_id, "status": status}
    except (httpx.HTTPError, ValueError) as exc:
        # Retry with the same delivery token; engine atomically fences duplicate starts.
        logger.error(
            "[factor-job] engine receipt missing for %s: %s",
            job_id,
            redact_text(f"{type(exc).__name__}: {exc}")[:500],
        )
        raise self.retry(exc=exc, countdown=15) from exc
