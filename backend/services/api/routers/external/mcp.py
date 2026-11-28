"""对外 MCP 支撑端点：配置下发 + 审计上报。

MCP 服务是独立容器，不直连 DB；它用用户自己的短期 token 调这里：
- ``GET /mcp/config``：全局开关/工具开关/配额 + 当前 key 的覆盖 + 权限；
- ``POST /mcp/audit``：批量上报工具调用（成功/拒绝/失败/配额）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.services.api import mcp_admin_service
from backend.services.api.routers.external.auth import (
    ExternalPrincipal,
    access_key_fp,
    require_external_principal,
)
from backend.shared.mcp_tool_catalog import MCP_USE, TOOLS_BY_NAME, tool_catalog_payload

router = APIRouter(tags=["External API · MCP"])


class AuditEvent(BaseModel):
    tool: str = Field(..., min_length=1, max_length=80)
    status: str = Field("ok", max_length=20)
    error_code: str | None = Field(None, max_length=80)
    latency_ms: int | None = Field(None, ge=0, le=3_600_000)
    args_digest: str | None = Field(None, max_length=128)
    request_id: str | None = Field(None, max_length=128)
    session_id: str | None = Field(None, max_length=128)
    client_info: str | None = Field(None, max_length=500)


class AuditBatch(BaseModel):
    events: list[AuditEvent] = Field(default_factory=list, max_length=100)


@router.get("/config")
async def mcp_config(
    principal: ExternalPrincipal = Depends(require_external_principal),
) -> dict[str, Any]:
    if MCP_USE not in (principal.permissions or []):
        raise HTTPException(status_code=403, detail="permission_denied")
    global_cfg = await mcp_admin_service.get_global_config()
    key_settings = await mcp_admin_service.get_key_settings(principal.access_key)
    effective = mcp_admin_service.effective_config(
        global_config=global_cfg,
        key_settings=key_settings,
        permissions=list(principal.permissions or []),
    )
    # 只返回当前凭据有权使用的工具，避免客户端把无权工具列给模型
    tools = [
        tool
        for tool in tool_catalog_payload()
        if tool["permission"] in effective["permissions"]
    ]
    return {
        "enabled": effective["enabled"],
        "tool_flags": effective["tool_flags"],
        "limits": effective["limits"],
        "permissions": effective["permissions"],
        "tools": tools,
        "principal": {
            "user_id": principal.user_id,
            "tenant_id": principal.tenant_id,
            "access_key_fp": access_key_fp(principal.access_key),
        },
        "global_enabled": global_cfg["enabled"],
    }


@router.post("/audit")
async def post_audit(
    body: AuditBatch,
    principal: ExternalPrincipal = Depends(require_external_principal),
) -> dict[str, Any]:
    fp = access_key_fp(principal.access_key)
    events = []
    for event in body.events:
        tool = TOOLS_BY_NAME.get(event.tool)
        events.append(
            {
                "tenant_id": principal.tenant_id,
                "user_id": principal.user_id,
                "access_key_fp": fp,
                "session_id": event.session_id,
                "tool": event.tool if tool is not None else event.tool[:80],
                "args_digest": event.args_digest or "",
                "status": event.status,
                "error_code": event.error_code,
                "latency_ms": event.latency_ms or 0,
                "request_id": event.request_id,
                "client_info": event.client_info or "",
            }
        )
    await mcp_admin_service.add_audit(events)
    return {"success": True, "data": {"accepted": len(events)}}
