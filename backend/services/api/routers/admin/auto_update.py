"""管理员 - 每日自动强制更新开关。

GET  /api/v1/admin/system/auto-update          读取开关与最近一次决策
POST /api/v1/admin/system/auto-update          保存开关 {enabled}

实际执行逻辑与闸门见 backend/shared/auto_update.py。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.services.api.user_app.middleware.auth import require_admin
from backend.shared import auto_update

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)])


class AutoUpdateRequest(BaseModel):
    enabled: bool


@router.get("/auto-update")
async def get_auto_update() -> dict[str, Any]:
    return {"success": True, "data": auto_update.describe_state()}


@router.post("/auto-update")
async def set_auto_update(req: AutoUpdateRequest) -> dict[str, Any]:
    state = auto_update.describe_state()
    if req.enabled and not state["available"]:
        raise HTTPException(
            status_code=400,
            detail="docker socket 未挂载，无法执行自动更新。请先在部署中挂载 /var/run/docker.sock",
        )
    auto_update.save_config(req.enabled)
    return {"success": True, "data": auto_update.describe_state()}
