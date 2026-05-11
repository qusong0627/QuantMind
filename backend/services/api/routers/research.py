"""投研平台聚合接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

import backend.services.api.routers.research_service as _research_service
from backend.services.api.routers.research_schemas import (
    PoolAddRequest,
    SymbolsFeaturesRequest,
    WatchlistAddRequest,
)
from backend.services.api.routers.research_service import (
    add_to_research_pool as add_to_research_pool_service,
    add_to_watchlist as add_to_watchlist_service,
    get_available_models as get_available_models_service,
    get_inference_runs as get_inference_runs_service,
    get_research_overview as get_research_overview_service,
    get_research_universe as get_research_universe_service,
    get_stock_kline as get_stock_kline_service,
    get_symbols_features as get_symbols_features_service,
    get_user_research_pool as get_user_research_pool_service,
    get_user_watchlist as get_user_watchlist_service,
)
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.database_manager_v2 import get_session

router = APIRouter(prefix="/api/v1/research", tags=["Research"])

# 向后兼容：保留测试与历史调用使用的私有符号
_format_candidate_record = _research_service._format_candidate_record  # noqa: SLF001


async def _do_get_overview(  # noqa: SLF001
    tid: str, uid: str, model_id: str | None, run_id: str | None, limit: int, offset: int
):
    original_get_session = _research_service.get_session
    _research_service.get_session = get_session
    try:
        return await _research_service._do_get_overview(tid, uid, model_id, run_id, limit, offset)  # noqa: SLF001
    finally:
        _research_service.get_session = original_get_session


@router.get("/models")
async def get_available_models(current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await get_available_models_service(tid, uid)


@router.get("/runs")
async def get_inference_runs(model_id: str, current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await get_inference_runs_service(tid, uid, model_id)


@router.get("/overview")
async def get_research_overview(
    model_id: str | None = Query(None),
    run_id: str | None = Query(None),
    limit: int = Query(50),
    offset: int = Query(0),
    current_user: dict = Depends(get_current_user),
):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await get_research_overview_service(tid, uid, model_id, run_id, limit, offset)


@router.get("/universe")
async def get_research_universe(
    run_id: str,
    limit: int = Query(2000),
    offset: int = Query(0),
    current_user: dict = Depends(get_current_user),
):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await get_research_universe_service(tid, uid, run_id, limit, offset)


@router.get("/watchlist")
async def get_user_watchlist(
    limit: int = Query(50),
    offset: int = Query(0),
    current_user: dict = Depends(get_current_user),
):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await get_user_watchlist_service(tid, uid, limit, offset)


@router.post("/watchlist/{symbol}")
async def add_to_watchlist(symbol: str, req: WatchlistAddRequest, current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await add_to_watchlist_service(tid, uid, symbol, req.run_id, req.stock_name, req.features_snapshot)


@router.get("/pool")
async def get_user_research_pool(
    status: str | None = Query(None),
    limit: int = Query(50),
    offset: int = Query(0),
    current_user: dict = Depends(get_current_user),
):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await get_user_research_pool_service(tid, uid, status, limit, offset)


@router.post("/pool/{symbol}")
async def add_to_research_pool(symbol: str, req: PoolAddRequest, current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await add_to_research_pool_service(
        tid,
        uid,
        symbol,
        req.run_id,
        req.stock_name,
        req.model_id,
        req.fusion_score,
        req.thesis_summary,
        req.features_snapshot,
    )


@router.post("/symbols/features")
async def get_symbols_features(
    req: SymbolsFeaturesRequest,
    lite: bool = Query(False, description="轻量模式：仅查询 stock_daily_latest 最新交易日核心字段"),
    current_user: dict = Depends(get_current_user),
):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await get_symbols_features_service(tid, uid, req.symbols, lite)


@router.get("/kline/{symbol}")
async def get_stock_kline(symbol: str, days: int = Query(60), current_user: dict = Depends(get_current_user)):
    _ = current_user
    return await get_stock_kline_service(symbol, days)
