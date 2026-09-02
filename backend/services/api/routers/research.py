"""投研平台聚合接口。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import backend.services.api.routers.research_service as _research_service
from backend.services.api.routers.research_schemas import (
    BatchFeaturesRequest,
    PoolAddRequest,
    SingleStockPredictionRequest,
    SymbolsFeaturesRequest,
    WatchlistAddRequest,
)
from backend.services.api.routers.research_features_service import (
    get_batch_full_features as get_batch_full_features_service,
)
from backend.services.api.routers.research_service import (
    add_to_research_pool as add_to_research_pool_service,
    add_to_watchlist as add_to_watchlist_service,
    get_available_models as get_available_models_service,
    get_inference_runs as get_inference_runs_service,
    get_research_overview as get_research_overview_service,
    get_research_universe as get_research_universe_service,
    get_research_universe_by_date as get_research_universe_by_date_service,
    get_stock_kline as get_stock_kline_service,
    get_symbols_features as get_symbols_features_service,
    get_user_research_pool as get_user_research_pool_service,
    get_user_watchlist as get_user_watchlist_service,
    predict_single_stock as predict_single_stock_service,
    remove_from_research_pool as remove_from_research_pool_service,
    remove_from_watchlist as remove_from_watchlist_service,
    sync_watchlist_positions_service,
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


_HK_STOCK_NAMES_CACHE: dict[str, tuple[float, dict[str, str]]] = {}


@router.get("/hk/stock-detail")
async def get_hk_stock_detail(
    symbol: str = Query(..., description="港股代码，如 0700.HK 或 0700"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """港股个股详情聚合：CCASS 席位 / 南向持股 / 估值 / 分红 / 财务 / 分析师。"""
    from backend.services.api.market_analysis_hk import quanthk_feed as _hk_feed

    return await asyncio.to_thread(_hk_feed.get_stock_detail, symbol)


@router.get("/stock-names")
async def get_stock_names(
    market: str = Query("CN", description="CN 或 HK；CN 走 instrument 表，HK 走 quanthk security_master"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """返回该市场全量证券代码→中文名映射（前端推理排名/联想展示用，约 2800 条）。

    HK: quanthk/security_master（2807 只全市场）；CN: instrument 简表兼容。
    """
    import time as _time

    _ = current_user
    market = market.upper()
    cache = _HK_STOCK_NAMES_CACHE.get(market)
    if cache and _time.time() - cache[0] < 1800:
        return {"market": market, "names": cache[1], "cached": True}

    names: dict[str, str] = {}
    if market == "HK":
        from backend.services.engine.data_platform.quanthk_hub import _resolve_quanthk_data_dir

        qdir = _resolve_quanthk_data_dir()
        import duckdb

        con = duckdb.connect()
        try:
            rows = con.execute(
                "SELECT symbol, cn_name FROM read_parquet("
                f"'{qdir}/2_base_sector/security_master/data.parquet')"
            ).fetchall()
            names = {
                str(sym): str(cn) for sym, cn in rows if cn and str(cn).strip()
            }
        finally:
            con.close()
    else:
        async with get_session(read_only=True) as session:
            from sqlalchemy import text as _text

            rows = (
                await session.execute(
                    _text("SELECT symbol, name FROM instrument LIMIT 6000")
                )
            ).fetchall()
            names = {str(r[0]): str(r[1]) for r in rows if r[1]}
    _HK_STOCK_NAMES_CACHE[market] = (_time.time(), names)
    return {"market": market, "names": names, "cached": False}


@router.get("/models")
async def get_available_models(
    market: str | None = Query(None),
    current_user: dict = Depends(get_current_user),
):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await get_available_models_service(tid, uid, market)


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
    run_id: str | None = Query(None),
    model_id: str | None = Query(None),
    date: str | None = Query(None, description="数据日 T（pred.parquet 口径），与 model_id 搭配直读全市场分数"),
    limit: int = Query(2000),
    offset: int = Query(0),
    current_user: dict = Depends(get_current_user),
):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    if model_id and date:
        return await get_research_universe_by_date_service(tid, uid, model_id, date, limit, offset)
    if not run_id:
        raise HTTPException(status_code=400, detail="run_id 或 model_id+date 必填")
    return await get_research_universe_service(tid, uid, run_id, limit, offset)


@router.get("/watchlist")
async def get_user_watchlist(
    limit: int = Query(50),
    offset: int = Query(0),
    current_user: dict = Depends(get_current_user),
):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await get_user_watchlist_service(tid, uid, limit, offset)


@router.post("/watchlist/sync-positions")
async def sync_watchlist_positions(request: Request, current_user: dict = Depends(get_current_user)):
    """模拟盘持仓自动加入自选。放 {symbol} 路由之前，避免被当作 symbol 捕获。"""
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    return await sync_watchlist_positions_service(
        str(current_user["tenant_id"]), str(current_user["user_id"]), auth
    )


@router.post("/watchlist/{symbol}")
async def add_to_watchlist(symbol: str, req: WatchlistAddRequest, current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await add_to_watchlist_service(tid, uid, symbol, req.run_id, req.stock_name, req.features_snapshot)


@router.delete("/watchlist/{symbol}")
async def remove_from_watchlist(symbol: str, current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await remove_from_watchlist_service(tid, uid, symbol)


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


@router.delete("/pool/{symbol}")
async def remove_from_research_pool(symbol: str, current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await remove_from_research_pool_service(tid, uid, symbol)


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


@router.post("/batch-features")
async def get_batch_features(
    req: BatchFeaturesRequest,
    current_user: dict = Depends(get_current_user),
):
    """批量 QuantDB 特征投影：按 fields 返回指定字段（按需加载）。"""
    _ = current_user
    return await get_batch_full_features_service(req.symbols, req.fields, req.trade_date)


@router.post("/predict-stock")
async def predict_single_stock(
    req: SingleStockPredictionRequest,
    current_user: dict = Depends(get_current_user),
):
    """个股未来预测与 10%-50%-90% 置信区间分位数推理。"""
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await predict_single_stock_service(
        tid,
        uid,
        symbol=req.symbol,
        model_id=req.model_id,
        target_date=req.date,
        horizon=req.horizon,
        market=req.market,
        consensus_model_ids=req.consensus_model_ids,
        execute=req.execute,
    )
