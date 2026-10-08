"""市场因子数据集定时填充配置 API。

GET  /api/v1/admin/data-platform/factor-fill-schedule            全部市场配置+新鲜度
GET  /api/v1/admin/data-platform/factor-fill-schedule/{market}   单市场配置+新鲜度
POST /api/v1/admin/data-platform/factor-fill-schedule/{market}   保存单市场配置
POST /api/v1/admin/data-platform/factor-fill-schedule/{market}/run  立即触发一次填充

覆盖范围与口径见 factor_fill_scheduler 模块 docstring：独立于市场同步调度，
「落后才建」——因子集追平来源数据即空跑，停更时自动补建。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from backend.services.api.user_app.middleware.auth import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)])


class FactorFillScheduleRequest(BaseModel):
    enabled: bool = False
    time: str = Field(
        "04:30",
        description="每天触发时间 HH:MM（Asia/Shanghai，建议排在该市场同步之后）",
    )
    datasets: list[str] = Field(
        default_factory=list, description="要填充的数据集；空=该市场全部因子集"
    )

    @field_validator("time")
    @classmethod
    def _validate_time(cls, v: str) -> str:
        from datetime import datetime

        try:
            datetime.strptime(v.strip(), "%H:%M")
        except ValueError as exc:
            raise ValueError("time 必须是 HH:MM 格式（如 04:30）") from exc
        return v.strip()


def _scheduler():
    from backend.services.engine.tasks.factor_fill_scheduler import (
        MARKETS,
        freshness,
        get_all_schedules,
        get_schedule,
        get_status,
        save_schedule,
    )

    return (
        MARKETS,
        get_all_schedules,
        get_schedule,
        save_schedule,
        freshness,
        get_status,
    )


def _freshness_safe(freshness_fn, market: str) -> dict[str, Any]:
    try:
        return freshness_fn(market)
    except Exception as exc:  # noqa: BLE001 - 目录不可读不应让配置接口 500
        logger.warning("[FactorFill] %s 新鲜度检查失败: %s", market, exc)
        return {"error": str(exc)}


@router.get("/factor-fill-schedule")
async def list_factor_fill_schedules(current_user: dict = Depends(require_admin)):
    MARKETS, get_all_schedules, _, _, freshness, get_status = _scheduler()
    schedules = get_all_schedules()
    return {
        "success": True,
        "data": {
            "schedules": [
                {
                    "market": m,
                    "label": MARKETS[m],
                    **schedules[m],
                    "freshness": _freshness_safe(freshness, m),
                    "last": get_status(m),
                }
                for m in MARKETS
            ]
        },
    }


@router.get("/factor-fill-schedule/{market}")
async def get_factor_fill_schedule(
    market: str, current_user: dict = Depends(require_admin)
):
    MARKETS, _, get_schedule, _, freshness, get_status = _scheduler()
    market = market.upper()
    if market not in MARKETS:
        raise HTTPException(status_code=404, detail=f"未知市场: {market}")
    return {
        "success": True,
        "data": {
            "market": market,
            "label": MARKETS[market],
            **get_schedule(market),
            "freshness": _freshness_safe(freshness, market),
            "last": get_status(market),
        },
    }


@router.post("/factor-fill-schedule/{market}")
async def save_factor_fill_schedule(
    market: str,
    payload: FactorFillScheduleRequest,
    current_user: dict = Depends(require_admin),
):
    MARKETS, _, _, save_schedule, freshness, get_status = _scheduler()
    market = market.upper()
    if market not in MARKETS:
        raise HTTPException(status_code=404, detail=f"未知市场: {market}")
    saved = save_schedule(
        market,
        {
            "enabled": payload.enabled,
            "time": payload.time,
            "datasets": payload.datasets,
        },
    )
    return {
        "success": True,
        "data": {
            "market": market,
            "label": MARKETS[market],
            **saved,
            "freshness": _freshness_safe(freshness, market),
            "last": get_status(market),
        },
    }


@router.post("/factor-fill-schedule/{market}/run")
async def run_factor_fill_now(market: str, current_user: dict = Depends(require_admin)):
    """立即触发一次该市场的因子填充（按已保存配置）。"""
    MARKETS, _, get_schedule, *_ = _scheduler()
    market = market.upper()
    if market not in MARKETS:
        raise HTTPException(status_code=404, detail=f"未知市场: {market}")
    cfg = get_schedule(market)
    if not cfg.get("enabled"):
        raise HTTPException(
            status_code=400, detail="该市场因子自动填充未启用，请先保存配置"
        )

    from backend.services.engine.qlib_app.celery_config import celery_app
    from backend.services.engine.tasks.factor_fill_scheduler import (
        FACTOR_FILL_TASK_NAME,
    )

    celery_app.send_task(
        FACTOR_FILL_TASK_NAME,
        args=[market, cfg],
        queue="qlib_backtest_srv",
    )
    return {
        "success": True,
        "data": {"market": market, "label": MARKETS[market], "status": "dispatched"},
    }
