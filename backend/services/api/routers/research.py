"""投研平台聚合接口。

直接从 qm_research_candidate_snapshot 读取用户隔离后的候选快照，
为前端提供模型/批次/候选列表的聚合视图。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from datetime import date, datetime
from threading import RLock
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.api.user_app.services.cache_service import get_cache_service
from backend.shared.database_manager_v2 import get_session
from backend.shared.stock_utils import StockCodeUtil

router = APIRouter(prefix="/api/v1/research", tags=["Research"])

_MODEL_LIMIT = 20
_RUN_LIMIT = 50
_OVERVIEW_CACHE_TTL_SECONDS = 60
_OVERVIEW_CACHE_KEY_PREFIX = "research:overview:v1"
_STOCK_INDEX_JSON_PATH = os.path.abspath(os.getenv("STOCK_INDEX_JSON_PATH", "data/stocks/stocks_index.json"))
_stock_name_map_lock = RLock()
_stock_name_map_mtime: float = -1.0
_stock_name_map: dict[str, str] = {}

_SORT_SQL_MAP = {
    "score": "COALESCE(fusion_score, 0) DESC, COALESCE(score_rank, 999999) ASC, symbol ASC",
    "latest_change": "COALESCE(latest_change_pct, 0) DESC, COALESCE(fusion_score, 0) DESC, symbol ASC",
    "amount": "COALESCE(amount, 0) DESC, COALESCE(fusion_score, 0) DESC, symbol ASC",
    "turnover_rate": "COALESCE(turnover_rate, 0) DESC, COALESCE(fusion_score, 0) DESC, symbol ASC",
    "updated_at": "updated_at DESC NULLS LAST, COALESCE(fusion_score, 0) DESC, symbol ASC",
}

# 核心：将 snap.symbol 标准化为数据库现有的 Prefix 格式 (SH600000)
_SYM_NORMALIZE = """
CASE
  WHEN snap.symbol IS NULL OR BTRIM(snap.symbol) = '' THEN NULL
  -- 已经是 Prefix 格式 (SH600000)
  WHEN snap.symbol ~* '^(SH|SZ|BJ)[0-9]{6}$' THEN UPPER(snap.symbol)
  -- 是 Suffix 格式 (600000.SH) -> 转换为 SH600000
  WHEN snap.symbol ~* '^[0-9]{6}\\.(SH|SZ|BJ)$' THEN UPPER(RIGHT(snap.symbol, 2)) || LEFT(snap.symbol, 6)
  -- 纯数字补全 (注意：8开头在北交所是BJ，6开头是SH)
  WHEN snap.symbol ~ '^[0-9]{6}$' AND LEFT(snap.symbol, 1) IN ('6', '9') THEN 'SH' || snap.symbol
  WHEN snap.symbol ~ '^[0-9]{6}$' AND LEFT(snap.symbol, 1) IN ('4', '8') THEN 'BJ' || snap.symbol
  WHEN snap.symbol ~ '^[0-9]{6}$' THEN 'SZ' || snap.symbol
  ELSE UPPER(snap.symbol)
END
"""

# 转换为 Suffix 格式 (600000.SH) 用于匹配 stock_daily_latest
_SYM_NORMALIZE_TO_SUFFIX = """
CASE
  WHEN snap.symbol IS NULL OR BTRIM(snap.symbol) = '' THEN NULL
  -- 已经是 Suffix 格式 (600000.SH)
  WHEN snap.symbol ~* '^[0-9]{6}\\.(SH|SZ|BJ)$' THEN UPPER(snap.symbol)
  -- 是 Prefix 格式 (SH600000) -> 转换为 600000.SH
  WHEN snap.symbol ~* '^(SH|SZ|BJ)[0-9]{6}$' THEN RIGHT(snap.symbol, 6) || '.' || UPPER(LEFT(snap.symbol, 2))
  -- 纯数字补全
  WHEN snap.symbol ~ '^[0-9]{6}$' AND LEFT(snap.symbol, 1) IN ('6', '9') THEN snap.symbol || '.SH'
  WHEN snap.symbol ~ '^[0-9]{6}$' AND LEFT(snap.symbol, 1) IN ('4', '8') THEN snap.symbol || '.BJ'
  WHEN snap.symbol ~ '^[0-9]{6}$' THEN snap.symbol || '.SZ'
  ELSE UPPER(snap.symbol)
END
"""

def _normalize_model_id(model_id: str | None) -> str | None:
    if not model_id:
        return model_id
    raw = str(model_id).strip()
    # 移除 script_v1_ 前缀（历史遗留兼容）
    if raw.startswith("script_v1_"):
        processed = raw[10:]
        return processed if processed else raw
    return raw


# stock_daily_latest.symbol 格式为 600000.SH（代码+后缀）。
# 右侧转换结果已标准化为大写后缀格式，左侧保持原列以命中 symbol/trade_date 索引。
_SDL_SYMBOL_MATCH = f"""
sdl.symbol = ({_SYM_NORMALIZE})
"""

# 从 stock_daily_latest 提取因子列 (兼容旧版 market_data_daily 字段名)
_MDD_SELECT = """
    mdd.main_flow AS flow_net_amount,
    mdd.ep_ttm AS style_ep_ttm,
    mdd.return_5d AS return_5d,
    mdd.return_10d AS return_10d,
    mdd.return_1d AS mdd_return_1d,
    mdd.return_20d AS mdd_profit_growth_proxy,
    mdd.ma_gap_5 AS mdd_ma_gap_5,
    mdd.rsi_14 AS mdd_rsi_14,
    NULL::BOOLEAN AS close_above_ma5,
    NULL::BOOLEAN AS close_above_ma10,
    0 AS recent_limit_up_count_5d,
    NULL::INTEGER AS volume_trend_3d_score,
    mdd.volume_ma_5 AS volume_ma_3,
    mdd.volume_ma_5 AS volume_ma_5,
    NULL::DOUBLE PRECISION AS high_5d,
    NULL::DOUBLE PRECISION AS low_5d
"""

_MDD_JOIN = f"""
    LEFT JOIN stock_daily_latest mdd ON mdd.symbol = ({_SYM_NORMALIZE_TO_SUFFIX})
      AND mdd.trade_date = snap.prediction_trade_date
"""

# 从 stock_daily_latest 获取基本行情和技术指标（现已合并）
# 注意：pct_change 来自最新交易日，其他指标来自预测日期对应的历史数据
_SDL_SELECT = """
    COALESCE(sdl.stock_name, '') AS stock_name,
    COALESCE(sdl.industry, '') AS industry,
    COALESCE(sdl.close, 0) AS close_price,
    COALESCE(sdl.turnover_rate, 0) AS turnover_rate,
    COALESCE(sdl.amount, 0) AS amount,
    COALESCE(sdl.total_mv, 0) AS total_mv,
    sdl.is_st <> 0 AS is_st,
    COALESCE(sdl.idx_hs300, 0) <> 0 AS is_hs300,
    COALESCE(sdl.idx_zz500, 0) <> 0 AS is_csi500,
    COALESCE(sdl.vol_atr_14, 0) AS atr,
    COALESCE(sdl.macd_hist, 0) AS macd_hist,
    COALESCE(sdl.kdj_j, 0) AS kdj_j,
    COALESCE(sdl.ma_gap_20, 0) AS ma_gap_20,
    sdl.listing_market,
    sdl.trade_date AS market_snapshot_trade_date,
    COALESCE(sdl.pe_ttm, 0) AS pe,
    COALESCE(sdl.pb, 0) AS pb,
    COALESCE(sdl.roe, 0) AS roe,
    COALESCE(sdl.ma5, 0) AS ma5,
    COALESCE(sdl.ma10, 0) AS ma10,
    COALESCE(sdl.ma20, 0) AS ma20,
    COALESCE(sdl.ma_gap_5, 0) AS ma_gap_5,
    COALESCE(sdl.main_flow, 0) AS main_flow,
    COALESCE(sdl.flow_net_amount, 0) AS flow_net_amount,
    COALESCE(sdl.inst_ownership, 0) AS inst_ownership,
    COALESCE(sdl.profit_growth, 0) AS profit_growth,
    COALESCE(sdl.volume_ratio_5, 0) AS volume_ratio_5,
    COALESCE(sdl.return_1d, 0) AS return_1d,
    COALESCE(sdl.rsi_14, sdl.rsi_6, 0) AS rsi,
    COALESCE(sdl.volume_trend_3d, FALSE) AS volume_trend_3d,
    FALSE AS volume_trend_5d,
    COALESCE(sdl.consecutive_limit_up_days, 0) AS consecutive_limit_up_days,
    COALESCE(
      to_jsonb(array_remove(ARRAY[
        CASE WHEN COALESCE(sdl.concept_ai, 0) <> 0 THEN 'AI' END,
        CASE WHEN COALESCE(sdl.concept_chip, 0) <> 0 THEN '芯片' END,
        CASE WHEN COALESCE(sdl.concept_new_energy, 0) <> 0 THEN '新能源' END,
        CASE WHEN COALESCE(sdl.concept_ev, 0) <> 0 THEN '电动车' END,
        CASE WHEN COALESCE(sdl.concept_pv, 0) <> 0 THEN '光伏' END,
        CASE WHEN COALESCE(sdl.concept_lithium, 0) <> 0 THEN '锂电' END,
        CASE WHEN COALESCE(sdl.concept_semiconductor, 0) <> 0 THEN '半导体' END,
        CASE WHEN COALESCE(sdl.concept_military, 0) <> 0 THEN '军工' END,
        CASE WHEN COALESCE(sdl.concept_medical, 0) <> 0 THEN '医药' END,
        CASE WHEN COALESCE(sdl.concept_cyber, 0) <> 0 THEN '网络安全' END,
        CASE WHEN COALESCE(sdl.concept_fintech, 0) <> 0 THEN '金融科技' END,
        CASE WHEN COALESCE(sdl.concept_consumption, 0) <> 0 THEN '消费' END,
        CASE WHEN COALESCE(sdl.concept_real_estate, 0) <> 0 THEN '地产' END,
        CASE WHEN COALESCE(sdl.concept_infrastructure, 0) <> 0 THEN '基建' END,
        CASE WHEN COALESCE(sdl.concept_state_owned, 0) <> 0 THEN '国企改革' END
      ]::text[], NULL)),
      '[]'::jsonb
    ) AS concept_tags,
    COALESCE(
      to_jsonb(array_remove(ARRAY[
        CASE WHEN COALESCE(sdl.idx_hs300, 0) <> 0 THEN '沪深300' END,
        CASE WHEN COALESCE(sdl.idx_zz500, 0) <> 0 THEN '中证500' END,
        CASE WHEN COALESCE(sdl.idx_zz1000, 0) <> 0 THEN '中证1000' END,
        CASE WHEN COALESCE(sdl.idx_chinext, 0) <> 0 THEN '创业板指数' END,
        CASE WHEN COALESCE(sdl.idx_margin, 0) <> 0 THEN '两融标的' END,
        CASE WHEN COALESCE(sdl.idx_all, 0) <> 0 THEN '全市场' END
      ]::text[], NULL)),
      '[]'::jsonb
    ) AS index_tags,
    sdl.province,
    NULL::TEXT AS city
