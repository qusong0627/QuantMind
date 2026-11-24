"""训练目标注册表 API（用户只读 / 管理端创建·启用·废弃）。

路由顺序：`user_router` 必须在 `model_training_router` 之前挂载——后者有
`GET /{model_id}` 兜底，静态 `/training/targets` 若在其后会整组 404。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from backend.services.api.user_app.middleware.auth import get_current_user, require_admin
from backend.shared.training.target_registry import (
    TargetRegistryError,
    activate,
    create_draft,
    deprecate,
    get_target,
    list_targets,
)
from backend.shared.training.target_spec import TargetSpecError, spec_from_dict

user_router = APIRouter()
admin_router = APIRouter()


class TargetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: dict[str, Any] = Field(..., description="LabelTargetSpec（完整 JSON）")
    note: str | None = Field(default=None, max_length=500)


def _actor(current_user: dict[str, Any]) -> str:
    return str(
        current_user.get("username")
        or current_user.get("user_id")
        or current_user.get("sub")
        or "unknown"
    )


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, TargetRegistryError):
        return HTTPException(
            status_code=exc.http_status, detail=f"{exc.code}: {exc.message}"
        )
    if isinstance(exc, TargetSpecError):
        return HTTPException(status_code=422, detail=f"{exc.code}: {exc.message}")
    return HTTPException(status_code=500, detail=f"target_registry_error: {exc}")


@user_router.get(
    "/training/targets",
    summary="列出训练目标（用户态，只读）",
)
async def list_training_targets(
    market: str | None = Query(None, description="市场过滤：CN/HK/US/..."),
    status: str | None = Query(None, description="状态过滤：draft/active/deprecated"),
    family: str | None = Query(None, description="目标族过滤"),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    _ = current_user
    try:
        items = await list_targets(market=market, status=status, family=family)
    except Exception as exc:  # noqa: BLE001 - 统一映射注册表/契约错误
        raise _http_error(exc) from exc
    return {"items": items, "count": len(items)}


@user_router.get(
    "/training/targets/{target_id}/{version}",
    summary="获取单个训练目标（用户态，只读）",
)
async def get_training_target(
    target_id: str,
    version: int,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    _ = current_user
    try:
        return await get_target(target_id, version)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@admin_router.post(
    "/training/targets",
    summary="创建训练目标 draft（管理员）",
    status_code=201,
)
async def create_training_target(
    payload: TargetCreateRequest,
    current_user: dict[str, Any] = Depends(require_admin),
):
    try:
        spec = spec_from_dict(payload.spec)
        return await create_draft(spec, actor=_actor(current_user), note=payload.note)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@admin_router.post(
    "/training/targets/{target_id}/{version}/activate",
    summary="启用训练目标（管理员）",
)
async def activate_training_target(
    target_id: str,
    version: int,
    current_user: dict[str, Any] = Depends(require_admin),
):
    try:
        return await activate(target_id, version, actor=_actor(current_user))
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@admin_router.post(
    "/training/targets/{target_id}/{version}/deprecate",
    summary="废弃训练目标（管理员）",
)
async def deprecate_training_target(
    target_id: str,
    version: int,
    current_user: dict[str, Any] = Depends(require_admin),
):
    try:
        return await deprecate(target_id, version, actor=_actor(current_user))
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


__all__ = ["admin_router", "user_router"]
