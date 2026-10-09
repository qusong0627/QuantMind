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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/internal/rolling", tags=["InternalRolling"])

Trigger = Literal["schedule", "manual", "sentinel", "drift"]


class DispatchRequest(BaseModel):
    market: str = Field(min_length=1, max_length=16)
    recipe_id: str = Field(min_length=1, max_length=128)
    trigger: Trigger = "schedule"
    dry_run: bool = False
    anchor_date: str | None = None


class ScheduleUpdateRequest(BaseModel):
    """单市场重训调度配置（键面与 retrain_scheduler.DEFAULT_SCHEDULE 对齐）。

    ``day_rule`` 不在入口做白名单——未知规则由调度器的 ``judge_due`` 拒绝并
    每日告警：坏配置要**可见**，不能被保存口静默改写成「每月首交易日」。

    ``window_policy`` / ``purge_days`` 只接受 ``None``（缺省=沿用配方策略；
    只为 GET→PUT 往返保真而留在键面）。窗口策略归**配方**所有——派发链路
    （rolling_dispatch / rolling_train.py）一律读 ``recipe.window_policy``，
    调度配置里的覆写没有任何消费者；非 None 一律 400 拒绝，宁可让配置人
    当场看到错误，也不存一份「看起来生效实则被忽略」的假配置。

    ``executor="remote"`` 尚未接线：派发 payload 不携带 ``node_id``，admin 侧
    编排器永远落回 ``"local"``，唯一实际效果是跳过调度器的内存守卫——PUT 一律
    400 拒绝；接线（节点路由）前只认 ``local``。
    """

    enabled: bool = False
    day_rule: str = "first_trading_day"
    time: str = "15:30"
    recipe_id: str = Field(min_length=1, max_length=128)
    observation_days: int = Field(default=20, ge=1, le=250)
    max_time_minutes: int = Field(default=240, ge=10, le=1440)
    executor: Literal["local", "remote"] = "local"
    window_policy: dict[str, Any] | None = None
    purge_days: int | None = None


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

    from backend.services.engine.tasks import retrain_scheduler as rts
    from backend.shared.training.recipe_registry import RecipeError, load_recipe

    if req.window_policy is not None or req.purge_days is not None:
        # 见 ScheduleUpdateRequest docstring：窗口策略归配方所有，调度层存了
        # 不生效 = 静默陷阱。rolling_train.py 对本地覆写参数同样硬拒。
        raise HTTPException(
            status_code=400,
            detail=(
                "窗口策略（window_policy/purge_days）归配方所有，调度配置不支持覆写；"
                "请修改配方后重新保存"
            ),
        )
    if req.executor == "remote":
        # 见 ScheduleUpdateRequest docstring：node_id 路由未打通，存 remote =
        # 实际本地执行还跳过内存守卫（拆安全闸的假配置）。
        raise HTTPException(
            status_code=400,
            detail="executor=remote 尚未接线（训练节点路由未打通），暂仅支持 local",
        )

    market_code = str(market or "").strip().upper()
    if market_code not in rts.recipe_markets():
        # 无有效配方的市场允许保存只会换来每天一条 recipe_invalid 告警
        raise HTTPException(
            status_code=404, detail=f"市场无有效重训配方: {market_code or market}"
        )
    try:
        recipe = load_recipe(req.recipe_id)
    except RecipeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if str(recipe.market or "").upper() != market_code:
        raise HTTPException(
            status_code=400,
            detail=f"配方 {req.recipe_id} 属 {recipe.market}，不能配置给 {market_code}",
        )
    saved = rts.save_schedule(market_code, req.model_dump())
    return {"market": market_code, "schedule": saved}