"""

# 从最新交易日获取实时涨跌幅（独立于预测日期）
_SDL_SELECT_LATEST = """
    COALESCE(sdl_latest.pct_change, 0) AS latest_change_pct,
    COALESCE(sdl_latest.close, 0) AS latest_close,
    COALESCE(sdl_latest.trade_date, NULL) AS latest_trade_date,
    COALESCE(sdl_latest.limit_up_today, 0) <> 0 AS is_limit_up,
    COALESCE(sdl_latest.limit_down_today, 0) <> 0 AS is_limit_down,
    COALESCE(sdl_latest.consecutive_limit_up_days, 0) AS latest_consecutive_limit_up_days,
    CASE WHEN COALESCE(sdl_latest.close, 0) > 0 AND COALESCE(sdl_latest.is_st, 0) = 0 THEN TRUE ELSE FALSE END AS tradable_flag
"""

# 次日收益计算：基于推理日(data_trade_date)的收盘价计算次日收益
_SDL_SELECT_NEXT_DAY = """
    CASE
      WHEN sdl_next.next_day_close IS NOT NULL AND sdl_data.data_day_close > 0
      THEN ROUND(((sdl_next.next_day_close - sdl_data.data_day_close) / sdl_data.data_day_close * 100)::NUMERIC, 2)
      ELSE NULL
    END AS next_day_return,
    sdl_next.next_trade_date
"""

# 3日收益计算：基于推理日(data_trade_date)的收盘价计算3个交易日后的收益
_SDL_JOIN_DAY3 = f"""
    LEFT JOIN LATERAL (
      SELECT sdl.close AS day3_close, sdl.trade_date AS day3_trade_date
      FROM stock_daily_latest sdl
      WHERE {_SDL_SYMBOL_MATCH}
        AND sdl.trade_date > snap.data_trade_date
      ORDER BY sdl.trade_date ASC
      LIMIT 1 OFFSET 2
    ) sdl_day3 ON true
"""

_SDL_SELECT_DAY3 = """
    CASE
      WHEN sdl_day3.day3_close IS NOT NULL AND sdl_data.data_day_close > 0
      THEN ROUND(((sdl_day3.day3_close - sdl_data.data_day_close) / sdl_data.data_day_close * 100)::NUMERIC, 2)
      ELSE NULL
    END AS day3_return,
    sdl_day3.day3_trade_date
"""

# 冗余定义移除（已在上方定义）

# 获取 stock_daily_latest 表中最新行情数据（用于显示当日实时涨跌幅）
# 使用独立的 LATERAL JOIN 获取最新交易日数据，不受 prediction_trade_date 限制
_SDL_JOIN_LATEST = f"""
    LEFT JOIN LATERAL (
      SELECT sdl.* FROM stock_daily_latest sdl
      WHERE {_SDL_SYMBOL_MATCH}
      ORDER BY sdl.trade_date DESC
      LIMIT 1
    ) sdl_latest ON true
"""

# 获取推理日(data_trade_date)下一交易日的收盘价，用于计算次日收益
# 次日收益 = (下一交易日收盘价 - 推理日收盘价) / 推理日收盘价 * 100
_SDL_JOIN_DATA_DAY = f"""
    LEFT JOIN LATERAL (
      SELECT sdl.close AS data_day_close, sdl.trade_date AS data_trade_date_actual
      FROM stock_daily_latest sdl
      WHERE {_SDL_SYMBOL_MATCH}
        AND sdl.trade_date <= snap.data_trade_date
      ORDER BY sdl.trade_date DESC
      LIMIT 1
    ) sdl_data ON true
"""

_SDL_JOIN_NEXT_DAY = f"""
    LEFT JOIN LATERAL (
      SELECT sdl.close AS next_day_close, sdl.trade_date AS next_trade_date
      FROM stock_daily_latest sdl
      WHERE {_SDL_SYMBOL_MATCH}
        AND sdl.trade_date > snap.data_trade_date
      ORDER BY sdl.trade_date ASC
      LIMIT 1
    ) sdl_next ON true
"""

# 获取 stock_daily_latest 表中预测日期对应的历史数据（用于其他历史指标）
# 优化：使用 LATERAL JOIN 避免对每行重复计算子查询
_SDL_JOIN = f"""
    LEFT JOIN LATERAL (
      SELECT sdl.* FROM stock_daily_latest sdl
      WHERE {_SDL_SYMBOL_MATCH}
        AND sdl.trade_date <= snap.prediction_trade_date
      ORDER BY sdl.trade_date DESC
      LIMIT 1
    ) sdl ON true
"""

# mdd.features 和 sdl 都不包含的字段，返回默认值
_SDL_FALLBACK = """
    NULL::TEXT AS market_type,
    0 AS listed_days,
    0 AS continued_rise_days,
    0 AS continued_fall_days,
    NULL::INTEGER AS amount_rank,
    NULL::DOUBLE PRECISION AS amount_ma_3,
    NULL::DOUBLE PRECISION AS amount_ma_5,
    NULL::DOUBLE PRECISION AS life_high_week,
    NULL::DOUBLE PRECISION AS life_high_month,
    NULL::DOUBLE PRECISION AS life_high_3month,
    NULL::DOUBLE PRECISION AS life_high_6month,
    NULL::DOUBLE PRECISION AS life_high_one_year,
    NULL::BOOLEAN AS is_suspended
