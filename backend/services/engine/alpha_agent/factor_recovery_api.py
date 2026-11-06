"""Session-scoped admission for explicit AI source reconstruction."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.owner_scope import OwnerScope
from backend.services.engine.alpha_agent.factor_job_api import (
    _jobs,
    _factors,
    _owner,
    _public,
    _cost_snapshot,
)
from backend.services.engine.alpha_agent.factor_jobs import (
    FactorJobIdempotencyConflict,
    FactorJobQueueFull,
)
from backend.services.engine.alpha_agent.factor_visibility import (
    FactorVisibilityConflict,
)
from backend.services.engine.alpha_agent.factor_codegen import (
    PROMPT_VERSION,
    input_fingerprint,
    source_input,
)

router = APIRouter()


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    factor_ids: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        min_length=1, max_length=20
    )
    universe: str = Field(default="csi300", min_length=1, max_length=128)
    start_date: str | None = None
    end_date: str | None = None
    confirm_ai: StrictBool


@router.post("/factors/recovery", status_code=202)
async def submit_recovery(payload: RecoveryRequest, request: Request):
    tenant, user = _owner(request)
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key or len(key) > 128 or not payload.confirm_ai:
        raise HTTPException(
            status_code=400, detail="恢复需明确确认 AI 调用并提供 Idempotency-Key"
        )
    if len(set(payload.factor_ids)) != len(payload.factor_ids):
        raise HTTPException(status_code=422, detail="因子 ID 不可重复")
    identity = payload.model_dump()
    try:
        existing = await _jobs.get_by_request_key(key, tenant_id=tenant, user_id=user)
        if existing:
            if (
                existing["kind"] != "recovery"
                or existing["request_snapshot"]["request"].get("submission") != identity
            ):
                raise FactorJobIdempotencyConflict("幂等键已用于其他作业")
            return {
                "code": 202,
                "data": _public(existing),
                "dispatch": "durable_outbox",
            }
        rows = await _factors.get_factors_by_ids(
            payload.factor_ids, tenant_id=tenant, user_id=user
        )
        if len(rows) != len(payload.factor_ids):
            raise LookupError("本人因子不存在或已归档")
        from backend.services.engine.routers import alpha_agent as alpha_router
        from backend.services.engine.alpha_agent.factor_job_executor import (
            _request_date,
        )
        from backend.services.engine.alpha_agent.llm_client import resolve_llm_config

        config = resolve_llm_config()

        def freeze_evaluation():
            if not alpha_router._universe_is_valid(
                payload.universe, tenant_id=tenant, user_id=user
            ):
                raise ValueError("本人 A 股股票池不可用")
            symbols = alpha_router._resolve_instruments_for_universe(
                "CN", payload.universe, tenant_id=tenant, user_id=user
            )
            if not symbols or len(symbols) > 20000:
                raise ValueError("股票池为空或超出恢复评估范围")
            from backend.shared.stock_utils import StockCodeUtil

            start, end = (
                _request_date(payload.start_date, "start_date"),
                _request_date(payload.end_date, "end_date"),
            )
            if start is None or end is None:
                first, last = alpha_router._default_backtest_window("a_share")
                start, end = start or first, end or last
            if start > end:
                raise ValueError("开始日期不能晚于结束日期")
            return {
                "market": "a_share",
                "universe": payload.universe,
                "universe_symbols": sorted(
                    {StockCodeUtil.to_qlib(value) for value in symbols}
                ),
                "start_date": start,
                "end_date": end,
                "data_source": "qlib_bin",
                "cost_model": _cost_snapshot(),
            }

        evaluation = await asyncio.to_thread(freeze_evaluation)
        items = []
        for row in rows:
            if row.get("market") not in {None, "", "a_share", "CN"}:
                raise ValueError("仅支持 A 股因子恢复")
            code = row.get("factor_code") or ""
            if len(code.encode()) > 32768:
                raise ValueError("因子代码超出恢复评估限制")
            if not code.strip() and config is None:
                raise HTTPException(status_code=412, detail="系统 AI 服务尚未配置")
            inputs = source_input(row)
            if not code.strip() and not (
                inputs["factor_formulation"].strip() or inputs["description"].strip()
            ):
                raise ValueError("无码因子缺少公式和描述，不能仅凭名称猜测")
            if code.strip() and type(row.get("factor_revision_no")) is not int:
                raise ValueError("现有代码缺少不可变版本")
            items.append(
                {
                    "factor_id": row["factor_id"],
                    "factor_code": code,
                    "factor_revision_no": row.get("factor_revision_no"),
                    "source_input": source_input(row),
                    "input_fingerprint": input_fingerprint(row),
                }
            )
        by_id = {item["factor_id"]: item for item in items}
        job_id = await _jobs.create(
            tenant_id=tenant,
            user_id=user,
            kind="recovery",
            request_key=key,
            request={
                "factor_ids": payload.factor_ids,
                "factor_items": [by_id[fid] for fid in payload.factor_ids],
                "submission": identity,
                "evaluation": evaluation,
                "universe": payload.universe,
                "prompt_version": PROMPT_VERSION,
                "model": config.model if config else None,
            },
        )
        job = await _jobs.get(job_id, tenant_id=tenant, user_id=user, consistent=True)
        if job is None:
            raise RuntimeError("恢复回执无法读取")
        return {"code": 202, "data": _public(job), "dispatch": "durable_outbox"}
    except HTTPException:
        raise
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (FactorJobIdempotencyConflict, FactorVisibilityConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FactorJobQueueFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="恢复作业暂时无法入队") from exc


@router.get("/factors/{factor_id}/recovery-history")
async def recovery_history(factor_id: str, request: Request):
    owner = OwnerScope(*_owner(request))
    async with get_session(read_only=True) as session:
        rows = await session.execute(
            text(
                "SELECT r.job_id,r.revision_no,r.code_sha256,r.prompt_version,r.model,r.published_at,"
                "r.created_at,r.status,r.result_snapshot,r.error_message,j.status AS job_status "
                "FROM alpha_factor_recovery_sources r JOIN rd_agent_factors f "
                "ON f.tenant_id=r.tenant_id AND f.user_id=r.user_id AND f.factor_id=r.factor_id "
                "JOIN alpha_factor_jobs j ON j.tenant_id=r.tenant_id AND j.user_id=r.user_id AND j.job_id=r.job_id "
                "WHERE r.tenant_id=:tenant_id AND r.user_id=:user_id AND r.factor_id=:factor "
                "ORDER BY r.created_at DESC,r.job_id DESC LIMIT 50"
            ),
            {**owner.sql_params(), "factor": factor_id},
        )
        from backend.shared.utc_datetime import to_utc_iso

        return {
            "code": 200,
            "data": [
                {
                    **dict(row),
                    "published_at": to_utc_iso(row["published_at"])
                    if row["published_at"]
                    else None,
                    "created_at": to_utc_iso(row["created_at"]),
                }
                for row in rows.mappings()
            ],
        }
