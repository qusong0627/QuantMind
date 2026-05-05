"""投研平台聚合接口。"""

from __future__ import annotations
import json
import math
from datetime import date, datetime
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.database_manager_v2 import get_session
from backend.shared.stock_utils import StockCodeUtil

router = APIRouter(prefix="/api/v1/research", tags=["Research"])
async def ensure_research_tables():
    """确保投研平台相关表存在 - 已禁用自动建表"""
    # 表结构由 quantmind_init.sql 初始化
    pass




class SymbolsFeaturesRequest(BaseModel):
    symbols: list[str]

class WatchlistAddRequest(BaseModel):
    run_id: str | None = None
    stock_name: str | None = None
    features_snapshot: dict[str, Any] | None = None

class PoolAddRequest(BaseModel):
    run_id: str | None = None
    stock_name: str | None = None
    model_id: str | None = None
    fusion_score: float | None = None
    thesis_summary: str | None = None
    features_snapshot: dict[str, Any] | None = None

# SQL 辅助：Join 条件与选择字段 (基于实际数据库列名映射)
_SDL_JOIN_CONDITION = """
(
    sdl.symbol = CASE
        WHEN snap.symbol ~* '^(SH|SZ|BJ)[0-9]{6}$' THEN UPPER(snap.symbol)
        WHEN snap.symbol ~* '^[0-9]{6}\\.(SH|SZ|BJ)$' THEN UPPER(RIGHT(snap.symbol, 2)) || LEFT(snap.symbol, 6)
        WHEN snap.symbol ~ '^[0-9]{6}$' AND LEFT(snap.symbol, 1) IN ('6', '9') THEN 'SH' || snap.symbol
        WHEN snap.symbol ~ '^[0-9]{6}$' AND LEFT(snap.symbol, 1) IN ('4', '8') THEN 'BJ' || snap.symbol
        WHEN snap.symbol ~ '^[0-9]{6}$' THEN 'SZ' || snap.symbol
        ELSE UPPER(snap.symbol)
    END
)
"""

# 带实时 return 计算的 stock_daily_latest 子查询
_SDL_WITH_RET = """
    SELECT s.*,
        LEAD(s.trade_date) OVER (PARTITION BY s.symbol ORDER BY s.trade_date) AS next_date,
        LEAD(s.trade_date, 3) OVER (PARTITION BY s.symbol ORDER BY s.trade_date) AS date_3d,
        MAX(s.trade_date) OVER (PARTITION BY s.symbol) as latest_date
    FROM stock_daily_latest s
    WHERE s.volume > 0
"""

