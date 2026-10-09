"""滚动重训内部端点（P1 · 设计文档《滚动训练与模型生命周期》§4.2/§4.6）。

- ``POST /dispatch``：一次滚动重训的唯一派发口。密钥治理同 ADR-0011
  （``X-Internal-Call-Secret``，fail-closed 401）。窗口在服务端计算 → 落
  ``qm_rolling_campaigns``（planned）→ 组 payload（配方 + 显式六键 split +
  ``rolling_meta``）→ 调既有 ``submit_training_job`` → 回填 run_id。
  **不新增第二套训练提交路径**；所有触发源（schedule/sentinel/drift/manual）
  共用 campaign 出口——没有任何路径可以直接改 ``is_default``。
- ``GET /campaigns`` / ``GET /recipes``：只读面（台账 / 配方摘要）。

admin 包（``submit_training_job`` / secret 校验的宿主）只在函数体内 lazy import：
该包曾因并行改动一度不可导入，模块导入期不依赖它是硬纪律——调度器（celery 侧）
与测试都要能安全 import 本模块的邻居而不被拖垮。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .rolling_shared import ScheduleUpdateRequest, apply_schedule_update

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/internal/rolling", tags=["InternalRolling"])

Trigger = Literal["schedule", "manual", "sentinel", "drift"]


class DispatchRequest(BaseModel):
    market: str = Field(min_length=1, max_length=16)
    recipe_id: str = Field(min_length=1, max_length=128)
    trigger: Trigger = "schedule"
    dry_run: bool = False
    anchor_date: str | None = None


def _verify(secret: str) -> None:
    from backend.services.api.routers.admin.admin_training_utils import (
        _verify_internal_call_secret,
    )

    _verify_internal_call_secret(secret)


@router.post(
    "/dispatch",
    summary="滚动重训派发（内部接口）",
    responses={
        401: {"description": "Invalid or missing X-Internal-Call-Secret"},
        404: {"description": "配方不存在"},
        400: {"description": "市场不匹配 / anchor 无法解析"},
    },
)
async def dispatch_rolling(
    req: DispatchRequest,
    background_tasks: BackgroundTasks,
    x_internal_call_secret: str = Header(default="", alias="X-Internal-Call-Secret"),
) -> dict[str, Any]:
    """返回 execute_dispatch 的裁决 dict（全部可 JSON 序列化）。

    ``status`` ∈ ``dispatched | duplicate | skipped | dry_run``。skipped（busy/
    数据未就绪等）也回 2xx——这是给调度器的「本轮不记账、下一 tick 重试」信号；
    真正的失败（配方缺失/市场错配/提交异常）才走非 2xx。
    """
    _verify(x_internal_call_secret)

    from backend.services.engine.training.rolling_dispatch import (
        DISPATCHED_BY_MANUAL,
        DISPATCHED_BY_SCHEDULER,
        execute_dispatch,
    )
    from backend.shared.training.recipe_registry import RecipeError

    dispatched_by = (
        DISPATCHED_BY_SCHEDULER if req.trigger == "schedule" else DISPATCHED_BY_MANUAL
    )
    try:
        return await execute_dispatch(
            market=req.market,
            recipe_id=req.recipe_id,
            trigger=req.trigger,
            dry_run=req.dry_run,
            anchor_date=req.anchor_date,
            dispatched_by=dispatched_by,
            background_tasks=background_tasks,
        )
    except RecipeError as exc:
        # RecipeError 是 ValueError 子类，必须先捕获（配方缺失是 404 不是 400）
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        # 市场不匹配 / anchor_date 无法解析（WindowCalculationError 亦走此支）
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/campaigns",
    summary="滚动重训台账（内部接口）",
    responses={401: {"description": "Invalid or missing X-Internal-Call-Secret"}},
)
async def list_rolling_campaigns(
    market: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    x_internal_call_secret: str = Header(default="", alias="X-Internal-Call-Secret"),
) -> dict[str, Any]:
    _verify(x_internal_call_secret)

    from backend.shared.rolling_campaigns import list_campaigns

    rows = await list_campaigns(market=market, status=status, limit=limit)
    return {"campaigns": rows, "count": len(rows)}


@router.get(
    "/recipes",
    summary="训练配方摘要（内部接口）",
    responses={401: {"description": "Invalid or missing X-Internal-Call-Secret"}},
)
async def list_rolling_recipes(
    x_internal_call_secret: str = Header(default="", alias="X-Internal-Call-Secret"),
) -> dict[str, Any]:
    _verify(x_internal_call_secret)

    from backend.shared.training.recipe_registry import list_recipes

    recipes = list_recipes()
    return {"recipes": recipes, "count": len(recipes)}


@router.get(
    "/schedule",
    summary="重训调度配置（内部接口）",
    responses={401: {"description": "Invalid or missing X-Internal-Call-Secret"}},
)
async def get_retrain_schedules(
    x_internal_call_secret: str = Header(default="", alias="X-Internal-Call-Secret"),
) -> dict[str, Any]:
    _verify(x_internal_call_secret)

    from backend.services.engine.tasks.retrain_scheduler import get_all_schedules

    schedules = get_all_schedules()
    return {"schedules": schedules, "markets": sorted(schedules)}


@router.put(
    "/schedule/{market}",
    summary="保存重训调度配置（内部接口）",
    responses={
        401: {"description": "Invalid or missing X-Internal-Call-Secret"},
        404: {"description": "市场无有效配方 / 配方不存在"},
        400: {"description": "配方市场与配置市场不匹配"},
    },
)
async def put_retrain_schedule(
    market: str,
    req: ScheduleUpdateRequest,
    x_internal_call_secret: str = Header(default="", alias="X-Internal-Call-Secret"),
) -> dict[str, Any]:
    _verify(x_internal_call_secret)

    # 用户态端点（model_rolling）共用同一实现，校验纪律见 rolling_shared
    return apply_schedule_update(market, req)
