from datetime import date
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status, File, UploadFile
from pydantic import BaseModel

from backend.services.trade.deps import AuthContext, get_auth_context, get_redis
from backend.services.trade.redis_client import RedisClient
from backend.services.trade.simulation.services.fund_snapshot_service import (
    SimulationFundSnapshotService,
)
from backend.services.trade.simulation.services.simulation_manager import (
    SimulationAccountManager,
)
from backend.services.trade.simulation.services.ocr_service import SimulationOCRService
from backend.services.trade.trade_config import settings
from backend.shared.database_manager_v2 import get_db_manager
from backend.shared.stock_utils import StockCodeUtil
import logging
import httpx
from sqlalchemy import text

logger = logging.getLogger(__name__)

router = APIRouter()


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

    # Level 2: 数据库兜底
    if price <= 0:
        try:
            db_manager = get_db_manager()
            # 数据库 stock_daily_latest 使用 Prefix 格式 (SH600191)
            query = text("""
                SELECT close, adj_factor FROM stock_daily_latest 
                WHERE symbol = :symbol 
                ORDER BY trade_date DESC LIMIT 1
            """)
            
            async with db_manager.get_master_session() as session:
                result = await session.execute(query, {"symbol": symbol})
                row = result.fetchone()
                if row:
                    hfq_close = float(row[0])
                    adj_factor = float(row[1] or 1.0)
                    # 计算名义价格 (除权价)
                    price = hfq_close / adj_factor if adj_factor > 0 else hfq_close
                    logger.info(f"Fallback to DB nominal price for {symbol}: {price} (Hfq: {hfq_close}, Adj: {adj_factor})")
        except Exception as e:
            logger.error(f"Database fallback failed for {symbol}: {e}")
            
    return price


async def _resolve_symbol_by_name(name: str) -> Optional[str]:
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


DEFAULT_INITIAL_CASH = 1_000_000.0
SIM_AMOUNT_STEP = 100_000
COOLDOWN_DAYS = 30


class AccountResetRequest(BaseModel):
    initial_cash: float | None = None


class HoldingItem(BaseModel):
    symbol: str
    quantity: float
    name: Optional[str] = None


class SyncHoldingsRequest(BaseModel):
    holdings: List[HoldingItem]
    available_cash: Optional[float] = None


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
    data = await manager.get_settings(
        user_id=auth.user_id,
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
):
    """
    Reset simulation account with initial cash.
    """
    manager = SimulationAccountManager(redis)
    if request.initial_cash is None:
        settings = await manager.get_settings(
            user_id=auth.user_id,
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
        await manager.set_initial_cash(auth.user_id, initial_cash, tenant_id=auth.tenant_id)

    account = await manager.init_account(auth.user_id, initial_cash, tenant_id=auth.tenant_id)
    await _capture_simulation_snapshot(redis)
    return {"success": True, "message": "Simulation account reset", "data": account}


@router.get("/account")
async def get_simulation_account(
    auth: AuthContext = Depends(get_auth_context),
    redis: RedisClient = Depends(get_redis),
):
    """
    Get current simulation account state.
    如果账户不存在，返回空账户（total_asset=0），不自动初始化。
    """
    manager = SimulationAccountManager(redis)
    account = await manager.get_account(auth.user_id, tenant_id=auth.tenant_id)
    if not account:
        # 不再自动初始化，返回空账户标记
        return {
            "success": True,
            "data": {
                "cash": 0.0,
                "total_asset": 0.0,
                "market_value": 0.0,
                "positions": {},
                "account_not_initialized": True,
            }
        }

    # 从 settings 中读取 initial_cash 作为 initial_equity
    settings = await manager.get_settings(
        user_id=auth.user_id,
        tenant_id=auth.tenant_id,
        default_initial_cash=DEFAULT_INITIAL_CASH,
        cooldown_days=COOLDOWN_DAYS,
    )
    initial_equity = float(settings.get("initial_cash", DEFAULT_INITIAL_CASH))

    # 添加 initial_equity 和 baseline 字段
    account["initial_equity"] = initial_equity
    account["baseline"] = {
        "initial_equity": initial_equity,
        "day_open_equity": initial_equity,  # 简化处理，日开盘权益也用初始权益
        "month_open_equity": initial_equity,
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
    images: List[UploadFile] = File(...),
    auth: AuthContext = Depends(get_auth_context),
    redis: RedisClient = Depends(get_redis),
):
    ocr_service = SimulationOCRService()
    image_data = []
    for img in images:
        content = await img.read()
        image_data.append(content)
        
    ocr_result = await ocr_service.analyze_images(image_data)
    recognized_items = ocr_result.get("holdings", [])
    available_cash = ocr_result.get("available_cash")
    
    # 4. 后处理：纠偏代码 & 获取实时行情
    results = []
    
    for item in recognized_items:
        original_symbol = item.get("symbol")
        name = item.get("name")
        
        # 优先使用 OCR 识别出的代码（已在服务层通过 stocks_index.json 对齐）
        symbol = original_symbol if original_symbol else await _resolve_symbol_by_name(name)
        
        if not symbol:
            logger.warning(f"Skipping holding {name}: Symbol could not be resolved.")
            continue
            
        current_price = await _get_latest_price(symbol)
            
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
    market_url = settings.MARKET_DATA_SERVICE_URL.rstrip("/")
    
    # 1. 预先获取所有股票的最新价格并计算总市值
    sync_positions = []
    total_market_value = 0.0
    
    for item in request.holdings:
        price = await _get_latest_price(item.symbol)
        
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
    await manager.set_initial_cash(auth.user_id, calculated_initial_cash, auth.tenant_id)

    # 4. 初始化账户 (重置现金为 calculated_initial_cash)
    await manager.init_account(auth.user_id, calculated_initial_cash, auth.tenant_id)

    # 5. 写入持仓 (通过 update_balance 扣除现金，从而使总资产保持不变，盈亏从 0 开始)
    for pos in sync_positions:
        # delta_cash = -(数量 * 现价)，这样操作后：
        # 现金减少，市值增加，总资产 = calculated_initial_cash 保持不变
        await manager.update_balance(
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
            symbol=pos["symbol"],
            delta_cash=-(pos["quantity"] * pos["price"]),
            delta_volume=pos["quantity"],
            price=pos["price"]
        )
            
    # 5. 捕获快照
    await _capture_simulation_snapshot(redis)
    
    return {"success": True, "message": f"持仓同步成功，初始资产已对齐至 {calculated_initial_cash:,.2f}"}