_SDL_SELECT = """
    COALESCE(sdl_latest.stock_name, sdl_base.stock_name, '') AS stock_name,
    COALESCE(sdl_latest.industry, sdl_base.industry, '') AS industry,
    COALESCE(sdl_latest.close, 0) AS close_price,
    COALESCE(sdl_latest.pe_ttm, 0) AS pe,
    COALESCE(sdl_latest.pb, 0) AS pb,
    COALESCE(sdl_latest.roe, 0) AS roe,
    COALESCE(sdl_latest.turnover_rate, 0) AS turnover_rate,
    COALESCE(sdl_latest.amount, 0) AS amount,
    COALESCE(sdl_latest.total_mv, 0) AS total_mv,
    COALESCE(sdl_latest.float_mv, 0) AS float_mv,
    COALESCE(sdl_latest.listed_days, 0) AS listed_days,
    COALESCE(sdl_latest.is_st, 0) <> 0 AS is_st,
    COALESCE(sdl_latest.idx_hs300, 0) <> 0 AS is_hs300,
    COALESCE(sdl_latest.idx_zz1000, 0) <> 0 AS is_csi1000,
    COALESCE(sdl_base.pct_change * 100, 0) AS latest_change_pct,
    COALESCE(sdl_next.close / NULLIF(sdl_base.close, 0) - 1, 0) AS return_1d,
    COALESCE(sdl_3d.close / NULLIF(sdl_base.close, 0) - 1, 0) AS return_3d,
    COALESCE(sdl_latest.ma5, 0) AS ma5,
    COALESCE(sdl_latest.ma10, 0) AS ma10,
    COALESCE(sdl_latest.ma_gap_5, 0) AS ma_gap_5,
    COALESCE(sdl_latest.ma_gap_10, 0) AS ma_gap_10,
    COALESCE(sdl_latest.ma_gap_20, 0) AS ma_gap_20,
    COALESCE(sdl_latest.rsi_14, sdl_latest.rsi_6, 0) AS rsi,
    COALESCE(sdl_latest.rsi_14, 0) AS rsi_14,
    COALESCE(sdl_latest.vol_atr_14, 0) AS atr,
    COALESCE(sdl_latest.macd_hist, 0) AS macd_hist,
    COALESCE(sdl_latest.volume_ratio_5, 0) AS volume_ratio_5,
    COALESCE(sdl_latest.volume_ratio_20, 0) AS volume_ratio_20,
    COALESCE(sdl_latest.volume_trend_3d, FALSE) AS volume_trend_3d,
    COALESCE(sdl_latest.main_flow, 0) AS main_flow,
    COALESCE(sdl_latest.flow_net_amount, 0) AS flow_net_amount,
    COALESCE(sdl_latest.inst_ownership, 0) AS inst_ownership,
    COALESCE(sdl_latest.profit_growth, 0) AS profit_growth,
    COALESCE(
      to_jsonb(array_remove(ARRAY[
        CASE WHEN COALESCE(sdl_latest.concept_ai, 0) <> 0 THEN 'AI' END,
        CASE WHEN COALESCE(sdl_latest.concept_chip, 0) <> 0 THEN '芯片' END,
        CASE WHEN COALESCE(sdl_latest.concept_new_energy, 0) <> 0 THEN '新能源' END,
        CASE WHEN COALESCE(sdl_latest.concept_pv, 0) <> 0 THEN '光伏' END,
        CASE WHEN COALESCE(sdl_latest.concept_lithium, 0) <> 0 THEN '锂电' END,
        CASE WHEN COALESCE(sdl_latest.concept_military, 0) <> 0 THEN '军工' END,
        CASE WHEN COALESCE(sdl_latest.concept_medical, 0) <> 0 THEN '医药' END,
        CASE WHEN COALESCE(sdl_latest.concept_fintech, 0) <> 0 THEN '金融科技' END,
        CASE WHEN COALESCE(sdl_latest.concept_consumption, 0) <> 0 THEN '消费' END,
        CASE WHEN COALESCE(sdl_latest.concept_state_owned, 0) <> 0 THEN '国企改革' END
      ]::text[], NULL)),
      '[]'::jsonb
    ) AS concept_tags,
    COALESCE(
      to_jsonb(array_remove(ARRAY[
        CASE WHEN COALESCE(sdl_latest.idx_hs300, 0) <> 0 THEN '沪深300' END,
        CASE WHEN COALESCE(sdl_latest.idx_zz1000, 0) <> 0 THEN '中证1000' END,
        CASE WHEN COALESCE(sdl_latest.idx_chinext, 0) <> 0 THEN '创业板指数' END,
        CASE WHEN COALESCE(sdl_latest.idx_margin, 0) <> 0 THEN '两融标的' END,
        CASE WHEN COALESCE(sdl_latest.idx_all, 0) <> 0 THEN '全市场' END
      ]::text[], NULL)),
      '[]'::jsonb
    ) AS index_tags,
    sdl_latest.trade_date AS latest_trade_date,
    COALESCE(sdl_latest.consecutive_limit_up_days, 0) AS consecutive_limit_up_days_sdl
"""

def _serialize_date(d: Any) -> str | None:
    return d.isoformat() if isinstance(d, (date, datetime)) else None

def _serialize_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        val = float(v)
        if math.isfinite(val):
            return val
        return None
    except (ValueError, TypeError):
        return None

def _serialize_int(v: Any) -> int | None:
    try: return int(v) if v is not None else None
    except: return None

