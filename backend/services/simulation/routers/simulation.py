from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status, File, UploadFile
from pydantic import BaseModel

from backend.services.trade_shared.deps import AuthContext, get_auth_context, get_db, get_redis
from backend.services.trade_shared.redis_client import RedisClient
from backend.services.simulation.services.fund_snapshot_service import (
    SimulationFundSnapshotService,
)
from backend.services.simulation.services.simulation_manager import (
    SimulationAccountManager,
    require_sim_user_id,
)
from backend.services.simulation.services.ocr_service import SimulationOCRService
from backend.services.trade_shared.trade_config import settings
from backend.shared.database_manager_v2 import get_db_manager
from backend.shared.stock_utils import StockCodeUtil
import logging
import httpx
from sqlalchemy import text

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_user_id(raw_user_id: str, tenant_id: str = "default") -> int:
    """兼容别名，统一走 require_sim_user_id（OSS admin 归保留账户 0）。"""
    return require_sim_user_id(raw_user_id, tenant_id=tenant_id)

def _to_quantdb_suffix(symbol: str) -> str:
    """prefix(SH600036) → suffix(600036.SH)；返回空即无法归一化，原样返回。"""
    suffix = StockCodeUtil.to_suffix(str(symbol or ""))
    return suffix or str(symbol or "")


async def _get_latest_close_from_quantdb(symbol: str) -> float:
    """从 QuantDB 未复权 K 线(daily_unadjusted)最新交易日读取收盘价。"""
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub.get_instance()
        if not hub.available:
            return 0.0
        end = date.today()
        start = end - timedelta(days=90)
        df = hub.fetch_daily_kline(
            _to_quantdb_suffix(symbol), start, end, adjust="none"
        )
        if df is None or df.empty or "close" not in df.columns:
            return 0.0
        df = df.dropna(subset=["close"])
        if df.empty:
            return 0.0
        return float(df["close"].iloc[-1])
    except Exception as exc:
        logger.error("Failed to fetch quantdb price for %s: %s", symbol, exc)

    return 0.0


async def _get_latest_price(symbol: str) -> float:
    """
    获取股票最新价格逻辑：
    1. 优先尝试行情服务实时数据
    2. 如果失败或数据为0，查询数据库 stock_daily_latest 获取最后一天收盘价
    """
    market_url = settings.MARKET_DATA_SERVICE_URL.rstrip("/")
    price = 0.0

    # Level 1: 实时行情
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{market_url}/api/v1/quotes/{symbol}")
            if resp.status_code == 200:
                q_data = resp.json()
                price = float(q_data.get("current_price") or q_data.get("last_price") or 0)
    except Exception as e:
        logger.warning(f"Failed to fetch real-time price for {symbol}: {e}")

    # Level 2: QuantDB 未复权 K 线兜底
    if price <= 0:
        price = await _get_latest_close_from_quantdb(symbol)
        if price > 0:
            logger.info(f"Fallback to QuantDB close for {symbol}: {price}")

    return price


async def _resolve_symbol_by_name(name: str) -> str | None:
    """
    通过股票名称反查标准 Prefix 代码
    """
    if not name:
        return None

    try:
        db_manager = get_db_manager()
        # 清理名称中的特殊字符，如 *ST
        clean_name = name.replace("*", "").strip()
        query = text("""
            SELECT symbol FROM stock_daily_latest
            WHERE stock_name LIKE :name
            ORDER BY trade_date DESC LIMIT 1
        """)

        async with db_manager.get_master_session() as session:
            # 先试完全匹配
            result = await session.execute(query, {"name": f"%{clean_name}%"})
            row = result.fetchone()
            if row:
                return row[0]
    except Exception as e:
        logger.error(f"Failed to resolve symbol for name {name}: {e}")

    return None


