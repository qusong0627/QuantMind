from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/engine", tags=["Realtime Contract"])


class FeatureReadyRequest(BaseModel):
    tenant_id: str = "default"
    user_id: str
    trade_date: date
    model_name: str = ""
    model_version: str
    feature_version: str
    feature_dim: int = Field(..., ge=1)
    window_start: datetime | None = None
    window_end: datetime | None = None
    expected_symbols: int = 0
    ready_symbols: int = 0
    missing_symbols: int = 0
    source: str = "l2_batch"
    checksum: str | None = None
    quality: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None


class SignalScoreItem(BaseModel):
    symbol: str
    light_score: float | None = None
    tft_score: float | None = None
    fusion_score: float
    risk_weight: float | None = 1.0
    # §6.4（P3）：缺省 None = 由写侧填当日 qm_regime_daily 生效值（此前默认 "normal" 是撒谎）
    regime: str | None = None
    score_rank: int | None = None
    universe_tag: str | None = None
    signal_side: Literal["BUY", "SELL", "HOLD"] | None = None
    expected_price: float | None = None
    quality: dict[str, Any] = Field(default_factory=dict)
    # T-P1-01 Signal 契约列：market/rank_pct 可显式给；缺省时后者按本批 fusion_score 现算
    market: str | None = None
    rank_pct: float | None = None


class SignalReadyRequest(BaseModel):
    tenant_id: str = "default"
    user_id: str
    trade_date: date
    model_version: str
    feature_version: str
    scores: list[SignalScoreItem] = Field(default_factory=list)


class DispatchStageRequest(BaseModel):
    run_id: str
    tenant_id: str = "default"
    user_id: str
    trade_date: date
    strategy_id: str | None = None
    trading_mode: Literal["REAL", "SHADOW", "SIMULATION"] = "REAL"
    stage: Literal[
        "signal_ready",
        "dispatched",
        "runner_applied",
        "order_sent",
        "fill_confirmed",
        "failed",
    ]
    total_signals: int = 0
    dispatched_signals: int = 0
    acked_signals: int = 0
    order_submitted_count: int = 0
    order_filled_count: int = 0
    failed_count: int = 0
    trace_id: str | None = None
    last_error: str | None = None


class DispatchItemUpsert(BaseModel):
    run_id: str
    signal_id: str | None = None
    client_order_id: str
    tenant_id: str = "default"
    user_id: str
    trade_date: date
    symbol: str
    action: Literal["BUY", "SELL", "HOLD"]
    quantity: float
    price: float | None = None
    score: float | None = None
    dispatch_status: Literal[
        "pending",
        "dispatched",
        "acked",
        "order_submitted",
        "order_filled",
        "rejected",
        "failed",
    ] = "pending"
    order_id: str | None = None
    exchange_order_id: str | None = None
    exchange_trade_id: str | None = None
    exec_message: str | None = None


class DispatchItemsUpsertRequest(BaseModel):
    run_id: str
    tenant_id: str = "default"
    user_id: str
    trade_date: date
    items: list[DispatchItemUpsert]


@router.get("/realtime/infer/status")
async def realtime_inference_status():
    """热集实时推理服务状态（T-P6-08）：配置/计数器/最近错误（只读）。"""
    from backend.services.engine.inference.realtime_service import default_service

    return {"ok": True, "data": default_service().status()}


@router.get("/realtime/regime/status")
async def realtime_regime_status():
    """日内市场状态服务状态（T-P6-13）：配置/计数器/最近状态（只读）。"""
    from backend.services.engine.realtime_regime import default_service

    return {"ok": True, "data": default_service().status()}


@router.get("/realtime/anomaly/status")
async def anomaly_engine_status():
    """识别引擎状态（T-P6-14）：配置/计数器/近 1h 异动标的（只读）。"""
    from backend.services.engine.anomaly_engine import default_service

    return {"ok": True, "data": default_service().status()}