"""


def _serialize_date(value: date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return value.isoformat()


def _serialize_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _serialize_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _build_overview_cache_key(
    *,
    tenant_id: str,
    user_id: str,
    model_id: str | None,
    run_id: str | None,
    keyword: str | None,
    min_score: float | None,
    min_consecutive_limit_up_days: int,
    min_turnover_rate: float | None,
    max_turnover_rate: float | None,
    min_amount: float | None,
    max_amount: float | None,
    volume_trend_only: bool,
    high_confidence_only: bool,
    sectors: Sequence[str],
    concepts: Sequence[str],
    indices: Sequence[str],
    sort_by: str,
    limit: int,
    offset: int,
) -> str:
    payload = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "model_id": model_id or "",
        "run_id": run_id or "",
        "keyword": (keyword or "").strip(),
        "min_score": min_score,
        "min_consecutive_limit_up_days": min_consecutive_limit_up_days,
        "min_turnover_rate": min_turnover_rate,
        "max_turnover_rate": max_turnover_rate,
        "min_amount": min_amount,
        "max_amount": max_amount,
        "volume_trend_only": volume_trend_only,
        "high_confidence_only": high_confidence_only,
        "sectors": sorted({item.strip() for item in sectors if item and item.strip()}),
        "concepts": sorted({item.strip() for item in concepts if item and item.strip()}),
        "indices": sorted({item.strip() for item in indices if item and item.strip()}),
        "sort_by": sort_by,
        "limit": limit,
        "offset": offset,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{_OVERVIEW_CACHE_KEY_PREFIX}:{tenant_id}:{user_id}:{digest}"


def _normalize_symbol_for_name_lookup(raw_symbol: str | None) -> tuple[str | None, str | None]:
    if not raw_symbol:
        return None, None
    s = raw_symbol.strip()
    if not s:
        return None, None
    upper_s = s.upper()
    if upper_s.startswith(("SH", "SZ", "BJ")) and len(upper_s) >= 8 and upper_s[2:].isdigit():
        code = upper_s[2:8]
        return f"{code}.{upper_s[:2]}", code
    if len(upper_s) == 9 and upper_s[6] == "." and upper_s[:6].isdigit():
        return upper_s, upper_s[:6]
    if len(upper_s) == 6 and upper_s.isdigit():
        exchange = "SH" if upper_s[0] in ("6", "8", "9") else "SZ"
        return f"{upper_s}.{exchange}", upper_s
    return None, None


def _load_stock_name_map_if_needed() -> dict[str, str]:
    global _stock_name_map_mtime, _stock_name_map
    try:
        mtime = os.path.getmtime(_STOCK_INDEX_JSON_PATH)
    except OSError:
        return _stock_name_map

    with _stock_name_map_lock:
        if mtime == _stock_name_map_mtime:
            return _stock_name_map
        try:
            with open(_STOCK_INDEX_JSON_PATH, encoding="utf-8") as f:
                payload = json.load(f)
            items = payload.get("items", [])
            loaded: dict[str, str] = {}
            if isinstance(items, list):
                for raw in items:
                    if not isinstance(raw, dict):
                        continue
                    name = str(raw.get("name") or "").strip()
                    symbol = str(raw.get("symbol") or "").strip().upper()
                    code = str(raw.get("code") or "").strip()
                    if name:
                        if symbol:
                            loaded[symbol] = name
                        if code:
                            loaded[code] = name
            _stock_name_map = loaded
            _stock_name_map_mtime = mtime
        except Exception:
            return _stock_name_map
    return _stock_name_map


def _resolve_stock_name(symbol: str, fallback_name: str | None) -> str:
    if fallback_name and str(fallback_name).strip():
        return str(fallback_name).strip()
    lookup_map = _load_stock_name_map_if_needed()
    normalized_symbol, code = _normalize_symbol_for_name_lookup(symbol)
    if normalized_symbol and normalized_symbol in lookup_map:
        return lookup_map[normalized_symbol]
    if code and code in lookup_map:
        return lookup_map[code]
    return ""


def _normalize_amount_to_yi(value: Any) -> float:
    """统一成交额为“亿元”口径。

    兼容两类源数据：
    - 已是“亿元”（例如 12.8）
    - “元”口径（例如 1_280_000_000）
    """
    raw = _serialize_float(value)
    if raw is None:
        return 0.0
    if abs(raw) >= 1_000_000:
        return raw / 100_000_000
    return raw


def _normalize_market_cap_to_yi(value: Any) -> float:
    """统一总市值为“亿元”口径。

    兼容两类源数据：
    - 已是“亿元”
    - “元”口径（当前 stock_daily_latest 常见）
    """
    raw = _serialize_float(value)
    if raw is None:
        return 0.0
    if abs(raw) >= 1_000_000:
        return raw / 100_000_000
    return raw


def _normalize_pct_value(value: Any) -> float:
    """统一百分比口径。

    数据源可能是小数口径（0.2 代表 20%）或百分号口径（20 代表 20%）。
    这里统一返回"百分比数值"给前端展示。
    """
    raw = _serialize_float(value)
    if raw is None:
        return 0.0
    if -1.0 <= raw <= 1.0:
        return raw * 100.0
    return raw


def _normalize_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    raw = str(value).strip()
    return [raw] if raw else []


def _humanize_model_name(model_id: str) -> str:
    text_value = (model_id or "").replace("-", " ").replace("_", " ").strip()
    if not text_value:
        return "Unknown Model"
    return " ".join(part.capitalize() for part in text_value.split())


def _candidate_scope_filters() -> list[str]:
    return [
        "tenant_id = :tenant_id",
        "user_id = :user_id",
        "run_id = :run_id",
    ]


def _build_candidate_filter_sql(
    *,
    tenant_id: str,
    user_id: str,
    run_id: str,
    keyword: str | None,
    min_score: float | None,
    min_consecutive_limit_up_days: int,
    min_turnover_rate: float | None,
    max_turnover_rate: float | None,
    min_amount: float | None,
    max_amount: float | None,
    volume_trend_only: bool,
    high_confidence_only: bool,
    sectors: Sequence[str],
    concepts: Sequence[str],
    indices: Sequence[str],
    exclude_st: bool = False,
) -> tuple[str, dict[str, Any]]:
    filters = _candidate_scope_filters()
    params: dict[str, Any] = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "run_id": run_id,
    }

    if keyword:
        params["keyword"] = f"%{keyword.strip()}%"
        filters.append(
            "("
            "symbol ILIKE :keyword OR "
            "COALESCE(stock_name, '') ILIKE :keyword OR "
            "COALESCE(industry, '') ILIKE :keyword OR "
            "COALESCE(thesis_summary, '') ILIKE :keyword OR "
            "COALESCE(concept_tags::text, '') ILIKE :keyword"
            ")"
        )

    if min_score is not None:
        params["min_score"] = min_score
        filters.append("COALESCE(fusion_score, 0) >= :min_score")

    if min_consecutive_limit_up_days > 0:
        params["min_consecutive_limit_up_days"] = min_consecutive_limit_up_days
        filters.append(
            "COALESCE(consecutive_limit_up_days, 0) >= :min_consecutive_limit_up_days"
        )

    if min_turnover_rate is not None:
        params["min_turnover_rate"] = min_turnover_rate
        filters.append("COALESCE(turnover_rate, 0) >= :min_turnover_rate")

    if max_turnover_rate is not None:
        params["max_turnover_rate"] = max_turnover_rate
        filters.append("COALESCE(turnover_rate, 0) <= :max_turnover_rate")

    if min_amount is not None:
        params["min_amount"] = min_amount
        filters.append("COALESCE(amount, 0) >= :min_amount")

    if max_amount is not None:
        params["max_amount"] = max_amount
        filters.append("COALESCE(amount, 0) <= :max_amount")

    if volume_trend_only:
        filters.append("COALESCE(volume_trend_3d, FALSE) = TRUE")

    if high_confidence_only:
        filters.append("COALESCE(confidence_level, 'watch') = 'high'")

    if sectors:
        params["sector_filter"] = ",".join(sorted({item.strip() for item in sectors if item.strip()}))
        filters.append(
            "industry = ANY(string_to_array(:sector_filter, ','))"
        )

    if concepts:
        params["concept_filter"] = ",".join(sorted({item.strip() for item in concepts if item.strip()}))
        filters.append(
            "EXISTS ("
            "SELECT 1 FROM jsonb_array_elements_text(COALESCE(concept_tags, '[]'::jsonb)) AS concept(value) "
            "WHERE concept.value = ANY(string_to_array(:concept_filter, ','))"
            ")"
        )

    if indices:
        params["index_filter"] = ",".join(sorted({item.strip() for item in indices if item.strip()}))
        filters.append(
            "EXISTS ("
            "SELECT 1 FROM jsonb_array_elements_text(COALESCE(index_tags, '[]'::jsonb)) AS idx(value) "
            "WHERE idx.value = ANY(string_to_array(:index_filter, ','))"
            ")"
        )

    if exclude_st:
        filters.append(
            "(NOT (COALESCE(is_st, FALSE) OR COALESCE(stock_name, '') LIKE '%%ST%%' OR COALESCE(stock_name, '') LIKE '%%退%%'))"
        )

    return " AND ".join(filters), params


def _format_model_record(row: dict[str, Any]) -> dict[str, Any]:
    model_id = str(row["model_id"])
    display_name = str(row.get("model_display_name") or "").strip()
    if not display_name:
        display_name = _humanize_model_name(model_id)
    return {
        "modelId": model_id,
        "name": display_name,
        "style": "",
        "description": "",
        "runCount": _serialize_int(row.get("run_count")) or 0,
        "latestPredictionDate": _serialize_date(row.get("latest_prediction_trade_date")),
        "lastUpdatedAt": _serialize_date(row.get("last_updated_at")),
    }


def _format_run_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "runId": row["run_id"],
        "modelId": row["model_id"],
        "inferenceDate": _serialize_date(row.get("inference_date")),
        "targetDate": _serialize_date(row.get("prediction_trade_date")),
        "status": "completed",
        "universeLabel": row.get("universe_label") or "默认候选池",
        "stockCount": _serialize_int(row.get("stock_count")) or 0,
        "avgScore": _serialize_float(row.get("avg_score")) or 0.0,
        "lastUpdatedAt": _serialize_date(row.get("last_updated_at")),
    }


def _format_candidate_record(row: dict[str, Any]) -> dict[str, Any]:
    concept_tags = _normalize_text_list(row.get("concept_tags"))
    index_tags = _normalize_text_list(row.get("index_tags"))
    hit_reasons = _normalize_text_list(row.get("hit_reasons"))
    risk_flags = _normalize_text_list(row.get("risk_flags"))
    symbol = str(row["symbol"])
    run_id = str(row["run_id"])
    latest_change_value = _serialize_float(row.get("latest_change_pct")) or 0.0
    pe_value = _serialize_float(row.get("pe")) or 0.0
    if pe_value <= 0:
        ep = _serialize_float(row.get("style_ep_ttm"))
        if ep and ep > 0:
            pe_value = 1.0 / ep
    rsi_value = _serialize_float(row.get("rsi"))
    if rsi_value is None:
        rsi_value = _serialize_float(row.get("mdd_rsi_14")) or 0.0
    ma_gap_5_value = _serialize_float(row.get("ma_gap_5"))
    if ma_gap_5_value is None:
        ma_gap_5_value = _serialize_float(row.get("mdd_ma_gap_5")) or 0.0
    return_1d_value = _serialize_float(row.get("return_1d"))
    if return_1d_value is None:
        return_1d_value = _serialize_float(row.get("mdd_return_1d")) or 0.0
    flow_amount = _serialize_float(row.get("flow_net_amount"))
    main_flow_million = (flow_amount / 1_000_000) if flow_amount is not None else None
    profit_growth_value = _serialize_float(row.get("mdd_profit_growth_proxy"))
    if profit_growth_value is not None and -1.0 <= profit_growth_value <= 1.0:
        profit_growth_value *= 100.0
    volume_ratio_5_value = _serialize_float(row.get("volume_ratio_5")) or 0.0
    db_volume_trend_3d = row.get("volume_trend_3d")
    if db_volume_trend_3d is None:
        volume_trend_3d_flag = volume_ratio_5_value >= 1.2
    else:
        volume_trend_3d_flag = bool(db_volume_trend_3d)
    volume_trend_5d_flag = volume_ratio_5_value >= 1.5

    # 连板天数：直接读取数据库中的原始字段，由后期数据库维护确保准确性。
    limit_up_days = _serialize_int(row.get("consecutive_limit_up_days")) or 0

    return {
        "key": f"{run_id}:{symbol}",
        "modelId": row.get("model_id"),
        "runId": run_id,
        "rank": _serialize_int(row.get("display_rank")) or _serialize_int(row.get("score_rank")) or 0,
        "code": symbol,
        "name": _resolve_stock_name(symbol, row.get("stock_name")),
        "score": _serialize_float(row.get("fusion_score")) or 0.0,
        "signal": (row.get("signal_side") or "BUY").upper(),
        # stock_daily_latest.pct_change 当前已是"百分比数值"（如 -0.659 表示 -0.659%），
        # 这里直接透传，避免被 _normalize_pct_value 二次放大到 -65.9%。
        "latestChange": latest_change_value,
        # 次日收益：如果存在下一交易日数据则返回，否则返回 null
        "nextDayReturn": _serialize_float(row.get("next_day_return")),
        # 3日收益：如果存在第3个交易日数据则返回，否则返回 null
        "day3Return": _serialize_float(row.get("day3_return")),
        "consecutiveLimitUpDays": limit_up_days,
        "volumeTrend3d": volume_trend_3d_flag,
        "volumeTrend5d": volume_trend_5d_flag,
        "turnoverRate": _serialize_float(row.get("turnover_rate")) or 0.0,
        # 统一成交额/市值口径为“亿元”，避免前后端单位错配导致候选被误过滤。
        "amount": round(_normalize_amount_to_yi(row.get("amount")), 4),
        "marketCap": round(_normalize_market_cap_to_yi(row.get("total_mv")), 2),
        "sector": row.get("industry") or "",
        "concept": " / ".join(concept_tags[:3]),
        "conceptTags": concept_tags,
        "indexTags": index_tags,
        "confidence": row.get("confidence_level") or "watch",
        "hitReasons": hit_reasons,
        "riskFlags": risk_flags,
        "closePrice": _serialize_float(row.get("close_price")) or 0.0,
        "pe": pe_value,
        "roe": round((_serialize_float(row.get("roe")) or 0.0) * 100, 2),  # 转为百分比
        "rsi": rsi_value,
        "maGap5": ma_gap_5_value,
        "maGap20": _serialize_float(row.get("ma_gap_20")) or 0.0,
        "volRatio5": volume_ratio_5_value,
        "return1d": return_1d_value,
        "profitGrowth": profit_growth_value,
        "atr": _serialize_float(row.get("atr")) or 0.0,
        "macdHist": _serialize_float(row.get("macd_hist")) or 0.0,
        "kdjJ": _serialize_float(row.get("kdj_j")) or 0.0,
        "isHs300": bool(row.get("is_hs300")),
        "isCsi500": bool(row.get("is_csi500")),
        "isCsi1000": bool(row.get("is_csi1000")),
        "isSt": bool(row.get("is_st")),
        "isTradable": bool(row.get("tradable_flag")),
        "mainFlow": main_flow_million,
        "instOwnership": _serialize_float(row.get("inst_ownership")) or 0.0,
        "buyVol": (_serialize_float(row.get("lrg_buy_vol")) or 0.0) / 1_000_000,
        "sellVol": (_serialize_float(row.get("lrg_sell_vol")) or 0.0) / 1_000_000,
        "volumeBars": [],
        "thesis": row.get("thesis_summary") or "",
        "expectedPrice": _serialize_float(row.get("expected_price")),
        "marketType": row.get("market_type"),
        "province": row.get("province"),
        "city": row.get("city"),
        "return5d": _serialize_float(row.get("return_5d")),
        "return10d": _serialize_float(row.get("return_10d")),
        "ma5": _serialize_float(row.get("ma5")),
        "ma10": _serialize_float(row.get("ma10")),
        "ma20": _serialize_float(row.get("ma20")),
        "listedDays": _serialize_int(row.get("listed_days")),
        "updatedAt": _serialize_date(row.get("updated_at")),
    }


async def _fetch_models(session: Any, tenant_id: str, user_id: str) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            """
            SELECT
              REPLACE(snap.model_id, 'script_v1_', '') AS model_id,
              COALESCE(
                NULLIF(TRIM(um.metadata_json->>'display_name'), ''),
                NULLIF(TRIM(um.metadata_json->>'model_name'), '')
              ) AS model_display_name,
              COUNT(DISTINCT run_id) AS run_count,
              MAX(prediction_trade_date) AS latest_prediction_trade_date,
              MAX(snap.updated_at) AS last_updated_at
            FROM qm_research_candidate_snapshot snap
            LEFT JOIN qm_user_models um
              ON um.tenant_id = snap.tenant_id
             AND um.user_id = snap.user_id
             AND um.model_id = REPLACE(snap.model_id, 'script_v1_', '')
            WHERE snap.tenant_id = :tenant_id
              AND snap.user_id = :user_id
            GROUP BY REPLACE(snap.model_id, 'script_v1_', ''), model_display_name
            ORDER BY MAX(snap.updated_at) DESC NULLS LAST, MAX(snap.prediction_trade_date) DESC NULLS LAST, model_id ASC
            LIMIT :limit
            """
        ),
        {"tenant_id": tenant_id, "user_id": user_id, "limit": _MODEL_LIMIT},
    )
    return [dict(row) for row in result.mappings().all()]


async def _fetch_runs(session: Any, tenant_id: str, user_id: str, model_id: str) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            """
            SELECT
              run_id,
              model_id,
              MAX(data_trade_date) AS inference_date,
              MAX(prediction_trade_date) AS prediction_trade_date,
              MAX(COALESCE(universe_tag, '默认候选池')) AS universe_label,
              COUNT(*) AS stock_count,
              AVG(COALESCE(fusion_score, 0)) AS avg_score,
              MAX(updated_at) AS last_updated_at
            FROM qm_research_candidate_snapshot
            WHERE tenant_id = :tenant_id
              AND user_id = :user_id
              AND model_id = :model_id
            GROUP BY run_id, model_id
            ORDER BY MAX(prediction_trade_date) DESC NULLS LAST, MAX(updated_at) DESC NULLS LAST, run_id DESC
            LIMIT :limit
            """
        ),
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "model_id": model_id,
            "limit": _RUN_LIMIT,
        },
    )
    return [dict(row) for row in result.mappings().all()]


async def _fetch_summary(session: Any, where_sql: str, params: dict[str, Any]) -> dict[str, Any]:
    result = await session.execute(
        text(
            f"""
            WITH base AS (
              SELECT snap.*,
                {_MDD_SELECT},
                {_SDL_SELECT},
                {_SDL_SELECT_LATEST},
                {_SDL_SELECT_NEXT_DAY},
                {_SDL_SELECT_DAY3},
                {_SDL_FALLBACK}
              FROM qm_research_candidate_snapshot snap
              {_MDD_JOIN}
              {_SDL_JOIN}
              {_SDL_JOIN_LATEST}
              {_SDL_JOIN_DATA_DAY}
              {_SDL_JOIN_NEXT_DAY}
              {_SDL_JOIN_DAY3}
            ),
            filtered AS (
              SELECT *
              FROM base
              WHERE {where_sql}
            )
            SELECT
              COUNT(*) AS total_count,
              AVG(COALESCE(fusion_score, 0)) AS avg_score,
              COUNT(*) FILTER (WHERE COALESCE(confidence_level, 'watch') = 'high') AS high_confidence_count,
              COUNT(*) FILTER (WHERE COALESCE(fusion_score, 0) >= 0.05) AS strong_count,
              MAX(updated_at) AS last_updated_at
            FROM filtered
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
        "avgScore": _serialize_float(payload.get("avg_score")) or 0.0,
        "highConfidenceCount": _serialize_int(payload.get("high_confidence_count")) or 0,
        "strongCount": _serialize_int(payload.get("strong_count")) or 0,
        "lastUpdatedAt": _serialize_date(payload.get("last_updated_at")),
    }


