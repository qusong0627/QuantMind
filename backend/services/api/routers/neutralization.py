"""推理中性化（Neutralization Layer）用户态 API。

- 配置：``GET/PUT /api/v1/models/{model_id}/neutralization``
- 重算：``POST /api/v1/models/{model_id}/neutralization/recompute``
- 诊断：``GET /api/v1/models/{model_id}/neutralization/diagnostics``
- 明细：``GET /api/v1/models/{model_id}/neutralization/scores``
- 审计：``GET /api/v1/models/neutralization/audit``（管理员）

路由必须注册在 ``model_training`` 的 ``GET /{model_id}`` 兜底之前（同一前缀）。
所有重活在线程池执行：服务层是同步 psycopg2 会话，不能阻塞事件循环。
"""

from __future__ import annotations

import logging
from datetime import date
from functools import partial
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.engine.inference import neutralization_service as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/models", tags=["Neutralization"])

_ALLOWED_CONTROLS = {"log_mv", "beta60", "mom20"}
_VALID_MODES = {"off", "shadow", "enforce"}


def _owner_scope(current_user: dict[str, Any]) -> tuple[str, str]:
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="用户身份无效")
    return tenant_id, user_id


class NeutralizationSettingsBody(BaseModel):
    mode: Literal["off", "shadow", "enforce"]
    universe_id: str = Field(default="cn_all_tradable", min_length=1, max_length=64)
    controls: list[str] = Field(default_factory=lambda: ["log_mv", "beta60", "mom20"])
    min_n: int = Field(default=300, ge=50, le=5000)
    winsor_pct: float = Field(default=0.01, ge=0.0, le=0.2)
    control_lag: int = Field(default=0, ge=0, le=5)


class RecomputeBody(BaseModel):
    data_trade_date: date
    run_id: str | None = None


def _read_settings_sync(tenant_id: str, user_id: str, model_id: str) -> dict:
    with svc.get_session_factory()() as session:
        settings = svc.load_settings(session, tenant_id, user_id, model_id)
        diagnostics = svc.list_diagnostics(
            session, tenant_id=tenant_id, user_id=user_id, model_id=model_id, limit=20
        )
    return {"settings": settings, "diagnostics": diagnostics}


@router.get("/neutralization/audit")
async def get_neutralization_audit(
    model_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
):
    tenant_id, _ = _owner_scope(current_user)
    if not current_user.get("is_admin", False):
        raise HTTPException(status_code=403, detail="仅管理员可查看审计")

    def _read() -> list[dict]:
        with svc.get_session_factory()() as session:
            return svc.list_audit(
                session, tenant_id=tenant_id, model_id=model_id, limit=limit
            )

    rows = await run_in_threadpool(_read)
    return {"ok": True, "data": rows}


@router.get("/neutralization/overview")
async def get_neutralization_overview(
    current_user: dict = Depends(get_current_user),
):
    """治理面板：当前用户所有已配置模型的中性化状态 + 最近诊断。"""
    tenant_id, user_id = _owner_scope(current_user)

    def _read() -> list[dict]:
        with svc.get_session_factory()() as session:
            return svc.overview(session, tenant_id=tenant_id, user_id=user_id)

    rows = await run_in_threadpool(_read)
    return {"ok": True, "data": rows}


@router.get("/{model_id}/neutralization")
async def get_neutralization_settings(
    model_id: str,
    current_user: dict = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    data = await run_in_threadpool(_read_settings_sync, tenant_id, user_id, model_id)
    return {"ok": True, "data": data}


@router.put("/{model_id}/neutralization")
async def put_neutralization_settings(
    model_id: str,
    body: NeutralizationSettingsBody,
    current_user: dict = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    controls = [c.strip() for c in body.controls if c.strip()]
    if not controls or any(c not in _ALLOWED_CONTROLS for c in controls):
        raise HTTPException(
            status_code=422,
            detail=f"controls 仅允许 {sorted(_ALLOWED_CONTROLS)} 的非空子集",
        )
    if body.mode == "enforce" and not current_user.get("is_admin", False):
        raise HTTPException(status_code=403, detail="enforce 模式需要管理员权限")
    operator = f"{user_id}:{current_user.get('username') or ''}".rstrip(":")

    def _save() -> dict:
        with svc.get_session_factory()() as session:
            return svc.upsert_settings(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                model_id=model_id,
                mode=body.mode,
                universe_id=body.universe_id,
                controls=controls,
                min_n=body.min_n,
                winsor_pct=body.winsor_pct,
                control_lag=body.control_lag,
                operator=operator,
            )

    try:
        data = await run_in_threadpool(_save)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "data": data}


@router.post("/{model_id}/neutralization/recompute")
async def recompute_neutralization(
    model_id: str,
    body: RecomputeBody,
    current_user: dict = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    run_id = body.run_id

    def _find_run() -> str | None:
        with svc.get_session_factory()() as session:
            return svc.latest_run_id(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                model_id=model_id,
                data_trade_date=body.data_trade_date,
            )

    if not run_id:
        run_id = await run_in_threadpool(_find_run)
    if not run_id:
        raise HTTPException(
            status_code=404,
            detail=f"{model_id} 在 {body.data_trade_date} 没有已完成的推理 run",
        )
    try:
        outcome = await run_in_threadpool(
            partial(
                svc.compute_for_run,
                run_id,
                tenant_id=tenant_id,
                user_id=user_id,
                model_id=model_id,
                data_trade_date=body.data_trade_date,
                force=True,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[NL] 重算失败 %s %s: %s", model_id, run_id, exc)
        raise HTTPException(status_code=500, detail=f"中性化重算失败: {exc}") from exc
    if outcome is None:
        raise HTTPException(status_code=409, detail="模型未启用中性化（mode=off）")
    return {"ok": True, "data": outcome.__dict__}


@router.get("/{model_id}/neutralization/diagnostics")
async def get_neutralization_diagnostics(
    model_id: str,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = Query(default=60, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)

    def _read() -> list[dict]:
        with svc.get_session_factory()() as session:
            return svc.list_diagnostics(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                model_id=model_id,
                limit=limit,
                start_date=start_date,
                end_date=end_date,
            )

    rows = await run_in_threadpool(_read)
    return {"ok": True, "data": rows}


@router.get("/{model_id}/neutralization/scores")
async def get_neutralization_scores(
    model_id: str,
    data_trade_date: date,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)

    def _read() -> tuple[list[dict], int]:
        with svc.get_session_factory()() as session:
            return svc.list_scores(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                model_id=model_id,
                data_trade_date=data_trade_date,
                limit=limit,
                offset=offset,
            )

    rows, total = await run_in_threadpool(_read)
    return {
        "ok": True,
        "data": rows,
        "meta": {"total": total, "limit": limit, "offset": offset},
    }
