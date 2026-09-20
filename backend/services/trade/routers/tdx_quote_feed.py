"""
TDX 实时行情 Feed 路由

- GET /tdx/quote-feed/status  - 行情 Feed 运行状态（Data Feed 检查用；含真实供数源段）
- GET/PUT /tdx/sltp-config    - 持仓股止损/止盈/移动止损配置
- GET /tdx/quote-tick-sessions - 持仓 tick 会话列表（开会话=持仓开始，闭会话=清仓）
- GET /tdx/quote-ticks        - 持仓 tick 明细查询（后期 tick 计算用）
"""
import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.services.trade_shared.deps import AuthContext, get_auth_context
from backend.services.live_trading.services.tdx_quote_feed import (
    feed_status,
    is_trading_time,
    load_sltp_config,
    save_sltp_config,
)

logger = logging.getLogger(__name__)
router = APIRouter()

#: quote_sources 段单次采样的标的数上限（持仓/热集 100~600 量级，键读走 pipeline 一次往返）
QUOTE_SOURCE_SAMPLE_LIMIT = 300


def _parse_symbols_arg(raw: str | None) -> list[str]:
    """逗号分隔采样标的（兼容全角逗号）；留空返回空列表（调用方回落到持仓）。"""
    if not raw:
        return []
    return [part.strip() for part in str(raw).replace("，", ",").split(",") if part.strip()]


class SltpConfigUpdate(BaseModel):
    stop_loss_pct: float = Field(0.08, ge=0.0, le=0.5, description="止损幅度 0.08=跌8%提醒")
    take_profit_pct: float | None = Field(None, ge=0.0, le=1.0, description="止盈幅度，空=不启用")
    trailing_stop_pct: float | None = Field(None, ge=0.0, le=0.5, description="移动止损幅度（离持仓最高价回撤），空=不启用")
    enabled: bool = Field(True, description="是否启用止损止盈提醒")


@router.get("/tdx/quote-feed/status")
async def get_quote_feed_status(
    symbols: str | None = Query(
        None,
        description="采样标的（逗号分隔，前缀/后缀/裸码均可）；留空=本馈送当前监控持仓",
    ),
    auth: AuthContext = Depends(get_auth_context),
):
    """实时行情 Feed 状态：持仓馈送 + **热集轮询（hot_set 段）** + **真实供数源（quote_sources 段）**。

    quote_sources 回答「谁在喂我的持仓」：按标的采样 ``market:snapshot:*`` 的 source 字段
    （桥 / QMT 备源 / TDX 订阅）聚合。WS 推送不带 source，页面此前只能拿本馈送的心跳
    （bridge_ok）冒充行情来源，两个写席轮转时文案就来回切——故在此按真实写侧字段聚合。
    采样读的是同步 Redis 客户端，放线程里跑，避免阻塞事件循环。
    """
    from backend.services.live_trading.services.quote_source_audit import (
        collect_quote_sources,
    )
    from backend.services.live_trading.services.tdx_hot_set_feed import (
        hot_set_feed_status,
    )

    status = dict(feed_status)
    status["hot_set"] = dict(hot_set_feed_status)
    status["is_trading_time"] = is_trading_time()
    status["server_time"] = datetime.now(timezone.utc).isoformat()
    if status.get("last_feed_at"):
        try:
            last = datetime.fromisoformat(status["last_feed_at"])
            age = int((datetime.now().astimezone() - last).total_seconds())
            status["last_feed_age_sec"] = max(0, age)
        except (TypeError, ValueError):
            pass

    sample = _parse_symbols_arg(symbols) or [str(s) for s in (status.get("symbols") or [])]
    status["quote_sources"] = await asyncio.to_thread(
        collect_quote_sources, sample, limit=QUOTE_SOURCE_SAMPLE_LIMIT
    )
    return status


@router.get("/tdx/sltp-config")
async def get_sltp_config(auth: AuthContext = Depends(get_auth_context)):
    """读取持仓股止损止盈提醒配置。"""
    return load_sltp_config(
        (auth.tenant_id or "default").strip() or "default",
        str(auth.user_id or "00000001").strip() or "00000001",
    )


