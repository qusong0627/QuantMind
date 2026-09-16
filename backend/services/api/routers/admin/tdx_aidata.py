"""TdxAiData 数据源管理 API（P6 T-P6-01）。

GET  /api/v1/admin/data-platform/tdx-aidata/config    配置 + worker 状态（不拉起）
POST /api/v1/admin/data-platform/tdx-aidata/config    保存配置（目录/开关/Token；Token 写 ini）
POST /api/v1/admin/data-platform/tdx-aidata/selfcheck 连通性自检（按需拉起 + 真实取一次快照）
POST /api/v1/admin/data-platform/tdx-aidata/restart   重启 worker（重读 ini/目录）

纪律：
- Token 只进 ini、绝不回传明文（响应仅掩码）；
- 自检如实报告（限流给 retry_after，不假绿）；自检消耗 1 次 token 窗口配额（响应含预算余量）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from backend.services.api.user_app.middleware.auth import require_admin
from backend.services.trade_shared.redis_client import redis_client

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)])

_SELFCHECK_SYMBOL = "600036.SH"


class TdxAiDataConfigRequest(BaseModel):
    dir: str | None = Field(None, description="安装目录（含 libTdxAiData.so + tqServer.py + TdxAiData.ini）")
    enabled: bool | None = Field(None, description="启用开关")
    token: str | None = Field(None, description="通达信 Token（写入 TdxAiData.ini 的 [Token] 段；不回显）")


def _redis_sync():
    if redis_client.client is None:
        redis_client.connect()
    return redis_client.client


def _config_and_status() -> dict[str, Any]:
    from backend.shared.tdx_aidata import config as tdx_config

    return {
        "config": tdx_config.public_config(),
        "socket_path": tdx_config.socket_path(),
    }


@router.get("/tdx-aidata/config")
async def get_tdx_aidata_config(current_user: dict = Depends(require_admin)):
    from backend.shared.tdx_aidata.client import TdxAiDataClient

    data = _config_and_status()
    client = TdxAiDataClient()
    try:
        status = await client.status()  # 只读：不拉起
    finally:
        await client.close()
    data["worker"] = status
    return {"success": True, "data": data}


@router.post("/tdx-aidata/config")
async def save_tdx_aidata_config(
    req: TdxAiDataConfigRequest, current_user: dict = Depends(require_admin)
):
    from backend.shared.tdx_aidata import config as tdx_config

    # 1) 目录/开关 → Redis（前端配置源）
    updates: dict[str, Any] = {}
    if req.dir is not None:
        updates["dir"] = req.dir
    if req.enabled is not None:
        updates["enabled"] = "true" if req.enabled else "false"
    if updates:
        await asyncio.to_thread(
            tdx_config.save_config_sync, _redis_sync(), updates
        )

    # 2) Token → ini（外科手术写入；目录取保存后的生效值）
    token_written = False
    if req.token is not None and str(req.token).strip():
        ini = tdx_config.ini_path()
        await asyncio.to_thread(tdx_config.write_token, ini, str(req.token).strip())
        token_written = True

    # 3) 配置变更后重启 worker（SDK 在 start() 读 ini/目录）
    from backend.shared.tdx_aidata.client import TdxAiDataClient

    client = TdxAiDataClient()
    restarted = False
    try:
        if updates or token_written:
            restarted = await client.restart()
    finally:
        await client.close()

    data = _config_and_status()
    data["token_written"] = token_written
    data["worker_restarted"] = restarted
    return {"success": True, "data": data}


@router.post("/tdx-aidata/selfcheck")
async def tdx_aidata_selfcheck(current_user: dict = Depends(require_admin)):
    """连通性自检：拉起 worker + 真实取一次快照（消耗 1 次窗口配额）。"""
    import time

    from backend.shared.tdx_aidata.client import TdxAiDataClient, TdxAiDataError

    client = TdxAiDataClient()
    t0 = time.monotonic()
    result: dict[str, Any] = {"ok": False, "symbol": _SELFCHECK_SYMBOL}
    try:
        status = await client.status(try_start=True)
        result["worker"] = status
        if status.get("worker") != "up":
            result["error"] = status.get("sdk_error") or "worker 未能就绪"
            return {"success": True, "data": result}
        try:
            quote = await client.get_quote(_SELFCHECK_SYMBOL, timeout=30)
            result["ok"] = True
            result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            # 只摘关键字段，避免整包回传（键名以 SDK 为准，缺失即缺省）
            for key in ("Now", "price", "PreClose", "pre_close", "Open", "High", "Low"):
                if key in quote:
                    result[key] = quote[key]
            result["field_count"] = len(quote)
        except TdxAiDataError as exc:
            result["error"] = exc.message
            result["error_code"] = exc.code
            if exc.retry_after_s is not None:
                result["retry_after_s"] = exc.retry_after_s
        st2 = await client.status()
        result["gate"] = (st2 or {}).get("gate")
    finally:
        await client.close()
    return {"success": True, "data": result}


@router.post("/tdx-aidata/restart")
async def tdx_aidata_restart(current_user: dict = Depends(require_admin)):
    from backend.shared.tdx_aidata.client import TdxAiDataClient

    client = TdxAiDataClient()
    try:
        restarted = await client.restart()
    finally:
        await client.close()
    return {"success": True, "data": {"restarted": restarted}}
