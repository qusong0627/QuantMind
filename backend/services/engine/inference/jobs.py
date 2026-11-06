"""Durable inference jobs, without business concurrency or queue quotas."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import socket
import threading
import time
from contextlib import closing
from dataclasses import asdict
from uuid import uuid4

from sqlalchemy import text

from backend.shared.inference_write_lock import sessions

logger = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "cancelled"}
active_job = contextvars.ContextVar("active_inference_job", default=None)
_pump = None
_threads = set()


class JobStore:
    def __init__(self, factory=None):
        self.factory = factory or sessions(os.getpid())

    def submit(self, tenant, user, payload, key):
        if not tenant or not user or not key or len(key) > 128:
            raise ValueError("Owner and a bounded idempotency key are required")
        request_identity = {k: v for k, v in payload.items() if k != "presentation"}
        fingerprint = hashlib.sha256(
            json.dumps(request_identity, sort_keys=True).encode()
        ).hexdigest()
        task = "infer_" + uuid4().hex
        with self.factory() as db, db.begin():
            # The unique owner/key index resolves concurrent duplicate submits.
            row = db.execute(
                text("""
                INSERT INTO qm_inference_jobs
                  (task_id,tenant_id,user_id,idempotency_key,fingerprint,request_payload)
                VALUES (:task,:tenant,:user,:key,:fingerprint,CAST(:payload AS jsonb))
                ON CONFLICT (tenant_id,user_id,idempotency_key) DO NOTHING
                RETURNING task_id
            """),
                {
                    "task": task,
                    "tenant": tenant,
                    "user": user,
                    "key": key,
                    "fingerprint": fingerprint,
                    "payload": json.dumps(payload),
                },
            ).scalar_one_or_none()
            if row:
                return self.get(task, tenant, user, db=db)
            existing = (
                db.execute(
                    text("""
                SELECT task_id,fingerprint FROM qm_inference_jobs
                WHERE tenant_id=:tenant AND user_id=:user AND idempotency_key=:key
            """),
                    {"tenant": tenant, "user": user, "key": key},
                )
                .mappings()
                .one()
            )
            if existing["fingerprint"] != fingerprint:
                raise ValueError("Idempotency key was already used for another request")
            return self.get(existing["task_id"], tenant, user, db=db)

    def get(self, task, tenant, user, *, db=None):
        if db is None:
            with self.factory() as connection:
                return self.get(task, tenant, user, db=connection)
        row = (
            db.execute(
                text("""
            SELECT * FROM qm_inference_jobs
            WHERE task_id=:task AND tenant_id=:tenant AND user_id=:user
        """),
                {"task": task, "tenant": tenant, "user": user},
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def claim(self, executor):
        with self.factory() as db, db.begin():
            row = (
                db.execute(
                    text("""
                SELECT * FROM qm_inference_jobs WHERE status='queued'
                ORDER BY created_at,task_id FOR UPDATE SKIP LOCKED LIMIT 1
            """)
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            job = dict(row)
            job["token"] = uuid4().hex
            db.execute(
                text("""
                UPDATE qm_inference_jobs SET status='running',token=:token,
                  heartbeat_at=NOW(),updated_at=NOW(),executor=CAST(:executor AS jsonb)
                WHERE task_id=:task AND tenant_id=:tenant AND user_id=:user
            """),
                {
                    "task": job["task_id"],
                    "tenant": job["tenant_id"],
                    "user": job["user_id"],
                    "token": job["token"],
                    "executor": json.dumps(executor),
                },
            )
            job["executor"] = executor
            return job

    def update(self, job, *, executor=None, result=None, status=None):
        with self.factory() as db, db.begin():
            return (
                db.execute(
                    text("""
                UPDATE qm_inference_jobs SET
                  executor=executor || CAST(:executor AS jsonb),
                  result_payload=COALESCE(CAST(:result AS jsonb),result_payload),
                  status=CASE WHEN status='cancelling' AND :status IN ('completed','failed')
                    THEN 'cancelled' ELSE COALESCE(:status,status) END,
                  heartbeat_at=NOW(),updated_at=NOW()
                WHERE task_id=:task AND tenant_id=:tenant AND user_id=:user
                  AND token=:token AND status IN ('running','cancelling')
            """),
                    {
                        "task": job["task_id"],
                        "tenant": job["tenant_id"],
                        "user": job["user_id"],
                        "token": job["token"],
                        "executor": json.dumps(executor or {}),
                        "result": json.dumps(result) if result is not None else None,
                        "status": status,
                    },
                ).rowcount
                == 1
            )

    def cancel(self, task, tenant, user):
        with self.factory() as db, db.begin():
            db.execute(
                text("""
                UPDATE qm_inference_jobs SET
                  status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END,
                  updated_at=NOW()
                WHERE task_id=:task AND tenant_id=:tenant AND user_id=:user
                  AND status IN ('queued','running','cancelling')
                  AND NOT COALESCE((executor->>'commit_started')::boolean,FALSE)
            """),
                {"task": task, "tenant": tenant, "user": user},
            )
            return self.get(task, tenant, user, db=db)

    def begin_commit(self, job):
        with self.factory() as db, db.begin():
            changed = db.execute(
                text("""
                UPDATE qm_inference_jobs SET executor=executor || jsonb_build_object('commit_started',TRUE)
                WHERE task_id=:task AND tenant_id=:tenant AND user_id=:user
                  AND token=:token AND status='running'
            """),
                {
                    "task": job["task_id"],
                    "tenant": job["tenant_id"],
                    "user": job["user_id"],
                    "token": job["token"],
                },
            ).rowcount
            if not changed:
                raise RuntimeError("Inference cancelled before persistence")

    def running(self):
        # Trusted engine recovery inventories all owners; public reads use get().
        with self.factory() as db:
            return [
                dict(r)
                for r in db.execute(
                    text("""
                SELECT * FROM qm_inference_jobs
                WHERE status IN ('running','cancelling')
            """)
                ).mappings()
            ]


def public_job(job):
    result = job.get("result_payload") or {}
    return {
        "task_id": job["task_id"],
        "status": job["status"],
        "cancellable": job["status"] in {"queued", "running"}
        and not job["executor"].get("commit_started"),
        "result": (result or None) if job["status"] in TERMINAL else None,
        "created_at": job["created_at"].isoformat(),
        "updated_at": job["updated_at"].isoformat(),
    }


class JobContext:
    def __init__(self, store, job):
        self.store, self.job = store, job
        self.last_check = 0.0
        self.cancelled = False
        self.loop = None

    def await_preparation(self, coroutine):
        """Keep async DB access on the engine loop while a worker waits."""
        from concurrent.futures import TimeoutError as FutureTimeout

        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            while True:
                self.check(force=True)
                try:
                    return future.result(timeout=1)
                except FutureTimeout:
                    if future.done():
                        raise
        except BaseException:
            future.cancel()
            raise

    def check(self, *, force=False):
        if self.cancelled:
            raise RuntimeError("Inference task cancelled or execution ownership lost")
        if force or time.monotonic() - self.last_check >= 1:
            row = self.store.get(
                self.job["task_id"], self.job["tenant_id"], self.job["user_id"]
            )
            self.last_check = time.monotonic()
            self.cancelled = (
                not row
                or row["token"] != self.job["token"]
                or row["status"] != "running"
            )
        if self.cancelled:
            raise RuntimeError("Inference task cancelled or execution ownership lost")

    def container_intent(self, name):
        self.check(force=True)
        if not self.store.update(self.job, executor={"container_name": name}):
            raise RuntimeError("Inference container intent could not be persisted")

    def labels(self):
        return {
            "quantmind.inference_job": self.job["task_id"],
            "quantmind.inference_token": self.job["token"],
        }

    def begin_commit(self):
        self.check(force=True)
        self.store.begin_commit(self.job)


def check_cancelled():
    ctx = active_job.get()
    if ctx:
        ctx.check(force=True)


def _controller(client):
    from backend.shared.process_witness import capture_process

    container = client.containers.get(socket.gethostname())
    return {
        "daemon": client.info()["ID"],
        "controller": container.id,
        "process": capture_process(),
    }


def _controller_ended(client, executor):
    from docker.errors import NotFound
    from backend.shared.container_witness import container_namespace_has_stopped
    from backend.shared.process_witness import capture_process

    if not executor.get("controller") or client.info()["ID"] != executor.get("daemon"):
        return False
    if executor.get("process") == capture_process():
        return False
    try:
        container = client.containers.get(executor["controller"])
    except NotFound:
        return True
    container.reload()
    if container.status in {"exited", "dead"}:
        return True
    return container_namespace_has_stopped(client, executor.get("process"))


def _stop_sandboxes(client, job):
    labels = [
        f"quantmind.inference_job={job['task_id']}",
        f"quantmind.inference_token={job['token']}",
        f"quantmind.tenant_id={job['tenant_id']}",
        f"quantmind.user_id={job['user_id']}",
    ]
    for container in client.containers.list(all=True, filters={"label": labels}):
        container.remove(force=True)
    return not client.containers.list(all=True, filters={"label": labels})


def _local_controller():
    """OSS 单容器本地执行见证：无 docker 时只保留本地进程看护。"""
    from backend.shared.process_witness import capture_process

    return {"process": capture_process()}


def recover_jobs():
    # OSS-ADAPT: OSS 单容器无 docker，容器恢复跳过（只保留本地进程看护语义）。
    try:
        import docker
    except ImportError:
        logger.warning("Inference recovery skipped: no docker in OSS container")
        return

    store = JobStore()
    with closing(docker.from_env()) as client:
        for job in store.running():
            try:
                finished = (
                    job["executor"].get("execution_finished") is True
                    and job["executor"].get("daemon") == client.info()["ID"]
                )
                if (
                    finished or _controller_ended(client, job["executor"])
                ) and _stop_sandboxes(client, job):
                    from backend.shared.inference_execution import ExecutionResult

                    result = job.get("result_payload") if finished else None
                    result = result or asdict(
                        ExecutionResult(
                            False,
                            -1,
                            "",
                            "",
                            error="Execution interrupted by engine restart; submit a new task",
                            failure_stage="recovery",
                        )
                    )
                    store.update(
                        job,
                        result=result,
                        status=(
                            "cancelled"
                            if job["status"] == "cancelling"
                            else "completed"
                            if result.get("success")
                            else "failed"
                        ),
                    )
            except Exception:
                logger.exception("Inference recovery deferred for %s", job["task_id"])


def _execute(store, job, loop):
    # OSS-ADAPT: OSS 单容器无 docker；执行见证降级为本地进程，跳过沙箱清理。
    try:
        import docker

        from backend.shared.inference_execution import ExecutionResult
    except ImportError:
        docker = None
        from backend.shared.inference_execution import ExecutionResult
    from backend.shared.model_registry import model_registry_service
    from backend.shared.redis_sentinel_client import get_redis_sentinel_client
    from backend.services.engine.inference.router_service import InferenceRouterService

    stopped = threading.Event()
    context = JobContext(store, job)
    context.loop = loop
    context_token = active_job.set(context)
    heartbeat = None
    result = None
    client = None
    try:
        if docker is None:
            if not store.update(job, executor=_local_controller()):
                raise RuntimeError("Execution claim lost")
        else:
            client = docker.from_env()
            if not store.update(job, executor=_controller(client)):
                raise RuntimeError("Execution claim lost")

        def renew():
            while not stopped.wait(15):
                try:
                    if not store.update(job):
                        context.cancelled = True
                        return
                except Exception:
                    context.cancelled = True
                    return

        heartbeat = threading.Thread(target=renew, daemon=True)
        heartbeat.start()
        payload = job["request_payload"]
        resolved = model_registry_service.resolve_effective_model_sync(
            tenant_id=job["tenant_id"],
            user_id=job["user_id"],
            strategy_id=payload.get("strategy_id"),
            model_id=payload.get("model_id"),
        )
        expected = payload.get("expected_model_id") or payload.get("model_id")
        if not resolved.get("storage_path") or (
            expected and resolved.get("effective_model_id") != expected
        ):
            raise ValueError("Model ownership or selection changed after submission")
        context.check(force=True)
        result = asdict(
            InferenceRouterService().run_daily_inference_script(
                date=payload["trade_date"],
                tenant_id=job["tenant_id"],
                user_id=job["user_id"],
                model_id=payload.get("model_id"),
                strategy_id=payload.get("strategy_id"),
                resolved_model=resolved,
                symbols=payload.get("symbols"),
                persist=payload.get("persist", True),
                pool_id=payload.get("pool_id"),
                redis_client=get_redis_sentinel_client()
                if payload.get("write_completion_marker")
                else None,
            )
        )
        if payload.get("presentation"):
            from backend.services.api.routers.model_training import (
                finalize_inference_job,
            )

            result = asyncio.run_coroutine_threadsafe(
                finalize_inference_job(job, result, resolved), loop
            ).result()
    except Exception as exc:
        logger.exception("Inference task failed: %s", job["task_id"])
        result = asdict(
            ExecutionResult(False, -1, "", "", error=str(exc), failure_stage="execute")
        )
    finally:
        stopped.set()
        if heartbeat:
            heartbeat.join(timeout=20)
        try:
            # No more containers or writes can start after this persisted fence.
            # Recovery may retry cleanup even while this controller remains alive.
            store.update(job, executor={"execution_finished": True}, result=result)
            # A failed Docker response never proves the sandbox ended.
            if client is None or _stop_sandboxes(client, job):
                current = store.get(job["task_id"], job["tenant_id"], job["user_id"])
                status = (
                    "cancelled"
                    if current and current["status"] == "cancelling"
                    else ("completed" if result and result.get("success") else "failed")
                )
                store.update(job, result=result, status=status)
        except Exception:
            logger.exception("Inference termination unconfirmed: %s", job["task_id"])
        if client:
            client.close()
        active_job.reset(context_token)


async def _run():
    # OSS-ADAPT: OSS 单容器无 docker；witness 降级为本地进程见证。
    try:
        import docker

        _docker_available = True
    except ImportError:
        docker = None
        _docker_available = False

    store = JobStore()
    while True:
        try:
            await asyncio.to_thread(recover_jobs)

            if not _docker_available:
                executor = await asyncio.to_thread(_local_controller)
            else:
                def witness():
                    with closing(docker.from_env()) as client:
                        return _controller(client)

                executor = await asyncio.to_thread(witness)
            while job := await asyncio.to_thread(store.claim, executor):
                thread = threading.Thread(
                    target=_execute,
                    args=(store, job, asyncio.get_running_loop()),
                    daemon=True,
                    name=f"inference-{job['task_id']}",
                )
                _threads.add(thread)
                try:
                    thread.start()
                except Exception as exc:
                    store.update(
                        job,
                        status="failed",
                        result={"success": False, "error": str(exc)},
                    )
                    raise
            _threads.intersection_update([t for t in _threads if t.is_alive()])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Inference queue dispatch deferred")
        await asyncio.sleep(1)


def start_jobs():
    global _pump
    _pump = asyncio.create_task(_run(), name="inference-jobs")


async def stop_jobs():
    if _pump:
        _pump.cancel()
        try:
            await _pump
        except asyncio.CancelledError:
            pass