async def _fetch_candidates(
    session: Any,
    where_sql: str,
    params: dict[str, Any],
    *,
    sort_by: str,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    order_sql = _SORT_SQL_MAP.get(sort_by, _SORT_SQL_MAP["score"])
    query_params = dict(params)
    query_params.update({"limit": limit, "offset": offset})

    result = await session.execute(
        text(
            f"""
            WITH base AS (
              SELECT snap.*,
                {_MDD_SELECT},
                {_SDL_SELECT},
                {_SDL_SELECT_LATEST},
                {_SDL_SELECT_NEXT_DAY},
                {_SDL_SELECT_DAY3},
                {_SDL_FALLBACK}
              FROM qm_research_candidate_snapshot snap
              {_MDD_JOIN}
              {_SDL_JOIN}
              {_SDL_JOIN_LATEST}
              {_SDL_JOIN_DATA_DAY}
              {_SDL_JOIN_NEXT_DAY}
              {_SDL_JOIN_DAY3}
            ),
            filtered AS (
              SELECT *
              FROM base
              WHERE {where_sql}
            ),
            ranked AS (
              SELECT
                filtered.*,
                (ROW_NUMBER() OVER (ORDER BY {order_sql}) + :offset)::int AS display_rank
              FROM filtered
            )
            SELECT *
            FROM ranked
            ORDER BY display_rank
            LIMIT :limit
            """
        ),
        query_params,
    )
    return [dict(row) for row in result.mappings().all()]


async def _fetch_sector_facets(session: Any, tenant_id: str, user_id: str, run_id: str) -> list[str]:
    result = await session.execute(
        text(
            f"""
            SELECT COALESCE(sdl.industry, '') AS industry
            FROM qm_research_candidate_snapshot snap
            LEFT JOIN stock_daily_latest sdl ON {_SDL_SYMBOL_MATCH}
            WHERE snap.tenant_id = :tenant_id
              AND snap.user_id = :user_id
              AND snap.run_id = :run_id
              AND COALESCE(sdl.industry, '') <> ''
            GROUP BY sdl.industry
            ORDER BY COUNT(*) DESC, sdl.industry ASC
            LIMIT 40
            """
        ),
        {"tenant_id": tenant_id, "user_id": user_id, "run_id": run_id},
    )
    return [str(row[0]) for row in result.fetchall()]


async def _fetch_concept_facets(session: Any, tenant_id: str, user_id: str, run_id: str) -> list[str]:
    result = await session.execute(
        text(
            f"""
            SELECT concept.value AS concept_name, COUNT(*) AS cnt
            FROM qm_research_candidate_snapshot snap
            LEFT JOIN LATERAL (
              SELECT sdl.* FROM stock_daily_latest sdl
              WHERE {_SDL_SYMBOL_MATCH}
                AND sdl.trade_date <= snap.prediction_trade_date
              ORDER BY sdl.trade_date DESC
              LIMIT 1
            ) sdl ON true
            CROSS JOIN LATERAL jsonb_array_elements_text(
              COALESCE(
                to_jsonb(array_remove(ARRAY[
                  CASE WHEN COALESCE(sdl.concept_ai, 0) <> 0 THEN 'AI' END,
                  CASE WHEN COALESCE(sdl.concept_chip, 0) <> 0 THEN '芯片' END,
                  CASE WHEN COALESCE(sdl.concept_new_energy, 0) <> 0 THEN '新能源' END,
                  CASE WHEN COALESCE(sdl.concept_ev, 0) <> 0 THEN '电动车' END,
                  CASE WHEN COALESCE(sdl.concept_pv, 0) <> 0 THEN '光伏' END,
                  CASE WHEN COALESCE(sdl.concept_lithium, 0) <> 0 THEN '锂电' END,
                  CASE WHEN COALESCE(sdl.concept_semiconductor, 0) <> 0 THEN '半导体' END,
                  CASE WHEN COALESCE(sdl.concept_military, 0) <> 0 THEN '军工' END,
                  CASE WHEN COALESCE(sdl.concept_medical, 0) <> 0 THEN '医药' END,
                  CASE WHEN COALESCE(sdl.concept_cyber, 0) <> 0 THEN '网络安全' END,
                  CASE WHEN COALESCE(sdl.concept_fintech, 0) <> 0 THEN '金融科技' END,
                  CASE WHEN COALESCE(sdl.concept_consumption, 0) <> 0 THEN '消费' END,
                  CASE WHEN COALESCE(sdl.concept_real_estate, 0) <> 0 THEN '地产' END,
                  CASE WHEN COALESCE(sdl.concept_infrastructure, 0) <> 0 THEN '基建' END,
                  CASE WHEN COALESCE(sdl.concept_state_owned, 0) <> 0 THEN '国企改革' END
                ]::text[], NULL)),
                '[]'::jsonb
              )
            ) concept(value)
            WHERE snap.tenant_id = :tenant_id
              AND snap.user_id = :user_id
              AND snap.run_id = :run_id
            GROUP BY concept.value
            ORDER BY cnt DESC, concept.value ASC
            LIMIT 80
            """
        ),
        {"tenant_id": tenant_id, "user_id": user_id, "run_id": run_id},
    )
    return [str(row[0]) for row in result.fetchall()]


async def _fetch_index_facets(session: Any, tenant_id: str, user_id: str, run_id: str) -> list[str]:
    result = await session.execute(
        text(
            f"""
            SELECT idx.value AS index_name, COUNT(*) AS cnt
            FROM qm_research_candidate_snapshot snap
            LEFT JOIN LATERAL (
              SELECT sdl.* FROM stock_daily_latest sdl
              WHERE {_SDL_SYMBOL_MATCH}
                AND sdl.trade_date <= snap.prediction_trade_date
              ORDER BY sdl.trade_date DESC
              LIMIT 1
            ) sdl ON true
            CROSS JOIN LATERAL jsonb_array_elements_text(
              COALESCE(
                to_jsonb(array_remove(ARRAY[
                  CASE WHEN COALESCE(sdl.idx_hs300, 0) <> 0 THEN '沪深300' END,
                  CASE WHEN COALESCE(sdl.idx_zz500, 0) <> 0 THEN '中证500' END,
                  CASE WHEN COALESCE(sdl.idx_zz1000, 0) <> 0 THEN '中证1000' END,
                  CASE WHEN COALESCE(sdl.idx_chinext, 0) <> 0 THEN '创业板指数' END,
                  CASE WHEN COALESCE(sdl.idx_margin, 0) <> 0 THEN '两融标的' END,
                  CASE WHEN COALESCE(sdl.idx_all, 0) <> 0 THEN '全市场' END
                ]::text[], NULL)),
                '[]'::jsonb
              )
            ) idx(value)
            WHERE snap.tenant_id = :tenant_id
              AND snap.user_id = :user_id
              AND snap.run_id = :run_id
            GROUP BY idx.value
            ORDER BY cnt DESC, idx.value ASC
            LIMIT 20
            """
        ),
        {"tenant_id": tenant_id, "user_id": user_id, "run_id": run_id},
    )
    return [str(row[0]) for row in result.fetchall()]


@router.get("/overview")
async def get_research_overview(
    model_id: str | None = Query(None, description="指定模型 ID，不传时默认取最新模型"),
    run_id: str | None = Query(None, description="指定推理批次 ID，不传时默认取最新批次"),
    keyword: str | None = Query(None, description="股票代码、名称、行业、概念关键词"),
    min_score: float | None = Query(None, ge=0, description="最低融合分数"),
    min_consecutive_limit_up_days: int = Query(0, ge=0, le=20, description="最少连板/连涨天数"),
    min_turnover_rate: float | None = Query(None, ge=0, description="最小换手率"),
    max_turnover_rate: float | None = Query(None, ge=0, description="最大换手率"),
    min_amount: float | None = Query(None, ge=0, description="最小成交额"),
    max_amount: float | None = Query(None, ge=0, description="最大成交额"),
    volume_trend_only: bool = Query(False, description="仅保留 3 日量能增强标的"),
    high_confidence_only: bool = Query(False, description="仅保留高置信标的"),
    sectors: list[str] = Query(default_factory=list, description="行业过滤，可重复传参"),
    concepts: list[str] = Query(default_factory=list, description="概念过滤，可重复传参"),
    indices: list[str] = Query(default_factory=list, description="指数过滤，可重复传参"),
    sort_by: str = Query("score", description="排序字段：score/latest_change/amount/turnover_rate/consecutive_limit_up_days/updated_at"),
    limit: int = Query(80, ge=1, le=1000, description="候选列表返回数量"),
    offset: int = Query(0, ge=0, description="候选列表偏移量"),
    exclude_st: bool = Query(False, description="是否剔除 ST/退市标的"),
    current_user: dict = Depends(get_current_user),
):
    tenant_id = str(current_user["tenant_id"])
    user_id = str(current_user["user_id"])
    model_id = _normalize_model_id(model_id)
    cache_service = None
    cache_key = _build_overview_cache_key(
        tenant_id=tenant_id,
        user_id=user_id,
        model_id=model_id,
        run_id=run_id,
        keyword=keyword,
        min_score=min_score,
        min_consecutive_limit_up_days=min_consecutive_limit_up_days,
        min_turnover_rate=min_turnover_rate,
        max_turnover_rate=max_turnover_rate,
        min_amount=min_amount,
        max_amount=max_amount,
        volume_trend_only=volume_trend_only,
        high_confidence_only=high_confidence_only,
        sectors=sectors,
        concepts=concepts,
        indices=indices,
        sort_by=sort_by,
        limit=limit,
        offset=offset,
    )
    try:
        cache_service = get_cache_service()
        cached_payload = cache_service.get_json(cache_key)
        if cached_payload is not None:
            return cached_payload
    except Exception:
        cache_service = None

    async with get_session(read_only=True) as session:
        model_rows = await _fetch_models(session, tenant_id=tenant_id, user_id=user_id)
        models = [_format_model_record(row) for row in model_rows]

        if not models:
            response_payload = {
                "code": 200,
                "message": "success",
                "data": {
                    "activeModelId": None,
                    "activeRunId": None,
                    "models": [],
                    "runs": [],
                    "summary": {
                        "total": 0,
                        "avgScore": 0.0,
                        "highConfidenceCount": 0,
                        "strongCount": 0,
                        "lastUpdatedAt": None,
                    },
                    "filters": {"sectors": [], "concepts": [], "indices": []},
                    "items": [],
                },
            }
            if cache_service is not None:
                try:
                    cache_service.set_json(cache_key, response_payload, ttl=_OVERVIEW_CACHE_TTL_SECONDS)
                except Exception:
                    pass
            return response_payload

        model_ids = {item["modelId"] for item in models}

        # 如果指定了 run_id，先查找对应的 model_id
        if run_id:
            run_model_result = await session.execute(
                text(
                    """
                    SELECT model_id FROM qm_research_candidate_snapshot
                    WHERE tenant_id = :tenant_id
                      AND user_id = :user_id
                      AND run_id = :run_id
                    LIMIT 1
                    """
                ),
                {"tenant_id": tenant_id, "user_id": user_id, "run_id": run_id},
            )
            run_model_row = run_model_result.mappings().first()
            if run_model_row:
                active_model_id = run_model_row["model_id"]
            else:
                raise HTTPException(status_code=404, detail=f"研究批次不存在: {run_id}")
        else:
            active_model_id = model_id or models[0]["modelId"]

        if active_model_id not in model_ids:
            raise HTTPException(status_code=404, detail=f"研究模型不存在: {active_model_id}")

        run_rows = await _fetch_runs(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            model_id=active_model_id,
        )
        runs = [_format_run_record(row) for row in run_rows]
        if not runs:
            response_payload = {
                "code": 200,
                "message": "success",
                "data": {
                    "activeModelId": active_model_id,
                    "activeRunId": None,
                    "models": models,
                    "runs": [],
                    "summary": {
                        "total": 0,
                        "avgScore": 0.0,
                        "highConfidenceCount": 0,
                        "strongCount": 0,
                        "lastUpdatedAt": None,
                    },
                    "filters": {"sectors": [], "concepts": [], "indices": []},
                    "items": [],
                },
            }
            if cache_service is not None:
                try:
                    cache_service.set_json(cache_key, response_payload, ttl=_OVERVIEW_CACHE_TTL_SECONDS)
                except Exception:
                    pass
            return response_payload

        run_ids = {item["runId"] for item in runs}
        active_run_id = run_id or runs[0]["runId"]
        if active_run_id not in run_ids:
            raise HTTPException(status_code=404, detail=f"研究批次不存在: {active_run_id}")

        where_sql, params = _build_candidate_filter_sql(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=active_run_id,
            keyword=keyword,
            min_score=min_score,
            min_consecutive_limit_up_days=min_consecutive_limit_up_days,
            min_turnover_rate=min_turnover_rate,
            max_turnover_rate=max_turnover_rate,
            min_amount=min_amount,
            max_amount=max_amount,
            volume_trend_only=volume_trend_only,
            high_confidence_only=high_confidence_only,
            sectors=sectors,
            concepts=concepts,
            indices=indices,
            exclude_st=exclude_st,
        )

        summary = await _fetch_summary(session, where_sql, params)
        candidate_rows = await _fetch_candidates(
            session,
            where_sql,
            params,
            sort_by=sort_by,
            limit=limit,
            offset=offset,
        )
        sector_facets = await _fetch_sector_facets(session, tenant_id, user_id, active_run_id)
        concept_facets = await _fetch_concept_facets(session, tenant_id, user_id, active_run_id)
        index_facets = await _fetch_index_facets(session, tenant_id, user_id, active_run_id)

    response_payload = {
        "code": 200,
        "message": "success",
        "data": {
            "activeModelId": active_model_id,
            "activeRunId": active_run_id,
            "models": models,
            "runs": runs,
            "summary": summary,
            "filters": {
                "sectors": sector_facets,
                "concepts": concept_facets,
                "indices": index_facets,
            },
            "items": [_format_candidate_record(row) for row in candidate_rows],
            "pagination": {
                "limit": limit,
                "offset": offset,
                "returned": len(candidate_rows),
                "total": summary.get("total", 0),
                "hasMore": offset + len(candidate_rows) < (summary.get("total", 0) or 0),
            },
        },
    }
    if cache_service is not None:
        try:
            cache_service.set_json(cache_key, response_payload, ttl=_OVERVIEW_CACHE_TTL_SECONDS)
        except Exception:
            pass
    return response_payload


@router.get("/models")
async def get_research_models(
    current_user: dict = Depends(get_current_user),
):
    tenant_id = str(current_user["tenant_id"])
    user_id = str(current_user["user_id"])
    async with get_session(read_only=True) as session:
        model_rows = await _fetch_models(session, tenant_id=tenant_id, user_id=user_id)
    return {
        "code": 200,
        "message": "success",
        "data": {
            "models": [_format_model_record(row) for row in model_rows],
        },
    }


@router.get("/runs")
async def get_research_runs(
    model_id: str = Query(..., description="模型ID"),
    current_user: dict = Depends(get_current_user),
):
    tenant_id = str(current_user["tenant_id"])
    user_id = str(current_user["user_id"])
    model_id = _normalize_model_id(model_id)
    async with get_session(read_only=True) as session:
        run_rows = await _fetch_runs(session, tenant_id=tenant_id, user_id=user_id, model_id=model_id)
    return {
        "code": 200,
        "message": "success",
        "data": {
            "runs": [_format_run_record(row) for row in run_rows],
        },
    }


@router.get("/universe")
async def get_research_universe(
    run_id: str = Query(..., description="研究批次ID"),
    keyword: str | None = Query(None, description="股票代码、名称、行业、概念关键词"),
    min_score: float | None = Query(None, ge=0, description="最低融合分数"),
    limit: int = Query(100, ge=1, le=1000, description="候选列表返回数量"),
    offset: int = Query(0, ge=0, description="候选列表偏移量"),
    sort_by: str = Query("score", description="排序字段"),
    exclude_st: bool = Query(False, description="是否剔除 ST/退市标的"),
    current_user: dict = Depends(get_current_user),
):
    tenant_id = str(current_user["tenant_id"])
    user_id = str(current_user["user_id"])

    where_sql, params = _build_candidate_filter_sql(
        tenant_id=tenant_id,
        user_id=user_id,
        run_id=run_id,
        keyword=keyword,
        min_score=min_score,
        min_consecutive_limit_up_days=0,
        min_turnover_rate=None,
        max_turnover_rate=None,
        min_amount=None,
        max_amount=None,
        volume_trend_only=False,
        high_confidence_only=False,
        sectors=[],
        concepts=[],
        indices=[],
        exclude_st=exclude_st,
    )

    async with get_session(read_only=True) as session:
        summary = await _fetch_summary(session, where_sql, params)
        candidate_rows = await _fetch_candidates(
            session,
            where_sql,
            params,
            sort_by=sort_by,
            limit=limit,
            offset=offset,
        )

    return {
        "code": 200,
        "message": "success",
        "data": {
            "runId": run_id,
            "summary": summary,
            "items": [_format_candidate_record(row) for row in candidate_rows],
            "pagination": {
                "limit": limit,
                "offset": offset,
                "returned": len(candidate_rows),
                "total": summary.get("total", 0),
                "hasMore": offset + len(candidate_rows) < (summary.get("total", 0) or 0),
            },
        },
    }


@router.get("/candidates/{symbol}")
async def get_research_candidate_detail(
    symbol: str,
    run_id: str = Query(..., description="所属研究批次 ID"),
    current_user: dict = Depends(get_current_user),
):
    tenant_id = str(current_user["tenant_id"])
    user_id = str(current_user["user_id"])

    async with get_session(read_only=True) as session:
        result = await session.execute(
            text(
                f"""
                SELECT snap.*,
                  {_MDD_SELECT},
                  {_SDL_SELECT},
                  {_SDL_SELECT_LATEST},
                  {_SDL_SELECT_NEXT_DAY},
                  {_SDL_FALLBACK}
                FROM qm_research_candidate_snapshot snap
                {_MDD_JOIN}
                {_SDL_JOIN}
                {_SDL_JOIN_LATEST}
                {_SDL_JOIN_NEXT_DAY}
                WHERE snap.tenant_id = :tenant_id
                  AND snap.user_id = :user_id
                  AND snap.run_id = :run_id
                  AND snap.symbol = :symbol
                LIMIT 1
                """
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "run_id": run_id,
                "symbol": StockCodeUtil.to_prefix(symbol),
            },
        )
        row = result.mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail=f"研究候选不存在: {symbol}")

    return {
        "code": 200,
        "message": "success",
        "data": _format_candidate_record(dict(row)),
    }