def _format_candidate_record(row: dict[str, Any]) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "unknown")
    run_id = str(row.get("run_id") or "unknown")

    def to_yi(v):
        val = _serialize_float(v) or 0.0
        return val / 100000000.0  # 元 → 亿元

    def parse_json(v):
        if not v: return []
        if isinstance(v, (list, dict)): return v
        try: return json.loads(v)
        except: return []

    concept_tags = parse_json(row.get("concept_tags"))
    index_tags = parse_json(row.get("index_tags"))
    risk_flags = parse_json(row.get("risk_flags"))
    return_1d_pct = (_serialize_float(row.get("return_1d")) or 0.0) * 100
    return_3d_pct = (_serialize_float(row.get("return_3d")) or 0.0) * 100

    return {
        "key": f"{run_id}:{symbol}",
        "modelId": row.get("model_id"),
        "runId": run_id,
        "rank": _serialize_int(row.get("score_rank")) or 0,
        "code": symbol,
        "name": row.get("stock_name") or symbol,
        "score": _serialize_float(row.get("fusion_score")) or 0.0,
        "latestChange": _serialize_float(row.get("latest_change_pct")) or 0.0,
        "consecutiveLimitUpDays": _serialize_int(row.get("consecutive_limit_up_days")) or _serialize_int(row.get("consecutive_limit_up_days_sdl")) or 0,
        "turnoverRate": _serialize_float(row.get("turnover_rate")) or 0.0,
        "amount": round(to_yi(row.get("amount")), 4),
        "marketCap": round(to_yi(row.get("total_mv")), 2),
        "totalMv": round(to_yi(row.get("total_mv")), 2),
        "floatMv": round(to_yi(row.get("float_mv")), 2),
        "listedDays": _serialize_int(row.get("listed_days")) or 0,
        "sector": row.get("industry") or "",
        "concept": " / ".join(concept_tags[:3]) if isinstance(concept_tags, list) and concept_tags else "",
        "conceptTags": concept_tags if isinstance(concept_tags, list) else [],
        "indexTags": index_tags if isinstance(index_tags, list) else [],
        "riskFlags": risk_flags if isinstance(risk_flags, list) else [],
        "closePrice": _serialize_float(row.get("close_price")) or 0.0,
        "pe": _serialize_float(row.get("pe")) or 0.0,
        "pb": _serialize_float(row.get("pb")) or 0.0,
        "roe": round((_serialize_float(row.get("roe")) or 0.0) * 100, 2),
        "ma5": _serialize_float(row.get("ma5")) or 0.0,
        "ma10": _serialize_float(row.get("ma10")) or 0.0,
        "maGap5": _serialize_float(row.get("ma_gap_5")) or 0.0,
        "maGap10": _serialize_float(row.get("ma_gap_10")) or 0.0,
        "maGap20": _serialize_float(row.get("ma_gap_20")) or 0.0,
        "rsi": _serialize_float(row.get("rsi")) or 0.0,
        "rsi14": _serialize_float(row.get("rsi_14")) or 0.0,
        "atr": _serialize_float(row.get("atr")) or 0.0,
        "macdHist": _serialize_float(row.get("macd_hist")) or 0.0,
        "volRatio5": _serialize_float(row.get("volume_ratio_5")) or 0.0,
        "volRatio20": _serialize_float(row.get("volume_ratio_20")) or 0.0,
        "volumeTrend3d": bool(row.get("volume_trend_3d")),
        "volumeTrend5d": False,
        "return1d": return_1d_pct,
        "return3d": return_3d_pct,
        "nextDayReturn": return_1d_pct,
        "day3Return": return_3d_pct,
        "mainFlow": (_serialize_float(row.get("main_flow")) or 0.0) / 1000000.0,
        "flowNetAmount": (_serialize_float(row.get("flow_net_amount")) or 0.0) / 1000000.0,
        "instOwnership": _serialize_float(row.get("inst_ownership")) or 0.0,
        "profitGrowth": _serialize_float(row.get("profit_growth")) or 0.0,
        "isSt": bool(row.get("is_st")),
        "isTradable": (_serialize_float(row.get("close_price")) or 0.0) > 0,
        "thesis": row.get("thesis_summary") or "",
        "updatedAt": _serialize_date(row.get("updated_at")),
        "isHs300": bool(row.get("is_hs300")),
        "isCsi1000": bool(row.get("is_csi1000")),
    }

