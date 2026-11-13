"""arena 数据面 · 族 A4：实况（`/live/*` 共 11 个 GET）。

移植自隔壁 `api_server.py` L374–1731 同族 handler（上游 `/api/live/*`）。
响应形状逐字段一致（`arena/api/client.ts` 契约）；全部数据源改写记账在服务层
`backend/services/agent_arena/live_family.py` 的移植注头里。与上游的四处
**有意偏离**（路由层视角）：

1. 全部端点加 `Depends(get_current_user)`（上游 baymax 无鉴权；QM 统一口径）。
2. `/live/ledger` 与 `/live/closed` 的账本基准：PG 活账（agent_ledger_store）非空
   即用；空或读败回退 `/data/logs/live_ledger.json`（退役快照）。**两源不合并**
   ——PG 有行说明活账链路已在写，就能是唯一事实；为空才认文件。
3. `/live/indices` 不做 10s 限频：上游那层是为「6 次桥调用聚合」加的；QM 侧
   CN 是单次 Redis pipeline、US/HK 是单次腾讯 HTTP，30s 轮询下无聚合压力。
4. `/live/trades`（2026-10-09）：文件行 + PG 真单成交（trades ⨝ orders）合并，
   按桥委托号（orders.exchange_order_id）去重；文件写入者随 quant-Trader 退役
   停摆后，这是新成交进「成交」页的唯一通路。PG 侧只并 **>= PG_FILLS_CUTOVER
   （2026-09-30，迁移切点）** 的成交——切点前 74 行经逐单核对全是镜像/守护
   测试残留（`mir-*` 单，QMT 通道，非模型交易），不入展示。**注意与第 2 条口径
   不同**：账本是「PG 非空即唯一源」，成交是「两源合并去重」。

错误路径语义照抄上游：桥失败 → 502（detail=`桥查询失败: …`）；未知账户通道 →
422；PG 读取失败 → 200 + `{success: false, error: "quantmind PG 读取失败: …"}`
（「降级返回错误，不影响主链路」——上游注释原话）；文件类端点永不失败
（读败降级空数据，成功信封）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.shared.database_manager_v2 import get_session
from backend.shared.remote_quote_config import make_sync_client
from backend.services.api.user_app.middleware.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


def _lf():
    from backend.services.agent_arena import live_family as lf

    return lf


def _names() -> dict:
    from backend.services.agent_arena import market_lab

    return market_lab._names()


_quote_redis_client = None


def _redis_or_none():
    """行情源客户端；不可用 → None（各调用点已有 0 价/文件兜底，不拦主数据）。

    T4-4（审计 H14）：行情键写侧全在远端行情 Redis（resolve_remote_quote_redis），
    此处此前用本地 sentinel 客户端 → 标准键路径永远查空（且 decode_responses=False，
    bytes 取 str 键必 miss）。make_sync_client 两项语义一次给齐；结果进程级缓存，
    构造失败/显式停用（返回 None）不缓存 → 下次请求重试。
    """
    global _quote_redis_client
    if _quote_redis_client is None:
        try:
            _quote_redis_client = make_sync_client()
        except Exception as e:  # noqa: BLE001
            logger.warning("arena live: 行情 Redis 客户端不可用: %s", e)
            return None
    return _quote_redis_client


async def _ledger_doc() -> dict:
    """账本文档（文件形状）：PG 活账非空即主源，空/读败回退文件快照。"""
    lf = _lf()
    try:
        async with get_session(read_only=True) as session:
            led = await lf.read_pg_ledger(session)
        if led.get("agents"):
            return led
    except Exception as e:  # noqa: BLE001 读败回退（上游文件口径兜底）
        logger.warning("arena live ledger: PG 活账读取失败，回退文件快照: %s", e)
    return await lf.read_file_ledger()


# ---------- 账户 / 成交 / 分账 ----------

@router.get("/live/account")
async def live_account(_current_user: dict = Depends(get_current_user)):
    """实盘账户（桥实时）+ 持仓补名/实时价/买时/浮盈。上游：一切失败 → 502。"""
    lf = _lf()
    try:
        async with get_session(read_only=True) as session:
            data = await lf.account_live(session, redis_client=_redis_or_none())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"桥查询失败: {e}") from e
    return {"success": True, "data": data}


@router.get("/live/trades")
async def live_trades(
    limit: int = Query(200, ge=1, le=5000),
    _current_user: dict = Depends(get_current_user),
):
    """实盘交易记录（上游文件买卖行 + 本仓 PG 真单成交，桥委托号去重合并，最新在前）。

    2026-10-09 修复：arena 文件写入者随 quant-Trader 退役停摆（09-29 冻结），
    迁移后的真单成交只落 PG——PG 读败按文件类端点既有口径降级（仅上游文件）。
    """
    lf = _lf()
    rows = lf.load_trades(limit)
    try:
        async with get_session(read_only=True) as session:
            pg_rows = await lf.load_real_fills_pg(session, limit)
    except Exception as e:  # noqa: BLE001 PG 读败降级（文件类端点永不失败）
        logger.warning("arena live trades: PG 真单成交读取失败，仅上游文件: %s", e)
        pg_rows = []
    return {"success": True, "data": lf.merge_live_trade_rows(rows, pg_rows)[:limit]}


@router.get("/live/ledger")
async def live_ledger(_current_user: dict = Depends(get_current_user)):
    """实盘分账账本：每 agent ¥10 万虚拟子账户（额度使用 + 名下持仓）。

    持仓补实时价（Redis 标准键 → 桥兜底）后返回市值/权益；缺价持仓
    ``equity=null``（UI 显示 —），不拿成本价冒充市值。
    """
    lf = _lf()
    led = await _ledger_doc()
    codes = sorted(
        {
            str(code)
            for rec in (led.get("agents") or {}).values()
            for code in (rec.get("positions") or {})
        }
    )
    quotes = await lf.live_prices_for(codes, redis_client=_redis_or_none())
    return {"success": True, "data": lf.ledger_view(led, _names(), prices=quotes)}


@router.get("/live/equity")
async def live_equity(_current_user: dict = Depends(get_current_user)):
    """实盘净值点：旧文件线（切换前分钟级历史）+ PG 分账重建（切换起逐日 + 当日实时）。

    2026-10-20：文件写入者随旧栈 09-29 退役后，``live_equity.jsonl`` 只剩冻结
    线与采样器新点；PG 侧按成交/持仓/日线重建同一条线，两段在切换点拼接
    （``merge_live_equity``）。PG 读败降级为仅文件线（文件类端点永不失败）。
    """
    lf = _lf()
    file_data = lf.load_equity()
    try:
        async with get_session(read_only=True) as session:
            ledger = await lf.read_pg_ledger(session)
            agents = sorted((ledger.get("agents") or {}).keys())
            if not agents:
                return {"success": True, "data": file_data}
            pg_series = await lf.equity_series_pg(
                session, agents, redis_client=_redis_or_none()
            )
    except Exception as e:  # noqa: BLE001 PG 读败降级（文件类端点口径）
        logger.warning("arena live equity: PG 分账重建失败，仅旧文件线: %s", e)
        return {"success": True, "data": file_data}
    return {"success": True, "data": lf.merge_live_equity(file_data, pg_series)}


@router.get("/live/closed")
async def live_closed(
    limit: int = Query(60, ge=1, le=300),
    _current_user: dict = Depends(get_current_user),
):
    """已平仓事件（**仅真清仓**：卖出后该 agent 该股归零）。

    2026-10-20：PG 回合台账（``closed=true``）为主源——文件写入者冻结于
    09-29，迁移后的 agent 清仓只落 PG；文件重建/补录作历史兜底，两源按
    (agent, symbol, exit_date) 去重。PG 读败降级为仅文件（文件类端点口径）。
    """
    lf = _lf()
    ledger = await _ledger_doc()
    file_rows = lf.load_closed(limit, ledger=ledger)
    try:
        async with get_session(read_only=True) as session:
            pg_rows = await lf.load_closed_pg(session, limit)
    except Exception as e:  # noqa: BLE001 PG 读败降级（文件类端点永不失败）
        logger.warning("arena live closed: PG 回合台账读取失败，仅文件重建: %s", e)
        pg_rows = []
    return {
        "success": True,
        "data": lf.merge_closed_rows(pg_rows, file_rows, limit),
    }


# ---------- QM PG 只读同步（实盘快照/台账/L2） ----------

@router.get("/live/real-account")
async def live_real_account(
    account: str = Query("tdx"),
    _current_user: dict = Depends(get_current_user),
):
    """通道最新实盘快照（总资产/现金/持仓）。account=tdx|qmt（按账户家族分账）。"""
    lf = _lf()
    try:
        async with get_session(read_only=True) as session:
            data = await lf.real_account(session, account)
        return {"success": True, "data": data}
    except ValueError as e:  # 未知通道（上游同款 422）
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.warning("arena live real-account 读取失败: %s", e)
        return {"success": False, "error": f"quantmind PG 读取失败: {e}"}


@router.get("/live/real-accounts")
async def live_real_accounts(_current_user: dict = Depends(get_current_user)):
    """两通道最新快照摘要（通道选择器 / 「交易所在线」区块，一次取两条）。"""
    try:
        async with get_session(read_only=True) as session:
            data = await _lf().real_accounts(session)
        return {"success": True, "data": data}
    except Exception as e:  # noqa: BLE001
        logger.warning("arena live real-accounts 读取失败: %s", e)
        return {"success": False, "error": f"quantmind PG 读取失败: {e}"}


@router.get("/live/real-ledger")
async def live_real_ledger(
    account: str = Query("tdx"),
    _current_user: dict = Depends(get_current_user),
):
    """日终台账（净值曲线数据源）。上游形状：data 为数组，account/account_id 在信封顶层。"""
    lf = _lf()
    try:
        async with get_session(read_only=True) as session:
            d = await lf.real_ledger(session, account)
        return {"success": True, "data": d["rows"],
                "account": d["account"], "account_id": d["account_id"]}
    except ValueError as e:  # 未知通道（上游同款 422）
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.warning("arena live real-ledger 读取失败: %s", e)
        return {"success": False, "error": f"quantmind PG 读取失败: {e}"}


@router.get("/live/l2-factors")
async def live_l2_factors(
    symbol: str | None = Query(default=None),
    limit: int = Query(60, ge=1, le=500),
    _current_user: dict = Depends(get_current_user),
):
    """L2 因子快照（tdx_l2_snapshot 最近 limit 条，可选 symbol 过滤；前缀码）。"""
    try:
        async with get_session(read_only=True) as session:
            rows = await _lf().l2_factors(session, symbol, limit)
        return {"success": True, "data": rows}
    except Exception as e:  # noqa: BLE001
        logger.warning("arena live l2-factors 读取失败: %s", e)
        return {"success": False, "error": f"quantmind PG 读取失败: {e}"}


# ---------- 指数 / 新闻 ----------

@router.get("/live/indices")
async def live_indices(
    market: str = Query("cn"),
    _current_user: dict = Depends(get_current_user),
):
    """当日实时指数（Live 顶部行情条）：CN=Redis 快照→基准文件兜底；US/HK=腾讯。"""
    rows = await _lf().load_indices(market, redis_client=_redis_or_none())
    return {"success": True, "data": {"indices": rows}}


@router.get("/live/news")
async def live_news(
    tickers: str = "",
    hours: int = 24,
    limit: int = 10,
    keyword: str = "",
    _current_user: dict = Depends(get_current_user),
):
    """盘中新闻 → 本机 /api/v1/news/articles（服务层直接给信封，同上游语义）。"""
    return await _lf().load_news(tickers, hours, limit, keyword)