# ============ Pydantic 请求模型 ============

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


class SymbolsFeaturesRequest(BaseModel):
    symbols: list[str]


# ============ 建表保障 ============

async def ensure_research_tables():
    async with get_session() as session:
        await session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS qm_user_watchlist (
                    tenant_id VARCHAR(64) NOT NULL,
                    user_id VARCHAR(64) NOT NULL,
                    symbol VARCHAR(20) NOT NULL,
                    stock_name VARCHAR(128),
                    source_run_id VARCHAR(64),
                    features_snapshot JSONB,
                    notes TEXT,
                    tags JSONB,
                    added_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
                    PRIMARY KEY (tenant_id, user_id, symbol)
                )
                """
            )
        )
        await session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS qm_user_research_pool (
                    tenant_id VARCHAR(64) NOT NULL,
                    user_id VARCHAR(64) NOT NULL,
                    symbol VARCHAR(20) NOT NULL,
                    stock_name VARCHAR(128),
                    source_run_id VARCHAR(64),
                    model_id VARCHAR(128),
                    fusion_score DOUBLE PRECISION,
                    thesis_summary TEXT,
                    status VARCHAR(32) DEFAULT 'pending',
                    features_snapshot JSONB,
                    notes TEXT,
                    tags JSONB,
                    added_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
                    PRIMARY KEY (tenant_id, user_id, symbol)
                )
                """
            )
        )
        await session.commit()