async def _resolve_user_api_key(user_id: str) -> str | None:
    """从 user_profiles 读取用户级 API Key，兼容常见 user_id 形态。"""
    uid = str(user_id or "").strip()
    if not uid:
        return None

    candidates = [uid]
    if uid.isdigit():
        candidates.extend([uid.zfill(8), str(int(uid))])

    try:
        db_manager = get_db_manager()
        query = text("SELECT api_key FROM user_profiles WHERE user_id = :uid LIMIT 1")
        async with db_manager.get_master_session() as session:
            for cand in candidates:
                row = await session.execute(query, {"uid": cand})
                found = row.fetchone()
                if found and found[0]:
                    key = str(found[0]).strip()
                    if key:
                        return key
    except Exception as e:
        logger.warning(f"Failed to resolve user API key for OCR, user_id={uid}: {e}")

    return None


DEFAULT_INITIAL_CASH = 1_000_000.0
SIM_AMOUNT_STEP = 100_000
COOLDOWN_DAYS = 30


class AccountResetRequest(BaseModel):
    initial_cash: float | None = None
    market: str | None = None  # 模拟账户市场维度（CN/HK/US/FUTURES/CRYPTO），缺省 CN


class HoldingItem(BaseModel):
    symbol: str
    quantity: float
    name: str | None = None
    current_price: float | None = None


class SyncHoldingsRequest(BaseModel):
    holdings: list[HoldingItem]
    available_cash: float | None = None


# SimulationSettingsRequest removed as it is deprecated.


class SimulationSettingsResponse(BaseModel):
    initial_cash: float
    last_modified_at: str | None = None
    next_allowed_modified_at: str | None = None
    can_modify: bool
    cooldown_days: int
    amount_step: int


class SimulationFundSnapshotResponse(BaseModel):
    snapshot_date: date
    total_asset: Decimal
    available_balance: Decimal
    frozen_balance: Decimal
    market_value: Decimal
    initial_capital: Decimal
    total_pnl: Decimal
    today_pnl: Decimal
    source: str


@router.get("/settings")
async def get_simulation_settings(
    auth: AuthContext = Depends(get_auth_context),
    redis: RedisClient = Depends(get_redis),
):
    manager = SimulationAccountManager(redis)
    uid = _require_user_id(auth.user_id, auth.tenant_id)
    data = await manager.get_settings(
        user_id=uid,
        tenant_id=auth.tenant_id,
        default_initial_cash=DEFAULT_INITIAL_CASH,
        cooldown_days=COOLDOWN_DAYS,
    )
    return {
        "success": True,
        "data": {
            "initial_cash": data.get("initial_cash", DEFAULT_INITIAL_CASH),
            "can_modify": False, # Modification deprecated
            "amount_step": SIM_AMOUNT_STEP,
            "cooldown_days": COOLDOWN_DAYS
        }
    }


async def _capture_simulation_snapshot(redis: RedisClient) -> None:
    try:
        await SimulationFundSnapshotService.capture_all(redis)
    except Exception as exc:
        # 快照失败不应影响主流程；只记录日志，避免配置/重置接口被历史数据采集问题阻断。
        import logging

        logging.getLogger(__name__).warning(
            "Failed to capture simulation fund snapshot: %s",
            exc,
            exc_info=True,
        )


# update_simulation_settings (PUT /settings) removed as it is deprecated.