@router.get("/realtime/news-intel/status")
async def news_intel_status():
    """新闻情报服务状态（T-P6-12）：配置/计数器/游标（只读）。"""
    from backend.services.engine.news_intel_engine import default_service

    return {"ok": True, "data": default_service().status()}


@router.post("/runs/{run_id}/feature-ready")
async def mark_feature_ready(run_id: str, payload: FeatureReadyRequest):
    sql = text("""
        INSERT INTO engine_feature_runs (
            run_id, tenant_id, user_id, trade_date, model_name, model_version,
            feature_version, feature_dim, window_start, window_end,
            status, expected_symbols, ready_symbols, missing_symbols,
            source, checksum, quality, error_message, created_at, updated_at
        ) VALUES (
            :run_id, :tenant_id, :user_id, :trade_date, :model_name, :model_version,
            :feature_version, :feature_dim, :window_start, :window_end,
            'feature_ready', :expected_symbols, :ready_symbols, :missing_symbols,
            :source, :checksum, CAST(:quality AS jsonb), :error_message, NOW(), NOW()
        )
        ON CONFLICT (run_id)
        DO UPDATE SET
            tenant_id = EXCLUDED.tenant_id,
            user_id = EXCLUDED.user_id,
            trade_date = EXCLUDED.trade_date,
            model_name = EXCLUDED.model_name,
            model_version = EXCLUDED.model_version,
            feature_version = EXCLUDED.feature_version,
            feature_dim = EXCLUDED.feature_dim,
            window_start = EXCLUDED.window_start,
            window_end = EXCLUDED.window_end,
            status = 'feature_ready',
            expected_symbols = EXCLUDED.expected_symbols,
            ready_symbols = EXCLUDED.ready_symbols,
            missing_symbols = EXCLUDED.missing_symbols,
            source = EXCLUDED.source,
            checksum = EXCLUDED.checksum,
            quality = EXCLUDED.quality,
            error_message = EXCLUDED.error_message,
            updated_at = NOW()
        """)
    params = {
        "run_id": run_id,
        # python 模式 dump：date/datetime 保持对象（asyncpg 原生适配）——
        # json 模式会转 ISO 字符串，asyncpg 传 date 列直接 DataError（2026-09-17 E2E 实测）
        **payload.model_dump(mode="python"),
        "quality": json.dumps(payload.quality or {}, ensure_ascii=False),
    }
    async with get_session(read_only=False) as db:
        await db.execute(sql, params)
    return {"ok": True, "run_id": run_id, "stage": "feature_ready"}


