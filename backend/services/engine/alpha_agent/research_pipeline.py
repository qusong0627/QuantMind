"""Durable finite research runs; every expensive step reuses existing admission."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import date
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text

from backend.services.engine.alpha_agent.api_models import MiningSubmitRequest
from backend.services.engine.auth_context import get_authenticated_identity
from backend.shared.database_manager_v2 import get_session
from backend.shared.owner_scope import OwnerScope
from backend.shared.secret_redaction import redact_text

logger = logging.getLogger(__name__)
router = APIRouter()
TERMINAL = {"completed", "failed", "cancelled", "rejected"}


class PipelineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mining: MiningSubmitRequest | None = None
    mining_task_id: str | None = Field(None, min_length=1, max_length=128)
    training: dict
    inference_date: date
    min_abs_rank_ic: float = Field(0.02, ge=0, le=1, allow_inf_nan=False)
    max_factors: int = Field(12, ge=1, le=20)
    activate_on_success: bool = False

    @model_validator(mode="after")
    def validate_plan(self):
        if (self.mining is None) == (self.mining_task_id is None):
            raise ValueError("Provide either a mining request or an owned mining task")
        if self.mining is not None and (
            not self.mining.valid_start or not self.mining.valid_end
        ):
            raise ValueError(
                "Automatic research requires an explicit validation window"
            )
        if not self.training.get("factor_source") or not self.training.get(
            "factor_catalog_version"
        ):
            raise ValueError(
                "Training requires a published factor source and catalog version"
            )
        if not self.training.get("pool_id"):
            raise ValueError(
                "Automatic research requires an explicit training stock pool"
            )
        if self.training.get("smoke") or self.training.get("job_kind"):
            raise ValueError("Research pipelines require actual model training")
        model_type = str(self.training.get("model_type") or "lightgbm").lower()
        if model_type not in {
            "lightgbm",
            "xgboost",
            "catboost",
            "random_forest",
            "linear",
        } or self.training.get("model_types") not in (None, [], [model_type]):
            raise ValueError(
                "Research pipelines currently support one tabular CPU model; private sequence-window serving is unavailable"
            )
        from backend.shared.training.constraints import (
            factor_count_limits,
            validate_training_span,
        )

        base_count = len(set(self.training.get("features") or []))
        minimum, maximum = factor_count_limits(model_type)
        if base_count + self.max_factors < minimum or (
            maximum and base_count >= maximum
        ):
            raise ValueError(
                "Training feature selection cannot meet the model factor-count policy with private factors"
            )
        if not self.training.get("train_start") or not self.training.get("test_end"):
            raise ValueError(
                "Research pipelines require explicit training and test dates"
            )
        validate_training_span(self.training["train_start"], self.training["test_end"])
        if not self.training.get("test_start"):
            raise ValueError(
                "Research pipelines require an explicit untouched model test window"
            )
        if self.mining is not None and self.mining.valid_end >= str(
            self.training["test_start"]
        ):
            raise ValueError("Model test window must start after research validation")
        ends = [
            str(self.training.get(key) or "")
            for key in ("train_end", "valid_end", "test_end")
        ]
        if (
            not self.training.get("test_end")
            or max(ends) >= self.inference_date.isoformat()
        ):
            raise ValueError("Inference must use a date after the training test window")
        if (
            len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode())
            > 256 * 1024
        ):
            raise ValueError("Research plan exceeds 256 KiB")
        return self


def _key(run_id: str, stage: str, item: str = "") -> str:
    return (
        "research-" + hashlib.sha256(f"{run_id}\0{stage}\0{item}".encode()).hexdigest()
    )


def _inference_key(run_id: str, snapshot: dict) -> str:
    attempt = int(snapshot.get("inference_attempt") or 0)
    return _key(run_id, "inference", str(attempt) if attempt else "")


def _retry_snapshot(row: dict, expected: str, child_status: str) -> dict:
    from copy import deepcopy

    snapshot = deepcopy(row["snapshot"])
    if snapshot.get("retry_of") == expected:
        return snapshot
    if (
        row["status"] != "failed"
        or row["stage"] != "inference"
        or snapshot.get("inference_id") != expected
        or child_status != "failed"
    ):
        raise ValueError("Only a failed inference stage can be retried")
    attempt = int(snapshot.get("inference_attempt") or 0) + 1
    if attempt > 3:
        raise ValueError("Inference retry limit reached")
    snapshot.setdefault("inference_history", []).append(
        {"task_id": expected, "error": row.get("error_message")}
    )
    snapshot.pop("inference_id", None)
    snapshot.pop("serving_snapshots", None)
    snapshot.update(inference_attempt=attempt, retry_of=expected)
    return snapshot


def _training_id(owner: OwnerScope, key: str) -> str:
    value = json.dumps(
        [owner.tenant_id, owner.user_id, "training_submit", key],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "train_idem_" + hashlib.sha256(value.encode()).hexdigest()[:32]


class Store:
    async def retry_inference(
        self, owner: OwnerScope, run_id: str, expected: str
    ) -> dict:
        async with get_session() as session:
            params = {**owner.sql_params(), "run": run_id}
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM qm_research_pipeline_runs WHERE tenant_id=:tenant_id AND user_id=:user_id AND run_id=:run FOR UPDATE"
                        ),
                        params,
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                raise HTTPException(status_code=404, detail="Research run not found")
            if row["snapshot"].get("retry_of") == expected:
                return dict(row)
            status = (
                await session.execute(
                    text(
                        "SELECT status FROM qm_inference_jobs WHERE tenant_id=:tenant_id AND user_id=:user_id AND task_id=:task FOR UPDATE"
                    ),
                    {**owner.sql_params(), "task": expected},
                )
            ).scalar_one_or_none()
            snapshot = _retry_snapshot(dict(row), expected, status or "")
            updated = (
                (
                    await session.execute(
                        text(
                            "UPDATE qm_research_pipeline_runs SET snapshot=CAST(:snapshot AS jsonb),status='running',error_message=NULL,"
                            "claim_token=:token,claim_until=NULL,next_check_at=now(),updated_at=now() "
                            "WHERE tenant_id=:tenant_id AND user_id=:user_id AND run_id=:run RETURNING *"
                        ),
                        {
                            **params,
                            "snapshot": json.dumps(snapshot),
                            "token": uuid4().hex,
                        },
                    )
                )
                .mappings()
                .one()
            )
            return dict(updated)

    @asynccontextmanager
    async def dispatch_guard(self, row: dict):
        """Serialize cancellation with child admission, including slow responses."""
        async with get_session() as session:
            current = (
                await session.execute(
                    text(
                        "SELECT status FROM qm_research_pipeline_runs WHERE tenant_id=:tenant "
                        "AND user_id=:user AND run_id=:run AND claim_token=:token FOR UPDATE"
                    ),
                    {
                        "tenant": row["tenant_id"],
                        "user": row["user_id"],
                        "run": row["run_id"],
                        "token": row["claim_token"],
                    },
                )
            ).scalar_one_or_none()
            if current not in {"queued", "running"}:
                raise RuntimeError("Research dispatch was cancelled or fenced")
            yield

    async def submit(self, owner: OwnerScope, payload: dict, key: str) -> dict:
        if not key or len(key) > 128:
            raise ValueError("A bounded Idempotency-Key is required")
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO qm_research_pipeline_runs "
                    "(tenant_id,user_id,run_id,idempotency_key,request_fingerprint,request_payload) "
                    "VALUES (:tenant_id,:user_id,:run,:key,:hash,CAST(:request AS jsonb)) "
                    "ON CONFLICT (tenant_id,user_id,idempotency_key) DO NOTHING"
                ),
                {
                    **owner.sql_params(),
                    "run": uuid4().hex,
                    "key": key,
                    "hash": fingerprint,
                    "request": encoded,
                },
            )
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM qm_research_pipeline_runs WHERE tenant_id=:tenant_id "
                            "AND user_id=:user_id AND idempotency_key=:key"
                        ),
                        {**owner.sql_params(), "key": key},
                    )
                )
                .mappings()
                .one()
            )
            if row["request_fingerprint"] != fingerprint:
                raise ValueError("Idempotency key belongs to a different research plan")
            return dict(row)

    async def get(self, owner: OwnerScope, run_id: str) -> dict | None:
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM qm_research_pipeline_runs WHERE tenant_id=:tenant_id "
                            "AND user_id=:user_id AND run_id=:run"
                        ),
                        {**owner.sql_params(), "run": run_id},
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def claim(self) -> dict | None:
        async with get_session() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM qm_research_pipeline_runs "
                            "WHERE status IN ('queued','running','stopping') AND next_check_at<=now() "
                            "AND (claim_until IS NULL OR claim_until<now()) "
                            "ORDER BY next_check_at,created_at FOR UPDATE SKIP LOCKED LIMIT 1"
                        )
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            job = dict(row)
            job["claim_token"] = uuid4().hex
            await session.execute(
                text(
                    "UPDATE qm_research_pipeline_runs SET claim_token=:token, "
                    "claim_until=now()+interval '120 seconds',updated_at=now() "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id AND run_id=:run"
                ),
                {
                    "tenant_id": job["tenant_id"],
                    "user_id": job["user_id"],
                    "run": job["run_id"],
                    "token": job["claim_token"],
                },
            )
            return job

    async def save(
        self,
        row: dict,
        *,
        status: str = "running",
        stage: str | None = None,
        error: str | None = None,
    ) -> None:
        owner = OwnerScope(row["tenant_id"], row["user_id"])
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE qm_research_pipeline_runs SET "
                    "status=CASE WHEN status='stopping' AND :status<>'cancelled' THEN 'stopping' ELSE :status END, "
                    "stage=COALESCE(:stage,stage),snapshot=CAST(:snapshot AS jsonb),error_message=:error, "
                    "claim_until=NULL,next_check_at=now()+interval '5 seconds',updated_at=now() "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id AND run_id=:run "
                    "AND claim_token=:token AND status IN ('queued','running','stopping')"
                ),
                {
                    **owner.sql_params(),
                    "run": row["run_id"],
                    "token": row["claim_token"],
                    "status": status,
                    "stage": stage,
                    "snapshot": json.dumps(row["snapshot"], ensure_ascii=False),
                    "error": error,
                },
            )
            if result.rowcount != 1:
                raise RuntimeError("Research pipeline execution claim lost")


def _request(owner: OwnerScope, key: str) -> Request:
    request = Request(
        {
            "type": "http",
            "headers": [(b"idempotency-key", key.encode())],
            "method": "POST",
            "path": "/internal/research",
        }
    )
    request.state.user = {"tenant_id": owner.tenant_id, "user_id": owner.user_id}
    return request


async def _advance(row: dict) -> None:
    from backend.services.engine.alpha_agent.factor_job_api import (
        FactorMaterializeRequest,
        submit_factor_materialization,
    )
    from backend.services.engine.alpha_agent.factor_jobs import (
        AlphaFactorJobPersistence,
    )
    from backend.services.engine.qlib_app.services.rd_agent_persistence import (
        AlphaMiningTaskPersistence,
    )
    from backend.services.api.routers.admin.admin_training_utils import (
        get_training_run_for_owner,
        submit_training_job,
    )

    store, jobs, tasks = (
        Store(),
        AlphaFactorJobPersistence(),
        AlphaMiningTaskPersistence(),
    )
    owner = OwnerScope(row["tenant_id"], row["user_id"])
    identity = {"tenant_id": owner.tenant_id, "user_id": owner.user_id}
    plan, snapshot, run_id = row["request_payload"], row["snapshot"], row["run_id"]
    if row["status"] == "stopping":
        if snapshot.get("training_id"):
            try:
                training = await get_training_run_for_owner(
                    snapshot["training_id"], identity
                )
                if training["status"] not in {"completed", "failed"}:
                    await store.save(
                        row,
                        status="stopping",
                        error="Waiting for the admitted training executor to finish safely",
                    )
                    return
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
        if not snapshot.get("mining_id") and not plan.get("mining_task_id"):
            recovered = await tasks.get_by_request_key(
                _key(run_id, "mining"), **identity
            )
            if recovered:
                snapshot["mining_id"] = recovered["task_id"]
        if not snapshot.get("inference_id"):
            async with get_session() as session:
                snapshot["inference_id"] = (
                    await session.execute(
                        text(
                            "SELECT task_id FROM qm_inference_jobs WHERE tenant_id=:tenant_id "
                            "AND user_id=:user_id AND idempotency_key=:key"
                        ),
                        {**owner.sql_params(), "key": _inference_key(run_id, snapshot)},
                    )
                ).scalar_one_or_none()
        if snapshot.get("inference_id"):
            from backend.services.engine.inference.jobs import JobStore

            inference = await asyncio.to_thread(
                JobStore().cancel,
                snapshot["inference_id"],
                owner.tenant_id,
                owner.user_id,
            )
            if inference and inference["status"] not in {
                "completed",
                "failed",
                "cancelled",
            }:
                await store.save(row, status="stopping")
                return
        mining_id = snapshot.get("mining_id")
        if mining_id and not plan.get("mining_task_id"):
            task = await tasks.get(mining_id, **identity)
            if task and task["status"] not in {"completed", "failed", "cancelled"}:
                from backend.services.engine.alpha_agent.launcher import get_launcher

                await get_launcher().cancel_task(mining_id, **identity)
                await store.save(row, status="stopping")
                return
        for factor in snapshot.get("factors", []):
            recovered = await jobs.get_by_request_key(
                _key(run_id, "materialize", factor["factor_id"]), **identity
            )
            if recovered is None:
                continue
            child = recovered["job_id"]
            task = await jobs.get(child, **identity, consistent=True)
            if task and task["status"] in {"queued", "running", "stopping"}:
                await jobs.request_cancel(
                    child, **identity, generation=task["execution_generation"]
                )
                await store.save(row, status="stopping")
                return
        await store.save(row, status="cancelled")
        return

    if not snapshot.get("mining_id"):
        existing = plan.get("mining_task_id")
        if existing:
            if not await tasks.get(existing, **identity):
                raise ValueError("Mining task is not owned by this user")
            snapshot["mining_id"] = existing
        else:
            from backend.services.engine.routers.alpha_agent import start_evolution

            mining = MiningSubmitRequest.model_validate(plan["mining"])
            # This coordinator owns materialization dispatch, avoiding duplicate
            # jobs from the standalone statistical auto-materialization outbox.
            mining.auto_materialize = False
            async with store.dispatch_guard(row):
                response = await start_evolution(
                    _request(owner, _key(run_id, "mining")),
                    user_id=None,
                    tenant_id=None,
                    market="a_share",
                    universe=mining.universe or "all_a",
                    loop_n=mining.loop_n or 3,
                    direction=mining.direction or "",
                    directions=mining.directions or [],
                    direction_mode=mining.direction_mode or "selected",
                    data_source=mining.data_source or "",
                    algorithm=mining.algorithm or "rdagent",
                    payload=mining,
                )
            snapshot["mining_id"] = response["data"]["task_id"]
        await store.save(row, stage="mining")
        return

    task = await tasks.get(snapshot["mining_id"], **identity)
    if not task or task["status"] in {"failed", "cancelled"}:
        raise RuntimeError("Mining task failed or was cancelled")
    if task["status"] != "completed":
        await store.save(row, stage="mining")
        return
    if "factors" not in snapshot:
        from backend.shared.training.constraints import factor_count_limits

        _, maximum = factor_count_limits(
            plan["training"].get("model_type") or "lightgbm"
        )
        limit = min(
            plan["max_factors"],
            maximum - len(set(plan["training"].get("features") or [])),
        )
        specification = await tasks.get_for_owner_with_spec(
            snapshot["mining_id"], **identity, consistent=True
        )
        research_params = (specification or {}).get("params_snapshot") or {}
        if isinstance(research_params, str):
            research_params = json.loads(research_params)
        research_end = research_params.get("valid_end")
        if not research_end or research_end >= plan["training"]["test_start"]:
            raise ValueError("Model test data must be untouched by research selection")
        snapshot["research_validation_end"] = research_end
        async with get_session(read_only=True) as session:
            factors = (
                (
                    await session.execute(
                        text(
                            "SELECT factor_id,factor_name,rank_ic,factor_code,universe FROM rd_agent_factors "
                            "WHERE tenant_id=:tenant_id AND user_id=:user_id AND metadata_json->>'task_id'=:task "
                            "AND archived_at IS NULL AND (status='completed' OR "
                            "(metadata_json->>'algorithm'='statistical' AND "
                            "(metadata_json->'evidence'->>'selected'='true' OR "
                            "metadata_json->'evidence'->>'eligible'='true'))) "
                            "AND ABS(rank_ic)>=:minimum ORDER BY ABS(rank_ic) DESC,factor_id LIMIT :limit"
                        ),
                        {
                            **owner.sql_params(),
                            "task": snapshot["mining_id"],
                            "minimum": plan["min_abs_rank_ic"],
                            "limit": limit,
                        },
                    )
                )
                .mappings()
                .all()
            )
        snapshot["factors"] = [
            {
                "factor_id": item["factor_id"],
                "universe": item["universe"],
                "sha256": hashlib.sha256(
                    str(item["factor_code"] or "").encode()
                ).hexdigest(),
            }
            for item in factors
        ]
        snapshot["materialization_ids"] = {}
        await store.save(
            row,
            stage="materializing",
            status="running" if factors else "rejected",
            error=None if factors else "no_candidates",
        )
        return

    generations = {}
    for factor in snapshot["factors"]:
        factor_id = factor["factor_id"]
        child_id = snapshot["materialization_ids"].get(factor_id)
        if not child_id:
            # Training dates extend beyond the research selection window. Build
            # a new frozen dataset from that formula; never reuse the old OOS data
            # as the model's supposedly untouched test evidence.
            request = _request(owner, _key(run_id, "materialize", factor_id))
            request.state.factor_parent_task_id = snapshot["mining_id"]
            request.state.research_training_snapshot = True
            async with store.dispatch_guard(row):
                response = await submit_factor_materialization(
                    FactorMaterializeRequest(
                        factor_id=factor_id,
                        universe=factor["universe"],
                        start_date=plan["training"].get("train_start"),
                        end_date=plan["training"]["test_end"],
                    ),
                    request,
                )
            child_id = response["data"]["job_id"]
            snapshot["materialization_ids"][factor_id] = child_id
            await store.save(row, stage="materializing")
            return
        child = await jobs.get(child_id, **identity, consistent=True)
        if not child or child["status"] in {"failed", "cancelled"}:
            raise RuntimeError("Factor materialization failed")
        if child["status"] != "completed":
            await store.save(row, stage="materializing")
            return
        result = child.get("result_snapshot") or {}
        if (
            result.get("status") != "materialized"
            or result.get("factor_code_sha256") != factor["sha256"]
        ):
            raise ValueError("Materialization did not publish the selected formula")
        generations[result["feature_key"]] = result["generation_id"]
    if "training_id" not in snapshot:
        snapshot["generation_map"] = generations
        snapshot["training_key"] = _key(run_id, "training")
        snapshot["training_id"] = _training_id(owner, snapshot["training_key"])
        await store.save(row, stage="training")
        return
    try:
        training = await get_training_run_for_owner(snapshot["training_id"], identity)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        payload = dict(plan["training"])
        payload["features"] = list(
            dict.fromkeys(
                [*(payload.get("features") or []), *snapshot["generation_map"]]
            )
        )
        async with store.dispatch_guard(row):
            response = await submit_training_job(
                payload,
                BackgroundTasks(),
                identity,
                idempotency_key=snapshot["training_key"],
                factor_generation_map=snapshot["generation_map"],
                research_pipeline_id=run_id,
            )
        if response["runId"] != snapshot["training_id"]:
            raise ValueError("Training admission returned a different run") from exc
        await store.save(row, stage="training")
        return
    if training["status"] == "failed":
        raise RuntimeError("Model training failed; inspect the owned training run")
    if training["status"] != "completed":
        await store.save(row, stage="training")
        return
    registration = (training.get("result") or {}).get("model_registration") or {}
    model_metadata = (training.get("result") or {}).get("metadata") or {}
    if registration.get("status") == "candidate":
        snapshot["model_id"] = registration.get("model_id")
        snapshot["quality_gate"] = registration.get("gate_reasons") or []
        await store.save(
            row,
            status="rejected",
            stage="candidate_ready",
            error="Model held by out-of-sample quality gate",
        )
        return
    if not set(model_metadata.get("user_factor_columns") or []).intersection(
        snapshot["generation_map"]
    ):
        await store.save(
            row,
            status="rejected",
            stage="no_incremental_factors",
            error="No selected private factor survived model fitting",
        )
        return
    model_id = registration.get("model_id")
    if not model_id or registration.get("status") not in {"ready", "active"}:
        raise RuntimeError("Training has no usable registered model")
    snapshot["model_id"] = model_id
    from backend.services.engine.inference.jobs import JobStore

    inference_store = JobStore()
    if "inference_id" not in snapshot:
        async with store.dispatch_guard(row):
            job = await asyncio.to_thread(
                inference_store.submit,
                owner.tenant_id,
                owner.user_id,
                {
                    "model_id": model_id,
                    "expected_model_id": model_id,
                    "trade_date": plan["inference_date"],
                    "persist": True,
                    "pool_id": plan["training"]["pool_id"],
                },
                _inference_key(run_id, snapshot),
            )
        snapshot["inference_id"] = job["task_id"]
        await store.save(row, stage="inference")
        return
    inference = await asyncio.to_thread(
        inference_store.get, snapshot["inference_id"], owner.tenant_id, owner.user_id
    )
    if not inference or inference["status"] in {"failed", "cancelled"}:
        raise RuntimeError("Model inference failed; inspect the owned inference job")
    if inference["status"] != "completed":
        await store.save(row, stage="inference")
        return
    if not (inference.get("result_payload") or {}).get("success"):
        raise RuntimeError("Inference ended without successful business output")
    snapshot["serving_snapshots"] = (inference.get("executor") or {}).get(
        "serving_snapshots"
    ) or []
    required_generations = {
        generation
        for feature, generation in (
            model_metadata.get("user_factor_generation_by_column") or {}
        ).items()
        if feature in snapshot["generation_map"]
    }
    if not required_generations or not required_generations.issubset(
        {item.get("source_generation_id") for item in snapshot["serving_snapshots"]}
    ):
        raise ValueError("Inference has no complete new-date private feature receipt")
    if not (inference.get("result_payload") or {}).get("signals_count"):
        raise ValueError("Inference completed without signals")
    if plan.get("activate_on_success"):
        from backend.shared.model_registry import model_registry_service

        async with store.dispatch_guard(row):
            await model_registry_service.set_default_model(
                **identity, model_id=model_id
            )
        snapshot["activated"] = True
    await store.save(row, stage="completed", status="completed")


async def run_pipelines() -> None:
    while True:
        row = None
        try:
            row = await Store().claim()
            if row:
                try:
                    await _advance(row)
                except HTTPException as exc:
                    await Store().save(
                        row,
                        status="running" if exc.status_code in {429, 503} else "failed",
                        error=redact_text(str(exc.detail))[:1000],
                    )
                except (ValueError, RuntimeError) as exc:
                    await Store().save(
                        row, status="failed", error=redact_text(str(exc))[:1000]
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Stable child keys and planned training IDs recover accepted calls
            # after a lost response. Never infer child termination from a TTL.
            logger.exception("Research pipeline deferred; durable state retained")
        await asyncio.sleep(1 if row else 5)


def _public(row: dict) -> dict:
    return {
        key: row[key]
        for key in (
            "run_id",
            "status",
            "stage",
            "snapshot",
            "error_message",
            "created_at",
            "updated_at",
        )
    }


def _owner(request: Request) -> OwnerScope:
    user, tenant = get_authenticated_identity(request)
    return OwnerScope(tenant, user)


@router.post("/research-pipelines", status_code=202)
async def submit_pipeline(payload: PipelineRequest, request: Request):
    try:
        return _public(
            await Store().submit(
                _owner(request),
                payload.model_dump(mode="json"),
                request.headers.get("Idempotency-Key", ""),
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/research-pipelines/{run_id}")
async def get_pipeline(run_id: str, request: Request):
    row = await Store().get(_owner(request), run_id)
    if not row:
        raise HTTPException(status_code=404, detail="Research run not found")
    return _public(row)


class InferenceRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inference_id: str = Field(min_length=1, max_length=128)


@router.post("/research-pipelines/{run_id}/retry-inference", status_code=202)
async def retry_pipeline_inference(
    run_id: str, payload: InferenceRetryRequest, request: Request
):
    try:
        return _public(
            await Store().retry_inference(_owner(request), run_id, payload.inference_id)
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/research-pipelines/{run_id}", status_code=202)
async def cancel_pipeline(run_id: str, request: Request):
    owner = _owner(request)
    async with get_session() as session:
        await session.execute(
            text(
                "UPDATE qm_research_pipeline_runs SET status='stopping',next_check_at=now(),updated_at=now() "
                "WHERE tenant_id=:tenant_id AND user_id=:user_id AND run_id=:run "
                "AND status IN ('queued','running')"
            ),
            {**owner.sql_params(), "run": run_id},
        )
    return await get_pipeline(run_id, request)