@router.post("/reset")
async def reset_simulation_account(
    request: AccountResetRequest,
    auth: AuthContext = Depends(get_auth_context),
    redis: RedisClient = Depends(get_redis),
    db=Depends(get_db),
):
    """
    Reset simulation account with initial cash.
    重置即视为全新起点：必须同步停止当前运行任务（沙箱+active_strategy+portfolio），
    否则会出现“资金已清零但控制台仍显示运行中”。
    """
    manager = SimulationAccountManager(redis)
    uid = _require_user_id(auth.user_id, auth.tenant_id)
    if request.initial_cash is None:
        settings = await manager.get_settings(
            user_id=uid,
            tenant_id=auth.tenant_id,
            default_initial_cash=DEFAULT_INITIAL_CASH,
            cooldown_days=COOLDOWN_DAYS,
        )
        initial_cash = float(settings.get("initial_cash", DEFAULT_INITIAL_CASH))
    else:
        initial_cash = float(request.initial_cash)
    if initial_cash < SIM_AMOUNT_STEP or int(initial_cash) % SIM_AMOUNT_STEP != 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"初始金额必须为{int(SIM_AMOUNT_STEP / 10000)}万元的整数倍",
        )

    # 当显式传入 initial_cash 时，同步更新 settings，保证后续 initial_equity 口径一致。
    if request.initial_cash is not None:
        await manager.set_initial_cash(uid, initial_cash, tenant_id=auth.tenant_id)

    market = str(request.market or "CN").upper()
    # 清空数据库中的历史交易/订单/快照，避免重置后前端仍拉到旧数据。
    # user_id 有 int 与原始 sub 两种口径（历史 varchar 兼容），一并清理。
    # 新台账（accounts/lots/ledger/daily/fills/orders_v2）同步清空，否则 PG 新旧两套
    # 台账分叉，对账与重建读到孤儿数据。
    _NEW_LEDGER_TABLES = (
        "simulation_accounts",
        "simulation_position_lots",
        "simulation_cash_ledger",
        "simulation_account_daily",
        "simulation_position_daily",
        "simulation_fills",
        "simulation_orders",
    )
    try:
        from sqlalchemy import text as _text
        from backend.shared.database_manager_v2 import get_session as _get_session
        from backend.services.simulation.services.market_rules import market_symbol_sql_regex
        # 成交/委托表没有 market 列，市场只隐含在 symbol 形态里。
        # 重置某一个市场的账户时，不能把其它市场的成交历史一起删掉
        # （历史行为是全删 —— 新开了「按市场开通模拟盘」入口后必须先按 symbol 收窄）。
        _sym_pat = market_symbol_sql_regex(market)
        _sym_clause = " AND symbol ~* :sym_pat" if _sym_pat else ""
        uid_str_variants = {str(uid), str(auth.user_id)}
        async with _get_session() as _session:
            await _session.execute(
                _text(f"DELETE FROM sim_trades WHERE tenant_id=:tid AND user_id=:uid{_sym_clause}"),
                {"tid": auth.tenant_id, "uid": uid, **({"sym_pat": _sym_pat} if _sym_pat else {})},
            )
            await _session.execute(
                _text(f"DELETE FROM sim_orders WHERE tenant_id=:tid AND user_id=:uid{_sym_clause}"),
                {"tid": auth.tenant_id, "uid": uid, **({"sym_pat": _sym_pat} if _sym_pat else {})},
            )
            for uv in uid_str_variants:
                await _session.execute(
                    _text(f"DELETE FROM sim_trades WHERE tenant_id=:tid AND cast(user_id as varchar)=:uid_str{_sym_clause}"),
                    {"tid": auth.tenant_id, "uid_str": uv, **({"sym_pat": _sym_pat} if _sym_pat else {})},
                )
                await _session.execute(
                    _text(f"DELETE FROM sim_orders WHERE tenant_id=:tid AND cast(user_id as varchar)=:uid_str{_sym_clause}"),
                    {"tid": auth.tenant_id, "uid_str": uv, **({"sym_pat": _sym_pat} if _sym_pat else {})},
                )
                await _session.execute(_text("DELETE FROM simulation_fund_snapshots WHERE tenant_id=:tid AND user_id=:uid2"), {"tid": auth.tenant_id, "uid2": uv})
                for _table in _NEW_LEDGER_TABLES:
                    try:
                        # SAVEPOINT 隔离：表不存在（如旧库）只回滚本条，不影响已删数据
                        async with _session.begin_nested():
                            await _session.execute(
                                _text(f"DELETE FROM {_table} WHERE tenant_id=:tid AND user_id=:uid2"),
                                {"tid": auth.tenant_id, "uid2": uv},
                            )
                    except Exception:
                        continue
            await _session.commit()
    except Exception as _e:
        logger.warning(f"Reset DB cleanup failed for {auth.tenant_id}:{uid}: {_e}")

    # 清空 Redis 缓存（交易列表/统计），避免重置后仍命中旧缓存秒级延迟
    # 运行态身份必须与 live_trading 完全同口径：数字补零8位、非数字保持原样。
    # 模拟账户 uid（admin->0）只用于资金键，运行时停止必须用 runtime_user。
    _raw_sub = str(auth.user_id or "").strip()
    _runtime_user = _raw_sub.zfill(8) if _raw_sub.isdigit() else _raw_sub
    _runtime_tenant = str(auth.tenant_id or "default").strip() or "default"
    try:
        if redis.client:
            redis.delete_pattern(f"sim_trade:list:{auth.tenant_id}:{uid}:*")
            redis.delete_pattern(f"sim_trade:stats:{auth.tenant_id}:{uid}:*")
            redis.delete_pattern(f"sim_trade:list:{auth.tenant_id}:{auth.user_id}:*")
            redis.delete_pattern(f"sim_trade:stats:{auth.tenant_id}:{auth.user_id}:*")
            # 订单缓存
            redis.delete_pattern(f"order:list:user:{uid}:*")
            # 重置后需可立即再次触发交易：清掉托管调度的幂等锁与 bootstrap 锁
            # 否则同日同策略会被 36h/24h 锁挡住，表现为“重置后不交易”
            for pat in (
                f"qm:hosted:simulation:{auth.tenant_id}:{uid}:*",
                f"qm:hosted:simulation:{auth.tenant_id}:{auth.user_id}:*",
                f"qm:hosted:simulation:{_runtime_tenant}:{_runtime_user}:*",
                f"qm:hosted:simulation:bootstrap:{auth.tenant_id}:{uid}:*",
                f"qm:hosted:simulation:bootstrap:{auth.tenant_id}:{auth.user_id}:*",
                f"qm:hosted:simulation:bootstrap:{_runtime_tenant}:{_runtime_user}:*",
            ):
                try:
                    redis.delete_pattern(pat)
                except Exception:
                    pass
            # 1) 先停沙箱：精确 sid + 按用户前缀兜底双保险，避免 sid 为空/口径不一致时漏杀
            try:
                from backend.services.trade.sandbox.manager import sandbox_manager

                _sids: set[str] = set()
                for key in (
                    f"trade:active_strategy:{_runtime_tenant}:{_runtime_user}",
                    f"trade:active_strategy:{_runtime_tenant}:{_runtime_user.zfill(8)}",
                    f"trade:active_strategy:{auth.tenant_id}:{_raw_sub}",
                    f"trade:active_strategy:{auth.tenant_id}:{_raw_sub.zfill(8)}",
                    f"trade:active_strategy:default:{_raw_sub}",
                    f"trade:active_strategy:default:{_raw_sub.zfill(8)}",
                ):
                    try:
                        raw = redis.client.get(key)
                        if raw:
                            import json as _json

                            d = _json.loads(raw)
                            sid = str(d.get("strategy_id") or d.get("strategy_name") or "").strip()
                            if sid:
                                _sids.add(sid)
                    except Exception:
                        pass
                for sid in _sids:
                    try:
                        sandbox_manager.stop_strategy(_runtime_tenant, _runtime_user, sid)
                    except Exception:
                        pass
                # 前缀兜底：即使 sid 取不到，也能杀掉该用户残留进程
                for t_uid in {(_runtime_tenant, _runtime_user), (auth.tenant_id, _raw_sub), ("default", _raw_sub)}:
                    try:
                        sandbox_manager.stop_user_strategies(t_uid[0], t_uid[1])
                    except Exception:
                        pass
            except Exception as _e:
                logger.warning(f"Reset sandbox stop failed for {_runtime_tenant}:{_runtime_user}: {_e}")
            # 2) 再删 active_strategy 全写法，避免 /status 仍读到旧运行态
            for key in {
                f"trade:active_strategy:{_runtime_tenant}:{_runtime_user}",
                f"trade:active_strategy:{_runtime_tenant}:{_runtime_user.zfill(8)}",
                f"trade:active_strategy:{auth.tenant_id}:{_raw_sub}",
                f"trade:active_strategy:{auth.tenant_id}:{_raw_sub.zfill(8)}",
                f"trade:active_strategy:default:{_raw_sub}",
                f"trade:active_strategy:default:{_raw_sub.zfill(8)}",
                f"trade:active_strategy:{auth.tenant_id}:{uid}",
                f"trade:active_strategy:{auth.tenant_id}:{str(uid).zfill(8)}",
            }:
                try:
                    redis.client.delete(key)
                except Exception:
                    pass
    except Exception:
        pass

    # 3) 同步 portfolio run_status running->stopped，与 /stop 接口同口径，前端不再显示运行中
    try:
        from sqlalchemy import desc as _desc
        from sqlalchemy import select as _select
        from backend.services.trade_shared.portfolio.models import Portfolio as _Portfolio

        for _pu in {_runtime_user, _raw_sub}:
            try:
                _stmt = (
                    _select(_Portfolio)
                    .where(
                        _Portfolio.tenant_id == _runtime_tenant,
                        _Portfolio.user_id == _pu,
                        _Portfolio.run_status == "running",
                        _Portfolio.is_deleted.is_(False),
                    )
                    .order_by(_desc(_Portfolio.updated_at))
                    .limit(1)
                )
                _res = await db.execute(_stmt)
                _pf = _res.scalars().first()
                if _pf is not None:
                    _pf.run_status = "stopped"
                    await db.commit()
            except Exception:
                try:
                    await db.rollback()
                except Exception:
                    pass
    except Exception as _e:
        logger.warning(f"Reset portfolio stop failed for {_runtime_tenant}:{_runtime_user}: {_e}")

    account = await manager.init_account(
        uid, initial_cash, tenant_id=auth.tenant_id, market=market
    )
    await _capture_simulation_snapshot(redis)
    return {
        "success": True,
        "message": "Simulation account reset",
        "data": account,
        "market": market,
    }


