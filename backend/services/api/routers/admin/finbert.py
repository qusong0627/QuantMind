"""FinBERT 开关管理 — 独立控制按键后端。

GET  /api/v1/admin/finbert/status  查询当前开关与模型就绪状态
POST /api/v1/admin/finbert/toggle  body {"enabled": bool} 即时生效并持久化到 /data/finbert/enabled
仅 admin 可访问。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.services.api.user_app.middleware.auth import require_admin
from backend.services.api.news.sentiment import get_finbert_status, set_finbert_enabled
from backend.shared.system_events import record_system_event

router = APIRouter(dependencies=[Depends(require_admin)])


class ToggleRequest(BaseModel):
    enabled: bool


@router.get("/status")
async def finbert_status():
    """查询 FinBERT 开关与模型状态。"""
    return {"success": True, "data": get_finbert_status()}


@router.post("/toggle")
async def finbert_toggle(req: ToggleRequest):
    """切换 FinBERT 开关，即时生效，无需重启。未安装时拒绝开启。"""
    from fastapi import HTTPException

    # 未安装时直接拒绝开启，避免无效切换
    st_before = get_finbert_status()
    if req.enabled and not st_before.get("installed"):
        raise HTTPException(status_code=400, detail=f"FinBERT 模型未安装（{st_before.get('model')} 缺失），无法开启。请先执行 backend/scripts/download_finbert.py 离线下载。")
    ok = set_finbert_enabled(req.enabled)
    st = get_finbert_status()
    if req.enabled and not ok:
        raise HTTPException(status_code=400, detail="FinBERT 开启失败（模型未安装或持久化失败）")
    # 记录系统事件便于审计
    try:
        record_system_event(
            event_type="data_sync",
            level="info",
            source="quantmind-api",
            title=f"FinBERT 已{'开启' if req.enabled else '关闭'}",
            message=f"管理员通过独立按键切换 FinBERT: {'启用' if req.enabled else '停用'}（模型 {st.get('model')}，device {st.get('device')}）",
            meta={"enabled": req.enabled, "status": st},
        )
    except Exception:
        pass
    return {"success": ok, "data": st}
