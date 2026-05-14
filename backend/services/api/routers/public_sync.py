from fastapi import APIRouter, Query, HTTPException
from sqlalchemy import text
from typing import List, Any, Optional
from datetime import date
from backend.shared.database_manager_v2 import get_session
import os
from sqlalchemy.ext.asyncio import create_async_engine

router = APIRouter(prefix="/public/sync", tags=["Public Data Sync"])

# 远程数据源（包含 152 维原始特征和行情）
REMOTE_DB_URL = "postgresql://readonly_sync:qm_sync_2026_readonly@139.199.75.121:5432/quantmind"

@router.get("/stock-daily")
async def sync_processed_data(
    trade_date: date = Query(..., description="Start date (YYYY-MM-DD)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(1000, ge=1, le=5000)
):
    """
    [88维] 拉取本地已校准的全指标数据 (stock_daily_latest)
    包含行情、基本面、技术指标和资金流向。
    """
    offset = (page - 1) * page_size
    sql = "SELECT * FROM stock_daily_latest WHERE trade_date >= :t_date ORDER BY trade_date, symbol LIMIT :limit OFFSET :offset"
    
    async with get_session(read_only=True) as session:
        result = await session.execute(text(sql), {"t_date": trade_date, "limit": page_size, "offset": offset})
        rows = [dict(r._mapping) for r in result]
        return {"code": 200, "data": rows}

@router.get("/feature-snapshots")
async def sync_feature_data(
    trade_date: date = Query(..., description="Start date (YYYY-MM-DD)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(500, ge=1, le=2000)
):
    """
    [152维] 拉取远程深度 AI 特征数据 (feature_snapshots)
    由于列数较多，建议单页大小控制在 2000 以内。
    """
    engine = create_async_engine(REMOTE_DB_URL.replace("postgresql://", "postgresql+asyncpg://"))
    offset = (page - 1) * page_size
    
    sql = "SELECT * FROM feature_snapshots WHERE trade_date >= :t_date ORDER BY trade_date, symbol LIMIT :limit OFFSET :offset"
    
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(sql), {"t_date": trade_date, "limit": page_size, "offset": offset})
            rows = [dict(r._mapping) for r in result]
            return {"code": 200, "data": rows}
    finally:
        await engine.dispose()

@router.get("/calendar")
async def sync_calendar(
    start_date: date = Query(..., description="Start date"),
    end_date: Optional[date] = None
):
    """
    同步交易日历
    """
    sql = "SELECT * FROM qm_market_calendar_day WHERE day >= :s_date"
    params = {"s_date": start_date}
    if end_date:
        sql += " AND day <= :e_date"
        params["e_date"] = end_date
    sql += " ORDER BY day ASC"
    
    async with get_session(read_only=True) as session:
        result = await session.execute(text(sql), params)
        return {"code": 200, "data": [dict(r._mapping) for r in result]}