@router.get("/account")
async def get_simulation_account(
    market: str = Query("CN", description="模拟账户市场（CN/HK/US/FUTURES/CRYPTO）"),
    auth: AuthContext = Depends(get_auth_context),
    redis: RedisClient = Depends(get_redis),
):
    """
    Get current simulation account state.
    如果账户不存在，返回空账户（total_asset=0），不自动初始化。
    需用户在个人中心显式重置为 100 万，避免自动重置覆盖手动任务后的持仓。
    """
    manager = SimulationAccountManager(redis)
    uid = _require_user_id(auth.user_id, auth.tenant_id)
    market = market.upper()
    account = await manager.get_account(uid, tenant_id=auth.tenant_id, market=market)
    if not account:
        # 不自动初始化，返回空账户标记，由前端引导用户去个人中心重置
        return {
            "success": True,
            "data": {
                "cash": 0.0,
                "total_asset": 0.0,
                "market_value": 0.0,
                "positions": {},
                "account_not_initialized": True,
            },
            "market": market,
        }

    # 从 settings 中读取 initial_cash 作为 initial_equity
    settings = await manager.get_settings(
        user_id=uid,
        tenant_id=auth.tenant_id,
        default_initial_cash=DEFAULT_INITIAL_CASH,
        cooldown_days=COOLDOWN_DAYS,
    )
    initial_equity = float(settings.get("initial_cash", DEFAULT_INITIAL_CASH))

    # 补算盈亏字段（手续费已从现金扣减，天然计入盈亏）：
    # 总盈亏 = 总资产 - 初始资金；今日/本月盈亏基于日快照基线推导。
    total_asset = float(account.get("total_asset") or 0.0)
    total_pnl = total_asset - initial_equity
    baselines = await SimulationFundSnapshotService.get_baselines(
        tenant_id=auth.tenant_id,
        user_id=str(uid),
        initial_capital=Decimal(str(initial_equity)),
    )
    day_open_equity = float(baselines["day_open_equity"])
    month_open_equity = float(baselines["month_open_equity"])
    today_pnl = total_asset - day_open_equity
    monthly_pnl = total_asset - month_open_equity

    account["initial_equity"] = initial_equity
    account["total_pnl"] = total_pnl
    account["today_pnl"] = today_pnl
    account["daily_pnl"] = today_pnl
    account["monthly_pnl"] = monthly_pnl
    # 持仓数量统一口径：只计 volume > 0 的有效持仓（与前端/组合快照一致）
    try:
        _positions = account.get("positions") or {}
        if isinstance(_positions, dict):
            account["position_count"] = sum(
                1
                for _pos in _positions.values()
                if isinstance(_pos, dict) and float(_pos.get("volume") or 0) > 0
            )
        elif isinstance(_positions, list):
            account["position_count"] = len(_positions)
    except Exception:
        pass
    account["total_return_ratio"] = (total_pnl / initial_equity) if initial_equity > 0 else 0.0
    # 日收益率（今日实时锚点，智能图表每日收益率用）
    account["daily_return_ratio"] = (today_pnl / day_open_equity) if day_open_equity > 0 else 0.0
    account["baseline"] = {
        "initial_equity": initial_equity,
        "day_open_equity": day_open_equity,
        "month_open_equity": month_open_equity,
    }

    return {"success": True, "data": account}


