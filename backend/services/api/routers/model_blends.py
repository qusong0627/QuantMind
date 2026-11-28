"""模型组合（组合选股）用户态端点 · ``/api/v1/models/blends``。

JWT 鉴权（``get_current_user``），所有读写按 ``(tenant_id, user_id)`` 隔离。
只涉及纸面台账 ``qm_model_blend_*``；**不产生任何订单/委托/成交语义**。

路由注册顺序硬约束：本路由必须在 ``model_training_router``（带
``GET /{model_id}`` 兜底）**之前**注册，否则 ``/api/v1/models/blends`` 会被
当成 ``model_id="blends"`` → 404（见 ``main.py`` 的注释与设计稿 §4.6）。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any
from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.engine.inference.blend_plan import PlanConfig
from backend.services.engine.services import combo_auto
from backend.services.engine.services.model_blend_service import (
    BlendError,
    ModelBlendService,
)
from backend.services.engine.services.model_blend_persistence import (
    ModelBlendPersistence,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/models/blends", tags=["Model Blends"])


def _owner(current_user: Mapping[str, Any]) -> tuple[str, str]:
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="用户身份无效")
    return tenant_id, user_id


async def _run(awaitable):
    """统一把 ``BlendError`` 映射为 HTTP 错误（detail 带机读 code）。"""
    try:
        return await awaitable
    except BlendError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


def _is_unique_conflict(exc: Exception) -> bool:
    text = str(exc).lower()
    return "unique" in text or "duplicate" in text


async def _annotate_confirmable(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """补 ``can_confirm``（最近 2 个交易日窗口）。

    日历不可用时只降级按钮态（置 None，前端退回本地序位判断），不让读接口 500。
    """
    try:
        await _service().annotate_confirmable(items)
    except Exception as exc:  # noqa: BLE001 - 仅影响确认按钮可用性
        logger.warning("annotate_confirmable failed: %s", exc)
        for item in items:
            item.setdefault("can_confirm", None)
    return items


def _service() -> ModelBlendService:
    return ModelBlendService()


async def _get_blend_or_404(
    blend_id: str, *, tenant_id: str, user_id: str
) -> dict[str, Any]:
    blend = await ModelBlendPersistence().get_blend(
        blend_id, tenant_id=tenant_id, user_id=user_id
    )
    if blend is None or blend.get("status") != "active":
        raise HTTPException(status_code=404, detail="组合不存在")
    return blend


# ── 请求体 ──────────────────────────────────────────────────────────────


class PreviewBody(BaseModel):
    market: str = Field(default="CN", max_length=16)
    model_ids: list[str] = Field(min_length=2, max_length=30)
    data_trade_date: str | None = None
    purpose: str = Field(default="rebalance", pattern=r"^(rebalance|next_day)$")
    method: str = Field(default="mean", pattern=r"^(mean|median|trimmed)$")
    trim_count: int = Field(default=2, ge=0, le=10)
    top_k: int = Field(default=50, ge=1, le=2000)
    top: int = Field(default=200, ge=1, le=2000)
    min_coverage: int = Field(default=2, ge=1, le=30)
    consensus_min_count: int = Field(default=0, ge=0, le=30)
    weights: dict[str, float] | None = None
    exclude_blacklist: bool = False


class BlendCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    market: str = Field(default="CN", max_length=16)
    model_ids: list[str] = Field(min_length=2, max_length=30)
    purpose: str = Field(default="rebalance", pattern=r"^(rebalance|next_day)$")
    method: str = Field(default="mean", pattern=r"^(mean|median|trimmed)$")
    trim_count: int = Field(default=2, ge=0, le=10)
    min_coverage: int = Field(default=8, ge=1, le=30)
    consensus_min_count: int = Field(default=0, ge=0, le=30)
    entry_per_day: int = Field(default=5, ge=1, le=50)
    rolling_window: int = Field(default=10, ge=1, le=60)
    target_positions: int = Field(default=50, ge=1, le=500)
    weights: dict[str, float] | None = None
    source: str = Field(default="manual", pattern=r"^(manual|system)$")
    template_id: str | None = Field(default=None, max_length=128)


class BlendPatchBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    purpose: str | None = Field(default=None, pattern=r"^(rebalance|next_day)$")
    method: str | None = Field(default=None, pattern=r"^(mean|median|trimmed)$")
    trim_count: int | None = Field(default=None, ge=0, le=10)
    min_coverage: int | None = Field(default=None, ge=1, le=30)
    consensus_min_count: int | None = Field(default=None, ge=0, le=30)
    entry_per_day: int | None = Field(default=None, ge=1, le=50)
    rolling_window: int | None = Field(default=None, ge=1, le=60)
    target_positions: int | None = Field(default=None, ge=1, le=500)
    model_ids: list[str] | None = Field(default=None, min_length=2, max_length=30)
    weights: dict[str, float] | None = None


class PlanBody(BaseModel):
    data_trade_date: str | None = None
    persist: bool = True
    # 可选覆盖（缺省沿用组合配置），让前端「资金 / TopK / 持有日 / 每日新进」可调
    capital: float | None = Field(default=None, gt=0, le=1e12)
    top_k: int | None = Field(default=None, ge=1, le=2000)
    hold_days: int | None = Field(default=None, ge=1, le=60)
    entry_per_day: int | None = Field(default=None, ge=1, le=50)
    target_positions: int | None = Field(default=None, ge=1, le=500)
    # 机构级硬条件：最低 ADV20（亿元）与 ST/退市剔除
    min_adv_yi: float | None = Field(default=None, ge=0, le=1000)
    exclude_st: bool | None = None


# ── 模型识别 ────────────────────────────────────────────────────────────


@router.get("/eligible", summary="可参与组合的模型与签名分组")
async def list_eligible(
    market: str = Query(default="CN", max_length=16),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    return await _run(
        _service().eligible(tenant_id=tenant_id, user_id=user_id, market=market)
    )


@router.get("/auto-candidates", summary="系统自动识别同特征组合模板")
async def list_auto_candidates(
    market: str = Query(default="CN", max_length=16),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    return await _run(
        _service().auto_candidates(tenant_id=tenant_id, user_id=user_id, market=market)
    )


@router.get("/evidence", summary="读取研究证据包（不现算）")
async def get_evidence(current_user: dict[str, Any] = Depends(get_current_user)):
    _owner(current_user)
    evidence = combo_auto.load_evidence()
    if evidence is None:
        return {"available": False, "path": str(combo_auto.evidence_path())}
    return {"available": True, "path": str(combo_auto.evidence_path()), **evidence}


# ── 预览 ────────────────────────────────────────────────────────────────


@router.post("/preview", summary="未保存组合的当日榜单预览")
async def preview_blend(
    body: PreviewBody,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    return await _run(
        _service().preview(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=body.model_ids,
            market=body.market,
            data_trade_date=_parse_date(body.data_trade_date),
            purpose=body.purpose,
            method=body.method,
            trim_count=body.trim_count,
            weights=body.weights,
            top_k=body.top_k,
            min_coverage=body.min_coverage,
            consensus_min_count=body.consensus_min_count,
            top=body.top,
            exclude_blacklist=body.exclude_blacklist,
        )
    )


_MIN_TRADE_DATE = date(2000, 1, 1)
_MAX_DATE_AHEAD_DAYS = 3650  # 10 年，足够覆盖任何在研模型的前瞻日期


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="日期格式必须是 YYYY-MM-DD"
        ) from exc
    # date.max 附近的日期会让交易日历兜底 day + 1 溢出成 500；在入参处拦成 400
    if parsed < _MIN_TRADE_DATE or parsed > date.today() + timedelta(
        days=_MAX_DATE_AHEAD_DAYS
    ):
        raise HTTPException(status_code=400, detail="日期超出可查询范围")
    return parsed


# ── 组合 CRUD ───────────────────────────────────────────────────────────


@router.get("", summary="我的组合列表")
async def list_blends(
    include_archived: bool = Query(default=False),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    items = await ModelBlendPersistence().list_blends(
        tenant_id=tenant_id, user_id=user_id, include_archived=include_archived
    )
    return {"items": items}


@router.post("", summary="保存组合")
async def create_blend(
    body: BlendCreateBody,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    try:
        blend = await ModelBlendService().create_blend(
            tenant_id=tenant_id,
            user_id=user_id,
            name=body.name,
            model_ids=body.model_ids,
            market=body.market,
            purpose=body.purpose,
            method=body.method,
            trim_count=body.trim_count,
            weights=body.weights,
            min_coverage=body.min_coverage,
            consensus_min_count=body.consensus_min_count,
            entry_per_day=body.entry_per_day,
            rolling_window=body.rolling_window,
            target_positions=body.target_positions,
            source=body.source,
            template_id=body.template_id,
        )
    except BlendError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 名称唯一等约束冲突
        if _is_unique_conflict(exc):
            raise HTTPException(status_code=409, detail="组合名称已存在") from exc
        raise
    return blend


@router.patch("/{blend_id}", summary="修改组合")
async def patch_blend(
    blend_id: str,
    body: BlendPatchBody,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    blend = await _get_blend_or_404(blend_id, tenant_id=tenant_id, user_id=user_id)
    fields = body.model_dump(exclude_unset=True, exclude={"model_ids", "weights"})
    try:
        await _run(
            ModelBlendService().update_blend(
                blend,
                tenant_id=tenant_id,
                user_id=user_id,
                fields=fields,
                model_ids=body.model_ids,
                weights=body.weights,
            )
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 改名撞唯一约束与新建同口径
        if _is_unique_conflict(exc):
            raise HTTPException(status_code=409, detail="组合名称已存在") from exc
        raise
    return await _get_blend_or_404(blend_id, tenant_id=tenant_id, user_id=user_id)


@router.delete("/{blend_id}", summary="归档组合")
async def delete_blend(
    blend_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    await _get_blend_or_404(blend_id, tenant_id=tenant_id, user_id=user_id)
    await ModelBlendPersistence().archive_blend(
        blend_id, tenant_id=tenant_id, user_id=user_id
    )
    return {"blend_id": blend_id, "status": "archived"}


# ── 榜单 / 日期 ─────────────────────────────────────────────────────────


@router.get("/{blend_id}/dates", summary="可用数据日")
async def get_blend_dates(
    blend_id: str,
    limit: int = Query(default=60, ge=1, le=365),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    blend = await _get_blend_or_404(blend_id, tenant_id=tenant_id, user_id=user_id)
    dates = await _run(
        ModelBlendService().available_dates(
            blend, tenant_id=tenant_id, user_id=user_id, limit=limit
        )
    )
    return {"blend_id": blend_id, "dates": dates}


@router.get("/{blend_id}/bounds", summary="历史数据端口（pred / runs 覆盖范围）")
async def get_blend_bounds(
    blend_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    blend = await _get_blend_or_404(blend_id, tenant_id=tenant_id, user_id=user_id)
    return {
        "blend_id": blend_id,
        **await _run(
            ModelBlendService().data_bounds(blend, tenant_id=tenant_id, user_id=user_id)
        ),
    }


@router.get("/{blend_id}/rolling", summary="滚动建仓视图（最近 N+1 日每日 TopK）")
async def get_blend_rolling(
    blend_id: str,
    date_str: str | None = Query(default=None, alias="date"),
    window: int = Query(default=10, ge=1, le=60),
    per_day: int = Query(default=5, ge=1, le=50),
    capital: float = Query(default=10_000_000.0, gt=0, le=1e12),
    # 可交易性硬条件：ST/退市剔除 + ADV20 最低成交额（亿元）+ 参与率
    exclude_st: bool = Query(default=True),
    min_adv_yi: float = Query(default=1.0, ge=0, le=1000),
    adv_participation: float = Query(default=0.01, gt=0, le=0.2),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    blend = await _get_blend_or_404(blend_id, tenant_id=tenant_id, user_id=user_id)
    return await _run(
        ModelBlendService().rolling(
            blend,
            tenant_id=tenant_id,
            user_id=user_id,
            end_date=_parse_date(date_str),
            window=window,
            per_day=per_day,
            capital=capital,
            exclude_st=exclude_st,
            min_adv_yi=min_adv_yi,
            adv_participation=adv_participation,
        )
    )


@router.get("/{blend_id}/period", summary="周期收益（固定持有期滚动，收盘口径无费用）")
async def get_blend_period(
    blend_id: str,
    start_str: str | None = Query(default=None, alias="start"),
    end_str: str | None = Query(default=None, alias="end"),
    capital: float = Query(default=10_000_000.0, gt=0, le=1e12),
    positions: int | None = Query(default=None, ge=1, le=50),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    blend = await _get_blend_or_404(blend_id, tenant_id=tenant_id, user_id=user_id)
    return await _run(
        ModelBlendService().period(
            blend,
            tenant_id=tenant_id,
            user_id=user_id,
            start_date=_parse_date(start_str),
            end_date=_parse_date(end_str),
            capital=capital,
            positions=positions,
        )
    )


@router.get("/{blend_id}/ranking", summary="当日组合榜单")
async def get_blend_ranking(
    blend_id: str,
    date_str: str | None = Query(default=None, alias="date"),
    top: int = Query(default=500, ge=1, le=2000),
    exclude_blacklist: bool = Query(
        default=False, description="按交易黑名单剔除榜单（推理中心「排除黑名单」开关）"
    ),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    blend = await _get_blend_or_404(blend_id, tenant_id=tenant_id, user_id=user_id)
    service = ModelBlendService()
    data_trade_date = _parse_date(date_str)
    if data_trade_date is None:
        model_ids = [str(m["model_id"]) for m in blend.get("members") or []]
        data_trade_date = await service.latest_common_date(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=model_ids,
            required=int(blend.get("min_coverage") or 2),
        )
        if data_trade_date is None:
            raise HTTPException(
                status_code=404,
                detail="组合没有可用数据日：请先对成员做串行补推，或调低最低覆盖数",
            )
    weights = (
        None
        if str(blend.get("weighting") or "equal") == "equal"
        else {
            str(m["model_id"]): float(m.get("weight") or 1.0)
            for m in blend.get("members") or []
        }
    )
    members = await _run(service.members_from_blend(blend))
    payload = await _run(
        service.ranking(
            tenant_id=tenant_id,
            user_id=user_id,
            members=members,
            data_trade_date=data_trade_date,
            method=str(blend.get("method") or "mean"),
            weights=weights,
            min_coverage=int(blend.get("min_coverage") or 2),
            trim_count=int(blend.get("trim_count") or 2),
            consensus_min_count=int(blend.get("consensus_min_count") or 0),
            top=top,
            blend_id=blend_id,
            annotate=True,
            exclude_blacklist=bool(exclude_blacklist),
        )
    )
    payload["blend_id"] = blend_id
    return payload


# ── 计划 ────────────────────────────────────────────────────────────────


@router.post("/{blend_id}/plans", summary="生成并存纸面调仓计划")
async def create_plan(
    blend_id: str,
    body: PlanBody,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    blend = await _get_blend_or_404(blend_id, tenant_id=tenant_id, user_id=user_id)
    plan_config = _plan_config(blend, body)
    payload = await _run(
        ModelBlendService().plan(
            blend,
            tenant_id=tenant_id,
            user_id=user_id,
            data_trade_date=_parse_date(body.data_trade_date),
            plan_config=plan_config,
            persist=body.persist,
        )
    )
    await _annotate_confirmable([payload])
    return payload


def _plan_config(blend: Mapping[str, Any], body: PlanBody) -> PlanConfig | None:
    """只在前端显式改参数时构造 PlanConfig；缺省仍走组合自身配置。"""
    if all(
        value is None
        for value in (
            body.capital,
            body.top_k,
            body.hold_days,
            body.entry_per_day,
            body.target_positions,
            body.min_adv_yi,
            body.exclude_st,
        )
    ):
        return None
    defaults = PlanConfig()
    return PlanConfig(
        capital=body.capital if body.capital is not None else defaults.capital,
        top_k=(
            body.top_k
            if body.top_k is not None
            else int(blend.get("target_positions") or defaults.top_k)
        ),
        hold_days=(
            body.hold_days
            if body.hold_days is not None
            else int(blend.get("rolling_window") or defaults.hold_days)
        ),
        entry_per_day=(
            body.entry_per_day
            if body.entry_per_day is not None
            else int(blend.get("entry_per_day") or defaults.entry_per_day)
        ),
        target_positions=(
            body.target_positions
            if body.target_positions is not None
            else int(blend.get("target_positions") or defaults.target_positions)
        ),
        min_adv_yi=(
            body.min_adv_yi if body.min_adv_yi is not None else defaults.min_adv_yi
        ),
        exclude_st=(
            body.exclude_st if body.exclude_st is not None else defaults.exclude_st
        ),
    )


@router.get("/{blend_id}/plans", summary="历史计划")
async def list_plans(
    blend_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    date_str: str | None = Query(default=None, alias="date"),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    blend = await _get_blend_or_404(blend_id, tenant_id=tenant_id, user_id=user_id)
    target = _parse_date(date_str)
    persistence = ModelBlendPersistence()
    if target is not None:
        existing = await persistence.get_plan_by_date(blend_id, data_trade_date=target)
        if existing is not None:
            return {"items": await _annotate_confirmable([existing])}
        # 无持久化计划 → 即时重算（只读，不写台账；前视防护靠 as_of 台账）
        recomputed = await _run(
            ModelBlendService().plan(
                blend,
                tenant_id=tenant_id,
                user_id=user_id,
                data_trade_date=target,
                persist=False,
            )
        )
        return {"items": await _annotate_confirmable([recomputed]), "recomputed": True}
    items = await persistence.list_plans(blend_id, limit=limit)
    return {"items": await _annotate_confirmable(items)}


@router.get("/{blend_id}/plans/{plan_id}", summary="计划详情")
async def get_plan(
    blend_id: str,
    plan_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    await _get_blend_or_404(blend_id, tenant_id=tenant_id, user_id=user_id)
    plan = await ModelBlendPersistence().get_plan(
        plan_id, tenant_id=tenant_id, user_id=user_id
    )
    if plan is None or plan.get("blend_id") != blend_id:
        raise HTTPException(status_code=404, detail="计划不存在")
    return (await _annotate_confirmable([plan]))[0]


@router.post("/{blend_id}/plans/{plan_id}/confirm", summary="确认纸面调仓")
async def confirm_plan(
    blend_id: str,
    plan_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    blend = await _get_blend_or_404(blend_id, tenant_id=tenant_id, user_id=user_id)
    persistence = ModelBlendPersistence()
    plan = await persistence.get_plan(plan_id, tenant_id=tenant_id, user_id=user_id)
    if plan is None or plan.get("blend_id") != blend_id:
        raise HTTPException(status_code=404, detail="计划不存在")
    confirmed = await _run(
        ModelBlendService().confirm_plan(
            blend, plan, tenant_id=tenant_id, user_id=user_id
        )
    )
    if isinstance(confirmed, dict):
        confirmed["can_confirm"] = False  # 刚确认的计划不再可确认
    return confirmed