@router.post("/runs/{run_id}/signal-ready")
async def mark_signal_ready(run_id: str, payload: SignalReadyRequest):
    if not payload.scores:
        raise HTTPException(status_code=400, detail="scores 不能为空")

    upsert_run_sql = text("""
        UPDATE engine_feature_runs
        SET status = 'signal_ready',
            updated_at = NOW()
        WHERE run_id = :run_id
        """)
    insert_score_sql = text("""
        INSERT INTO engine_signal_scores (
            run_id, tenant_id, user_id, trade_date, symbol,
            model_version, feature_version,
            light_score, tft_score, fusion_score, risk_weight, regime, score_rank,
            universe_tag, signal_side, expected_price, quality, created_at,
            market, rank_pct, source, signal_ts
        ) VALUES (
            :run_id, :tenant_id, :user_id, :trade_date, :symbol,
            :model_version, :feature_version,
            :light_score, :tft_score, :fusion_score, :risk_weight, :regime, :score_rank,
            :universe_tag, :signal_side, :expected_price, CAST(:quality AS jsonb), NOW(),
            :market, :rank_pct, :source, NOW()
        )
        ON CONFLICT (
            tenant_id, user_id, trade_date, symbol, model_version, feature_version, run_id
        )
        DO UPDATE SET
            light_score = EXCLUDED.light_score,
            tft_score = EXCLUDED.tft_score,
            fusion_score = EXCLUDED.fusion_score,
            risk_weight = EXCLUDED.risk_weight,
            regime = EXCLUDED.regime,
            score_rank = EXCLUDED.score_rank,
            universe_tag = EXCLUDED.universe_tag,
            signal_side = EXCLUDED.signal_side,
            expected_price = EXCLUDED.expected_price,
            quality = EXCLUDED.quality,
            market = EXCLUDED.market,
            rank_pct = EXCLUDED.rank_pct,
            source = EXCLUDED.source,
            signal_ts = EXCLUDED.signal_ts
        """)

    from backend.shared.signal_contract import (
        SOURCE_REALTIME,
        compute_rank_pct,
        ensure_signal_contract_columns_async,
        normalize_market,
    )

    # 身份归一唯一入口（审计 M2）：symbol 一律后缀身份（600036.SH）——000001.SH（指数）
    # 与 000001.SZ（平安银行）在上方冲突键里必须是两行，绝不被折叠合并。
    from backend.services.engine.inference.realtime_core import identity

    rank_pcts = compute_rank_pct([item.fusion_score for item in payload.scores])

    # §6.4（P3）：regime 缺省填当日生效值（qm_regime_daily）；表外市场/缺行 → NULL。
    # 按 (market, trade_date) 缓存——同批各标的 market 通常一致，只读一次库。
    from backend.shared.market_regime import REGIME_INDEX_BY_MARKET
    from backend.shared.regime_daily_store import load_day_state_async

    regime_cache: dict[tuple[str, date], str | None] = {}

    # T-P1-01：契约列自愈（独立会话，进程内一次；不混入下方业务事务）
    await ensure_signal_contract_columns_async()

    async with get_session(read_only=False) as db:
        run_ret = await db.execute(upsert_run_sql, {"run_id": run_id})
        if int(run_ret.rowcount or 0) <= 0:
            raise HTTPException(status_code=404, detail=f"run_id 不存在: {run_id}")

        for idx, item in enumerate(payload.scores):
            item_market = normalize_market(item.market or item.universe_tag)
            regime_value = item.regime
            if regime_value is None and item_market in REGIME_INDEX_BY_MARKET:
                cache_key = (item_market, payload.trade_date)
                if cache_key not in regime_cache:
                    try:
                        regime_cache[cache_key] = await load_day_state_async(
                            db, item_market, payload.trade_date
                        )
                    except Exception as regime_err:  # noqa: BLE001 — 读失败写 NULL，不阻断
                        logger.warning(
                            "[RealtimeContract] regime 生效值读取失败（regime 写 NULL）: %s",
                            regime_err,
                        )
                        regime_cache[cache_key] = None
                regime_value = regime_cache[cache_key]
            await db.execute(
                insert_score_sql,
                {
                    "run_id": run_id,
                    "tenant_id": payload.tenant_id,
                    "user_id": payload.user_id,
                    "trade_date": payload.trade_date,  # date 对象（asyncpg 原生；勿转字符串）
                    "model_version": payload.model_version,
                    "feature_version": payload.feature_version,
                    "symbol": identity(item.symbol),
                    "light_score": item.light_score,
                    "tft_score": item.tft_score,
                    "fusion_score": item.fusion_score,
                    "risk_weight": item.risk_weight if item.risk_weight is not None else 1.0,
                    "regime": regime_value,
                    "score_rank": item.score_rank,
                    "universe_tag": item.universe_tag,
                    "signal_side": item.signal_side,
                    "expected_price": item.expected_price,
                    "quality": json.dumps(item.quality or {}, ensure_ascii=False),
                    "market": item_market,
                    "rank_pct": item.rank_pct if item.rank_pct is not None else rank_pcts[idx],
                    "source": SOURCE_REALTIME,
                },
            )

    return {
        "ok": True,
        "run_id": run_id,
        "stage": "signal_ready",
        "upserted_scores": len(payload.scores),
    }