@router.put("/tdx/sltp-config")
async def update_sltp_config(
    data: SltpConfigUpdate,
    auth: AuthContext = Depends(get_auth_context),
):
    """保存止损止盈提醒配置（仅提醒不下单，通达信桥守护进程仍负责真实止损单）。"""
    if not data.enabled and not data.take_profit_pct and not data.trailing_stop_pct and data.stop_loss_pct <= 0:
        raise HTTPException(status_code=400, detail="至少保留一个有效提醒条件")
    return save_sltp_config(
        (auth.tenant_id or "default").strip() or "default",
        str(auth.user_id or "00000001").strip() or "00000001",
        data.model_dump(),
    )


@router.get("/tdx/quote-tick-sessions")
async def get_quote_tick_sessions(
    symbol: str | None = Query(None, description="过滤股票（SH600036），留空返回全部"),
    status: str | None = Query(None, pattern="^(OPEN|CLOSED)$", description="OPEN=持仓中 CLOSED=已清仓"),
    limit: int = Query(50, ge=1, le=200),
    auth: AuthContext = Depends(get_auth_context),
):
    """持仓 tick 会话列表：开盘会话=开始记录 tick，闭会话=该持仓已清掉。

    每个会话对应一次完整持仓周期，后期 tick 计算按 session_id 取数。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    tenant_id = (auth.tenant_id or "default").strip() or "default"
    user_id = str(auth.user_id or "00000001").strip() or "00000001"
    where = ["tenant_id = :tenant_id", "user_id = :user_id"]
    params: dict = {"tenant_id": tenant_id, "user_id": user_id}
    if symbol:
        where.append("symbol = :symbol")
        params["symbol"] = str(symbol).upper()
    if status:
        where.append("status = :status")
        params["status"] = status
    sql = (
        f"SELECT session_id, symbol, entry_tick_time::text AS entry_tick_time, "
        f"exit_tick_time::text AS exit_tick_time, status "
        f"FROM tdx_position_sessions WHERE {' AND '.join(where)} "
        f"ORDER BY entry_tick_time DESC LIMIT :limit"
    )
    async with get_session(read_only=True) as db:
        rows = (await db.execute(text(sql), {**params, "limit": limit})).mappings().all()
    return {"sessions": [dict(r) for r in rows], "total": len(rows)}


@router.get("/tdx/quote-ticks")
async def get_quote_ticks(
    symbol: str = Query(..., description="股票代码（SH600036 前缀格式）"),
    session_id: str | None = Query(None, description="持仓会话 ID，留空按 symbol+时间范围查"),
    start: str | None = Query(None, description="起始时间 ISO（含）"),
    end: str | None = Query(None, description="结束时间 ISO（含）"),
    limit: int = Query(5000, ge=1, le=50000, description="最大行数"),
    direction: str = Query("asc", pattern="^(asc|desc)$"),
    auth: AuthContext = Depends(get_auth_context),
):
    """查询持仓实时 tick 明细（3s 间隔快照），供后期 tick 计算。

    指定 session_id 时返回该次完整持仓周期的全部 tick（从开会话到清仓）。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    tenant_id = (auth.tenant_id or "default").strip() or "default"
    user_id = str(auth.user_id or "00000001").strip() or "00000001"
    symbol = str(symbol).upper()
    where = [
        "tenant_id = :tenant_id",
        "user_id = :user_id",
        "symbol = :symbol",
    ]
    params: dict = {"tenant_id": tenant_id, "user_id": user_id, "symbol": symbol}
    if session_id:
        where.append("session_id = :session_id")
        params["session_id"] = session_id
    if start:
        where.append("tick_time >= :start")
        params["start"] = start
    if end:
        where.append("tick_time <= :end")
        params["end"] = end
    order = "ASC" if direction == "asc" else "DESC"
    sql = (
        f"SELECT id, session_id, symbol, tick_time::text AS tick_time, "
        f"price, open, high, low, volume, amount, is_stale, source "
        f"FROM tdx_position_ticks WHERE {' AND '.join(where)} "
        f"ORDER BY tick_time {order} LIMIT :limit"
    )
    async with get_session(read_only=True) as db:
        rows = (await db.execute(text(sql), {**params, "limit": limit})).mappings().all()
    return {"symbol": symbol, "ticks": [dict(r) for r in rows], "total": len(rows)}