# ============ 批量股票特征接口 ============

@router.post("/symbols/features")
async def get_symbols_features(
    req: SymbolsFeaturesRequest,
    current_user: dict = Depends(get_current_user),
):
    tid = str(current_user["tenant_id"])
    uid = str(current_user["user_id"])
    symbols = [StockCodeUtil.to_prefix(s.strip()) for s in req.symbols if s.strip()]
    if not symbols:
        return {"code": 200, "data": {"items": []}}

    vals = ", ".join(f"('{s}')" for s in symbols)
    sql = f"""
        WITH sym_list(raw_symbol) AS (VALUES {vals}),
        pool_snap AS (
            SELECT symbol, features_snapshot
            FROM qm_user_research_pool
            WHERE tenant_id = :tid AND user_id = :uid
              AND symbol IN (SELECT raw_symbol FROM sym_list)
        ),
        watchlist_snap AS (
            SELECT symbol, features_snapshot
            FROM qm_user_watchlist
            WHERE tenant_id = :tid AND user_id = :uid
              AND symbol IN (SELECT raw_symbol FROM sym_list)
        ),
        snap AS (
            SELECT
                sym_list.raw_symbol AS symbol,
                COALESCE(s.run_id, 'history') AS run_id,
                COALESCE(
                    s.fusion_score,
                    (ps.features_snapshot->>'score')::double precision,
                    (ws.features_snapshot->>'score')::double precision
                ) AS fusion_score,
                COALESCE(
                    s.risk_flags,
                    (ps.features_snapshot->'riskFlags')::jsonb,
                    (ws.features_snapshot->'riskFlags')::jsonb
                ) AS risk_flags,
                COALESCE(
                    s.thesis_summary,
                    (ps.features_snapshot->>'thesis')::text,
                    (ws.features_snapshot->>'thesis')::text
                ) AS thesis_summary,
                COALESCE(s.confidence_level, 'watch') AS confidence_level,
                COALESCE(s.model_id, '') AS model_id,
                0 AS score_rank,
                0 AS consecutive_limit_up_days,
                ROW_NUMBER() OVER (
                    PARTITION BY sym_list.raw_symbol
                    ORDER BY s.prediction_trade_date DESC NULLS LAST
                ) AS rn
            FROM sym_list
            LEFT JOIN qm_user_research_pool ps
                ON ps.tenant_id = :tid AND ps.user_id = :uid
                AND ps.symbol = sym_list.raw_symbol
            LEFT JOIN qm_user_watchlist ws
                ON ws.tenant_id = :tid AND ws.user_id = :uid
                AND ws.symbol = sym_list.raw_symbol
            LEFT JOIN qm_research_candidate_snapshot s
                ON s.symbol = sym_list.raw_symbol
                AND s.tenant_id = :tid AND s.user_id = :uid
        )
        SELECT snap.*, {_SDL_SELECT},
               COALESCE(sdl.pct_change, 0) AS latest_change_pct
        FROM snap
        LEFT JOIN LATERAL (
            SELECT sdl.* FROM stock_daily_latest sdl
            WHERE sdl.symbol = UPPER(snap.symbol)
            ORDER BY sdl.trade_date DESC
            LIMIT 1
        ) sdl ON true
        WHERE snap.rn = 1
    """
    async with get_session(read_only=True) as session:
        result = await session.execute(text(sql), {"tid": tid, "uid": uid})
        items = [_format_candidate_record(dict(r)) for r in result.mappings()]
    return {"code": 200, "data": {"items": items}}