@router.post("/snapshots/capture")
async def capture_simulation_fund_snapshot(
    auth: AuthContext = Depends(get_auth_context),
    redis: RedisClient = Depends(get_redis),
):
    """手动触发一次模拟账户资金快照采集（按天 upsert）。"""
    result = await SimulationFundSnapshotService.capture_all(redis)
    return {
        "success": True,
        "message": "simulation fund snapshot captured",
        "data": {
            "upserted_rows": result.upserted_rows,
            "scanned_accounts": result.scanned_accounts,
            "requested_by": str(auth.user_id),
        },
    }


@router.get("/snapshots/daily", response_model=list[SimulationFundSnapshotResponse])
async def list_simulation_fund_snapshots(
    days: int = Query(default=30, ge=1, le=3650),
    auth: AuthContext = Depends(get_auth_context),
):
    """查询当前用户的模拟盘日级资金快照历史。"""
    snapshots = await SimulationFundSnapshotService.list_user_daily(
        tenant_id=auth.tenant_id,
        user_id=str(auth.user_id),
        days=days,
    )
    return [
        SimulationFundSnapshotResponse(
            snapshot_date=s.snapshot_date,
            total_asset=s.total_asset,
            available_balance=s.available_balance,
            frozen_balance=s.frozen_balance,
            market_value=s.market_value,
            initial_capital=s.initial_capital,
            total_pnl=s.total_pnl,
            today_pnl=s.today_pnl,
            source=s.source,
        )
        for s in snapshots
    ]
