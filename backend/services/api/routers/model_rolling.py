"""模型管理「滚动训练」用户态端点（P1 · 前端界面后端）。

与 ``internal_rolling``（``X-Internal-Call-Secret``、给调度器/运维）的分工：
本路由走 JWT（``get_current_user``），是**前端唯一可达**的滚动训练面——
前端不得持有内部密钥（安全纪律，同 ADR-0011）。

- ``GET  /api/v1/models/rolling/recipes``            配方摘要（内建+用户派生）
- ``GET  /api/v1/models/rolling/campaigns``          滚动台账（market/status 过滤）
- ``GET  /api/v1/models/rolling/schedule``           重训调度配置（全市场）
- ``PUT  /api/v1/models/rolling/schedule/{market}``  保存调度配置（校验与内部端点同实现）
- ``POST /api/v1/models/rolling/dispatch``           手动派发（trigger 定死 manual；
  非 dry_run 先过内存守卫——低内存 409，避免把训练机打爆）
- ``POST /api/v1/models/rolling/derive``             从模型目录派生滚动配方
  （metadata.json + config.yaml → 用户配方文件；云端导入模型同路径可用）

派发归属：``current_user`` 全量传给 ``execute_dispatch`` —— 台账归属人=真实
登录用户（而非调度器兜底的 CUSTOM_USER）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.services.api.user_app.middleware.auth import get_current_user

from .rolling_shared import ScheduleUpdateRequest, apply_schedule_update

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/models/rolling", tags=["Model Rolling"])


def _owner_scope(current_user: dict[str, Any]) -> tuple[str, str]:
    """身份口径与 ``model_rollouts`` 一致（users.user_id 优先，历史别名 sub）。"""
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="用户身份无效")
    return tenant_id, user_id


class DispatchRollingBody(BaseModel):
    market: str = Field(min_length=1, max_length=16)
    # recipe_id 即文件名：字符集白名单先行（装载侧还有同款守卫，双层）
    recipe_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]{1,128}$"
    )
    dry_run: bool = False
    anchor_date: str | None = None
    # 注意：不接受 trigger——客户端无法把手动派发伪装成 schedule（记账人服务端定死）


class DeriveRecipeBody(BaseModel):
    model_id: str = Field(min_length=1, max_length=200)
    dry_run: bool = False
    window_policy: dict[str, Any] | None = None


def _allowed_model_roots() -> list[Path]:
    """模型目录允许根（env 调用时读取——测试/部署可临时指到别处）。

    与 ``model_registry`` 的根解析同口径（相对路径按 /app 展开）。
    """
    roots = [
        Path(os.getenv("USER_MODELS_ROOT", "models/users")),
        Path(os.getenv("MODELS_PRODUCTION", "/app/models/production")),
    ]
    return [(r if r.is_absolute() else Path("/app") / r).resolve() for r in roots]


def _resolve_model_dir(storage_path: str) -> Path:
    """注册表 storage_path → 受控模型目录；越界/缺失一律拒绝（防任意路径读）。"""
    raw = str(storage_path or "").strip()
    if not raw:
        raise HTTPException(
            status_code=400, detail="模型记录缺少 storage_path，无法定位模型目录"
        )
    path = Path(raw)
    if not path.is_absolute():
        path = Path("/app") / path
    resolved = path.resolve()
    for root in _allowed_model_roots():
        if resolved == root or root in resolved.parents:
            return resolved
    raise HTTPException(
        status_code=400, detail=f"模型目录越界（不在允许根内）: {resolved}"
    )


def _derive_response(
    result: Any, *, status: str, path: str | None = None
) -> dict[str, Any]:
    from backend.shared.training.recipe_registry import recipe_summary

    summary = recipe_summary(result.recipe)
    return {
        "status": status,
        "recipe_id": summary["recipe_id"],
        "recipe_hash": summary["recipe_hash"],
        "market": summary["market"],
        "factor_market": summary["factor_market"],
        "factor_source": summary["factor_source"],
        "feature_count": len(result.recipe.payload["features"]),
        "target_horizon_days": summary["target_horizon_days"],
        "window_policy": summary["window_policy"],
        "description": summary["description"],
        "source_model_id": result.source_model_id,
        "derived_at": summary["derived_at"],
        "source_files": result.source_files,
        "warnings": result.warnings,
        "path": path,
    }


@router.get("/recipes")
async def list_rolling_recipes(current_user: dict = Depends(get_current_user)):
    from backend.shared.training import recipe_registry as rr

    recipes = rr.list_recipes()
    return {"recipes": recipes, "count": len(recipes)}


@router.get("/campaigns")
async def list_rolling_campaigns(
    market: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
):
    from backend.shared import rolling_campaigns as rc

    rows = await rc.list_campaigns(market=market, status=status, limit=limit)
    return {"campaigns": rows, "count": len(rows)}


@router.get("/schedule")
async def get_retrain_schedules(current_user: dict = Depends(get_current_user)):
    from backend.services.engine.tasks import retrain_scheduler as rts
    from backend.shared.scheduler_registry import read_heartbeats

    schedules = rts.get_all_schedules()
    # 派发器心跳（与体检 C07 同一判定：ok/stale/off/missing，不产生第二口径）。
    # 调度保存了 enabled 但 ticker 死掉时（M5 复盘：调度存了、没人派发），
    # 本面板是用户唯一能看见真相的地方。读不到如实报 missing，不假装 ok。
    heartbeats = read_heartbeats(["retrain_dispatch"])
    return {
        "schedules": schedules,
        "markets": sorted(schedules),
        "dispatch": heartbeats[0] if heartbeats else None,
    }


@router.put("/schedule/{market}")
async def put_retrain_schedule(
    market: str,
    req: ScheduleUpdateRequest,
    current_user: dict = Depends(get_current_user),
):
    # 校验/保存与内部端点同一实现（rolling_shared）——两份实现必然漂移
    return apply_schedule_update(market, req)


@router.post(
    "/dispatch",
    responses={
        404: {"description": "配方不存在"},
        400: {"description": "市场不匹配 / anchor 无法解析"},
        409: {"description": "内存不足，派发被守卫阻止"},
    },
)
async def dispatch_rolling(
    req: DispatchRollingBody,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    """手动派发一次滚动重训（trigger=manual 写死；返回 execute_dispatch 裁决 dict）。"""
    from backend.services.engine.training import rolling_dispatch as rd
    from backend.shared.training import recipe_registry as rr

    if not req.dry_run:
        # dry_run 只算窗口计划、不提交，不占训练机内存 → 不过守卫
        guard = rd.mem_guard()
        if not guard.get("ok"):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"可用内存 {guard.get('available_gb')} GB 低于阈值 "
                    f"{guard.get('min_gb')} GB，已阻止派发（可先用预览查看窗口计划）"
                ),
            )
    try:
        return await rd.execute_dispatch(
            market=req.market,
            recipe_id=req.recipe_id,
            trigger="manual",
            dry_run=req.dry_run,
            anchor_date=req.anchor_date,
            dispatched_by=rd.DISPATCHED_BY_MANUAL,
            current_user=current_user,
            background_tasks=background_tasks,
        )
    except rr.RecipeError as exc:
        # RecipeError 是 ValueError 子类，必须先捕获（配方缺失是 404 不是 400）
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/derive",
    responses={
        400: {"description": "模型目录越界 / 模型包信息不足，无法派生"},
        404: {"description": "模型不存在"},
    },
)
async def derive_rolling_recipe(
    req: DeriveRecipeBody,
    current_user: dict = Depends(get_current_user),
):
    """从模型目录派生滚动配方：dry_run=预览（不落盘），否则保存进用户配方目录。

    保存后立即出现在 ``/recipes``（``source: "user"``），可直接用于派发与调度。
    """
    import backend.shared.model_registry as mreg

    from backend.shared.training import recipe_registry as rr
    from backend.shared.training.model_recipe import derive_recipe_from_model

    tenant_id, user_id = _owner_scope(current_user)
    model = await mreg.model_registry_service.get_model(
        tenant_id=tenant_id, user_id=user_id, model_id=req.model_id
    )
    if model is None:
        raise HTTPException(status_code=404, detail=f"模型不存在: {req.model_id}")

    model_dir = _resolve_model_dir(str(model.get("storage_path") or ""))
    try:
        result = derive_recipe_from_model(
            model_dir, model_id=req.model_id, window_policy=req.window_policy
        )
    except rr.RecipeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        logger.exception("派生配方读取模型目录失败: %s", model_dir)
        raise HTTPException(
            status_code=500, detail="模型目录不可读（权限或磁盘问题），详见服务端日志"
        ) from exc

    if req.dry_run:
        return _derive_response(result, status="preview")

    try:
        recipe, changed = rr.save_user_recipe(result.recipe_dict)
    except rr.RecipeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        logger.exception("用户配方落盘失败: %s", rr.user_recipe_dir())
        raise HTTPException(
            status_code=500,
            detail="用户配方目录不可写（权限或磁盘问题），详见服务端日志",
        ) from exc
    path = rr.user_recipe_dir() / f"{recipe.recipe_id}.json"
    return _derive_response(
        result, status="saved" if changed else "unchanged", path=str(path)
    )
