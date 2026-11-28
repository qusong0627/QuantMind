"""后台管理 · MCP 服务配置 / 客户端 / 审计 / 健康检查。

入口：`/api/v1/admin/mcp/*`（require_admin）。
MCP 服务本身是独立容器，这里只做控制面；健康检查经内部 HTTP 探测，不直连。
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.services.api import mcp_admin_service
from backend.services.api.user_app.middleware.auth import require_admin
from backend.services.api.user_app.schemas.api_key import ApiKeyCreate
from backend.services.api.user_app.services.api_key_service import ApiKeyService
from backend.shared.database_manager_v2 import get_session
from backend.shared.mcp_tool_catalog import (
    DEFAULT_LIMITS,
    DEFAULT_TOOL_FLAGS,
    LIMIT_SPECS,
    PERMISSION_LABELS,
    PERMISSION_TEMPLATES,
    TOOLS,
    TOOLS_BY_NAME,
    tool_catalog_payload,
)
from sqlalchemy import text

router = APIRouter(dependencies=[Depends(require_admin)])

MCP_SERVICE_URL = os.getenv("MCP_SERVICE_URL", "http://quantmind-mcp:8765").rstrip("/")


class GlobalConfigPatch(BaseModel):
    enabled: bool | None = None
    tool_flags: dict[str, bool] | None = None
    limits: dict[str, float | int] | None = None


class KeySettingsPatch(BaseModel):
    enabled: bool | None = None
    permissions: list[str] | None = None
    tool_flags: dict[str, bool] | None = None
    limits: dict[str, float | int] | None = None


class IssueKeyRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    tenant_id: str = Field("default", min_length=1, max_length=64)
    name: str = Field("MCP 客户端", min_length=1, max_length=100)
    template: Literal["readonly", "research", "sim_trading"] = "readonly"
    permissions: list[str] | None = None


def _validate_tool_flags(flags: dict[str, bool] | None) -> dict[str, bool]:
    if not flags:
        return {}
    unknown = sorted(set(flags) - set(TOOLS_BY_NAME))
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知工具：{', '.join(unknown)}")
    return {k: bool(v) for k, v in flags.items()}


def _validate_limits(limits: dict[str, Any] | None) -> dict[str, Any]:
    if not limits:
        return {}
    unknown = sorted(set(limits) - set(LIMIT_SPECS))
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知配额项：{', '.join(unknown)}")
    out: dict[str, Any] = {}
    for key, value in limits.items():
        lo, hi, _ = LIMIT_SPECS[key]
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"配额 {key} 必须是数字")
        number = min(hi, max(lo, number))
        out[key] = int(number) if key != "sim_order_max_value" else number
    return out


def _validate_permissions(permissions: list[str]) -> list[str]:
    try:
        return mcp_admin_service.validate_permissions(permissions)
    except mcp_admin_service.McpConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/config")
async def get_config():
    return {
        "success": True,
        "data": {
            "global": await mcp_admin_service.get_global_config(),
            "tools": tool_catalog_payload(),
            "defaults": {
                "tool_flags": DEFAULT_TOOL_FLAGS,
                "limits": DEFAULT_LIMITS,
            },
            "limit_specs": {
                key: {"min": lo, "max": hi, "label": label}
                for key, (lo, hi, label) in LIMIT_SPECS.items()
            },
            "permissions": [
                {"code": code, "label": label}
                for code, label in PERMISSION_LABELS.items()
            ],
            "templates": {
                name: list(codes) for name, codes in PERMISSION_TEMPLATES.items()
            },
            "service_url": MCP_SERVICE_URL,
        },
    }


@router.put("/config")
async def update_config(body: GlobalConfigPatch, admin: dict = Depends(require_admin)):
    patch: dict[str, Any] = {}
    if body.enabled is not None:
        patch["enabled"] = body.enabled
    if body.tool_flags is not None:
        patch["tool_flags"] = _validate_tool_flags(body.tool_flags)
    if body.limits is not None:
        patch["limits"] = _validate_limits(body.limits)
    global_cfg = await mcp_admin_service.update_global_config(
        patch=patch,
        admin_id=str(admin.get("user_id") or admin.get("sub") or "admin"),
    )
    return {"success": True, "data": global_cfg}


@router.get("/clients")
async def list_clients(limit: int = Query(200, ge=1, le=1000)):
    return {"success": True, "data": {"items": await mcp_admin_service.list_clients(limit)}}


@router.put("/clients/{access_key}")
async def update_client(
    access_key: str,
    body: KeySettingsPatch,
):
    tenant_id: str | None = None
    user_id: str | None = None
    permissions: list[str] | None = None
    async with get_session(read_only=False) as session:
        row = (
            await session.execute(
                text(
                    "SELECT tenant_id, user_id, permissions FROM api_keys WHERE access_key = :key"
                ),
                {"key": access_key},
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="API Key 不存在")
        tenant_id = str(row["tenant_id"])
        user_id = str(row["user_id"])
        current_permissions = row["permissions"] or []
        if isinstance(current_permissions, str):
            try:
                current_permissions = json.loads(current_permissions)
            except ValueError:
                current_permissions = []
        permissions = list(current_permissions)
        if body.permissions is not None:
            permissions = _validate_permissions(body.permissions)
            await session.execute(
                text(
                    "UPDATE api_keys SET permissions = CAST(:permissions AS json) "
                    "WHERE access_key = :key"
                ),
                {"permissions": json.dumps(permissions), "key": access_key},
            )
        await session.commit()
    settings = await mcp_admin_service.upsert_key_settings(
        access_key=access_key,
        tenant_id=tenant_id,
        user_id=user_id,
        enabled=body.enabled,
        tool_flags=_validate_tool_flags(body.tool_flags) or None,
        limits=_validate_limits(body.limits) or None,
    )
    return {"success": True, "data": {**settings, "permissions": permissions}}


@router.delete("/clients/{access_key}")
async def revoke_client(access_key: str):
    """吊销 MCP 访问：停用 API Key 与 MCP 开关（不物理删除审计历史）。"""
    async with get_session(read_only=False) as session:
        result = await session.execute(
            text("UPDATE api_keys SET is_active = FALSE WHERE access_key = :key"),
            {"key": access_key},
        )
        if not result.rowcount:
            raise HTTPException(status_code=404, detail="API Key 不存在")
        await session.commit()
    row = (
        await mcp_admin_service.get_key_settings(access_key)
        or {"tenant_id": "", "user_id": ""}
    )
    await mcp_admin_service.upsert_key_settings(
        access_key=access_key,
        tenant_id=str(row.get("tenant_id") or ""),
        user_id=str(row.get("user_id") or ""),
        enabled=False,
    )
    return {"success": True, "data": {"access_key": access_key, "revoked": True}}


@router.post("/clients")
async def issue_client_key(body: IssueKeyRequest):
    """管理员为某个用户签发 MCP 凭据（Secret 只返回这一次）。"""
    permissions = (
        _validate_permissions(body.permissions)
        if body.permissions is not None
        else list(PERMISSION_TEMPLATES[body.template])
    )
    async with get_session(read_only=False) as session:
        service = ApiKeyService(session)
        created = await service.create_api_key(
            user_id=body.user_id,
            tenant_id=body.tenant_id,
            data=ApiKeyCreate(name=body.name, permissions=permissions),
        )
    await mcp_admin_service.upsert_key_settings(
        access_key=created.access_key,
        tenant_id=body.tenant_id,
        user_id=body.user_id,
        enabled=True,
    )
    return {
        "success": True,
        "data": {
            "access_key": created.access_key,
            "secret_key": created.secret_key,
            "name": created.name,
            "user_id": body.user_id,
            "tenant_id": body.tenant_id,
            "permissions": created.permissions,
            "note": "Secret Key 只展示一次，请立即保存",
        },
    }


@router.get("/audit")
async def get_audit(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user_id: str | None = None,
    tool: str | None = None,
    status: str | None = None,
):
    return {
        "success": True,
        "data": await mcp_admin_service.get_audit(
            limit=limit, offset=offset, user_id=user_id, tool=tool, status=status
        ),
    }


@router.post("/audit/prune")
async def prune_audit():
    cfg = await mcp_admin_service.get_global_config()
    deleted = await mcp_admin_service.prune_audit(
        int(cfg["limits"].get("audit_retention_days") or 90)
    )
    return {"success": True, "data": {"deleted": deleted}}


@router.get("/health")
async def mcp_health():
    url = f"{MCP_SERVICE_URL}/health"
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            resp = await client.get(url)
        return {
            "success": True,
            "data": {
                "reachable": resp.status_code < 500,
                "status_code": resp.status_code,
                "detail": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text[:300],
                "url": MCP_SERVICE_URL,
            },
        }
    except Exception as exc:  # noqa: BLE001 - 健康检查失败要如实返回
        return {
            "success": True,
            "data": {
                "reachable": False,
                "status_code": None,
                "detail": f"{type(exc).__name__}: {exc}",
                "url": MCP_SERVICE_URL,
            },
        }