@router.post("/sync/ocr")
async def ocr_sync_holdings(
    images: list[UploadFile] = File(...),
    auth: AuthContext = Depends(get_auth_context),
    redis: RedisClient = Depends(get_redis),
):
    user_api_key = await _resolve_user_api_key(str(auth.user_id))
    ocr_service = SimulationOCRService(api_key=user_api_key)
    image_data = []
    for img in images:
        content = await img.read()
        image_data.append(content)

    ocr_result = await ocr_service.analyze_images(image_data)
    recognized_items = ocr_result if isinstance(ocr_result, list) else (ocr_result.get("holdings", []) if isinstance(ocr_result, dict) else [])
    available_cash = ocr_result.get("available_cash") if isinstance(ocr_result, dict) else None

    # 4. 后处理：纠偏代码 & 统一价格口径（OCR 识别价优先，后端仅兜底）
    results = []

    for item in recognized_items:
        original_symbol = item.get("symbol")
        name = item.get("name")

        # 优先使用 OCR 识别出的代码（已在服务层通过 stocks_index.json 对齐）
        symbol = original_symbol if original_symbol else await _resolve_symbol_by_name(name)

        if not symbol:
            logger.warning(f"Skipping holding {name}: Symbol could not be resolved.")
            continue

        raw_price = item.get("current_price")
        try:
            current_price = float(raw_price or 0)
        except (TypeError, ValueError):
            current_price = 0.0

        # OCR 未识别到有效价格时，才用后端价格源兜底
        if current_price <= 0:
            current_price = await _get_latest_close_from_quantdb(symbol)

        results.append({
            **item,
            "symbol": symbol,
            "current_price": current_price,
            "market_value": round(current_price * item["quantity"], 2)
        })

    return {
        "success": True,
        "data": results,
        "available_cash": available_cash
    }