# ============ 用户自选接口 ============

@router.get("/watchlist")
async def get_user_watchlist(
    limit: int = Query(50, ge=1, le=200, description="返回数量"),
    offset: int = Query(0, ge=0, description="偏移量"),
    current_user: dict = Depends(get_current_user),
):
    """获取用户自选列表"""
    tenant_id = str(current_user["tenant_id"])
    user_id = str(current_user["user_id"])

    async with get_session(read_only=True) as session:
        result = await session.execute(
            text(
                """
                SELECT symbol, stock_name, added_at, source_run_id, notes, tags
                FROM qm_user_watchlist
                WHERE tenant_id = :tenant_id AND user_id = :user_id
                ORDER BY added_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"tenant_id": tenant_id, "user_id": user_id, "limit": limit, "offset": offset},
        )
        rows = [dict(row) for row in result.mappings().all()]

        count_result = await session.execute(
            text(
                "SELECT COUNT(*) AS total FROM qm_user_watchlist WHERE tenant_id = :tenant_id AND user_id = :user_id"
            ),
            {"tenant_id": tenant_id, "user_id": user_id},
        )
        total = count_result.mappings().first()["total"]

    return {
        "code": 200,
        "message": "success",
        "data": {
            "items": [
                {
                    "symbol": row["symbol"],
                    "stockName": row["stock_name"],
                    "addedAt": _serialize_date(row["added_at"]),
                    "sourceRunId": row["source_run_id"],
                    "notes": row["notes"],
                    "tags": _normalize_text_list(row["tags"]),
                }
                for row in rows
            ],
            "total": total,
        },
    }


@router.post("/watchlist/{symbol}")
async def add_to_watchlist(
    symbol: str,
    req: WatchlistAddRequest = Body(default_factory=WatchlistAddRequest),
    current_user: dict = Depends(get_current_user),
):
    """添加股票到自选"""
    tenant_id = str(current_user["tenant_id"])
    user_id = str(current_user["user_id"])

    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO qm_user_watchlist
                    (tenant_id, user_id, symbol, stock_name, source_run_id, features_snapshot, updated_at)
                VALUES
                    (:tenant_id, :user_id, :symbol, :stock_name, :run_id, :features_snapshot, NOW())
                ON CONFLICT (tenant_id, user_id, symbol) DO UPDATE SET
                  stock_name = COALESCE(EXCLUDED.stock_name, qm_user_watchlist.stock_name),
                  source_run_id = COALESCE(EXCLUDED.source_run_id, qm_user_watchlist.source_run_id),
                  features_snapshot = COALESCE(EXCLUDED.features_snapshot, qm_user_watchlist.features_snapshot),
                  updated_at = NOW()
                """
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "symbol": symbol,
                "stock_name": req.stock_name,
                "run_id": req.run_id,
                "features_snapshot": json.dumps(req.features_snapshot) if req.features_snapshot else None,
            },
        )

    return {"code": 200, "message": "已加入自选", "data": {"symbol": symbol}}


@router.delete("/watchlist/{symbol}")
async def remove_from_watchlist(
    symbol: str,
    current_user: dict = Depends(get_current_user),
):
    """从自选移除股票"""
    tenant_id = str(current_user["tenant_id"])
    user_id = str(current_user["user_id"])

    async with get_session() as session:
        result = await session.execute(
            text(
                "DELETE FROM qm_user_watchlist WHERE tenant_id = :tenant_id AND user_id = :user_id AND symbol = :symbol"
            ),
            {"tenant_id": tenant_id, "user_id": user_id, "symbol": symbol},
        )

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"自选中不存在: {symbol}")

    return {"code": 200, "message": "已从自选移除", "data": {"symbol": symbol}}


# ============ 用户研究池接口 ============

