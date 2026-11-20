"""User-scoped simulation risk-rule CRUD, events, and dry-run.

普通用户在「模拟盘 Tab → 风险控制」管理**本人**模拟盘风控规则的接口。
与 admin/risk.py 的区别：
- 鉴权为普通登录用户（get_current_user），不再要求 admin；
- 创建/修改强制 applies_to_all=False、user_ids=[本人]、parameters.trading_mode=SIMULATION；
- 列表只返回本人规则 + 全局生效中的规则；修改/删除/预演仅限本人规则（全局规则只读）。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from backend.services.api.user_app.middleware.auth import get_current_user
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
    RiskEventResponse,
    RiskRuleCreate,
    RiskRuleResponse,
    RiskRuleUpdate,
)
from backend.services.trade_shared.simulation_manager import SimulationAccountManager
from backend.shared.database_manager_v2 import get_session

router = APIRouter()


class RiskDryRunUserRequest(BaseModel):
    """用户级预演请求：身份一律以后端 token 为准，不接受 user_id。"""

    tenant_id: str = "default"
    market: str = "CN"


def _ensure_redis():
    if redis_client.client is None:
        redis_client.connect()
    return redis_client


def _ok(data, message: str = "success"):
    return {"success": True, "code": 200, "message": message, "data": data}


def _uid_of(current_user: dict) -> int:
    return parse_user_id((current_user or {}).get("user_id"))


def _is_owner(rule, uid: int) -> bool:
    if rule is None or bool(getattr(rule, "applies_to_all", True)):
        return False
    try:
        return uid in {int(item) for item in (getattr(rule, "user_ids", None) or [])}
    except (TypeError, ValueError):
        return False


def _visible_to_user(rule, uid: int) -> bool:
    """本人规则全部可见；全局规则仅生效中的可见（只读）。"""
    if _is_owner(rule, uid):
        return True
    return bool(getattr(rule, "applies_to_all", False)) and bool(
        getattr(rule, "is_active", False)
    )


async def _require_owned_rule(service: RiskService, rule_id: int, uid: int):
    rule = await service.get_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    if not _is_owner(rule, uid):
        raise HTTPException(status_code=403, detail="仅可管理本人创建的模拟盘规则")
    return rule


@router.get("/risk-rules")
async def list_my_risk_rules(
    active_only: bool = Query(False),
    current_user: dict = Depends(get_current_user),
):
    uid = _uid_of(current_user)
    async with get_session() as db:
        service = RiskService(db, _ensure_redis())
        rules = await service.list_rules(active_only=active_only)
        visible = [rule for rule in rules if _visible_to_user(rule, uid)]
        return _ok([RiskRuleResponse.model_validate(rule).model_dump() for rule in visible])


@router.post("/risk-rules")
async def create_my_risk_rule(
    payload: RiskRuleCreate,
    current_user: dict = Depends(get_current_user),
):
    uid = _uid_of(current_user)
    if not uid:
        raise HTTPException(status_code=401, detail="无法识别当前用户")
    async with get_session() as db:
        service = RiskService(db, _ensure_redis())
        # 名称全局唯一（DB unique 约束）：提前给出明确提示。
        existing = await service.get_rule_by_name(payload.rule_name)
        if existing is not None:
            raise HTTPException(status_code=400, detail="规则名称已存在请换一个")
        payload.applies_to_all = False
        payload.user_ids = [uid]
        params = dict(payload.parameters or {})
        params["trading_mode"] = "SIMULATION"
        params.setdefault("markets", ["CN"])
        payload.parameters = params
        try:
            rule = await service.create_rule(payload)
        except RiskRuleValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _ok(RiskRuleResponse.model_validate(rule).model_dump(), "created")


@router.patch("/risk-rules/{rule_id}")
async def update_my_risk_rule(
    rule_id: int,
    payload: RiskRuleUpdate,
    current_user: dict = Depends(get_current_user),
):
    uid = _uid_of(current_user)
    async with get_session() as db:
        service = RiskService(db, _ensure_redis())
        rule = await _require_owned_rule(service, rule_id, uid)
        if payload.rule_name and payload.rule_name != rule.rule_name:
            existing = await service.get_rule_by_name(payload.rule_name)
            if existing is not None and existing.id != rule.id:
                raise HTTPException(status_code=400, detail="规则名称已存在请换一个")
        # 作用域字段普通用户不可改；交易模式锁定模拟盘。
        payload.applies_to_all = None
        payload.user_ids = None
        if payload.parameters is not None or payload.rule_type is not None:
            params = dict(
                payload.parameters
                if payload.parameters is not None
                else (rule.parameters or {})
            )
            params["trading_mode"] = "SIMULATION"
            params.setdefault("markets", ["CN"])
            payload.parameters = params
            if payload.rule_type is None:
                payload.rule_type = rule.rule_type
        try:
            updated = await service.update_rule(rule_id, payload)
        except RiskRuleValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail="规则不存在")
        return _ok(RiskRuleResponse.model_validate(updated).model_dump(), "updated")


@router.delete("/risk-rules/{rule_id}")
async def delete_my_risk_rule(
    rule_id: int,
    current_user: dict = Depends(get_current_user),
):
    uid = _uid_of(current_user)
    async with get_session() as db:
        service = RiskService(db, _ensure_redis())
        await _require_owned_rule(service, rule_id, uid)
        deleted = await service.delete_rule(rule_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="规则不存在")
        return _ok({"deleted": True})


@router.get("/risk-events")
async def list_my_risk_events(
    rule_type: str | None = Query(None),
    trade_date: date | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    uid = _uid_of(current_user)
    async with get_session() as db:
        await ensure_risk_events_table(db)
        stmt = select(RiskEvent).order_by(RiskEvent.created_at.desc(), RiskEvent.id.desc())
        stmt = stmt.where(RiskEvent.user_id == uid)
        if rule_type:
            stmt = stmt.where(RiskEvent.rule_type == rule_type)
        if trade_date is not None:
            stmt = stmt.where(RiskEvent.trade_date == trade_date)
        if status:
            stmt = stmt.where(RiskEvent.status == status)
        else:
            # 预演（dry_run）只用于单次试算不应污染“最近触发”列表；
            # 需要查看时显式传 ?status=dry_run。
            stmt = stmt.where(RiskEvent.status != "dry_run")
        stmt = stmt.limit(limit)
        rows = list((await db.execute(stmt)).scalars().all())
        return _ok([RiskEventResponse.model_validate(row).model_dump() for row in rows])


@router.post("/risk-rules/{rule_id}/dry-run")
async def dry_run_my_risk_rule(
    rule_id: int,
    payload: RiskDryRunUserRequest,
    current_user: dict = Depends(get_current_user),
):
    uid = _uid_of(current_user)
    if not uid:
        raise HTTPException(status_code=401, detail="无法识别当前用户")
    redis = _ensure_redis()
    async with get_session() as db:
        await ensure_risk_events_table(db)
        service = RiskService(db, redis)
        rule = await service.get_rule(rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="规则不存在")
        if not _visible_to_user(rule, uid):
            raise HTTPException(status_code=403, detail="仅可预演本人可见的规则")
        manager = SimulationAccountManager(redis)
        account = await manager.get_account(
            uid, tenant_id=payload.tenant_id, market=payload.market
        )
        if not account:
            raise HTTPException(status_code=404, detail="模拟账户不存在")
        positions = account.get("positions") or {}
        from backend.services.live_trading.services.risk_trigger_eval import RuleView

        views = [RuleView.from_orm(rule)]
        implicit = load_implicit_stop_loss(redis, payload.tenant_id, uid)
        quotes = fetch_quotes_from_redis(
            redis, collect_needed_symbols(positions, views + ([implicit] if implicit else []))
        )
        candidates = evaluate_user_account(
            positions=positions,
            quotes=quotes,
            rules=views,
            user_id=uid,
            market=payload.market,
            account_mode="SIMULATION",
            implicit_rule=implicit,
        )
        applied = await apply_candidates(
            db,
            redis,
            candidates,
            tenant_id=payload.tenant_id,
            user_id=uid,
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