@router.post("/sync/confirm")
async def confirm_holding_sync(
    request: SyncHoldingsRequest,
    auth: AuthContext = Depends(get_auth_context),
    redis: RedisClient = Depends(get_redis),
):
    """
    确认同步 OCR 识别的持仓。
    逻辑：根据识别出的股票和数量，拉取当前最新市价，并重新计算账户初始金额，使同步后的盈亏对齐。
    """
    manager = SimulationAccountManager(redis)
    uid = _require_user_id(auth.user_id, auth.tenant_id)

    # OCR 同步即“重新对齐起点”：先清旧成交/快照/新台账，避免旧基线导致
    # today_pnl 脉冲、历史曲线串基线（与 reset 同口径）。
    try:
        from sqlalchemy import text as _text
        from backend.shared.database_manager_v2 import get_session as _get_session
        _uid_variants = {str(uid), str(auth.user_id)}
        async with _get_session() as _session:
            await _session.execute(_text("DELETE FROM sim_trades WHERE tenant_id=:tid AND user_id=:uid"), {"tid": auth.tenant_id, "uid": uid})
            await _session.execute(_text("DELETE FROM sim_orders WHERE tenant_id=:tid AND user_id=:uid"), {"tid": auth.tenant_id, "uid": uid})
            for _uv in _uid_variants:
                await _session.execute(_text("DELETE FROM sim_trades WHERE tenant_id=:tid AND cast(user_id as varchar)=:uid_str"), {"tid": auth.tenant_id, "uid_str": _uv})
                await _session.execute(_text("DELETE FROM sim_orders WHERE tenant_id=:tid AND cast(user_id as varchar)=:uid_str"), {"tid": auth.tenant_id, "uid_str": _uv})
                await _session.execute(_text("DELETE FROM simulation_fund_snapshots WHERE tenant_id=:tid AND user_id=:uid2"), {"tid": auth.tenant_id, "uid2": _uv})
                for _table in (
                    "simulation_accounts",
                    "simulation_position_lots",
                    "simulation_cash_ledger",
                    "simulation_account_daily",
                    "simulation_position_daily",
                    "simulation_fills",
                    "simulation_orders",
                ):
                    try:
                        async with _session.begin_nested():
                            await _session.execute(
                                _text(f"DELETE FROM {_table} WHERE tenant_id=:tid AND user_id=:uid2"),
                                {"tid": auth.tenant_id, "uid2": _uv},
                            )
                    except Exception:
                        continue
            await _session.commit()
    except Exception as _e:
        logger.warning(f"OCR sync DB cleanup failed for {auth.tenant_id}:{uid}: {_e}")

    # 1. 预先获取所有股票的最新价格并计算总市值
    sync_positions = []
    total_market_value = 0.0

    for item in request.holdings:
        # 与预览一致：优先使用前端回传的 OCR 识别价，仅在缺失时兜底
        try:
            price = float(item.current_price or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            price = await _get_latest_close_from_quantdb(item.symbol)

        if price <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"无法获取股票 {item.symbol} 的实时价格或历史收盘价，同步中止。请检查代码是否正确。"
            )

        total_market_value += price * item.quantity
        sync_positions.append({
            "symbol": item.symbol,
            "quantity": item.quantity,
            "price": price
        })

    # 2. 计算同步后的”初始总资产”
    # 逻辑：我们将”可用现金”优先使用截图识别到的数值，如果没有则使用默认基数
    sync_cash = request.available_cash if request.available_cash is not None else 100.0
    calculated_initial_cash = total_market_value + sync_cash

    # 3. 更新 settings 中的 initial_cash（用于前端显示初始权益）
    await manager.set_initial_cash(uid, calculated_initial_cash, auth.tenant_id)

    # 4. 初始化账户 (重置现金为 calculated_initial_cash)
    await manager.init_account(uid, calculated_initial_cash, auth.tenant_id)

    # 5. 写入持仓 (通过 update_balance 扣除现金，从而使总资产保持不变，盈亏从 0 开始)
    for pos in sync_positions:
        # delta_cash = -(数量 * 现价)，这样操作后：
        # 现金减少，市值增加，总资产 = calculated_initial_cash 保持不变
        await manager.update_balance(
            user_id=uid,
            tenant_id=auth.tenant_id,
            symbol=pos["symbol"],
            delta_cash=-(pos["quantity"] * pos["price"]),
            delta_volume=pos["quantity"],
            price=pos["price"]
        )

    # 5. 捕获快照
    await _capture_simulation_snapshot(redis)

    return {"success": True, "message": f"持仓同步成功，初始资产已对齐至 {calculated_initial_cash:,.2f}"}
