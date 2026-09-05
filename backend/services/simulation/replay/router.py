"""时光回放 API 端点。

POST   /sessions                   创建回放会话（信号直读模型 pred.parquet，创建即就绪）
GET    /sessions                   列出当前用户的会话
GET    /sessions/{id}              查看会话详情 + 进度
POST   /sessions/{id}/step         单步推演（执行下一个交易日）
POST   /sessions/{id}/propose      生成当日提案（手动模式）
DELETE /sessions/{id}              丢弃会话
GET    /strategy-templates         可选策略模板（含参数定义，供前端渲染表单）
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade_shared.deps import AuthContext, get_auth_context, get_db
from backend.services.simulation.models.replay import (
    ReplaySession,
    ReplayStatus,
)
from backend.services.simulation.replay.account import ReplayAccountManager
from backend.services.simulation.replay.analytics import (
    compute_attribution,
    compute_core_metrics,
    compute_nav_curve,
    compute_rolling_metrics,
)
from backend.services.simulation.replay.day_runner import ReplayDayRunner
from backend.services.simulation.replay.proposal import validate_confirmed
from backend.services.simulation.replay.signal_generator import (
    _find_pred_parquet,
)
from backend.services.simulation.services.ashare_matcher import MatchConfig
from backend.services.simulation.services.local_market_data import (
    get_local_market_data,
)
from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/replay", tags=["Replay"])


def _match_config_from_params(strategy_params: dict[str, Any]) -> MatchConfig:
    """从 strategy_params 构造撮合配置（bug 8 fix）。

    回放默认按开盘价撮合：信号是昨收后算出来的，次日开盘才可交易。
    费率/滑点未指定时沿用 MatchConfig 的 A 股默认值。
    """
    defaults = MatchConfig(price_mode="open")
    return MatchConfig(
        price_mode=str(strategy_params.get("price_mode", defaults.price_mode)),
        slippage_bps=float(
            strategy_params.get("slippage_bps", defaults.slippage_bps)
        ),
        commission_rate=float(
            strategy_params.get("commission_rate", defaults.commission_rate)
        ),
        commission_min=float(
            strategy_params.get("commission_min", defaults.commission_min)
        ),
        stamp_duty_rate=float(
            strategy_params.get("stamp_duty_rate", defaults.stamp_duty_rate)
        ),
        transfer_fee_rate=float(
            strategy_params.get("transfer_fee_rate", defaults.transfer_fee_rate)
        ),
        lot_size=int(strategy_params.get("lot_size", defaults.lot_size)),
    )


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    model_config = {"protected_namespaces": ()}

    name: str = Field(default="", max_length=128, description="会话名称")
    model_id: str | None = Field(default=None, description="模型 ID，空则用主模型")
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    initial_cash: float = Field(default=1_000_000.0, gt=0)
    start_date: date = Field(description="回放起始日（含）")
    end_date: date = Field(description="回放结束日（含）")
    auto_trade: bool = Field(
        default=True,
        description="true=自动执行策略提案；false=手动模式，需先 /propose 再带 confirmed 调 /step",
    )
    stop_loss_pct: float | None = Field(default=None, ge=0, le=1)


class ConfirmedOrder(BaseModel):
    symbol: str
    side: str = Field(description="BUY | SELL")
    quantity: int = Field(gt=0)


class StepRequest(BaseModel):
    # 手动模式：用户勾选并（可选）调小数量后的确认清单。
    # 只能是提案的子集且数量只能调小，止损笔无论是否勾选都强制执行。
    confirmed: list[ConfirmedOrder] | None = Field(
        default=None, description="手动模式确认清单"
    )
    skip: bool = Field(
        default=False, description="跳过今日：不下单，只做收盘估值并推进游标"
    )
    approved_orders: list[dict[str, Any]] | None = Field(
        default=None,
        description="[deprecated] 旧字段，等价于 confirmed，保留以兼容既有调用",
    )


class ProposalItem(BaseModel):
    symbol: str
    side: str
    quantity: int
    est_price: float
    origin: str
    cancellable: bool
    reason: str = ""
    avg_cost: float | None = None
    est_pnl: float | None = None
    est_amount: float | None = None
    stop_price: float | None = None
    gap_down: bool | None = None


class ProposalResponse(BaseModel):
    trade_date: str
    signal_count: int
    proposals: list[ProposalItem]
    error: str | None = None


class SessionResponse(BaseModel):
    session_id: str
    name: str
    status: str
    model_id: str | None
    initial_cash: float
    start_date: str
    end_date: str
    cursor_date: str | None
    next_date: str | None
    sessions_total: int
    sessions_done: int
    auto_trade: bool
    stop_loss_pct: float | None
    signal_progress: dict[str, Any]
    error_message: str | None
    strategy_params: dict[str, Any] = {}
    # 最近一个交易日的收盘估值快照（无推演记录时为 null），
    # 前端资产卡用它展示随推演日期变化的总资产/盈亏。
    latest_snapshot: dict[str, Any] | None = None


class StepResponse(BaseModel):
    trade_date: str
    signal_count: int
    filled: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    stop_loss_fills: list[dict[str, Any]]
    account: dict[str, Any]
    snapshot: dict[str, Any]
    error: str | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_to_response(
    s: ReplaySession, latest_snapshot: dict[str, Any] | None = None
) -> SessionResponse:
    return SessionResponse(
        session_id=str(s.session_id),
        name=s.name,
        status=s.status.value,
        model_id=s.model_id,
        initial_cash=s.initial_cash,
        start_date=s.start_date.isoformat(),
        end_date=s.end_date.isoformat(),
        cursor_date=s.cursor_date.isoformat() if s.cursor_date else None,
        next_date=s.next_date.isoformat() if s.next_date else None,
        sessions_total=s.sessions_total,
        sessions_done=s.sessions_done,
        auto_trade=s.auto_trade,
        stop_loss_pct=s.stop_loss_pct,
        signal_progress=s.signal_progress or {},
        error_message=s.error_message,
        strategy_params=s.strategy_params or {},
        latest_snapshot=latest_snapshot,
    )


def _compute_next_date(
    cursor: date | None, start: date, end: date, sessions: list[int]
) -> date | None:
    """从 cursor 推算下一个交易日。cursor=None 时返回 start。"""
    if cursor is None:
        start_int = int(start.strftime("%Y%m%d"))
        on_or_after = [d for d in sessions if d >= start_int]
        if not on_or_after:
            return None
        nxt = on_or_after[0]
    else:
        cursor_int = int(cursor.strftime("%Y%m%d"))
        after = [d for d in sessions if d > cursor_int]
        if not after:
            return None
        nxt = after[0]

    end_int = int(end.strftime("%Y%m%d"))
    if nxt > end_int:
        return None
    return date(nxt // 10000, (nxt % 10000) // 100, nxt % 100)


def _count_sessions(start: date, end: date, sessions: list[int]) -> int:
    s = int(start.strftime("%Y%m%d"))
    e = int(end.strftime("%Y%m%d"))
    return sum(1 for d in sessions if s <= d <= e)


def _snapshot_to_dict(r: Any) -> dict[str, Any]:
    return {
        "trade_date": r.trade_date.isoformat() if r.trade_date else None,
        "cash": float(r.cash),
        "market_value": float(r.market_value),
        "total_asset": float(r.total_asset),
        "day_pnl": float(r.day_pnl),
        "cum_pnl": float(r.cum_pnl),
        "position_count": int(r.position_count),
    }


async def _latest_snapshots(
    db: AsyncSession, session_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """批量取每个会话最近一个交易日的估值快照（DISTINCT ON，一次查询）。"""
    from backend.services.simulation.models.replay import (
        ReplayEquitySnapshot,
    )

    if not session_ids:
        return {}
    rows = (
        (
            await db.execute(
                select(ReplayEquitySnapshot)
                .where(ReplayEquitySnapshot.session_id.in_(session_ids))
                .order_by(
                    ReplayEquitySnapshot.session_id,
                    ReplayEquitySnapshot.trade_date.desc(),
                )
                .distinct(ReplayEquitySnapshot.session_id)
            )
        )
        .scalars()
        .all()
    )
    return {r.session_id: _snapshot_to_dict(r) for r in rows}


# ---------------------------------------------------------------------------
# 模型目录解析（生产模型 + 用户训练模型）
# ---------------------------------------------------------------------------


async def _resolve_model_dir_for_user(
    model_id: str,
    tenant_id: str,
    user_id: str,
) -> Path:
    """解析 model_id 对应的模型目录，并校验可用性。

    回放的模型有两类存放位置：
    - 系统/生产模型：MODELS_PRODUCTION/<model_id>（如 model_qlib、alpha158）
    - 用户训练模型：qm_user_models.storage_path（USER_MODELS_ROOT/<tenant>/<user>/<id>）

    先查生产目录，再查用户模型注册表。两者都找不到才报错 —— 原实现只查
    生产目录，导致用户选自己训练的模型必然 400「模型不存在」。

    抛 HTTPException(400) 而不是静默回落到默认模型：用户以为在跑自选模型、
    实际跑的是 model_qlib，比直接报错更难排查。
    """
    import json as _json
    import os as _os

    # 1. 生产模型目录
    prod_base = Path(_os.getenv("MODELS_PRODUCTION", "/app/models/production"))
    candidate = prod_base / model_id
    if candidate.is_dir():
        return _validate_model_dir(candidate, model_id, _json)

    # 2. 用户模型注册表（按 storage_path 定位，不猜路径拼法）
    #
    # user_id 在库里有两种形态：裸数字（"1001"）和 8 位补齐（"00000001"，
    # 训练服务写入时用了 normalize_user_id）。JWT 的 sub 只有一种，
    # 所以两种都试，否则用户看得到模型却选不了。
    uid_variants: list[str] = []
    raw_uid = str(user_id or "").strip()
    if raw_uid:
        uid_variants.append(raw_uid)
        if raw_uid.isdigit():
            padded = raw_uid.zfill(8)
            if padded != raw_uid:
                uid_variants.append(padded)
            stripped = str(int(raw_uid))
            if stripped not in uid_variants:
                uid_variants.append(stripped)

    record = None
    for uid in uid_variants:
        try:
            from backend.shared.model_registry import model_registry_service

            record = await model_registry_service.get_model(
                tenant_id=tenant_id,
                user_id=uid,
                model_id=model_id,
            )
        except Exception as exc:
            logger.warning(
                "查询用户模型注册表失败 model=%s uid=%s: %s", model_id, uid, exc
            )
            record = None
        if record:
            break

    if record:
        storage_path = str(record.get("storage_path") or "").strip()
        if storage_path:
            user_dir = Path(storage_path)
            if user_dir.is_dir():
                return _validate_model_dir(user_dir, model_id, _json)
            raise HTTPException(
                400, f"模型目录不存在: {model_id}（storage_path={storage_path}）"
            )

    raise HTTPException(400, f"模型不存在: {model_id}")


def _validate_model_dir(model_dir: Path, model_id: str, _json: Any) -> Path:
    """校验模型目录里 metadata.json 和 pred.parquet 都真实可读。

    回放信号直读模型的 pred.parquet（训练产出 + 每日推理回写的全量历史
    分数），没有它就无信号可用；断链 symlink 会让推演阶段才炸，所以在创建时就查。
    """
    meta_path = model_dir / "metadata.json"
    if not meta_path.is_file():
        raise HTTPException(400, f"模型缺少 metadata.json: {model_id}")
    try:
        _json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(400, f"模型元数据无法解析: {exc}") from None
    if _find_pred_parquet(model_dir) is None:
        raise HTTPException(
            400,
            f"模型缺少 pred.parquet（回放信号直接读取模型历史分数，"
            f"请先完成训练/推理产出）: {model_id}",
        )
    return model_dir


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED
)
async def create_session(
    req: CreateSessionRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """创建回放会话。

    信号直接读模型 pred.parquet（训练产出 + 每日推理回写的全量历史分数），
    无后台信号生成步骤，创建即就绪可直接推演。
    """
    if req.start_date >= req.end_date:
        raise HTTPException(400, "start_date 必须 < end_date")

    # model_id 前置校验 + 目录解析。支持生产模型和用户训练模型两类存放位置。
    # 注意不能用 signal_generator._resolve_model_dir —— 它对无效 id 会静默
    # 回落到 model_qlib，用户以为在跑自选模型，实际跑的是默认模型。
    resolved_model_dir: Path | None = None
    if req.model_id:
        resolved_model_dir = await _resolve_model_dir_for_user(
            model_id=req.model_id,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
        )

    market_data = get_local_market_data()
    # 目录枚举虽已降到毫秒级，仍是同步磁盘 IO，放线程里跑，不占用事件循环
    sessions = await asyncio.to_thread(market_data._sessions)
    if not sessions:
        raise HTTPException(503, "本地行情数据不可用")

    total_sessions = _count_sessions(req.start_date, req.end_date, sessions)
    if total_sessions == 0:
        raise HTTPException(400, "区间内无交易日")

    next_date = _compute_next_date(None, req.start_date, req.end_date, sessions)

    # 固化模型目录到 strategy_params（下划线前缀，与策略参数区分）：
    # 推演时信号直读器按它定位模型目录的 pred.parquet，无需再解析注册表。
    strategy_params = dict(req.strategy_params)
    if resolved_model_dir is not None:
        strategy_params["_model_dir"] = str(resolved_model_dir)

    row = ReplaySession(
        tenant_id=auth.tenant_id,
        user_id=int(auth.user_id) if auth.user_id.isdigit() else 0,
        name=req.name,
        model_id=req.model_id,
        strategy_params=strategy_params,
        initial_cash=req.initial_cash,
        start_date=req.start_date,
        end_date=req.end_date,
        next_date=next_date,
        sessions_total=total_sessions,
        sessions_done=0,
        status=ReplayStatus.READY,
        signal_progress={"done": total_sessions, "total": total_sessions},
        auto_trade=req.auto_trade,
        stop_loss_pct=req.stop_loss_pct,
    )
    db.add(row)
    await db.flush()

    # 初始化回放账户
    accounts = ReplayAccountManager(session_id=row.session_id)
    await accounts.init(initial_cash=req.initial_cash)

    await db.commit()

    return _session_to_response(row)


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """列出当前用户的回放会话。"""
    uid = int(auth.user_id) if auth.user_id.isdigit() else 0
    rows = (
        (
            await db.execute(
                select(ReplaySession)
                .where(
                    ReplaySession.tenant_id == auth.tenant_id,
                    ReplaySession.user_id == uid,
                )
                .order_by(ReplaySession.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    snaps = await _latest_snapshots(db, [r.session_id for r in rows])
    return [_session_to_response(r, snaps.get(r.session_id)) for r in rows]


async def _load_owned_session(
    db: AsyncSession,
    session_id: uuid.UUID,
    auth: AuthContext,
) -> ReplaySession:
    """按 session_id 取会话，并校验 tenant/user 归属。

    bug 9 fix: 原先 GET/{id}、step、DELETE 都只按 session_id 查，
    任意用户可读写他人会话。
    """
    uid = int(auth.user_id) if str(auth.user_id).isdigit() else 0
    row = (
        (
            await db.execute(
                select(ReplaySession).where(
                    ReplaySession.session_id == session_id,
                    ReplaySession.tenant_id == auth.tenant_id,
                    ReplaySession.user_id == uid,
                )
            )
        )
        .scalars()
        .first()
    )
    if not row:
        raise HTTPException(404, "会话不存在")
    return row


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session_detail(
    session_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """查看会话详情（含最新估值快照）。"""
    row = await _load_owned_session(db, session_id, auth)
    snaps = await _latest_snapshots(db, [session_id])
    return _session_to_response(row, snaps.get(session_id))


@router.post("/sessions/{session_id}/propose", response_model=ProposalResponse)
async def propose_day(
    session_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """生成当日调仓提案（手动模式）。不撮合、不落库，只写 pending_orders。

    幂等：已处于 awaiting_confirm 时直接返回已存提案，不重算 —— 避免用户
    刷新页面后提案变化（信号虽固定，但账户可能已被其他操作改动）。
    """
    row = await _load_owned_session(db, session_id, auth)

    if row.auto_trade:
        raise HTTPException(400, "自动模式无需提案，请直接调用 /step")
    if row.status == ReplayStatus.STEPPING:
        raise HTTPException(409, "正在执行中，请稍候")
    if row.status in (
        ReplayStatus.FINISHED,
        ReplayStatus.FAILED,
        ReplayStatus.DISCARDED,
    ):
        raise HTTPException(400, f"会话已终止（{row.status.value}）")
    if row.next_date is None:
        row.status = ReplayStatus.FINISHED
        await db.commit()
        raise HTTPException(400, "已到达回放终点")

    # 幂等返回
    if row.status == ReplayStatus.AWAITING_CONFIRM and row.pending_orders:
        cached = row.pending_orders
        return ProposalResponse(
            trade_date=cached.get("trade_date", row.next_date.isoformat()),
            signal_count=int(cached.get("signal_count") or 0),
            proposals=[ProposalItem(**p) for p in cached.get("proposals", [])],
        )

    accounts = ReplayAccountManager(session_id=session_id)
    runner = ReplayDayRunner()
    try:
        out = await runner.propose_day(
            db=db,
            session_id=session_id,
            trade_date=row.next_date,
            accounts=accounts,
            strategy_params=row.strategy_params,
            stop_loss_pct=row.stop_loss_pct,
            day_index=row.sessions_done,
        )
    except Exception as exc:
        logger.exception("生成提案失败 session=%s", session_id)
        raise HTTPException(500, f"生成提案失败: {exc}") from None

    if out.get("error"):
        raise HTTPException(500, f"生成提案失败: {out['error']}")

    row.pending_orders = {
        "trade_date": out["trade_date"],
        "signal_count": out["signal_count"],
        "proposals": out["proposals"],
    }
    row.status = ReplayStatus.AWAITING_CONFIRM
    await db.commit()

    return ProposalResponse(
        trade_date=out["trade_date"],
        signal_count=out["signal_count"],
        proposals=[ProposalItem(**p) for p in out["proposals"]],
    )


@router.post("/sessions/{session_id}/step", response_model=StepResponse)
async def step_session(
    session_id: uuid.UUID,
    req: StepRequest | None = None,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """单步推演：执行下一个交易日。stepping 状态返回 409 防连点。

    三条路径：
    - auto_trade=true          → 策略提案直接执行（忽略 confirmed）
    - auto_trade=false + skip  → 不下单，只结算并推进游标
    - auto_trade=false + confirmed → 服务端复校验后按清单执行
    """
    row = await _load_owned_session(db, session_id, auth)

    if row.status == ReplayStatus.STEPPING:
        raise HTTPException(409, "正在执行中，请勿重复点击")
    if row.status in (
        ReplayStatus.FINISHED,
        ReplayStatus.FAILED,
        ReplayStatus.DISCARDED,
    ):
        raise HTTPException(400, f"会话已终止（{row.status.value}）")

    if row.next_date is None:
        row.status = ReplayStatus.FINISHED
        await db.commit()
        raise HTTPException(400, "已到达回放终点")

    skip = bool(req.skip) if req else False
    # confirmed 为主，approved_orders 是兼容旧字段
    confirmed_in: list[dict[str, Any]] | None = None
    if req is not None:
        if req.confirmed is not None:
            confirmed_in = [c.model_dump() for c in req.confirmed]
        elif req.approved_orders is not None:
            confirmed_in = req.approved_orders

    manual = not row.auto_trade
    validation_rejected: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []

    if manual and not skip:
        if row.status != ReplayStatus.AWAITING_CONFIRM or not row.pending_orders:
            raise HTTPException(400, "手动模式请先调用 /propose 生成提案")
        if confirmed_in is None:
            raise HTTPException(400, "手动模式需提供 confirmed 确认清单")
        proposals = (row.pending_orders or {}).get("proposals") or []
        account_data = await ReplayAccountManager(session_id=session_id).get() or {}
        accepted, validation_rejected = validate_confirmed(
            confirmed=confirmed_in,
            proposals=proposals,
            account_data=account_data,
            lot_size=int((row.strategy_params or {}).get("lot_size", 100)),
        )

    prev_status = row.status
    row.status = ReplayStatus.STEPPING
    await db.commit()

    try:
        accounts = ReplayAccountManager(session_id=session_id)
        runner = ReplayDayRunner()
        cfg = _match_config_from_params(row.strategy_params or {})
        if manual:
            result = await runner.execute_day(
                db=db,
                session_id=session_id,
                trade_date=row.next_date,
                accounts=accounts,
                accepted=accepted,
                initial_cash=float(row.initial_cash),
                match_config=cfg,
                skip=skip,
            )
        else:
            result = await runner.run_day(
                db=db,
                session_id=session_id,
                trade_date=row.next_date,
                tenant_id=row.tenant_id,
                user_id=str(row.user_id),
                accounts=accounts,
                strategy_params=row.strategy_params,
                stop_loss_pct=row.stop_loss_pct,
                approved_orders=None,
                initial_cash=float(row.initial_cash),
                match_config=cfg,
                day_index=row.sessions_done,
            )
    except Exception as exc:
        row.status = ReplayStatus.FAILED
        row.error_message = str(exc)[:500]
        await db.commit()
        raise HTTPException(500, f"推演失败: {exc}") from None

    # bug 3 fix: run_day 内部报错时不推进游标，否则会静默跳过一天、曲线断档
    if result.error:
        row.status = prev_status
        row.error_message = result.error[:500]
        await db.commit()
        raise HTTPException(500, f"推演失败: {result.error}")

    # 校验阶段的拒绝要合并进返回，否则用户不知道自己哪笔被驳回
    if validation_rejected:
        result.rejected = validation_rejected + result.rejected

    # 更新游标
    market_data = get_local_market_data()
    sessions = await asyncio.to_thread(market_data._sessions)
    row.cursor_date = row.next_date
    row.sessions_done += 1
    row.next_date = _compute_next_date(
        row.cursor_date, row.start_date, row.end_date, sessions
    )
    row.status = ReplayStatus.READY if row.next_date else ReplayStatus.FINISHED
    row.pending_orders = None  # 已执行/跳过，清空提案
    await db.commit()

    return StepResponse(
        trade_date=result.trade_date.isoformat(),
        signal_count=result.signal_count,
        filled=result.filled,
        rejected=result.rejected,
        stop_loss_fills=result.stop_loss_fills,
        account=result.account,
        snapshot=result.snapshot,
        error=result.error,
    )


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """丢弃会话：CASCADE 删除所有关联数据 + 清除 Redis 账户。"""
    row = await _load_owned_session(db, session_id, auth)

    # 清除 Redis 账户
    accounts = ReplayAccountManager(session_id=session_id)
    accounts.drop()

    # CASCADE 删除 DB 数据
    await db.delete(row)
    await db.commit()


# ---------------------------------------------------------------------------
# 统计报告（R4）
# ---------------------------------------------------------------------------


class ReportResponse(BaseModel):
    metrics: dict[str, Any]
    nav_curve: list[dict[str, Any]]
    rolling: dict[str, Any]


class TradeRowResponse(BaseModel):
    id: int
    trade_date: str
    symbol: str
    side: str
    origin: str
    quantity: float
    price: float
    trade_value: float
    total_fee: float
    realized_pnl: float | None
    avg_cost_before: float | None
    holding_days: int | None
    return_pct: float | None


class AttributionRowResponse(BaseModel):
    symbol: str
    realized_pnl: float
    buy_count: int
    sell_count: int
    win_count: int
    loss_count: int
    avg_holding_days: float
    total_fee: float
    contribution: float


@router.get("/sessions/{session_id}/report", response_model=ReportResponse)
async def get_report(
    session_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """统计报告：核心指标 + 净值曲线 + 滚动指标。"""
    row = await _load_owned_session(db, session_id, auth)

    snapshots = await _load_snapshots(db, session_id)
    trades = await _load_trades(db, session_id)

    metrics = compute_core_metrics(snapshots, trades, float(row.initial_cash))
    nav_curve = compute_nav_curve(snapshots, float(row.initial_cash))
    rolling = compute_rolling_metrics(snapshots, float(row.initial_cash))

    return ReportResponse(metrics=metrics, nav_curve=nav_curve, rolling=rolling)


@router.get("/sessions/{session_id}/trades", response_model=list[TradeRowResponse])
async def get_trades(
    session_id: uuid.UUID,
    page: int = 1,
    size: int = 50,
    sort: str = "trade_date",
    side: str | None = None,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """逐笔流水（分页 + 排序 + 筛选）。"""
    await _load_owned_session(db, session_id, auth)

    trades = await _load_trades(db, session_id, side=side)

    # Sort
    reverse = sort.startswith("-")
    sort_key = sort.lstrip("-")
    valid_keys = {"trade_date", "symbol", "realized_pnl", "total_fee", "quantity"}
    if sort_key not in valid_keys:
        sort_key = "trade_date"
    trades.sort(key=lambda t: str(t.get(sort_key, "")), reverse=reverse)

    # Paginate
    start = (page - 1) * size
    page_trades = trades[start : start + size]

    # Compute return_pct for sells
    result: list[TradeRowResponse] = []
    for t in page_trades:
        ret_pct = None
        rpnl = t.get("realized_pnl")
        cost = t.get("avg_cost_before")
        px = float(t.get("price", 0))
        if rpnl is not None and cost and cost > 0 and px > 0:
            ret_pct = round((px - cost) / cost, 6)
        result.append(
            TradeRowResponse(
                id=int(t.get("id", 0)),
                trade_date=str(t.get("trade_date", "")),
                symbol=str(t.get("symbol", "")),
                # 统一输出大写（BUY/SELL），与前端比较口径一致；
                # 库里枚举值为小写，直接透出会导致方向列全部渲染成「卖出」
                side=str(t.get("side", "")).upper(),
                origin=str(t.get("origin", "signal")),
                quantity=float(t.get("quantity", 0)),
                price=round(float(t.get("price", 0)), 4),
                trade_value=round(float(t.get("trade_value", 0)), 2),
                total_fee=round(float(t.get("total_fee", 0)), 2),
                realized_pnl=round(float(rpnl), 2) if rpnl is not None else None,
                avg_cost_before=round(float(cost), 4) if cost is not None else None,
                holding_days=int(t["holding_days"]) if t.get("holding_days") is not None else None,
                return_pct=ret_pct,
            )
        )
    return result


@router.get(
    "/sessions/{session_id}/attribution",
    response_model=list[AttributionRowResponse],
)
async def get_attribution(
    session_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """个股归因：按 symbol 聚合盈亏/胜负/持有天数/贡献度。"""
    await _load_owned_session(db, session_id, auth)

    trades = await _load_trades(db, session_id)
    return [AttributionRowResponse(**a) for a in compute_attribution(trades)]


# ---------------------------------------------------------------------------
# Report data loaders
# ---------------------------------------------------------------------------


async def _load_snapshots(
    db: AsyncSession, session_id: uuid.UUID
) -> list[dict[str, Any]]:
    """加载 equity snapshots 为 plain dict 列表。"""
    from backend.services.simulation.models.replay import ReplayEquitySnapshot

    rows = (
        (
            await db.execute(
                select(ReplayEquitySnapshot)
                .where(ReplayEquitySnapshot.session_id == session_id)
                .order_by(ReplayEquitySnapshot.trade_date)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "trade_date": r.trade_date,
            "cash": float(r.cash),
            "market_value": float(r.market_value),
            "total_asset": float(r.total_asset),
            "day_pnl": float(r.day_pnl),
            "cum_pnl": float(r.cum_pnl),
            "realized_pnl_cum": float(r.realized_pnl_cum),
            "unrealized_pnl": float(r.unrealized_pnl),
            "position_count": int(r.position_count),
        }
        for r in rows
    ]


async def _load_trades(
    db: AsyncSession,
    session_id: uuid.UUID,
    side: str | None = None,
) -> list[dict[str, Any]]:
    """加载 trades 为 plain dict 列表。"""
    from backend.services.simulation.models.replay import ReplayTrade

    q = select(ReplayTrade).where(ReplayTrade.session_id == session_id)
    if side:
        # 兼容前端传大写（BUY/SELL），库内枚举值为小写
        q = q.where(ReplayTrade.side == side.lower())  # type: ignore[assignment]
    rows = (await db.execute(q.order_by(ReplayTrade.trade_date))).scalars().all()
    return [
        {
            "id": r.id,
            "trade_date": r.trade_date,
            "symbol": r.symbol,
            "side": r.side.value if hasattr(r.side, "value") else str(r.side),
            "origin": r.origin.value if hasattr(r.origin, "value") else str(r.origin),
            "quantity": float(r.quantity),
            "price": float(r.price),
            "trade_value": float(r.trade_value),
            "total_fee": float(r.total_fee),
            "realized_pnl": float(r.realized_pnl) if r.realized_pnl is not None else None,
            "avg_cost_before": float(r.avg_cost_before) if r.avg_cost_before is not None else None,
            "holding_days": int(r.holding_days) if r.holding_days is not None else None,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 策略模板（供前端渲染「选策略」表单）
# ---------------------------------------------------------------------------


class StrategyTemplateParam(BaseModel):
    name: str
    description: str
    default: Any
    min: float | None = None
    max: float | None = None


class StrategyTemplateResponse(BaseModel):
    id: str
    name: str
    description: str
    category: str
    difficulty: str
    params: list[StrategyTemplateParam]
    # 映射到 replay 可识别的 strategy_params（Qlib 口径 → replay 6 key）
    replay_params: dict[str, Any]


# Qlib 模板参数名 → replay strategy_params key
_PARAM_ALIAS = {
    "topk": "topk",
    "max_weight": "max_position_pct",
    "stop_loss": "stop_loss_pct",
    # TopkDropout 增量调仓参数（与回测引擎同语义，不再丢弃）
    "n_drop": "n_drop",
    "n_drop_ratio": "n_drop_ratio",
    "rebalance_days": "rebalance_days",
}


def _to_replay_params(tpl: Any) -> dict[str, Any]:
    """把模板默认参数翻译成 replay 的 strategy_params。

    replay 的 RebalanceCalculator 只认 topk / weight_mode / custom_weights /
    min_score / max_position_pct / lot_size / n_drop / n_drop_ratio /
    rebalance_days，Qlib 模板里的 signal 等在回放里没有对应语义，直接丢弃。
    """
    out: dict[str, Any] = {}
    for p in tpl.params or []:
        key = _PARAM_ALIAS.get(p.name)
        if key and p.default is not None:
            val = p.default
            # Qlib 的 stop_loss 是负数（-0.08 = 跌 8% 止损），
            # replay 的 stop_loss_pct 约束 ge=0 le=1，取绝对值
            if key == "stop_loss_pct":
                try:
                    val = abs(float(val))
                except (TypeError, ValueError):
                    continue
            out[key] = val
    # 按模板 id 推断权重模式
    tid = (tpl.id or "").lower()
    if "score_weighted" in tid:
        out["weight_mode"] = "score_weighted"
    elif "volatility" in tid:
        out["weight_mode"] = "score_weighted"
    else:
        out["weight_mode"] = "equal"
    return out


@router.get("/strategy-templates", response_model=list[StrategyTemplateResponse])
async def list_strategy_templates(
    auth: AuthContext = Depends(get_auth_context),
):
    """列出可用策略模板。复用 qlib_app 的模板加载器（单一数据源）。"""
    try:
        from backend.services.engine.qlib_app.services.strategy_templates import (
            get_all_templates,
        )
    except ImportError as exc:
        logger.warning("策略模板加载器不可用: %s", exc)
        raise HTTPException(503, "策略模板服务不可用") from None

    try:
        templates = get_all_templates()
    except Exception as exc:
        logger.error("读取策略模板失败: %s", exc)
        raise HTTPException(500, f"读取策略模板失败: {exc}") from None

    out: list[StrategyTemplateResponse] = []
    for tpl in templates:
        # 回放只支持 A 股（markets 空=全市场适用）
        if tpl.markets and "a_share" not in tpl.markets:
            continue
        out.append(
            StrategyTemplateResponse(
                id=tpl.id,
                name=tpl.name,
                description=tpl.description,
                category=tpl.category,
                difficulty=tpl.difficulty,
                params=[
                    StrategyTemplateParam(
                        name=p.name,
                        description=p.description,
                        default=p.default,
                        min=p.min,
                        max=p.max,
                    )
                    for p in (tpl.params or [])
                ],
                replay_params=_to_replay_params(tpl),
            )
        )
    return out