async def _fetch_summary(session, where: str, params: dict[str, Any]) -> dict[str, Any]:
    result = await session.execute(
        text(
            f"""
            SELECT
                COUNT(*) AS total_count,
                COUNT(*) FILTER (WHERE (sdl.close > 0)) AS tradable_count,
                COUNT(*) FILTER (WHERE (sdl.idx_hs300 <> 0)) AS hs300_count,
                COUNT(*) FILTER (WHERE (sdl.idx_zz1000 <> 0)) AS zz1000_count,
                COUNT(*) FILTER (WHERE (sdl.idx_margin <> 0)) AS margin_count,
                COUNT(*) FILTER (WHERE (sdl.idx_chinext <> 0)) AS chinext_count,
                AVG(COALESCE(snap.fusion_score, 0)) AS avg_score,
                COUNT(*) FILTER (WHERE COALESCE(snap.confidence_level, 'watch') = 'high') AS high_confidence_count,
                COUNT(*) FILTER (WHERE COALESCE(snap.fusion_score, 0) >= 0.05) AS strong_count,
                MAX(snap.updated_at) AS last_updated_at
            FROM qm_research_candidate_snapshot snap
            LEFT JOIN stock_daily_latest sdl ON (
                sdl.symbol = CASE
                    WHEN snap.symbol ~* '^(SH|SZ|BJ)[0-9]{6}$' THEN UPPER(snap.symbol)
                    ELSE snap.symbol
                END
                AND sdl.trade_date = snap.prediction_trade_date
            )
            WHERE {where}
            """
        ),
        params,
    )
    row = result.mappings().first()
    if row is None:
        return {
            "total": 0,
            "avgScore": 0.0,
            "highConfidenceCount": 0,
            "strongCount": 0,
            "lastUpdatedAt": None,
        }
    payload = dict(row)
    return {
        "total": _serialize_int(payload.get("total_count")) or 0,
        "totalMarket": _serialize_int(payload.get("tradable_count")) or 0,
        "hs300": _serialize_int(payload.get("hs300_count")) or 0,
        "zz1000": _serialize_int(payload.get("zz1000_count")) or 0,
        "margin": _serialize_int(payload.get("margin_count")) or 0,
        "chinext": _serialize_int(payload.get("chinext_count")) or 0,
        "avgScore": round(_serialize_float(payload.get("avg_score")) or 0.0, 4),
        "highConfidenceCount": _serialize_int(payload.get("high_confidence_count")) or 0,
        "strongCount": _serialize_int(payload.get("strong_count")) or 0,
        "lastUpdatedAt": _serialize_date(payload.get("last_updated_at")),
    }

async def _do_get_overview(tid: str, uid: str, model_id: str | None, run_id: str | None, limit: int, offset: int):
    where = "snap.tenant_id = :tid AND snap.user_id = :uid"
    params = {"tid": tid, "uid": uid, "limit": limit, "offset": offset}
    if model_id: where += " AND snap.model_id = :mid"; params["mid"] = model_id
    if run_id: where += " AND snap.run_id = :rid"; params["rid"] = run_id

    async with get_session(read_only=True) as session:
        sql = f"""WITH sdl_with_ret AS NOT MATERIALIZED ({_SDL_WITH_RET})
        SELECT snap.*, {_SDL_SELECT} 
        FROM qm_research_candidate_snapshot snap 
        LEFT JOIN sdl_with_ret sdl_base ON (
            {_SDL_JOIN_CONDITION.replace('sdl', 'sdl_base')}
            AND (
                sdl_base.next_date = snap.prediction_trade_date
                OR sdl_base.trade_date = snap.prediction_trade_date
            )
        )
        LEFT JOIN sdl_with_ret sdl_latest ON ({_SDL_JOIN_CONDITION.replace('sdl', 'sdl_latest')}) AND sdl_latest.trade_date = sdl_base.latest_date
        LEFT JOIN sdl_with_ret sdl_next ON ({_SDL_JOIN_CONDITION.replace('sdl', 'sdl_next')}) AND sdl_next.trade_date = sdl_base.next_date
        LEFT JOIN sdl_with_ret sdl_3d ON ({_SDL_JOIN_CONDITION.replace('sdl', 'sdl_3d')}) AND sdl_3d.trade_date = sdl_base.date_3d
        WHERE {where} ORDER BY snap.score_rank ASC LIMIT :limit OFFSET :offset"""
        result = await session.execute(text(sql), params)
        items = [_format_candidate_record(dict(r)) for r in result.mappings()]
        summary = await _fetch_summary(session, where, params)
    return {"items": items, "summary": summary}

