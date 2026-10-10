"""Admin risk-rule CRUD, events, and dry-run."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from backend.services.api.user_app.middleware.auth import require_admin
from backend.services.live_trading.services.risk_rule_types import RiskRuleValidationError
from backend.services.live_trading.services.risk_service import RiskService
from backend.services.live_trading.services.risk_trigger_eval import parse_user_id
from backend.services.live_trading.services.risk_trigger_service import (
    apply_candidates,
    collect_needed_symbols,
    ensure_risk_events_table,
    evaluate_user_account,
    fetch_quotes_from_redis,
    load_implicit_stop_loss,
    today_trade_date,
)
from backend.services.trade_shared.models.risk_event import RiskEvent
from backend.services.trade_shared.redis_client import redis_client
from backend.services.trade_shared.schemas.risk_rule import (
    RiskDryRunRequest,
    RiskEventResponse,
    RiskRuleCreate,
    RiskRuleResponse,
    RiskRuleUpdate,
)
from backend.services.trade_shared.simulation_manager import SimulationAccountManager
from backend.shared.database_manager_v2 import get_session

router = APIRouter(dependencies=[Depends(require_admin)])


def _ensure_redis():
    if redis_client.client is None:
        redis_client.connect()
    return redis_client


def _ok(data, message: str = "success"):
    return {"success": True, "code": 200, "message": message, "data": data}


@router.get("/risk-rules")
async def list_risk_rules(
    active_only: bool = Query(False),
    current_user: dict = Depends(require_admin),
):
    async with get_session() as db:
        service = RiskService(db, _ensure_redis())
        rules = await service.list_rules(active_only=active_only)
        return _ok([RiskRuleResponse.model_validate(rule).model_dump() for rule in rules])


@router.post("/risk-rules")
async def create_risk_rule(
    payload: RiskRuleCreate,
    current_user: dict = Depends(require_admin),
):
    async with get_session() as db:
        service = RiskService(db, _ensure_redis())
        try:
            rule = await service.create_rule(payload)
        except RiskRuleValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _ok(RiskRuleResponse.model_validate(rule).model_dump(), "created")


@router.patch("/risk-rules/{rule_id}")
async def update_risk_rule(
    rule_id: int,
    payload: RiskRuleUpdate,
    current_user: dict = Depends(require_admin),
):
    async with get_session() as db:
        service = RiskService(db, _ensure_redis())
        try:
            rule = await service.update_rule(rule_id, payload)
        except RiskRuleValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if rule is None:
            raise HTTPException(status_code=404, detail="规则不存在")
        return _ok(RiskRuleResponse.model_validate(rule).model_dump(), "updated")


@router.delete("/risk-rules/{rule_id}")
async def delete_risk_rule(
    rule_id: int,
    current_user: dict = Depends(require_admin),
):
    async with get_session() as db:
        service = RiskService(db, _ensure_redis())
        deleted = await service.delete_rule(rule_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="规则不存在")
        return _ok({"deleted": True})


@router.get("/risk-events")
async def list_risk_events(
    user_id: int | None = Query(None),
    rule_type: str | None = Query(None),
    trade_date: date | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_admin),
):
    async with get_session() as db:
        await ensure_risk_events_table(db)
        stmt = select(RiskEvent).order_by(RiskEvent.created_at.desc(), RiskEvent.id.desc())
        if user_id is not None:
            stmt = stmt.where(RiskEvent.user_id == user_id)
        if rule_type:
            stmt = stmt.where(RiskEvent.rule_type == rule_type)
        if trade_date is not None:
            stmt = stmt.where(RiskEvent.trade_date == trade_date)
        if status:
            stmt = stmt.where(RiskEvent.status == status)
        else:
            # 预演（dry_run）只用于单次试算，不应污染“最近触发”列表；
            # 需要查看时显式传 ?status=dry_run。
            stmt = stmt.where(RiskEvent.status != "dry_run")
        stmt = stmt.limit(limit)
        rows = list((await db.execute(stmt)).scalars().all())
        return _ok([RiskEventResponse.model_validate(row).model_dump() for row in rows])


@router.post("/risk-rules/{rule_id}/dry-run")
async def dry_run_risk_rule(
    rule_id: int,
    payload: RiskDryRunRequest,
    current_user: dict = Depends(require_admin),
):
    redis = _ensure_redis()
    async with get_session() as db:
        await ensure_risk_events_table(db)
        service = RiskService(db, redis)
        rule = await service.get_rule(rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="规则不存在")
        manager = SimulationAccountManager(redis)
        user_id = parse_user_id(payload.user_id)
        account = await manager.get_account(
            user_id, tenant_id=payload.tenant_id, market=payload.market
        )
        if not account:
            raise HTTPException(status_code=404, detail="模拟账户不存在")
        positions = account.get("positions") or {}
        from backend.services.live_trading.services.risk_trigger_eval import RuleView

        views = [RuleView.from_orm(rule)]
        implicit = load_implicit_stop_loss(redis, payload.tenant_id, payload.user_id)
        quotes = fetch_quotes_from_redis(
            redis, collect_needed_symbols(positions, views + ([implicit] if implicit else []))
        )
        candidates = evaluate_user_account(
            positions=positions,
            quotes=quotes,
            rules=views,
            user_id=user_id,
            market=payload.market,
            account_mode="SIMULATION",
            implicit_rule=implicit,
        )
        applied = await apply_candidates(
            db,
            redis,
            candidates,
            tenant_id=payload.tenant_id,
            user_id=user_id,
            trade_date=today_trade_date(),
            dry_run=True,
        )
        return _ok(
            [
                {
                    "rule_id": item.rule_id,
                    "rule_name": item.rule_name,
                    "rule_type": item.rule_type,
                    "symbol": item.symbol,
                    "action": item.action,
                    "quantity": item.quantity,
                    "trigger_price": item.trigger_price,
                    "cost_price": item.cost_price,
                    "pnl_pct": item.pnl_pct,
                    "status": item.status,
                    "message": item.message,
                }
                for item in applied
            ]
        )