@router.post("/dispatch/{batch_id}/stage")
async def update_dispatch_stage(batch_id: str, payload: DispatchStageRequest):
    sql = text("""
        INSERT INTO engine_dispatch_batches (
            batch_id, run_id, tenant_id, user_id, trade_date, strategy_id, trading_mode,
            stage, stage_updated_at,
            total_signals, dispatched_signals, acked_signals,
            order_submitted_count, order_filled_count, failed_count,
            trace_id, last_error, created_at, updated_at
        ) VALUES (
            :batch_id, :run_id, :tenant_id, :user_id, :trade_date, :strategy_id, :trading_mode,
            :stage, NOW(),
            :total_signals, :dispatched_signals, :acked_signals,
            :order_submitted_count, :order_filled_count, :failed_count,
            :trace_id, :last_error, NOW(), NOW()
        )
        ON CONFLICT (batch_id)
        DO UPDATE SET
            stage = EXCLUDED.stage,
            stage_updated_at = NOW(),
            total_signals = EXCLUDED.total_signals,
            dispatched_signals = EXCLUDED.dispatched_signals,
            acked_signals = EXCLUDED.acked_signals,
            order_submitted_count = EXCLUDED.order_submitted_count,
            order_filled_count = EXCLUDED.order_filled_count,
            failed_count = EXCLUDED.failed_count,
            trace_id = EXCLUDED.trace_id,
            last_error = EXCLUDED.last_error,
            updated_at = NOW()
        """)
    params = {"batch_id": batch_id, **payload.model_dump(mode="python")}
    async with get_session(read_only=False) as db:
        await db.execute(sql, params)
    return {"ok": True, "batch_id": batch_id, "stage": payload.stage}


@router.post("/dispatch/{batch_id}/items/upsert")
async def upsert_dispatch_items(batch_id: str, payload: DispatchItemsUpsertRequest):
    if not payload.items:
        raise HTTPException(status_code=400, detail="items 不能为空")

    sql = text("""
        INSERT INTO engine_dispatch_items (
            batch_id, run_id, signal_id, client_order_id, tenant_id, user_id, trade_date,
            symbol, action, quantity, price, score, dispatch_status, order_id,
            exchange_order_id, exchange_trade_id, exec_message, created_at, updated_at
        ) VALUES (
            :batch_id, :run_id, :signal_id, :client_order_id, :tenant_id, :user_id, :trade_date,
            :symbol, :action, :quantity, :price, :score, :dispatch_status, CAST(:order_id AS uuid),
            :exchange_order_id, :exchange_trade_id, :exec_message, NOW(), NOW()
        )
        ON CONFLICT (client_order_id)
        DO UPDATE SET
            dispatch_status = EXCLUDED.dispatch_status,
            order_id = EXCLUDED.order_id,
            exchange_order_id = EXCLUDED.exchange_order_id,
            exchange_trade_id = EXCLUDED.exchange_trade_id,
            exec_message = EXCLUDED.exec_message,
            updated_at = NOW()
        """)

    upserted = 0
    async with get_session(read_only=False) as db:
        for item in payload.items:
            await db.execute(
                sql,
                {
                    "batch_id": batch_id,
                    "run_id": item.run_id or payload.run_id,
                    "signal_id": item.signal_id,
                    "client_order_id": item.client_order_id,
                    "tenant_id": item.tenant_id or payload.tenant_id,
                    "user_id": item.user_id or payload.user_id,
                    "trade_date": (item.trade_date or payload.trade_date),  # date 对象
                    "symbol": item.symbol.upper().strip(),
                    "action": item.action,
                    "quantity": item.quantity,
                    "price": item.price,
                    "score": item.score,
                    "dispatch_status": item.dispatch_status,
                    "order_id": item.order_id,
                    "exchange_order_id": item.exchange_order_id,
                    "exchange_trade_id": item.exchange_trade_id,
                    "exec_message": item.exec_message,
                },
            )
            upserted += 1
    return {"ok": True, "batch_id": batch_id, "upserted_items": upserted}