def _humanize_model_name(model_id: str) -> str:
    if not model_id: return "Unknown Model"
    if model_id == "alpha158": return "Alpha158 (Baseline)"
    if model_id == "model_qlib": return "Qlib LightGBM"
    if model_id.startswith("mdl_train_"):
        parts = model_id.split("_")
        if len(parts) >= 3:
            ts = parts[2]
            if len(ts) >= 12:
                try:
                    dt = datetime.strptime(ts[:12], "%Y%m%d%H%M")
                    return f"训练模型 ({dt.strftime('%m/%d %H:%M')})"
                except: pass
    return model_id.replace("_", " ").title()

@router.get("/models")
async def get_available_models(current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    async with get_session(read_only=True) as session:
        sql = """
            SELECT DISTINCT snap.model_id,
                   COALESCE(
                     um.metadata_json->>'display_name',
                     um.metadata_json->>'model_name'
                   ) AS display_name
            FROM qm_research_candidate_snapshot snap
            LEFT JOIN qm_user_models um ON um.tenant_id = snap.tenant_id 
                                        AND um.user_id = snap.user_id 
                                        AND um.model_id = snap.model_id
            WHERE snap.tenant_id = :tid AND snap.user_id = :uid
        """
        res = await session.execute(text(sql), {"tid": tid, "uid": uid})
        models = []
        for r in res.mappings():
            mid = r["model_id"]
            name = r["display_name"] or _humanize_model_name(mid)
            models.append({"modelId": mid, "name": name})
        return {"code": 200, "data": {"models": models}}

@router.get("/runs")
async def get_inference_runs(model_id: str, current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    async with get_session(read_only=True) as session:
        res = await session.execute(
            text(
                """
                SELECT run_id, data_trade_date, prediction_trade_date, status, updated_at
                FROM qm_model_inference_runs
                WHERE tenant_id = :tid AND user_id = :uid AND model_id = :mid
                ORDER BY prediction_trade_date DESC, created_at DESC
                """
            ),
            {"tid": tid, "uid": uid, "mid": model_id},
        )
        return {
            "code": 200,
            "data": {
                "runs": [
                    {
                        "runId": r[0],
                        "modelId": model_id,
                        "inferenceDate": _serialize_date(r[1]),
                        "targetDate": _serialize_date(r[2]),
                        "status": str(r[3] or "completed"),
                        "lastUpdatedAt": _serialize_date(r[4]),
                        "universeLabel": "",
                    }
                    for r in res
                ]
            },
        }

@router.get("/overview")
async def get_research_overview(
    model_id: str | None = Query(None),
    run_id: str | None = Query(None),
    limit: int = Query(50),
    offset: int = Query(0),
    current_user: dict = Depends(get_current_user)
):
    data = await _do_get_overview(str(current_user["tenant_id"]), str(current_user["user_id"]), model_id, run_id, limit, offset)
    return {"code": 200, "data": {"items": data["items"], "summary": data["summary"]}}

@router.get("/universe")
async def get_research_universe(run_id: str, limit: int = Query(200), current_user: dict = Depends(get_current_user)):
    data = await _do_get_overview(str(current_user["tenant_id"]), str(current_user["user_id"]), None, run_id, limit, 0)
    return {"code": 200, "data": {"items": data["items"], "summary": data["summary"]}}

@router.get("/watchlist")
async def get_user_watchlist(limit: int = Query(50), offset: int = Query(0), current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    async with get_session(read_only=True) as session:
        res = await session.execute(text("SELECT symbol, stock_name, added_at, source_run_id FROM qm_user_watchlist WHERE tenant_id = :tid AND user_id = :uid ORDER BY added_at DESC LIMIT :limit OFFSET :offset"), {"tid": tid, "uid": uid, "limit": limit, "offset": offset})
        items = [{"symbol": r[0], "stockName": r[1], "addedAt": _serialize_date(r[2]), "sourceRunId": r[3]} for r in res]
        total = (await session.execute(text("SELECT COUNT(*) FROM qm_user_watchlist WHERE tenant_id = :tid AND user_id = :uid"), {"tid": tid, "uid": uid})).scalar() or 0
    return {"code": 200, "data": {"items": items, "total": total}}

@router.post("/watchlist/{symbol}")
async def add_to_watchlist(symbol: str, req: WatchlistAddRequest, current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    async with get_session() as session:
        await session.execute(text("INSERT INTO qm_user_watchlist (tenant_id, user_id, symbol, stock_name, source_run_id, features_snapshot, updated_at) VALUES (:tid, :uid, :s, :n, :rid, :f, NOW()) ON CONFLICT (tenant_id, user_id, symbol) DO UPDATE SET features_snapshot = EXCLUDED.features_snapshot, updated_at = NOW()"), {"tid": tid, "uid": uid, "s": symbol, "n": req.stock_name, "rid": req.run_id, "f": json.dumps(req.features_snapshot or {})})
    return {"code": 200, "message": "success"}

@router.get("/pool")
async def get_user_research_pool(status: str | None = Query(None), limit: int = Query(50), offset: int = Query(0), current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    where = "tenant_id = :tid AND user_id = :uid"
    params = {"tid": tid, "uid": uid, "limit": limit, "offset": offset}
    if status: where += " AND status = :status"; params["status"] = status
    async with get_session(read_only=True) as session:
        res = await session.execute(text(f"SELECT symbol, stock_name, added_at, source_run_id, status FROM qm_user_research_pool WHERE {where} ORDER BY added_at DESC LIMIT :limit OFFSET :offset"), params)
        items = [{"symbol": r[0], "stockName": r[1], "addedAt": _serialize_date(r[2]), "sourceRunId": r[3], "status": r[4]} for r in res]
        total = (await session.execute(text(f"SELECT COUNT(*) FROM qm_user_research_pool WHERE {where}"), params)).scalar() or 0
    return {"code": 200, "data": {"items": items, "total": total}}

@router.post("/pool/{symbol}")
async def add_to_research_pool(symbol: str, req: PoolAddRequest, current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    async with get_session() as session:
        await session.execute(text("INSERT INTO qm_user_research_pool (tenant_id, user_id, symbol, stock_name, source_run_id, model_id, fusion_score, thesis_summary, features_snapshot, updated_at) VALUES (:tid, :uid, :s, :n, :rid, :mid, :fs, :ts, :f, NOW()) ON CONFLICT (tenant_id, user_id, symbol) DO UPDATE SET features_snapshot = EXCLUDED.features_snapshot, updated_at = NOW()"), {"tid": tid, "uid": uid, "s": symbol, "n": req.stock_name, "rid": req.run_id, "mid": req.model_id, "fs": req.fusion_score, "ts": req.thesis_summary, "f": json.dumps(req.features_snapshot or {})})
    return {"code": 200, "message": "success"}

_SYMBOL_NORM_SQL = """
    CASE
        WHEN UPPER(col) ~ '^(SH|SZ|BJ)[0-9]{6}$' THEN UPPER(col)
        WHEN col ~ '^[0-9]{6}$' AND LEFT(col, 1) IN ('6', '9') THEN 'SH' || col
        WHEN col ~ '^[0-9]{6}$' AND LEFT(col, 1) IN ('4', '8') THEN 'BJ' || col
        WHEN col ~ '^[0-9]{6}$' THEN 'SZ' || col
        ELSE UPPER(col)
    END
"""

@router.post("/symbols/features")
async def get_symbols_features(req: SymbolsFeaturesRequest, current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    symbols = [StockCodeUtil.to_prefix(s.strip()) for s in req.symbols if s.strip()]
    if not symbols: return {"code": 200, "data": {"items": []}}
    vals = ", ".join(f"('{s}')" for s in symbols)

    norm = _SYMBOL_NORM_SQL.replace("col", "symbol")
    norm_s = _SYMBOL_NORM_SQL.replace("col", "s.symbol")

    sql = f"""
        WITH sym_list(raw_symbol) AS (VALUES {vals}),
        sdl_with_ret AS NOT MATERIALIZED ({_SDL_WITH_RET}),
        pool_norm AS (
            SELECT symbol, features_snapshot, ({norm}) AS prefix_symbol
            FROM qm_user_research_pool WHERE tenant_id = :tid AND user_id = :uid
        ),
        watchlist_norm AS (
            SELECT symbol, features_snapshot, ({norm}) AS prefix_symbol
            FROM qm_user_watchlist WHERE tenant_id = :tid AND user_id = :uid
        ),
        snap AS (
            SELECT sym_list.raw_symbol AS symbol,
                   COALESCE(s.run_id, 'history') as run_id,
                   s.prediction_trade_date,
                   COALESCE(s.fusion_score, (ps.features_snapshot->>'score')::double precision, (ws.features_snapshot->>'score')::double precision) as fusion_score,
                   COALESCE(s.risk_flags, (ps.features_snapshot->'riskFlags')::jsonb, (ws.features_snapshot->'riskFlags')::jsonb) as risk_flags,
                   COALESCE(s.thesis_summary, (ps.features_snapshot->>'thesis')::text, (ws.features_snapshot->>'thesis')::text) as thesis_summary,
                   ROW_NUMBER() OVER(PARTITION BY sym_list.raw_symbol ORDER BY s.prediction_trade_date DESC NULLS LAST) as rn
            FROM sym_list
            LEFT JOIN pool_norm ps ON ps.prefix_symbol = sym_list.raw_symbol
            LEFT JOIN watchlist_norm ws ON ws.prefix_symbol = sym_list.raw_symbol
            LEFT JOIN qm_research_candidate_snapshot s ON ({norm_s}) = sym_list.raw_symbol AND s.tenant_id = :tid AND s.user_id = :uid
        )
        SELECT snap.*, {_SDL_SELECT} 
        FROM snap 
        LEFT JOIN sdl_with_ret sdl_base ON (
            {_SDL_JOIN_CONDITION.replace('sdl', 'sdl_base')}
            AND (
                sdl_base.next_date = snap.prediction_trade_date
                OR sdl_base.trade_date = snap.prediction_trade_date
            )
        )
        LEFT JOIN sdl_with_ret sdl_latest ON ({_SDL_JOIN_CONDITION.replace('sdl', 'sdl_latest')}) AND sdl_latest.trade_date = COALESCE(sdl_base.latest_date, (SELECT MAX(trade_date) FROM stock_daily_latest WHERE symbol = snap.symbol))
        LEFT JOIN sdl_with_ret sdl_next ON ({_SDL_JOIN_CONDITION.replace('sdl', 'sdl_next')}) AND sdl_next.trade_date = sdl_base.next_date
        LEFT JOIN sdl_with_ret sdl_3d ON ({_SDL_JOIN_CONDITION.replace('sdl', 'sdl_3d')}) AND sdl_3d.trade_date = sdl_base.date_3d
        WHERE snap.rn = 1
    """
    async with get_session(read_only=True) as session:
        res = await session.execute(text(sql), {"tid": tid, "uid": uid})
        items = [_format_candidate_record(dict(r)) for r in res.mappings()]
    return {"code": 200, "data": {"items": items}}

@router.get("/kline/{symbol}")
async def get_stock_kline(symbol: str, days: int = Query(60), current_user: dict = Depends(get_current_user)):
    s = StockCodeUtil.to_prefix(symbol)
    cache_key = f"qm:research:kline:{s}:{days}"
    
    # 1. 尝试从缓存读取
    from backend.shared.market_data.stock_daily_latest_cache import stock_latest_cache
    cached = stock_latest_cache.redis.get(cache_key)
    if cached:
        try:
            return {"code": 200, "data": {"symbol": s, "items": json.loads(cached), "cached": True}}
        except: pass

    # 2. 缓存未命中则查询数据库
    async with get_session(read_only=True) as session:
        # 针对 stock_daily_latest 的查询优化
        res = await session.execute(
            text(f"SELECT trade_date, open, high, low, close, volume, adj_factor FROM stock_daily_latest WHERE symbol = :s ORDER BY trade_date DESC LIMIT :l"), 
            {"s": s, "l": days}
        )
        items = []
        for r in res:
            f = float(r[6]) if r[6] else 1.0
            items.append({
                "date": str(r[0]), 
                "open": round(float(r[1]) / f, 2), 
                "high": round(float(r[2]) / f, 2), 
                "low": round(float(r[3]) / f, 2), 
                "close": round(float(r[4]) / f, 2), 
                "volume": float(r[5])
            })
        items.reverse()
        
    # 3. 写入缓存 (1小时有效)
    if items:
        try:
            stock_latest_cache.redis.set(cache_key, json.dumps(items), ex=3600)
        except: pass
        
    return {"code": 200, "data": {"symbol": s, "items": items}}