@router.get("/pool")
async def get_user_research_pool(
    status: str | None = Query(None, description="状态过滤: pending/confirmed/rejected"),
    limit: int = Query(50, ge=1, le=200, description="返回数量"),
    offset: int = Query(0, ge=0, description="偏移量"),
    current_user: dict = Depends(get_current_user),
):
    """获取用户研究池列表"""
    tenant_id = str(current_user["tenant_id"])
    user_id = str(current_user["user_id"])

    where_clauses = ["tenant_id = :tenant_id", "user_id = :user_id"]
    params: dict[str, Any] = {"tenant_id": tenant_id, "user_id": user_id}

    if status:
        params["status"] = status
        where_clauses.append("status = :status")

    where_sql = " AND ".join(where_clauses)

    async with get_session(read_only=True) as session:
        result = await session.execute(
            text(
                f"""
                SELECT symbol, stock_name, added_at, source_run_id, model_id, fusion_score,
                       thesis_summary, status, notes, tags
                FROM qm_user_research_pool
                WHERE {where_sql}
                ORDER BY added_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {**params, "limit": limit, "offset": offset},
        )
        rows = [dict(row) for row in result.mappings().all()]

        count_result = await session.execute(
            text(f"SELECT COUNT(*) AS total FROM qm_user_research_pool WHERE {where_sql}"),
            params,
        )
        total = count_result.mappings().first()["total"]

    return {
        "code": 200,
        "message": "success",
        "data": {
            "items": [
                {
                    "symbol": row["symbol"],
                    "stockName": row["stock_name"],
                    "addedAt": _serialize_date(row["added_at"]),
                    "sourceRunId": row["source_run_id"],
                    "modelId": row["model_id"],
                    "fusionScore": _serialize_float(row["fusion_score"]),
                    "thesisSummary": row["thesis_summary"],
                    "status": row["status"],
                    "notes": row["notes"],
                    "tags": _normalize_text_list(row["tags"]),
                }
                for row in rows
            ],
            "total": total,
        },
    }


@router.post("/pool/{symbol}")
async def add_to_research_pool(
    symbol: str,
    req: PoolAddRequest = Body(default_factory=PoolAddRequest),
    current_user: dict = Depends(get_current_user),
):
    """添加股票到研究池"""
    tenant_id = str(current_user["tenant_id"])
    user_id = str(current_user["user_id"])
    model_id = _normalize_model_id(req.model_id)

    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO qm_user_research_pool
                  (tenant_id, user_id, symbol, stock_name, source_run_id, model_id,
                   fusion_score, thesis_summary, features_snapshot, updated_at)
                VALUES
                  (:tenant_id, :user_id, :symbol, :stock_name, :run_id, :model_id,
                   :fusion_score, :thesis_summary, :features_snapshot, NOW())
                ON CONFLICT (tenant_id, user_id, symbol) DO UPDATE SET
                  stock_name = COALESCE(EXCLUDED.stock_name, qm_user_research_pool.stock_name),
                  source_run_id = COALESCE(EXCLUDED.source_run_id, qm_user_research_pool.source_run_id),
                  model_id = COALESCE(EXCLUDED.model_id, qm_user_research_pool.model_id),
                  fusion_score = COALESCE(EXCLUDED.fusion_score, qm_user_research_pool.fusion_score),
                  thesis_summary = COALESCE(EXCLUDED.thesis_summary, qm_user_research_pool.thesis_summary),
                  features_snapshot = COALESCE(EXCLUDED.features_snapshot, qm_user_research_pool.features_snapshot),
                  updated_at = NOW()
                """
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "symbol": symbol,
                "stock_name": req.stock_name,
                "run_id": req.run_id,
                "model_id": model_id,
                "fusion_score": req.fusion_score,
                "thesis_summary": req.thesis_summary,
                "features_snapshot": json.dumps(req.features_snapshot) if req.features_snapshot else None,
            },
        )

    return {"code": 200, "message": "已加入研究池", "data": {"symbol": symbol}}


@router.delete("/pool/{symbol}")
async def remove_from_research_pool(
    symbol: str,
    current_user: dict = Depends(get_current_user),
):
    """从研究池移除股票"""
    tenant_id = str(current_user["tenant_id"])
    user_id = str(current_user["user_id"])

    async with get_session() as session:
        result = await session.execute(
            text(
                "DELETE FROM qm_user_research_pool WHERE tenant_id = :tenant_id AND user_id = :user_id AND symbol = :symbol"
            ),
            {"tenant_id": tenant_id, "user_id": user_id, "symbol": symbol},
        )

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"研究池中不存在: {symbol}")

    return {"code": 200, "message": "已从研究池移除", "data": {"symbol": symbol}}


# ============ K 线数据接口 ============

_KLINE_REDIS_PREFIX = "kline:"
_KLINE_REDIS_TTL = 3600  # 1 小时缓存
_KLINE_REDIS_CLIENT = None


def _get_kline_redis_client():
    """获取 K 线缓存专用的 Redis 客户端（使用 market Redis）。"""
    import os

    import redis

    global _KLINE_REDIS_CLIENT

    if _KLINE_REDIS_CLIENT is not None:
        return _KLINE_REDIS_CLIENT

    try:
        host = os.getenv("REDIS_MARKET_HOST", "localhost")
        port = int(os.getenv("REDIS_MARKET_PORT", "36379"))
        password = os.getenv("REDIS_MARKET_PASSWORD", None)
        db = int(os.getenv("REDIS_DB_MARKET", "3"))

        _KLINE_REDIS_CLIENT = redis.Redis(
            host=host,
            port=port,
            password=password,
            db=db,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
        )
        return _KLINE_REDIS_CLIENT
    except Exception:
        return None


def _strip_symbol_prefix(symbol: str) -> str:
    """移除股票代码的市场前缀。"""
    symbol = symbol.upper().strip()
    if symbol.startswith(("SH", "SZ")):
        return symbol[2:]
    return symbol


async def _get_kline_from_db(symbol: str, days: int) -> list[dict[str, Any]]:
    """从 stock_daily_latest 表读取 K 线数据。

    注意：stock_daily_latest 存储的是后复权价格。
    当 adj_factor 数据完整时，可计算前复权价格：前复权价 = 原价 × (当日复权因子 / 最新复权因子)
    """
    from datetime import date, timedelta

    # 统一使用 Prefix 格式进行查询
    query_symbol = StockCodeUtil.to_prefix(symbol)
    end_date = date.today()

    async with get_session(read_only=True) as session:
        # 先获取最新的复权因子作为基准
        result = await session.execute(
            text(
                """
                SELECT adj_factor FROM stock_daily_latest
                WHERE symbol = :symbol
                  AND adj_factor IS NOT NULL
                ORDER BY trade_date DESC
                LIMIT 1
                """
            ),
            {"symbol": query_symbol},
        )
        latest_row = result.mappings().first()
        latest_adj_factor = (latest_row.get("adj_factor") if latest_row else None) or 1.0

        # 获取 K 线数据：按日期倒序取最近的 limit 条，确保包含最新行情
        result = await session.execute(
            text(
                """
                SELECT trade_date, open, high, low, close, volume, adj_factor
                FROM stock_daily_latest
                WHERE symbol = :symbol
                  AND trade_date <= :end_date
                  AND open IS NOT NULL
                  AND high IS NOT NULL
                  AND low IS NOT NULL
                  AND close IS NOT NULL
                ORDER BY trade_date DESC
                LIMIT :limit
                """
            ),
            {
                "symbol": query_symbol,
                "end_date": end_date,
                "limit": days,
            },
        )
        # 获取结果并反转，保持时间正序返回给前端
        rows = [dict(row) for row in result.mappings().all()]
        rows.reverse()

    # 计算前复权价格：
    # 因为数据库 stock_daily_latest 已经存储的是后复权价格 (Post-Adjusted)，
    # 所以 前复权价 = 后复权价 / 最新复权因子 (这将价格平移到以今日为基准的量级，且保持走势平滑)
    return [
        {
            "date": str(row["trade_date"]),
            "open": round((_serialize_float(row["open"]) or 0.0) / latest_adj_factor, 2),
            "high": round((_serialize_float(row["high"]) or 0.0) / latest_adj_factor, 2),
            "low": round((_serialize_float(row["low"]) or 0.0) / latest_adj_factor, 2),
            "close": round((_serialize_float(row["close"]) or 0.0) / latest_adj_factor, 2),
            "volume": _serialize_float(row["volume"]) or 0.0,
        }
        for row in rows
    ]


async def _get_kline_from_snapshot(symbol: str, days: int) -> list[dict[str, Any]]:
    """从 stock_daily_latest 历史快照回退读取 K 线数据（已废弃，保留兼容）。"""
    return await _get_kline_from_db(symbol, days)


async def _get_kline_with_cache(symbol: str, days: int) -> list[dict[str, Any]]:
    """获取 K 线数据，优先从 Redis 缓存读取。"""
    import json

    cache_key = f"{_KLINE_REDIS_PREFIX}{symbol.upper()}:{days}"

    # 尝试从 Redis 读取
    try:
        redis_client = _get_kline_redis_client()
        if redis_client:
            cached = redis_client.get(cache_key)
            if cached:
                return json.loads(cached)
    except Exception:
        pass

    # 从数据库读取
    items = await _get_kline_from_db(symbol, days)
    if not items:
        items = await _get_kline_from_snapshot(symbol, days)

    # 写入 Redis 缓存
    try:
        redis_client = _get_kline_redis_client()
        if redis_client and items:
            redis_client.setex(cache_key, _KLINE_REDIS_TTL, json.dumps(items))
    except Exception:
        pass

    return items


@router.get("/kline/{symbol}")
async def get_stock_kline(
    symbol: str,
    days: int = Query(60, ge=10, le=365, description="返回天数"),
    current_user: dict = Depends(get_current_user),
):
    """获取股票 K 线数据。

    从 market_data_daily 表读取原始价格数据，
    使用 Redis 缓存近 60 天数据，缓存有效期 1 小时。
    """
    normalized_symbol = StockCodeUtil.to_prefix(symbol)
    items = await _get_kline_with_cache(normalized_symbol, days)

    return {
        "code": 200,
        "message": "success",
        "data": {
            "symbol": normalized_symbol,
            "items": items,
            "count": len(items),
        },
    }
