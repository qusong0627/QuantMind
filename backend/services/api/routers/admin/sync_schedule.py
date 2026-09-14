"""市场定时同步配置 API。

GET  /api/v1/admin/data-platform/sync-schedule            全部市场定时配置
GET  /api/v1/admin/data-platform/sync-schedule/{market}   单市场配置
POST /api/v1/admin/data-platform/sync-schedule/{market}   保存单市场配置
POST /api/v1/admin/data-platform/sync-schedule/{market}/run  立即触发一次同步（测试用）
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from backend.services.api.user_app.middleware.auth import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)])


class SyncScheduleRequest(BaseModel):
    enabled: bool = False
    time: str = Field("03:00", description="每天触发时间 HH:MM（Asia/Shanghai，建议凌晨执行如 03:00）")
    days: int = Field(5, ge=1, le=365, description="同步最近 N 个交易日（BC 为自然日）")
    datasets: list[str] = Field(default_factory=list, description="要同步的数据集；空=按默认全量")
    with_qlib: bool = Field(False, description="同步后重建 Qlib 缓存")

    @field_validator("time")
    @classmethod
    def _validate_time(cls, v: str) -> str:
        from datetime import datetime

        try:
            datetime.strptime(v.strip(), "%H:%M")
        except ValueError as exc:
            raise ValueError("time 必须是 HH:MM 格式（如 22:30）") from exc
        return v.strip()


def _scheduler():
    from backend.services.engine.tasks.market_sync_scheduler import (
        MARKETS,
        get_all_schedules,
        get_schedule,
        run_market_sync,
        save_schedule,
    )

    return MARKETS, get_all_schedules, get_schedule, save_schedule, run_market_sync


@router.get("/sync-schedule")
async def list_schedules(current_user: dict = Depends(require_admin)):
    MARKETS, get_all_schedules, *_ = _scheduler()
    schedules = get_all_schedules()
    return {
        "success": True,
        "data": {
            "schedules": [
                {"market": m, "label": MARKETS[m], **schedules[m]} for m in MARKETS
            ]
        },
    }


@router.get("/sync-schedule/{market}")
async def get_market_schedule(market: str, current_user: dict = Depends(require_admin)):
    MARKETS, _, get_schedule, *_ = _scheduler()
    market = market.upper()
    if market not in MARKETS:
        raise HTTPException(status_code=404, detail=f"未知市场: {market}")
    return {"success": True, "data": {"market": market, "label": MARKETS[market], **get_schedule(market)}}


@router.post("/sync-schedule/{market}")
async def save_market_schedule(
    market: str,
    payload: SyncScheduleRequest,
    current_user: dict = Depends(require_admin),
):
    MARKETS, _, _, save_schedule, _ = _scheduler()
    market = market.upper()
    if market not in MARKETS:
        raise HTTPException(status_code=404, detail=f"未知市场: {market}")
    saved = save_schedule(
        market,
        {
            "enabled": payload.enabled,
            "time": payload.time,
            "days": payload.days,
            "datasets": payload.datasets,
            "with_qlib": payload.with_qlib,
        },
    )
    return {
        "success": True,
        "data": {"market": market, "label": MARKETS[market], **saved},
    }


@router.post("/sync-schedule/{market}/run")
async def run_market_schedule_now(
    market: str, current_user: dict = Depends(require_admin)
):
    """立即触发一次该市场的定时同步（按已保存配置）。"""
    MARKETS, _, get_schedule, _, run_market_sync = _scheduler()
    market = market.upper()
    if market not in MARKETS:
        raise HTTPException(status_code=404, detail=f"未知市场: {market}")
    cfg = get_schedule(market)
    if not cfg.get("enabled"):
        raise HTTPException(status_code=400, detail="该市场定时同步未启用，请先保存配置")

    from backend.services.engine.qlib_app.celery_config import celery_app
    from backend.services.engine.tasks.market_sync_scheduler import task_name_for

    celery_app.send_task(
        task_name_for(market),
        args=[market, cfg],
        queue="qlib_backtest_srv",
    )
    return {
        "success": True,
        "data": {"market": market, "label": MARKETS[market], "status": "dispatched"},
    }
