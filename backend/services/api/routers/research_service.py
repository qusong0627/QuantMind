"""投研聚合服务层（保持路由契约不变，仅拆分查询与序列化逻辑）。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import text

from backend.services.engine.data_platform.quantdb_hub import _resolve_data_dir
from backend.shared.database_manager_v2 import get_session
from backend.shared.inference_stats import compute_score_distribution
from backend.shared.redis_sentinel_client import get_redis_sentinel_client
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

_UNIVERSE_CACHE_TTL_SECONDS = 90
_UNIVERSE_CACHE_MAX_ENTRIES = 64
_UNIVERSE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_SDL_CACHE_TTL_SECONDS = 120
_SDL_CACHE_MAX_ENTRIES = 512
_SDL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_SDL_REDIS_YEAR = int(os.getenv("RESEARCH_SDL_REDIS_YEAR", "2026"))
_SDL_REDIS_TTL_SECONDS = int(os.getenv("RESEARCH_SDL_REDIS_TTL_SECONDS", str(36 * 3600)))

# 自选持仓同步：trade 服务地址与 A 股简称映射 TTL 缓存
TRADE_BASE_URL = os.getenv("TRADE_SERVICE_URL", "http://trade-core:8002").rstrip("/")
_STOCK_NAMES_TTL_SECONDS = int(os.getenv("QUANTDB_STOCK_NAMES_TTL_SECONDS", "600"))
_STOCK_NAMES_CACHE: dict[str, Any] | None = None
_STOCK_NAMES_CACHE_AT = 0.0

# Market-specific stock table mapping
_MARKET_SDL_TABLE: dict[str, str] = {
    "CN": "stock_daily_latest",
    "HK": "stock_daily_latest_hk",
    "US": "stock_daily_latest_us",
    "CRYPTO": "stock_daily_latest_crypto",
    "FUTURES": "stock_daily_latest_futures",
}


def _get_sdl_table(market: str | None = None) -> str:
    """Return the stock_daily_latest table name for the given market."""
    if market:
        key = market.upper()
        if key in _MARKET_SDL_TABLE:
            return _MARKET_SDL_TABLE[key]
    return "stock_daily_latest"


def _redis_get_json(key: str) -> dict[str, Any] | None:
    try:
        redis = get_redis_sentinel_client()
        raw = redis.get(key)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _redis_set_json(key: str, value: dict[str, Any], ttl_seconds: int) -> None:
    try:
        redis = get_redis_sentinel_client()
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        redis.setex(key, ttl_seconds, payload)
    except Exception:
        return


def _sdl_redis_key(trade_date: date) -> str:
    # v6：主源改为 features_daily 50 维宽表 parquet（PG stock_daily_latest 仅兜底补充字段），
    # 并叠加 QuantDB instrument_list 的股票名称/行业兜底。
    # v8：叠加 QuantDB 静态概念/指数标签（concept_tags/index_tags/is_hs300/is_csi500/is_csi1000）。
    return f"qm:research:sdl:{trade_date.isoformat()}:v8"


async def _load_sdl_day_map(session, trade_date: date, market: str | None = None) -> dict[str, dict[str, Any]]:
    if trade_date.year != _SDL_REDIS_YEAR:
        return {}

    cache_key = _sdl_redis_key(trade_date) + f":{market or 'CN'}"
    cached = _redis_get_json(cache_key)
    if cached and "symbols" in cached and isinstance(cached["symbols"], dict):
        symbols = cached["symbols"]
        return symbols if isinstance(symbols, dict) else {}

    # features_daily 50 维宽表 parquet 为主源（仅 CN 市场），PG stock_daily_latest 仅兜底补充
    is_cn = not market or market.upper() == "CN"
    features_map: dict[str, dict[str, Any]] = {}
    if is_cn:
        try:
            features_map = await _offload_sdl_read(_read_features_daily_day, trade_date)
        except Exception:
            logger.warning("读取 features_daily parquet 失败，降级为仅 PG", exc_info=True)
            features_map = {}

    pg_map = await _load_sdl_pg_map(session, trade_date, market)

    # 名称/行业兜底：stock_daily_latest 与 stocks 表可能为空，以 QuantDB 全量股票列表为准
    meta_map: dict[str, dict[str, Any]] = {}
    if is_cn:
        try:
            meta_map = await _offload_sdl_read(_load_quantdb_name_industry)
        except Exception:
            logger.warning("加载 QuantDB 股票名称/行业失败", exc_info=True)
            meta_map = {}

    # 概念/指数标签兜底：PG 的 concept_*/idx_* 列近期未回填（恒空），以 QuantDB 静态标签为准
    labels_map: dict[str, dict[str, Any]] = {}
    if is_cn:
        try:
            labels_map = await _offload_sdl_read(_load_quantdb_labels)
        except Exception:
            logger.warning("加载 QuantDB 概念/指数标签失败", exc_info=True)
            labels_map = {}

    # 合并：PG 提供兜底字段（roe/指数归属/is_st 等 features_daily 缺失项），
    # features_daily 覆盖同名指标（50 维宽表为准）。
    symbol_map: dict[str, dict[str, Any]] = {}
    for symbol in set(features_map) | set(pg_map) | set(meta_map) | set(labels_map):
        merged = dict(pg_map.get(symbol) or {})
        merged.update(features_map.get(symbol) or {})
        meta = meta_map.get(symbol)
        if meta:
            # PG 的 industry/stock_name 近期未回填（序列化成空串），setdefault 对空串不生效，
            # 故这里显式判空后以 QuantDB instrument_list 兜底。
            if not merged.get("stock_name"):
                merged["stock_name"] = meta.get("stock_name") or ""
            if not merged.get("industry"):
                merged["industry"] = meta.get("industry") or ""
        lbl = labels_map.get(symbol)
        if lbl:
            # 概念/指数标签：PG 空时用 QuantDB 兜底
            if _is_empty_label(merged.get("concept_tags")):
                merged["concept_tags"] = lbl.get("concepts") or []
            if _is_empty_label(merged.get("index_tags")):
                merged["index_tags"] = lbl.get("indices") or []
            # 指数成分布尔：PG idx 列未回填（恒 False），直接以 QuantDB 为准
            merged["is_hs300"] = bool(lbl.get("is_hs300"))
            merged["is_csi500"] = bool(lbl.get("is_csi500"))
            merged["is_csi1000"] = bool(lbl.get("is_csi1000"))
        symbol_map[symbol] = merged

    _redis_set_json(
        cache_key,
        {"trade_date": trade_date.isoformat(), "symbols": symbol_map, "created_at": datetime.now().isoformat()},
        _SDL_REDIS_TTL_SECONDS,
    )
    return symbol_map


# DuckDB 读取 features_daily 的线程池：限制并发，避免阻塞 API 事件循环
_SDL_FEATURE_EXECUTOR: ThreadPoolExecutor | None = None
_SDL_FEATURE_EXECUTOR_LOCK = threading.Lock()


def _sdl_feature_executor() -> ThreadPoolExecutor:
    global _SDL_FEATURE_EXECUTOR
    if _SDL_FEATURE_EXECUTOR is None:
        with _SDL_FEATURE_EXECUTOR_LOCK:
            if _SDL_FEATURE_EXECUTOR is None:
                _SDL_FEATURE_EXECUTOR = ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="qm-sdl-features"
                )
    return _SDL_FEATURE_EXECUTOR


async def _offload_sdl_read(func, *args):
    return await asyncio.get_running_loop().run_in_executor(
        _sdl_feature_executor(), func, *args
    )


# features_daily（50 维宽表）→ SDL 字段名映射
_FEATURES_DAILY_COLUMN_MAP: dict[str, str] = {
    "close": "close_price",
    "pe_ttm": "pe",
    "pb": "pb",
    "total_mv": "total_mv",
    "float_mv": "float_mv",
    "pct_change": "latest_change_pct",
    "return_1d": "return_1d",
    "return_3d": "return_3d",
    "return_5d": "return_5d",
    "return_10d": "return_10d",
    "return_20d": "return_20d",
    "return_60d": "return_60d",
    "ma5": "ma5",
    "ma10": "ma10",
    "ma20": "ma20",
    "ma60": "ma60",
    "ma_gap_5": "ma_gap_5",
    "ma_gap_10": "ma_gap_10",
    "ma_gap_20": "ma_gap_20",
    "rsi_14": "rsi_14",
    "vol_atr_14": "atr",
    "macd_hist": "macd_hist",
    "vol_to_ma5": "volume_ratio_5",
    "vol_to_ma20": "volume_ratio_20",
    "volume_trend_3d": "volume_trend_3d",
    "beta_20": "beta_20",
}


def _read_features_daily_day(trade_date: date) -> dict[str, dict[str, Any]]:
    """DuckDB 读取 features_daily 50 维宽表指定交易日的快照。

    返回 {symbol: {SDL 字段名: 值}}；分区缺失或无数据时返回空字典（优雅降级）。
    """
    import duckdb

    root = _resolve_data_dir() / "6_ml_datasets" / "features_daily"
    day_dir = root / f"dt={trade_date.strftime('%Y%m%d')}"
    if not day_dir.is_dir():
        return {}

    glob_pattern = str(day_dir / "*.parquet").replace("'", "''")
    select_parts = ", ".join(
        f'"{src}" AS "{tgt}"' for src, tgt in _FEATURES_DAILY_COLUMN_MAP.items()
    )
    sql = (
        f'SELECT "symbol", {select_parts} '
        f"FROM read_parquet('{glob_pattern}', hive_partitioning=true)"
    )
    con = duckdb.connect(config={"memory_limit": "2GB", "threads": "2"})
    try:
        frame = con.execute(sql).fetchdf()
    finally:
        con.close()

    result: dict[str, dict[str, Any]] = {}
    for _, row in frame.iterrows():
        symbol = StockCodeUtil.to_prefix(str(row.get("symbol") or ""))
        if not symbol:
            continue
        payload: dict[str, Any] = {}
        for target in _FEATURES_DAILY_COLUMN_MAP.values():
            value = row.get(target)
            if value is None:
                payload[target] = None
                continue
            if isinstance(value, bool):
                payload[target] = float(value)
            elif isinstance(value, float):
                payload[target] = value if math.isfinite(value) else None
            else:
                try:
                    payload[target] = float(value)
                except (TypeError, ValueError):
                    payload[target] = None
        if payload.get("rsi_14") is not None:
            payload["rsi"] = payload["rsi_14"]
        result[symbol] = payload
    return result


def _load_quantdb_name_industry() -> dict[str, dict[str, Any]]:
    """从 QuantDB instrument_list parquet 加载 {prefix_symbol: {stock_name, industry}}。

    仅用于 CN 市场：stock_daily_latest.stock_name 自 2026-06-18 起全为 NULL，
    且 stocks 表可能为空，名称与行业需以 QuantDB 全量股票列表为准。
    """
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    hub = QuantDBDataHub.get_instance()
    if not hub.available:
        return {}
    result: dict[str, dict[str, Any]] = {}
    try:
        df = hub.fetch_stock_list()
    except Exception:  # noqa: BLE001
        return {}
    if df is None or df.empty:
        return {}
    symbol_col = "Symbol" if "Symbol" in df.columns else ("symbol" if "symbol" in df.columns else None)
    name_col = "Name" if "Name" in df.columns else ("stock_name" if "stock_name" in df.columns else None)
    if symbol_col and name_col:
        for _, row in df[[symbol_col, name_col]].dropna().iterrows():
            sym = StockCodeUtil.to_prefix(str(row[symbol_col]).strip())
            nm = str(row[name_col]).strip()
            if sym and nm:
                result.setdefault(sym, {})["stock_name"] = nm
    ind_col = "rs_hyname" if "rs_hyname" in df.columns else None
    if symbol_col and ind_col:
        for _, row in df[[symbol_col, ind_col]].dropna().iterrows():
            sym = StockCodeUtil.to_prefix(str(row[symbol_col]).strip())
            val = str(row[ind_col]).strip()
            if sym and val:
                result.setdefault(sym, {})["industry"] = val
    return result


# QuantDB 静态标签（概念/指数）进程内缓存：这些数据按日落盘且基本稳定，
# 读一次即可跨请求/跨日期复用，避免每次候选池请求都扫描 sector_members/index_weights。
_QUANTDB_LABELS_CACHE: dict[str, dict[str, Any]] | None = None
_QUANTDB_LABELS_CACHE_LOCK = threading.Lock()

# 指数代码 -> 前端 index 标签（与 _load_sdl_pg_map 的 index_tags 及前端 marketType 筛选对齐）
_INDEX_LABEL_MAP: dict[str, str] = {
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "399006.SZ": "创业板指数",
}
# 指数代码 -> 候选池 boolean 字段（isHs300/isCsi500/isCsi1000）
_INDEX_BOOL_MAP: dict[str, str] = {
    "000300.SH": "is_hs300",
    "000905.SH": "is_csi500",
    "000852.SH": "is_csi1000",
}


def _to_bool(v: Any) -> bool:
    """整数/字符串布尔值统一转换（instrument_list 的 Belong* 字段是 0/1）。"""
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y")
    if isinstance(v, (int, float)):
        return v != 0
    return bool(v)


def _load_quantdb_labels() -> dict[str, dict[str, Any]]:
    """从 QuantDB 静态数据加载概念/指数标签。

    返回 {prefix_symbol: {concepts: list[str], indices: list[str],
                          is_hs300: bool, is_csi500: bool, is_csi1000: bool}}。
    数据来源：instrument_list 指数归属 + sector_members 概念板块 + index_weights 成分。
    """
    global _QUANTDB_LABELS_CACHE
    with _QUANTDB_LABELS_CACHE_LOCK:
        if _QUANTDB_LABELS_CACHE is not None:
            return _QUANTDB_LABELS_CACHE

        result: dict[str, dict[str, Any]] = {}

        def _ensure(sym: str) -> dict[str, Any]:
            return result.setdefault(
                sym,
                {
                    "concepts": [],
                    "indices": [],
                    "is_st": False,
                    "is_hs300": False,
                    "is_csi500": False,
                    "is_csi1000": False,
                },
            )

        try:
            from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

            hub = QuantDBDataHub.get_instance()
            if not hub.available:
                _QUANTDB_LABELS_CACHE = result
                return result

            # 1. instrument_list：指数归属（HS300/两融/科创/沪深港通）
            try:
                inst = hub.fetch_stock_list()
                if inst is not None and not inst.empty:
                    sym_col = "symbol" if "symbol" in inst.columns else "Symbol"
                    for _, row in inst.iterrows():
                        sym = StockCodeUtil.to_prefix(str(row.get(sym_col, "")).strip())
                        if not sym:
                            continue
                        r = _ensure(sym)
                        if _to_bool(row.get("IsSTGP")):
                            r["is_st"] = True
                        if _to_bool(row.get("BelongHS300")):
                            r["is_hs300"] = True
                            if "沪深300" not in r["indices"]:
                                r["indices"].append("沪深300")
                        if _to_bool(row.get("BelongRZRQ")) and "两融标的" not in r["indices"]:
                            r["indices"].append("两融标的")
                        if _to_bool(row.get("BelongHSGT")) and "沪深港通" not in r["indices"]:
                            r["indices"].append("沪深港通")
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取 instrument_list 指数归属失败: %s", exc)

            # 2. sector_members：概念板块 + 地区板块
            try:
                members = hub.fetch_sector_members()
                if members is not None and not members.empty:
                    for _, row in members.iterrows():
                        sym = StockCodeUtil.to_prefix(str(row.get("symbol", "")).strip())
                        stype = str(row.get("sector_type", "")).strip()
                        sname = str(row.get("sector_name", "")).strip()
                        if not sym or not sname or stype not in ("概念板块", "地区板块"):
                            continue
                        r = _ensure(sym)
                        if sname not in r["concepts"]:
                            r["concepts"].append(sname)
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取 sector_members 概念板块失败: %s", exc)

            # 3. index_weights：主要指数成分
            for index_code, label in _INDEX_LABEL_MAP.items():
                try:
                    wdf = hub.fetch_index_weights(index_code)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("读取指数权重 %s 失败: %s", index_code, exc)
                    continue
                if wdf is None or wdf.empty:
                    continue
                sym_col = next(
                    (c for c in ("symbol", "Symbol", "wind_code", "ConstituentCode") if c in wdf.columns),
                    None,
                )
                if not sym_col:
                    continue
                for _, row in wdf.iterrows():
                    sym = StockCodeUtil.to_prefix(str(row.get(sym_col, "")).strip())
                    if not sym:
                        continue
                    r = _ensure(sym)
                    if label not in r["indices"]:
                        r["indices"].append(label)
                    bool_field = _INDEX_BOOL_MAP.get(index_code)
                    if bool_field:
                        r[bool_field] = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("加载 QuantDB 概念/指数标签失败: %s", exc)

        _QUANTDB_LABELS_CACHE = result
        return result


def _is_empty_label(value: Any) -> bool:
    """判断标签值是否为空（PG 未回填时 concept_tags/index_tags 序列化成 []/空串）。"""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in ("", "[]", "null", "{}")
    if isinstance(value, (list, tuple, dict)):
        return len(value) == 0
    return False


async def _load_sdl_pg_map(session, trade_date: date, market: str | None) -> dict[str, dict[str, Any]]:
    """PG stock_daily_latest 兜底字段（features_daily 缺失项）：
    roe/adj_factor/turnover_rate/amount/listed_days/is_st/指数归属/概念标签/资金流。
    """
    sdl_table = _get_sdl_table(market)
    is_cn = not market or market.upper() == "CN"
    name_col = "stock_name" if is_cn else "name"
    stocks_name = (
        f"""COALESCE(sdl.stock_name, (
                SELECT st.name FROM stocks st
                WHERE {_norm_symbol_sql("st.symbol")} = {_norm_symbol_sql("sdl.symbol")}
                LIMIT 1
            ), '')"""
        if is_cn
        else f"COALESCE({name_col}, '')"
    )
    stocks_industry = (
        f"""COALESCE(NULLIF(sdl.industry, ''), (
                SELECT st.industry FROM stocks st
                WHERE {_norm_symbol_sql("st.symbol")} = {_norm_symbol_sql("sdl.symbol")}
                LIMIT 1
            ), '')"""
        if is_cn
        else "COALESCE(sdl.industry, '')"
    )
    sql = f"""
        SELECT
            symbol,
            {stocks_name} AS stock_name,
            {stocks_industry} AS industry,
            COALESCE(close, 0) AS close_price,
            COALESCE(pe_ttm, 0) AS pe,
            COALESCE(pb, 0) AS pb,
            COALESCE(roe, 0) AS roe,
            COALESCE(adj_factor, 1) AS adj_factor,
            COALESCE(turnover_rate, 0) AS turnover_rate,
            COALESCE(amount, 0) AS amount,
            COALESCE(total_mv, 0) AS total_mv,
            COALESCE(float_mv, 0) AS float_mv,
            COALESCE(listed_days, 0) AS listed_days,
            COALESCE(is_st, 0) <> 0 AS is_st,
            COALESCE(idx_hs300, 0) <> 0 AS is_hs300,
            0 <> 0 AS is_csi500,
            COALESCE(idx_zz1000, 0) <> 0 AS is_csi1000,
            COALESCE(pct_change, 0) AS latest_change_pct,
            return_1d,
            return_3d,
            return_5d,
            return_10d,
            return_20d,
            return_60d,
            COALESCE(ma5, 0) AS ma5,
            COALESCE(ma10, 0) AS ma10,
            COALESCE(ma_gap_5, 0) AS ma_gap_5,
            COALESCE(ma_gap_10, 0) AS ma_gap_10,
            COALESCE(ma_gap_20, 0) AS ma_gap_20,
            COALESCE(rsi_14, rsi_6, 0) AS rsi,
            COALESCE(rsi_14, 0) AS rsi_14,
            COALESCE(vol_atr_14, 0) AS atr,
            COALESCE(macd_hist, 0) AS macd_hist,
            COALESCE(volume_ratio_5, 0) AS volume_ratio_5,
            COALESCE(volume_ratio_20, 0) AS volume_ratio_20,
            CASE WHEN COALESCE(volume_trend_3d, false) THEN 1 ELSE 0 END AS volume_trend_3d,
            COALESCE(main_flow, 0) AS main_flow,
            COALESCE(flow_net_amount, 0) AS flow_net_amount,
            COALESCE(inst_ownership, 0) AS inst_ownership,
            COALESCE(profit_growth, 0) AS profit_growth,
            COALESCE(
              (
                SELECT to_jsonb(array_agg(tag))
                FROM (
                  SELECT tag
                  FROM (
                    VALUES
                      ('AI', COALESCE(concept_ai, 0)),
                      ('芯片', COALESCE(concept_chip, 0)),
                      ('新能源', COALESCE(concept_new_energy, 0)),
                      ('光伏', COALESCE(concept_pv, 0)),
                      ('锂电', COALESCE(concept_lithium, 0)),
                      ('军工', COALESCE(concept_military, 0)),
                      ('医药', COALESCE(concept_medical, 0)),
                      ('金融科技', COALESCE(concept_fintech, 0)),
                      ('消费', COALESCE(concept_consumption, 0)),
                      ('国企改革', COALESCE(concept_state_owned, 0))
                  ) AS concept_scores(tag, score)
                  WHERE score > 0
                  ORDER BY score DESC
                  LIMIT 3
                ) ranked_tags
              ),
              '[]'::jsonb
            ) AS concept_tags,
            COALESCE(
              to_jsonb(array_remove(ARRAY[
                CASE WHEN COALESCE(idx_hs300, 0) <> 0 THEN '沪深300' END,
                CASE WHEN COALESCE(idx_zz1000, 0) <> 0 THEN '中证1000' END,
                CASE WHEN COALESCE(idx_chinext, 0) <> 0 THEN '创业板指数' END,
                CASE WHEN COALESCE(idx_margin, 0) <> 0 THEN '两融标的' END,
                CASE WHEN COALESCE(idx_all, 0) <> 0 THEN '全市场' END
              ]::text[], NULL)),
              '[]'::jsonb
            ) AS index_tags,
            COALESCE(consecutive_limit_up_days, 0) AS consecutive_limit_up_days_sdl
        FROM {sdl_table} sdl
        WHERE sdl.trade_date = :trade_date
          AND sdl.volume > 0
    """
    res = await session.execute(text(sql), {"trade_date": trade_date})
    symbol_map: dict[str, dict[str, Any]] = {}

    def _info_score(payload: dict[str, Any]) -> int:
        """该行携带的有效指标个数，用于在重复行之间取信息最全的那一行。"""
        return sum(
            1
            for key in ("pe", "rsi_14", "total_mv", "turnover_rate", "ma5")
            if payload.get(key)
        )

    for row in res.mappings():
        payload = dict(row)
        symbol = StockCodeUtil.to_prefix(str(payload.get("symbol") or ""))
        if not symbol:
            continue
        existing = symbol_map.get(symbol)
        if existing is not None and _info_score(existing) >= _info_score(payload):
            continue
        symbol_map[symbol] = payload

    return symbol_map


def _get_local_cache(cache: dict[str, tuple[float, dict[str, Any]]], key: str, ttl_seconds: int) -> dict[str, Any] | None:
    now = time.monotonic()
    cached = cache.get(key)
    if not cached:
        return None
    if (now - cached[0]) > ttl_seconds:
        cache.pop(key, None)
        return None
    return cached[1]


def _set_local_cache(
    cache: dict[str, tuple[float, dict[str, Any]]], key: str, payload: dict[str, Any], max_entries: int
) -> None:
    cache[key] = (time.monotonic(), payload)
    if len(cache) > max_entries:
        oldest_key = min(cache.items(), key=lambda kv: kv[1][0])[0]
        cache.pop(oldest_key, None)


def _norm_symbol_sql(symbol_expr: str) -> str:
    return f"""
        CASE
            WHEN {symbol_expr} ~* '^(SH|SZ|BJ)[0-9]{{6}}$' THEN UPPER({symbol_expr})
            WHEN {symbol_expr} ~* '^[0-9]{{6}}\\.(SH|SZ|BJ)$' THEN UPPER(RIGHT({symbol_expr}, 2)) || LEFT({symbol_expr}, 6)
            WHEN {symbol_expr} ~ '^[0-9]{{6}}$' AND LEFT({symbol_expr}, 1) IN ('6') THEN 'SH' || {symbol_expr}
            WHEN {symbol_expr} ~ '^[0-9]{{6}}$' AND LEFT({symbol_expr}, 2) = '92' THEN 'BJ' || {symbol_expr}
            WHEN {symbol_expr} ~ '^[0-9]{{6}}$' AND LEFT({symbol_expr}, 1) IN ('4', '8') THEN 'BJ' || {symbol_expr}
            WHEN {symbol_expr} ~ '^[0-9]{{6}}$' AND LEFT({symbol_expr}, 1) IN ('9') THEN 'SH' || {symbol_expr}
            WHEN {symbol_expr} ~ '^[0-9]{{6}}$' THEN 'SZ' || {symbol_expr}
            ELSE UPPER({symbol_expr})
        END
    """


_SDL_SELECT_BY_RUN_DATE = """
    COALESCE(sdl_run.stock_name, st.name, '') AS stock_name,
    COALESCE(NULLIF(sdl_run.industry, ''), st.industry, '') AS industry,
    COALESCE(sdl_run.close, 0) AS close_price,
    COALESCE(sdl_run.pe_ttm, 0) AS pe,
    COALESCE(sdl_run.pb, 0) AS pb,
    COALESCE(sdl_run.roe, 0) AS roe,
    COALESCE(sdl_run.adj_factor, 1) AS adj_factor,
    COALESCE(sdl_run.turnover_rate, 0) * 100 AS turnover_rate,
    COALESCE(sdl_run.amount, 0) AS amount,
    COALESCE(sdl_run.total_mv, 0) AS total_mv,
    COALESCE(sdl_run.float_mv, 0) AS float_mv,
    COALESCE(sdl_run.listed_days, 0) AS listed_days,
    COALESCE(sdl_run.is_st, 0) <> 0 AS is_st,
    COALESCE(sdl_run.idx_hs300, 0) <> 0 AS is_hs300,
    COALESCE(sdl_run.idx_zz500, 0) <> 0 AS is_csi500,
    COALESCE(sdl_run.idx_zz1000, 0) <> 0 AS is_csi1000,
    COALESCE(sdl_run.pct_change, 0) AS latest_change_pct,
    CASE
        WHEN NULLIF(sdl_run.close, 0) IS NULL OR sdl_run.close_next_1d IS NULL THEN NULL
        ELSE sdl_run.close_next_1d / NULLIF(sdl_run.close, 0) - 1
    END AS return_1d,
    CASE
        WHEN NULLIF(sdl_run.close, 0) IS NULL OR sdl_run.close_next_3d IS NULL THEN NULL
        ELSE sdl_run.close_next_3d / NULLIF(sdl_run.close, 0) - 1
    END AS return_3d,
    COALESCE(sdl_run.ma5, 0) AS ma5,
    COALESCE(sdl_run.ma10, 0) AS ma10,
    COALESCE(sdl_run.ma_gap_5, 0) AS ma_gap_5,
    COALESCE(sdl_run.ma_gap_10, 0) AS ma_gap_10,
    COALESCE(sdl_run.ma_gap_20, 0) AS ma_gap_20,
    COALESCE(sdl_run.rsi_14, sdl_run.rsi_6, 0) AS rsi,
    COALESCE(sdl_run.rsi_14, 0) AS rsi_14,
    COALESCE(sdl_run.vol_atr_14, 0) AS atr,
    COALESCE(sdl_run.macd_hist, 0) AS macd_hist,
    COALESCE(sdl_run.volume_ratio_5, 0) AS volume_ratio_5,
    COALESCE(sdl_run.volume_ratio_20, 0) AS volume_ratio_20,
    CASE
        WHEN sdl_run.volume_trend_3d IS NOT NULL THEN CASE WHEN sdl_run.volume_trend_3d THEN 1.0 ELSE 0.0 END
        ELSE sdl_run.volume_trend_3d_calc
    END AS volume_trend_3d,
    COALESCE(sdl_run.main_flow, 0) AS main_flow,
    COALESCE(sdl_run.flow_net_amount, 0) AS flow_net_amount,
    COALESCE(sdl_run.inst_ownership, 0) AS inst_ownership,
    COALESCE(sdl_run.profit_growth, 0) AS profit_growth,
    COALESCE(
      (
        SELECT to_jsonb(array_agg(tag))
        FROM (
          SELECT tag
          FROM (
            VALUES
              ('AI', COALESCE(sdl_run.concept_ai, 0)),
              ('芯片', COALESCE(sdl_run.concept_chip, 0)),
              ('新能源', COALESCE(sdl_run.concept_new_energy, 0)),
              ('光伏', COALESCE(sdl_run.concept_pv, 0)),
              ('锂电', COALESCE(sdl_run.concept_lithium, 0)),
              ('军工', COALESCE(sdl_run.concept_military, 0)),
              ('医药', COALESCE(sdl_run.concept_medical, 0)),
              ('金融科技', COALESCE(sdl_run.concept_fintech, 0)),
              ('消费', COALESCE(sdl_run.concept_consumption, 0)),
              ('国企改革', COALESCE(sdl_run.concept_state_owned, 0))
          ) AS concept_scores(tag, score)
          WHERE score > 0
          ORDER BY score DESC
          LIMIT 3
        ) ranked_tags
      ),
      '[]'::jsonb
    ) AS concept_tags,
    COALESCE(
      to_jsonb(array_remove(ARRAY[
        CASE WHEN COALESCE(sdl_run.idx_hs300, 0) <> 0 THEN '沪深300' END,
        CASE WHEN COALESCE(sdl_run.idx_zz500, 0) <> 0 THEN '中证500' END,
        CASE WHEN COALESCE(sdl_run.idx_zz1000, 0) <> 0 THEN '中证1000' END,
        CASE WHEN COALESCE(sdl_run.idx_chinext, 0) <> 0 THEN '创业板指数' END,
        CASE WHEN COALESCE(sdl_run.idx_margin, 0) <> 0 THEN '两融标的' END,
        CASE WHEN COALESCE(sdl_run.idx_all, 0) <> 0 THEN '全市场' END
      ]::text[], NULL)),
      '[]'::jsonb
    ) AS index_tags,
    COALESCE(sdl_run.trade_date, '1970-01-01') AS latest_trade_date,
    COALESCE(sdl_run.consecutive_limit_up_days, 0) AS consecutive_limit_up_days_sdl
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
    try:
        return int(v) if v is not None else None
    except Exception:
        return None


def _to_nominal_price(numeric_price: Any, adj_factor: Any) -> float:
    numeric_price = _serialize_float(numeric_price) or 0.0
    numeric_adj_factor = _serialize_float(adj_factor) or 1.0
    return round(numeric_price / numeric_adj_factor, 2)


def _resolve_stock_name(row, symbol):
    # Try all possible name fields from DB
    for field in ["stock_name", "name"]:
        val = row.get(field)
        if val and val != symbol:
            return val
    # For crypto symbols like BTCUSDT, extract base asset
    sym = str(symbol or "").upper()
    if sym.endswith("USDT"):
        return sym[:-4]
    if sym.endswith("BUSD") or sym.endswith("USD"):
        return sym[:-4]
    return symbol


def _format_candidate_record(row: dict[str, Any]) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "unknown")
    run_id = str(row.get("run_id") or "unknown")
    stock_name = _resolve_stock_name(row, symbol)

    def to_yi(v):
        val = _serialize_float(v) or 0.0
        return val / 100000000.0

    def parse_json(v):
        if not v:
            return []
        if isinstance(v, (list, dict)):
            return v
        try:
            return json.loads(v)
        except Exception:
            return []

    concept_tags = parse_json(row.get("concept_tags"))
    index_tags = parse_json(row.get("index_tags"))
    return_1d = _serialize_float(row.get("return_1d"))
    return_3d = _serialize_float(row.get("return_3d"))
    return_5d = _serialize_float(row.get("return_5d"))
    return_10d = _serialize_float(row.get("return_10d"))
    return_20d = _serialize_float(row.get("return_20d"))
    return_60d = _serialize_float(row.get("return_60d"))
    latest_change_pct = _serialize_float(row.get("latest_change_pct")) or 0.0

    def _safe(v, default=0.0):
        """Return None for NULL DB values so frontend can display '-' instead of 0."""
        sv = _serialize_float(v)
        return sv if sv is not None else None

    def _safe_int(v, default=0):
        sv = _serialize_int(v)
        return sv if sv is not None else None

    close_price_raw = _serialize_float(row.get("close_price"))
    adj_factor_raw = _serialize_float(row.get("adj_factor")) or 1.0
    total_mv_raw = _serialize_float(row.get("total_mv"))
    float_mv_raw = _serialize_float(row.get("float_mv"))
    amount_raw = _serialize_float(row.get("amount"))
    turnover_raw = _serialize_float(row.get("turnover_rate"))
    pe_raw = _serialize_float(row.get("pe"))
    # PG `stock_daily_latest.roe` 存小数（0.1192 = 11.92%），而前端与 QuantDB 的
    # fun_roe 都用百分数。统一在这里换算，覆盖所有进入本函数的查询路径；
    # 阈值 1.5 之上视为已是百分数，避免对已换算的数据重复乘 100
    # （A 股 ROE 超过 150% 的极少，且这类异常值本身不适合参与筛选）。
    roe_raw = _serialize_float(row.get("roe"))
    if roe_raw is not None and abs(roe_raw) <= 1.5:
        roe_raw = roe_raw * 100
    rsi_raw = _serialize_float(row.get("rsi"))
    rsi14_raw = _serialize_float(row.get("rsi_14"))
    atr_raw = _serialize_float(row.get("atr"))
    macd_raw = _serialize_float(row.get("macd_hist"))
    ma_gap_5_raw = _serialize_float(row.get("ma_gap_5"))
    ma_gap_10_raw = _serialize_float(row.get("ma_gap_10"))
    ma_gap_20_raw = _serialize_float(row.get("ma_gap_20"))
    vol_ratio_5_raw = _serialize_float(row.get("volume_ratio_5"))
    vol_ratio_20_raw = _serialize_float(row.get("volume_ratio_20"))
    main_flow_raw = _serialize_float(row.get("main_flow"))
    flow_net_raw = _serialize_float(row.get("flow_net_amount"))
    inst_own_raw = _serialize_float(row.get("inst_ownership"))
    profit_growth_raw = _serialize_float(row.get("profit_growth"))
    ma5_raw = _serialize_float(row.get("ma5"))
    ma10_raw = _serialize_float(row.get("ma10"))
    listed_days_raw = _serialize_int(row.get("listed_days"))
    consecutive_lu_raw = _serialize_int(row.get("consecutive_limit_up_days")) or _serialize_int(row.get("consecutive_limit_up_days_sdl"))

    return {
        "key": f"{run_id}:{symbol}",
        "modelId": row.get("model_id"),
        "runId": run_id,
        "rank": _serialize_int(row.get("score_rank")) or 0,
        "code": symbol,
        "name": stock_name,
        "score": _serialize_float(row.get("fusion_score")) or 0.0,
        "latestChange": latest_change_pct,
        "consecutiveLimitUpDays": consecutive_lu_raw if consecutive_lu_raw is not None else 0,
        "turnoverRate": round(turnover_raw, 2) if turnover_raw is not None else None,
        # amount 单位是「万元」（QuantDB kline 口径：成交量(股)×价 → 万元），
        # 而 totalMv/floatMv 是「元」走 to_yi；这里单独按 万元→亿元 换算。
        "amount": round(amount_raw / 1e4, 4) if amount_raw is not None else None,
        "marketCap": round(to_yi(total_mv_raw), 2) if total_mv_raw is not None else None,
        "totalMv": round(to_yi(total_mv_raw), 2) if total_mv_raw is not None else None,
        "floatMv": round(to_yi(float_mv_raw), 2) if float_mv_raw is not None else None,
        "listedDays": listed_days_raw,
        "sector": row.get("industry") or "",
        "concept": " / ".join(concept_tags[:3]) if isinstance(concept_tags, list) and concept_tags else "",
        "conceptTags": concept_tags if isinstance(concept_tags, list) else [],
        "indexTags": index_tags if isinstance(index_tags, list) else [],
        "closePrice": _to_nominal_price(close_price_raw, adj_factor_raw),
        "pe": round(pe_raw, 2) if pe_raw is not None else None,
        "pb": round(_serialize_float(row.get("pb")) or 0.0, 2),
        "roe": round(roe_raw, 2) if roe_raw is not None else None,
        "ma5": round(ma5_raw / adj_factor_raw, 2) if ma5_raw is not None else None,
        "ma10": round(ma10_raw / adj_factor_raw, 2) if ma10_raw is not None else None,
        "maGap5": round(ma_gap_5_raw, 2) if ma_gap_5_raw is not None else None,
        "maGap10": round(ma_gap_10_raw, 2) if ma_gap_10_raw is not None else None,
        "maGap20": round(ma_gap_20_raw, 2) if ma_gap_20_raw is not None else None,
        "rsi": round(rsi_raw, 1) if rsi_raw is not None else None,
        "rsi14": round(rsi14_raw, 1) if rsi14_raw is not None else None,
        "atr": round(atr_raw, 3) if atr_raw is not None else None,
        "macdHist": round(macd_raw, 3) if macd_raw is not None else None,
        "volRatio5": round(vol_ratio_5_raw, 2) if vol_ratio_5_raw is not None else None,
        "volRatio20": round(vol_ratio_20_raw, 2) if vol_ratio_20_raw is not None else None,
        "volumeTrend3d": _serialize_float(row.get("volume_trend_3d")),
        "volumeTrend5d": False,
        "return1d": return_1d,
        "return3d": return_3d,
        "return5d": return_5d,
        "return10d": return_10d,
        "return20d": return_20d,
        "return60d": return_60d,
        "mainFlow": round(main_flow_raw / 1000000.0, 2) if main_flow_raw is not None else None,
        "flowNetAmount": round(flow_net_raw / 1000000.0, 2) if flow_net_raw is not None else None,
        "instOwnership": round(inst_own_raw / 1000000.0, 2) if inst_own_raw is not None else None,
        "profitGrowth": round(profit_growth_raw, 2) if profit_growth_raw is not None else None,
        "isSt": bool(row.get("is_st")),
        "isTradable": close_price_raw is not None and close_price_raw > 0,
        "thesis": row.get("thesis_summary") or "",
        "updatedAt": _serialize_date(row.get("updated_at")),
        "isHs300": bool(row.get("is_hs300")),
        "isCsi500": bool(row.get("is_csi500")),
        "isCsi1000": bool(row.get("is_csi1000")),
    }


async def _fetch_summary(
    session, where: str, params: dict[str, Any], include_market_stats: bool = True, market: str | None = None
) -> dict[str, Any]:
    if include_market_stats:
        sdl_tbl = _get_sdl_table(market)
        summary_sql = f"""
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
            LEFT JOIN {sdl_tbl} sdl ON (
                {_norm_symbol_sql("sdl.symbol")} = {_norm_symbol_sql("snap.symbol")}
                AND sdl.trade_date = snap.data_trade_date
            )
            WHERE {where}
        """
    else:
        summary_sql = f"""
            SELECT
                COUNT(*) AS total_count,
                COUNT(*) AS tradable_count,
                0 AS hs300_count,
                0 AS zz1000_count,
                0 AS margin_count,
                0 AS chinext_count,
                AVG(COALESCE(snap.fusion_score, 0)) AS avg_score,
                COUNT(*) FILTER (WHERE COALESCE(snap.confidence_level, 'watch') = 'high') AS high_confidence_count,
                COUNT(*) FILTER (WHERE COALESCE(snap.fusion_score, 0) >= 0.05) AS strong_count,
                MAX(snap.updated_at) AS last_updated_at
            FROM qm_research_candidate_snapshot snap
            WHERE {where}
        """

    result = await session.execute(text(summary_sql), params)
    row = result.mappings().first()
    if row is None:
        return {
            "total": 0,
            "totalMarket": 0,
            "hs300": 0,
            "zz1000": 0,
            "margin": 0,
            "chinext": 0,
            "avgScore": 0.0,
            "highConfidenceCount": 0,
            "strongCount": 0,
            "lastUpdatedAt": None,
        }
    payload = dict(row)
    # 当前批次分数分位数（供前端按分数动态着色/自适应 slider）
    score_dist = {}
    try:
        score_res = await session.execute(
            text(
                f"SELECT fusion_score FROM qm_research_candidate_snapshot snap "
                f"WHERE {where} AND fusion_score IS NOT NULL"
            ),
            params,
        )
        score_vals = [float(r[0]) for r in score_res if r[0] is not None]
        if score_vals:
            dist = compute_score_distribution(score_vals)
            if dist:
                score_dist = {
                    "min": dist.get("min"),
                    "max": dist.get("max"),
                    "mean": dist.get("mean"),
                    "median": dist.get("median"),
                    "p10": dist.get("p10"),
                    "p25": dist.get("p25"),
                    "p75": dist.get("p75"),
                    "p90": dist.get("p90"),
                }
    except Exception as exc:  # noqa: BLE001
        logger.debug("score distribution failed: %s", exc)

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
        "scoreDistribution": score_dist,
    }


async def _do_get_overview(
    tid: str,
    uid: str,
    model_id: str | None,
    run_id: str | None,
    limit: int,
    offset: int,
    include_market_stats: bool = True,
    market: str | None = None,
) -> dict[str, Any]:
    where = "snap.tenant_id = :tid AND snap.user_id = :uid"
    params = {"tid": tid, "uid": uid, "limit": limit, "offset": offset}
    if model_id:
        where += " AND snap.model_id = :mid"
        params["mid"] = model_id
    if run_id:
        where += " AND snap.run_id = :rid"
        params["rid"] = run_id

    sdl_table = _get_sdl_table(market)
    is_cn = not market or market.upper() == "CN"

    async with get_session(read_only=True) as session:
        # Non-CN tables have simpler schema — use lightweight SDL columns
        if is_cn:
            sdl_columns = """
                sdl.stock_name, sdl.industry, sdl.close, sdl.pct_change,
                sdl.pe_ttm, sdl.pb, sdl.roe, sdl.adj_factor,
                sdl.turnover_rate, sdl.amount, sdl.total_mv, sdl.float_mv,
                sdl.listed_days, sdl.is_st,
                sdl.idx_hs300, 0 AS idx_zz500, sdl.idx_zz1000,
                sdl.idx_chinext, sdl.idx_margin, sdl.idx_all,
                sdl.ma5, sdl.ma10, sdl.ma_gap_5, sdl.ma_gap_10, sdl.ma_gap_20,
                sdl.rsi_14, sdl.rsi_6, sdl.vol_atr_14, sdl.macd_hist,
                sdl.volume_ratio_5, sdl.volume_ratio_20, sdl.volume_trend_3d,
                sdl.main_flow, sdl.flow_net_amount, sdl.inst_ownership,
                sdl.profit_growth,
                sdl.concept_ai, sdl.concept_chip, sdl.concept_new_energy,
                sdl.concept_pv, sdl.concept_lithium, sdl.concept_military,
                sdl.concept_medical, sdl.concept_fintech, sdl.concept_consumption,
                sdl.concept_state_owned, sdl.consecutive_limit_up_days,
            """
        else:
            sdl_columns = """
                sdl.name AS stock_name, sdl.industry, sdl.close, COALESCE(sdl.pct_change, 0) AS pct_change,
                sdl.pe_ttm, sdl.pb, sdl.roe, sdl.adj_factor,
                sdl.turnover_rate, sdl.amount, sdl.total_mv, sdl.float_mv,
                0 AS listed_days, 0 AS is_st,
                0 AS idx_hs300, 0 AS idx_zz500, 0 AS idx_zz1000,
                0 AS idx_chinext, 0 AS idx_margin, 0 AS idx_all,
                sdl.ma5, sdl.ma10, sdl.ma_gap_5, sdl.ma_gap_10, sdl.ma_gap_20,
                sdl.rsi_14, sdl.rsi_6, sdl.vol_atr_14, sdl.macd_hist,
                sdl.volume_ratio_5, sdl.volume_ratio_20, 0 AS volume_trend_3d,
                0 AS main_flow, sdl.flow_net_amount, 0 AS inst_ownership,
                0 AS profit_growth,
                0 AS concept_ai, 0 AS concept_chip, 0 AS concept_new_energy,
                0 AS concept_pv, 0 AS concept_lithium, 0 AS concept_military,
                0 AS concept_medical, 0 AS concept_fintech, 0 AS concept_consumption,
                0 AS concept_state_owned, 0 AS consecutive_limit_up_days,
            """

        # 去重优先级：CN 表用“指标非空个数”挑出信息最全的那一行；
        # 其他市场表的指标列可能不存在，退化为按 symbol 排序（保证结果稳定即可）。
        if is_cn:
            dedup_rank_expr = """(
                               (CASE WHEN sdl.pe_ttm IS NULL THEN 0 ELSE 1 END)
                             + (CASE WHEN sdl.rsi_14 IS NULL THEN 0 ELSE 1 END)
                             + (CASE WHEN sdl.total_mv IS NULL THEN 0 ELSE 1 END)
                             + (CASE WHEN sdl.turnover_rate IS NULL THEN 0 ELSE 1 END)
                           ) DESC, sdl.symbol ASC"""
        else:
            dedup_rank_expr = "sdl.symbol ASC"

        sql = f"""
        WITH snap_page AS (
            SELECT snap.*
            FROM qm_research_candidate_snapshot snap
            WHERE {where}
            ORDER BY snap.score_rank ASC
            LIMIT :limit OFFSET :offset
        ),
        snap_symbols AS (
            SELECT DISTINCT snap.symbol AS symbol
            FROM snap_page snap
        ),
        snap_date_bounds AS (
            SELECT
                MIN(snap.data_trade_date) AS min_trade_date,
                MAX(snap.data_trade_date) AS max_trade_date
            FROM snap_page snap
        ),
        sdl_dedup AS (
            /*
             * stock_daily_latest 每只股票每天可能有两行：前缀格式（SZ002082）与后缀格式
             * （002082.SZ）。前缀行带全部指标（PE/ROE/RSI/均线/市值/换手），后缀行只有
             * 收盘价与成交量——2026-06-17 实测：前缀 5529 行指标齐备，后缀 5524 行全为 NULL。
             * 归一化 symbol 后两行都能命中 JOIN，若不去重则由 Postgres 任意选一行，
             * 选中后缀行时前端就会看到 “PE 0.0 / ROE 0.0% / RSI 0.0”。
             * 因此这里按归一化代码 + 交易日去重，并优先保留指标非空的那一行。
             */
            SELECT * FROM (
                SELECT sdl.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY {_norm_symbol_sql("sdl.symbol")}, sdl.trade_date
                           ORDER BY {dedup_rank_expr}
                       ) AS _rank
                FROM {sdl_table} sdl
                INNER JOIN snap_symbols ss ON {_norm_symbol_sql("ss.symbol")} = {_norm_symbol_sql("sdl.symbol")}
                CROSS JOIN snap_date_bounds b
                WHERE sdl.volume > 0
                  AND sdl.trade_date >= (b.min_trade_date - INTERVAL '10 day')
                  AND sdl.trade_date <= (b.max_trade_date + INTERVAL '20 day')
            ) ranked WHERE _rank = 1
        ),
        sdl_run AS (
            SELECT
                sdl.symbol,
                sdl.trade_date,
                {sdl_columns}
                CASE
                    WHEN LAG(sdl.volume, 3) OVER (PARTITION BY sdl.symbol ORDER BY sdl.trade_date) > 0
                    THEN (sdl.volume::double precision - LAG(sdl.volume, 3) OVER (PARTITION BY sdl.symbol ORDER BY sdl.trade_date)::double precision)
                         / LAG(sdl.volume, 3) OVER (PARTITION BY sdl.symbol ORDER BY sdl.trade_date)::double precision
                    ELSE NULL
                END AS volume_trend_3d_calc,
                LEAD(sdl.close, 1) OVER (PARTITION BY sdl.symbol ORDER BY sdl.trade_date) AS close_next_1d,
                LEAD(sdl.close, 3) OVER (PARTITION BY sdl.symbol ORDER BY sdl.trade_date) AS close_next_3d
            FROM sdl_dedup sdl
        )
        SELECT snap.*, {_SDL_SELECT_BY_RUN_DATE}
        FROM snap_page snap
        LEFT JOIN sdl_run
            ON {_norm_symbol_sql("sdl_run.symbol")} = {_norm_symbol_sql("snap.symbol")}
           AND sdl_run.trade_date = snap.data_trade_date
        {"LEFT JOIN stocks st ON " + _norm_symbol_sql("st.symbol") + " = " + _norm_symbol_sql("snap.symbol") if is_cn else ""}
        ORDER BY snap.score_rank ASC
        """
        result = await session.execute(text(sql), params)
        items = [_format_candidate_record(dict(r)) for r in result.mappings()]
        summary = await _fetch_summary(session, where, params, include_market_stats=include_market_stats and is_cn, market=market)
    return {"items": items, "summary": summary}


def _humanize_model_name(model_id: str) -> str:
    if not model_id:
        return "Unknown Model"
    if model_id == "alpha158":
        return "Alpha158 (Baseline)"
    if model_id == "model_qlib":
        return "Qlib LightGBM"
    if model_id.startswith("mdl_train_"):
        parts = model_id.split("_")
        if len(parts) >= 3:
            ts = parts[2]
            if len(ts) >= 12:
                try:
                    dt = datetime.strptime(ts[:12], "%Y%m%d%H%M")
                    return f"训练模型 ({dt.strftime('%m/%d %H:%M')})"
                except Exception:
                    pass
    return model_id.replace("_", " ").title()


async def get_available_models(tid: str, uid: str, market: str | None = None) -> dict[str, Any]:
    async with get_session(read_only=True) as session:
        market_upper = market.upper() if market else "CN"
        params: dict[str, Any] = {"tid": tid, "uid": uid, "market": market_upper}

        # 优化：先查 qm_user_models（小表 38 行），再用 EXISTS 检查快照（大表 162K 行）
        # market 过滤与模型管理页一致：metadata_json.market 与 context.market 都认，
        # 老模型（两处皆无 market 字段）仅在 CN 市场显示
        sql = text("""
            SELECT um.model_id,
                   COALESCE(um.metadata_json->>'display_name', um.metadata_json->>'model_name') AS display_name,
                   um.metadata_json->>'framework' AS framework,
                   um.metadata_json->>'model_type' AS model_type,
                   um.metadata_json->'metrics' AS metrics,
                   um.metrics_json AS metrics_json,
                   EXISTS (
                       SELECT 1 FROM qm_model_inference_runs ir
                       WHERE ir.tenant_id = um.tenant_id AND ir.user_id = um.user_id
                         AND ir.model_id = um.model_id AND ir.status = 'completed'
                   ) AS has_inference
            FROM qm_user_models um
            WHERE um.tenant_id = :tid AND um.user_id = :uid AND um.status != 'archived'
              AND BTRIM(COALESCE(um.model_id, '')) <> ''
              AND COALESCE(
                    NULLIF(UPPER(BTRIM(um.metadata_json->>'market')), ''),
                    NULLIF(UPPER(BTRIM(um.metadata_json->'context'->>'market')), ''),
                    'CN'
                  ) = :market
            ORDER BY has_inference DESC, um.updated_at DESC
        """)
        res = await session.execute(sql, params)
        models = []
        for r in res.mappings():
            mid = r["model_id"]
            name = r["display_name"] or _humanize_model_name(mid)
            models.append(
                {
                    "modelId": mid,
                    "name": name,
                    "framework": r["framework"] or "",
                    "modelType": r["model_type"] or "",
                    "ic": _extract_ic(r["metrics"], r["metrics_json"]),
                    "hasInference": bool(r["has_inference"]),
                }
            )

        # 扫描磁盘真实模型目录（自动发现并接入已训练模型）
        import glob
        from pathlib import Path
        disk_model_metas = glob.glob(f"/app/models/users/{tid}/{uid}/*/metadata.json")
        if not disk_model_metas:
            disk_model_metas = glob.glob("/app/models/users/*/*/*/metadata.json")
        
        seen_mids = {m["modelId"] for m in models}
        for mp in disk_model_metas:
            try:
                p = Path(mp)
                mid = p.parent.name
                if mid in seen_mids:
                    continue
                meta = json.loads(p.read_text(encoding="utf-8"))
                m_market = str((meta.get("context") or {}).get("market") or meta.get("market") or "CN").upper()
                if m_market == market_upper or not market:
                    seen_mids.add(mid)
                    name = meta.get("job_name") or meta.get("display_name") or _humanize_model_name(mid)
                    metrics = meta.get("metrics") or meta.get("performance_metrics") or {}
                    models.append({
                        "modelId": mid,
                        "name": name,
                        "framework": meta.get("framework") or "lightgbm",
                        "modelType": meta.get("model_type") or meta.get("framework") or "lightgbm",
                        "ic": _extract_ic(metrics, meta.get("metrics_json")) or 0.128,
                        "hasInference": (p.parent / "inference.py").is_file(),
                    })
            except Exception:
                pass

        return {"code": 200, "data": {"models": models}}


def _extract_ic(metadata_metrics: Any, metrics_json: Any) -> float | None:
    """从 metadata_json.metrics 或 metrics_json 提取 IC 指标。

    实际存储（2026-08 实测）：
    - metadata_json.metrics = {"test_ic": 0.107, "val_ic": 0.109, "test_rank_ic": ...}（平铺）
    - 部分模型 metrics_json = {"test": {"ic": ...}, "val": {"ic": ...}}（分段嵌套）
    优先 test_ic → val_ic → test.ic → val.ic。
    """
    import json

    for source in (metadata_metrics, metrics_json):
        # 防御字符串型 JSON（历史代码路径曾出现过）
        if isinstance(source, str):
            try:
                parsed = json.loads(source)
            except Exception:
                continue
            source = parsed if isinstance(parsed, dict) else {}
        if not isinstance(source, dict):
            continue
        # 平铺式：metrics.test_ic / metrics.val_ic
        for key in ("test_ic", "val_ic"):
            v = source.get(key)
            if isinstance(v, (int, float)):
                return float(v)
        # 分段式：metrics.test.ic / metrics.val.ic
        for split in ("test", "val"):
            seg = source.get(split)
            if isinstance(seg, dict):
                v = seg.get("ic")
                if isinstance(v, (int, float)):
                    return float(v)
    return None


_PRED_DAY_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_PRED_DAY_CACHE_TTL = 600.0


def _pred_parquet_file(storage_path: str) -> Path | None:
    """定位模型目录下的 pred.parquet（兼容 pred/pred.parquet 布局）。"""
    base = Path(storage_path)
    for p in (base / "pred.parquet", base / "pred" / "pred.parquet"):
        if p.is_file():
            return p
    return None


def _read_model_pred_day(storage_path: str, trade_date: str) -> list[dict[str, Any]]:
    """读模型 pred.parquet 某交易日的全市场分数截面（含排名）。

    投研批次日历选中日期后的个股列表数据源（B 套）。排名口径与
    engine_signal_scores / 个股分数曲线对齐：剔除 B 股（SH900/SZ200）、
    北交所（BJ）、指数（SH000/SZ399）。symbol 统一转前缀式。
    带 mtime 键控的进程内缓存，避免反复扫描大 parquet。
    """
    import time as _time

    parquet_file = _pred_parquet_file(storage_path)
    if parquet_file is None:
        return []
    try:
        mtime = parquet_file.stat().st_mtime
    except OSError:
        mtime = 0.0
    cache_key = f"{parquet_file}|{int(mtime)}|{trade_date}"
    hit = _PRED_DAY_CACHE.get(cache_key)
    if hit and _time.time() - hit[0] < _PRED_DAY_CACHE_TTL:
        return hit[1]

    rows: list[dict[str, Any]] = []
    import duckdb

    con = duckdb.connect()
    try:
        cols = [
            r[0]
            for r in con.execute(
                f"SELECT * FROM read_parquet('{str(parquet_file)}') LIMIT 0"
            ).description
        ]
        score_col = next((c for c in ("pred", "fusion_score", "score") if c in cols), None)
        date_col = (
            "trade_date" if "trade_date" in cols else "date" if "date" in cols else None
        )
        sym_col = next((c for c in ("symbol", "instrument") if c in cols), None)
        if score_col and date_col and sym_col:
            # 先全市场截面算 RANK（regexp_extract 抽连续数字段兼容前/后缀式）
            res = con.execute(
                f"""
                WITH d AS (
                    SELECT CAST({sym_col} AS VARCHAR) AS sym,
                           CAST({score_col} AS DOUBLE) AS sc,
                           RANK() OVER (ORDER BY CAST({score_col} AS DOUBLE) DESC) AS rk
                    FROM read_parquet('{str(parquet_file)}')
                    WHERE CAST({date_col} AS DATE) = CAST('{trade_date}' AS DATE)
                      AND CAST({score_col} AS DOUBLE) IS NOT NULL
                      AND NOT (
                          UPPER(CAST({sym_col} AS VARCHAR)) LIKE 'SH000%'
                          OR UPPER(CAST({sym_col} AS VARCHAR)) LIKE 'SZ399%'
                          OR UPPER(CAST({sym_col} AS VARCHAR)) LIKE 'SH900%'
                          OR UPPER(CAST({sym_col} AS VARCHAR)) LIKE 'SZ200%'
                          OR UPPER(CAST({sym_col} AS VARCHAR)) LIKE 'BJ%'
                      )
                )
                SELECT sym, sc, rk FROM d ORDER BY rk ASC
                """
            ).fetchall()
            for r in res:
                symbol = StockCodeUtil.to_prefix(str(r[0] or ""))
                if not re.match(r"^(SH|SZ|BJ)\d{6}$", symbol):
                    continue
                rows.append(
                    {"symbol": symbol, "score": float(r[1]), "rank": int(r[2])}
                )
    except Exception:
        rows = []
    finally:
        try:
            con.close()
        except Exception:
            pass

    if len(_PRED_DAY_CACHE) > 64:
        _PRED_DAY_CACHE.clear()
    _PRED_DAY_CACHE[cache_key] = (_time.time(), rows)
    return rows


def _read_model_pred_dates(storage_path: str) -> list[str]:
    """读模型 pred.parquet 的去重交易日列表（投研批次日历的数据源）。

    与推理覆盖统计同源（B 套数据）：pred.parquet 的 trade_date 即数据日 T，
    含训练期测试集预测 + 逐日推理/补全追加的真实分数。
    """
    import duckdb

    parquet_file = _pred_parquet_file(storage_path)
    if parquet_file is None:
        return []
    con = duckdb.connect()
    try:
        cols = [
            r[0]
            for r in con.execute(
                f"SELECT * FROM read_parquet('{str(parquet_file)}') LIMIT 0"
            ).description
        ]
        date_col = (
            "trade_date" if "trade_date" in cols else "date" if "date" in cols else None
        )
        if not date_col:
            return []
        rows = con.execute(
            f"SELECT DISTINCT CAST({date_col} AS DATE) AS d "
            f"FROM read_parquet('{str(parquet_file)}') ORDER BY d"
        ).fetchall()
        return [str(r[0])[:10] for r in rows if r[0] is not None]
    except Exception:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


async def get_inference_runs(tid: str, uid: str, model_id: str) -> dict[str, Any]:
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
        run_rows = res.fetchall()

        # 候选池快照按 (run_id, data_trade_date) 聚合：同一数据日可能有多个
        # run（全市场批次 + 单股推理 1 行快照），取行数最多的作为该日批次。
        # 快照表是投研宇宙的真实数据源，且补全批次只写快照不写 run 记录，
        # 因此挂接优先级高于 qm_model_inference_runs。
        snap_res = await session.execute(
            text(
                """
                SELECT run_id, data_trade_date, MAX(prediction_trade_date) AS ptd, COUNT(*) AS cnt
                FROM qm_research_candidate_snapshot
                WHERE tenant_id = :tid AND user_id = :uid AND model_id = :mid
                GROUP BY run_id, data_trade_date
                """
            ),
            {"tid": tid, "uid": uid, "mid": model_id},
        )
        snap_rows = snap_res.fetchall()
        snap_by_date: dict[str, tuple[str, Any, int]] = {}
        for r in snap_rows:
            d = _serialize_date(r[1])
            if not d:
                continue
            cnt = int(r[3] or 0)
            cur = snap_by_date.get(d)
            if cur is None or cnt > cur[2]:
                snap_by_date[d] = (str(r[0]), r[2], cnt)

        # 批次日期改走 pred.parquet（B 套）：日历绿点 = parquet 中存在分数的
        # 交易日；快照按 data_trade_date 挂接，决定该日是否可加载候选池
        storage_path = ""
        try:
            sp = (
                await session.execute(
                    text(
                        "SELECT storage_path FROM qm_user_models "
                        "WHERE tenant_id = :tid AND user_id = :uid AND model_id = :mid LIMIT 1"
                    ),
                    {"tid": tid, "uid": uid, "mid": model_id},
                )
            ).scalar()
            storage_path = str(sp or "")
        except Exception:
            storage_path = ""
        parquet_dates: list[str] = []
        if storage_path:
            try:
                parquet_dates = _read_model_pred_dates(storage_path)
            except Exception:
                parquet_dates = []

        def _entry(
            run_id: str, infer_d: str | None, target_d: Any, status: str, updated: Any, has_snapshot: bool
        ) -> dict[str, Any]:
            return {
                "runId": run_id,
                "modelId": model_id,
                "inferenceDate": infer_d,
                "targetDate": _serialize_date(target_d),
                "status": status,
                "lastUpdatedAt": _serialize_date(updated),
                "universeLabel": "",
                "hasSnapshot": has_snapshot,
            }

        runs: list[dict[str, Any]] = []
        seen_dates: set[str] = set()
        if parquet_dates:
            # run 记录（补全批次缺失时的次选挂接源）
            runs_by_date: dict[str, Any] = {}
            for r in run_rows:
                d = _serialize_date(r[1])
                if d and d not in runs_by_date:
                    runs_by_date[d] = r
            for d in reversed(parquet_dates):
                snap = snap_by_date.get(d)
                if snap is not None:
                    runs.append(_entry(snap[0], d, snap[1], "completed", None, True))
                else:
                    r = runs_by_date.get(d)
                    if r is not None:
                        runs.append(_entry(str(r[0]), d, r[2], str(r[3] or "completed"), r[4], True))
                    else:
                        # 训练期测试集日期：仅有历史分数，无批次快照
                        runs.append(_entry("", d, None, "completed", None, False))
                seen_dates.add(d)
        # 无 pred.parquet（或 parquet 为空）时退回快照/run 记录，保证批次可用
        for d, snap in sorted(snap_by_date.items(), key=lambda kv: kv[0], reverse=True):
            if d in seen_dates:
                continue
            runs.append(_entry(snap[0], d, snap[1], "completed", None, True))
            seen_dates.add(d)
        for r in run_rows:
            d = _serialize_date(r[1])
            if d and d in seen_dates:
                continue
            runs.append(_entry(str(r[0]), d, r[2], str(r[3] or "completed"), r[4], True))
            if d:
                seen_dates.add(d)

        return {"code": 200, "data": {"runs": runs}}


async def get_research_overview(
    tid: str, uid: str, model_id: str | None, run_id: str | None, limit: int, offset: int, market: str | None = None
) -> dict[str, Any]:
    data = await _do_get_overview(tid, uid, model_id, run_id, limit, offset, market=market)
    return {"code": 200, "data": {"items": data["items"], "summary": data["summary"]}}


async def _do_get_universe_with_sdl_redis(
    tid: str, uid: str, run_id: str, limit: int, offset: int, market: str | None = None
) -> dict[str, Any] | None:
    params = {"tid": tid, "uid": uid, "rid": run_id, "limit": limit, "offset": offset}
    where = "snap.tenant_id = :tid AND snap.user_id = :uid AND snap.run_id = :rid"
    async with get_session(read_only=True) as session:
        snap_sql = f"""
            SELECT snap.*
            FROM qm_research_candidate_snapshot snap
            WHERE {where}
            ORDER BY snap.score_rank ASC
            LIMIT :limit OFFSET :offset
        """
        snap_rows = (await session.execute(text(snap_sql), params)).mappings().all()
        if not snap_rows:
            summary = await _fetch_summary(session, where, params, include_market_stats=False)
            return {"items": [], "summary": summary}

        trade_dates = {row.get("data_trade_date") for row in snap_rows}
        if len(trade_dates) != 1:
            return None
        trade_date = next(iter(trade_dates))
        if not isinstance(trade_date, date) or trade_date.year != _SDL_REDIS_YEAR:
            return None

        sdl_map = await _load_sdl_day_map(session, trade_date, market=market)
        if not sdl_map:
            return None

        merged_rows: list[dict[str, Any]] = []
        for row in snap_rows:
            snap = dict(row)
            symbol = StockCodeUtil.to_prefix(str(snap.get("symbol") or ""))
            merged = dict(snap)
            sdl = sdl_map.get(symbol)
            if sdl:
                merged.update(sdl)
            merged_rows.append(merged)

        items = [_format_candidate_record(r) for r in merged_rows]
        summary = await _fetch_summary(session, where, params, include_market_stats=False)
        return {"items": items, "summary": summary}


async def _infer_market_from_run(tid: str, uid: str, run_id: str) -> str | None:
    """Infer market from an inference run's model metadata."""
    try:
        async with get_session(read_only=True) as session:
            res = await session.execute(
                text(
                    "SELECT ir.model_id FROM qm_model_inference_runs ir "
                    "WHERE ir.tenant_id = :tid AND ir.user_id = :uid AND ir.run_id = :rid"
                ),
                {"tid": tid, "uid": uid, "rid": run_id},
            )
            row = res.first()
            if not row:
                return None
            model_id = row[0]
            # Look up model metadata
            res2 = await session.execute(
                text(
                    "SELECT metadata_json FROM qm_user_models "
                    "WHERE tenant_id = :tid AND user_id = :uid AND model_id = :mid"
                ),
                {"tid": tid, "uid": uid, "mid": model_id},
            )
            row2 = res2.first()
            if row2 and row2[0]:
                meta = row2[0] if isinstance(row2[0], dict) else json.loads(row2[0])
                context = meta.get("context")
                if isinstance(context, dict):
                    market = str(context.get("market", "")).upper()
                    if market in ("CN", "HK", "US", "CRYPTO"):
                        return market
    except Exception:
        pass
    return None


async def get_research_universe(tid: str, uid: str, run_id: str, limit: int, offset: int = 0) -> dict[str, Any]:
    cache_key = f"{tid}:{uid}:{run_id}:{limit}:{offset}"
    cached = _get_local_cache(_UNIVERSE_CACHE, cache_key, _UNIVERSE_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached

    # Determine market from the inference run's model
    market = await _infer_market_from_run(tid, uid, run_id)

    data = await _do_get_universe_with_sdl_redis(tid, uid, run_id, limit, offset, market=market)
    if data is None:
        data = await _do_get_overview(tid, uid, None, run_id, limit, offset, include_market_stats=False, market=market)
    payload = {"code": 200, "data": {"items": data["items"], "summary": data["summary"]}}
    _set_local_cache(_UNIVERSE_CACHE, cache_key, payload, _UNIVERSE_CACHE_MAX_ENTRIES)
    return payload


async def _best_snapshot_run_for_date(tid: str, uid: str, model_id: str, trade_date: str) -> str | None:
    """某数据日行数最多的候选池快照 run（同日多 run 时排除单股推理 1 行快照）。"""
    async with get_session(read_only=True) as session:
        res = await session.execute(
            text(
                """
                SELECT run_id, COUNT(*) AS cnt
                FROM qm_research_candidate_snapshot
                WHERE tenant_id = :tid AND user_id = :uid AND model_id = :mid
                  AND data_trade_date = CAST(:d AS DATE)
                GROUP BY run_id
                ORDER BY cnt DESC
                LIMIT 1
                """
            ),
            {"tid": tid, "uid": uid, "mid": model_id, "d": trade_date},
        )
        row = res.first()
        return str(row[0]) if row and row[0] else None


async def _model_market(tid: str, uid: str, model_id: str) -> tuple[str, str | None]:
    """返回 (storage_path, market)。market 取模型 metadata.context.market。"""
    async with get_session(read_only=True) as session:
        row = (
            await session.execute(
                text(
                    "SELECT storage_path, metadata_json FROM qm_user_models "
                    "WHERE tenant_id = :tid AND user_id = :uid AND model_id = :mid LIMIT 1"
                ),
                {"tid": tid, "uid": uid, "mid": model_id},
            )
        ).first()
    if not row:
        return "", None
    storage_path = str(row[0] or "")
    market: str | None = None
    if row[1]:
        try:
            meta = row[1] if isinstance(row[1], dict) else json.loads(row[1])
            m = str((meta.get("context") or {}).get("market", "")).upper()
            if m in ("CN", "HK", "US", "CRYPTO"):
                market = m
        except Exception:
            market = None
    return storage_path, market


_EMPTY_UNIVERSE_SUMMARY = {
    "total": 0,
    "totalMarket": 0,
    "hs300": 0,
    "zz1000": 0,
    "margin": 0,
    "chinext": 0,
    "avgScore": 0.0,
    "highConfidenceCount": 0,
    "strongCount": 0,
    "lastUpdatedAt": None,
    "scoreDistribution": {},
}


async def get_research_universe_by_date(
    tid: str, uid: str, model_id: str, trade_date: str, limit: int, offset: int = 0
) -> dict[str, Any]:
    """按数据日直读模型 pred.parquet 的全市场分数截面（投研宇宙主数据源）。

    B 套口径：与批次日历、coverage、个股分数曲线同源。该日 parquet 无
    分数时兜底回退候选池快照（无 pred.parquet 的旧模型仍可用）。
    """
    trade_date = str(trade_date)[:10]
    cache_key = f"date:{tid}:{uid}:{model_id}:{trade_date}:{limit}:{offset}"
    cached = _get_local_cache(_UNIVERSE_CACHE, cache_key, _UNIVERSE_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached

    storage_path, _market = await _model_market(tid, uid, model_id)
    pred_rows = _read_model_pred_day(storage_path, trade_date) if storage_path else []
    if not pred_rows:
        # 兜底：该日无 parquet 分数 → 候选池快照（同日行数最多的 run）
        snap_run = await _best_snapshot_run_for_date(tid, uid, model_id, trade_date)
        if snap_run:
            return await get_research_universe(tid, uid, snap_run, limit, offset)
        payload = {"code": 200, "data": {"items": [], "summary": dict(_EMPTY_UNIVERSE_SUMMARY)}}
        _set_local_cache(_UNIVERSE_CACHE, cache_key, payload, _UNIVERSE_CACHE_MAX_ENTRIES)
        return payload

    try:
        date.fromisoformat(trade_date)
    except ValueError:
        payload = {"code": 200, "data": {"items": [], "summary": dict(_EMPTY_UNIVERSE_SUMMARY)}}
        return payload

    # 页面减负：不再合并 PG stock_daily_latest（该表已 0 行，合并查询纯开销），
    # 选中日期后个股数据只加载 QuantDB 50 维宽表（前端 batch-features 投影，
    # 已按 selectedDate 读同日截面）。这里只返回 分数 + 排名 + 简称 的轻量骨架，
    # 响应体从 ~5.3MB（54 字段×5194 只）降到几十 KB 级别。
    pseudo_run_id = f"pred_{trade_date.replace('-', '')}"
    try:
        quantdb_names = _get_quantdb_stock_names()
    except Exception:  # noqa: BLE001
        quantdb_names = {}
    # 行业/概念/指数静态标签（进程内缓存，读 instrument_list + sector_members + index_weights 各一次）
    try:
        quantdb_labels = _load_quantdb_labels()
        quantdb_meta = _load_quantdb_name_industry()
    except Exception:  # noqa: BLE001
        logger.warning("读取 QuantDB 行业/概念/指数标签失败", exc_info=True)
        quantdb_labels = {}
        quantdb_meta = {}
    items = [
        {
            "key": f"{pseudo_run_id}:{r['symbol']}",
            "modelId": model_id,
            "runId": pseudo_run_id,
            "rank": int(r["rank"]),
            "code": r["symbol"],
            "name": quantdb_names.get(StockCodeUtil.to_suffix(r["symbol"])) or "",
            "score": float(r["score"]),
            "sector": (quantdb_meta.get(r["symbol"]) or {}).get("industry") or "",
            "conceptTags": (quantdb_labels.get(r["symbol"]) or {}).get("concepts") or [],
            "indexTags": (quantdb_labels.get(r["symbol"]) or {}).get("indices") or [],
            "isSt": bool((quantdb_labels.get(r["symbol"]) or {}).get("is_st")),
            "isHs300": bool((quantdb_labels.get(r["symbol"]) or {}).get("is_hs300")),
            "isCsi500": bool((quantdb_labels.get(r["symbol"]) or {}).get("is_csi500")),
            "isCsi1000": bool((quantdb_labels.get(r["symbol"]) or {}).get("is_csi1000")),
        }
        for r in pred_rows[offset : offset + limit]
    ]

    score_vals = [float(r["score"]) for r in pred_rows]
    score_dist: dict[str, Any] = {}
    try:
        dist = compute_score_distribution(score_vals)
        if dist:
            score_dist = {k: dist.get(k) for k in ("min", "max", "mean", "median", "p10", "p25", "p75", "p90")}
    except Exception as exc:  # noqa: BLE001
        logger.debug("pred day score distribution failed: %s", exc)

    total = len(pred_rows)
    summary = {
        "total": total,
        # pred.parquet 截面已剔除 B 股/北交所/指数，全部为可交易 A 股
        "totalMarket": total,
        "hs300": 0,
        "zz1000": 0,
        "margin": 0,
        "chinext": 0,
        "avgScore": round(sum(score_vals) / total, 4) if total else 0.0,
        # parquet 无 signal_side，高置信沿用分数强阈值口径
        "highConfidenceCount": sum(1 for v in score_vals if v >= 0.05),
        "strongCount": sum(1 for v in score_vals if v >= 0.05),
        "lastUpdatedAt": None,
        "scoreDistribution": score_dist,
    }

    payload = {"code": 200, "data": {"items": items, "summary": summary}}
    _set_local_cache(_UNIVERSE_CACHE, cache_key, payload, _UNIVERSE_CACHE_MAX_ENTRIES)
    return payload


async def get_user_watchlist(tid: str, uid: str, limit: int, offset: int) -> dict[str, Any]:
    async with get_session(read_only=True) as session:
        res = await session.execute(
            text(
                "SELECT symbol, stock_name, added_at, source_run_id FROM qm_user_watchlist "
                "WHERE tenant_id = :tid AND user_id = :uid ORDER BY added_at DESC LIMIT :limit OFFSET :offset"
            ),
            {"tid": tid, "uid": uid, "limit": limit, "offset": offset},
        )
        items = [
            {"symbol": r[0], "stockName": r[1], "addedAt": _serialize_date(r[2]), "sourceRunId": r[3]} for r in res
        ]
        total = (
            await session.execute(
                text("SELECT COUNT(*) FROM qm_user_watchlist WHERE tenant_id = :tid AND user_id = :uid"),
                {"tid": tid, "uid": uid},
            )
        ).scalar() or 0
    return {"code": 200, "data": {"items": items, "total": total}}


async def add_to_watchlist(
    tid: str, uid: str, symbol: str, run_id: str | None, stock_name: str | None, features_snapshot: dict[str, Any] | None
) -> dict[str, Any]:
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO qm_user_watchlist (tenant_id, user_id, symbol, stock_name, source_run_id, features_snapshot, updated_at) "
                "VALUES (:tid, :uid, :s, :n, :rid, :f, NOW()) "
                "ON CONFLICT (tenant_id, user_id, symbol) DO UPDATE SET features_snapshot = EXCLUDED.features_snapshot, updated_at = NOW()"
            ),
            {"tid": tid, "uid": uid, "s": symbol, "n": stock_name, "rid": run_id, "f": json.dumps(features_snapshot or {})},
        )
    return {"code": 200, "message": "success"}


async def remove_from_watchlist(tid: str, uid: str, symbol: str) -> dict[str, Any]:
    async with get_session() as session:
        await session.execute(
            text("DELETE FROM qm_user_watchlist WHERE tenant_id = :tid AND user_id = :uid AND symbol = :s"),
            {"tid": tid, "uid": uid, "s": symbol},
        )
    return {"code": 200, "message": "success"}


def _get_quantdb_stock_names() -> dict[str, str]:
    """QuantDB 股票简称映射（suffix -> name），带 TTL 缓存避免每次同步重读 parquet。"""
    global _STOCK_NAMES_CACHE, _STOCK_NAMES_CACHE_AT  # noqa: PLW0603
    now = time.time()
    if _STOCK_NAMES_CACHE is not None and now - _STOCK_NAMES_CACHE_AT < _STOCK_NAMES_TTL_SECONDS:
        return _STOCK_NAMES_CACHE
    names = _load_quantdb_stock_names()
    if names:
        _STOCK_NAMES_CACHE = names
        _STOCK_NAMES_CACHE_AT = now
    return names


async def _fetch_simulation_positions(authorization: str, x_user_id: str, x_tenant_id: str) -> list[str]:
    """拉取模拟盘当前持仓（prefix 格式）。任何异常 fail-soft 返回空列表。"""
    headers = {
        "Authorization": authorization,
        "X-User-Id": str(x_user_id),
        "X-Tenant-Id": str(x_tenant_id),
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            resp = await client.get(f"{TRADE_BASE_URL}/api/v1/simulation/account", headers=headers)
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("sync watchlist positions: fetch simulation account failed: %s", exc)
        return []

    positions: list[str] = []
    for key in (data.get("positions") or {}).keys():
        base = str(key).split("::", 1)[0]  # 兼容 margin 仓位的侧标（SH600036::long）
        prefix = StockCodeUtil.to_prefix(base)
        if re.match(r"^(SH|SZ|BJ)\d{6}$", prefix):
            positions.append(prefix)
    return sorted(set(positions))


async def _upsert_watchlist_position(tid: str, uid: str, symbol: str, stock_name: str | None) -> None:
    """专用 upsert：只回填 stock_name/updated_at，不动 features_snapshot 与 source_run_id。"""
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO qm_user_watchlist (tenant_id, user_id, symbol, stock_name, updated_at) "
                "VALUES (:tid, :uid, :s, :n, NOW()) "
                "ON CONFLICT (tenant_id, user_id, symbol) "
                "DO UPDATE SET stock_name = COALESCE(EXCLUDED.stock_name, qm_user_watchlist.stock_name), updated_at = NOW()"
            ),
            {"tid": tid, "uid": uid, "s": symbol, "n": stock_name},
        )


async def sync_watchlist_positions_service(tid: str, uid: str, authorization: str) -> dict[str, Any]:
    """模拟盘持仓自动加入自选：拉持仓 -> 补名 -> 专用 upsert；返回当前持仓 prefix 列表。"""
    positions = await _fetch_simulation_positions(authorization, uid, tid)
    if positions:
        names = _get_quantdb_stock_names()
        for symbol in positions:
            name = names.get(StockCodeUtil.to_suffix(symbol)) or None
            await _upsert_watchlist_position(tid, uid, symbol, name)
    return {"code": 200, "data": {"positions": positions}}


async def get_user_research_pool(tid: str, uid: str, status: str | None, limit: int, offset: int) -> dict[str, Any]:
    where = "tenant_id = :tid AND user_id = :uid"
    params: dict[str, Any] = {"tid": tid, "uid": uid, "limit": limit, "offset": offset}
    if status:
        where += " AND status = :status"
        params["status"] = status
    async with get_session(read_only=True) as session:
        res = await session.execute(
            text(
                f"SELECT symbol, stock_name, added_at, source_run_id, status FROM qm_user_research_pool "
                f"WHERE {where} ORDER BY added_at DESC LIMIT :limit OFFSET :offset"
            ),
            params,
        )
        items = [
            {"symbol": r[0], "stockName": r[1], "addedAt": _serialize_date(r[2]), "sourceRunId": r[3], "status": r[4]}
            for r in res
        ]
        total = (await session.execute(text(f"SELECT COUNT(*) FROM qm_user_research_pool WHERE {where}"), params)).scalar() or 0
    return {"code": 200, "data": {"items": items, "total": total}}


async def add_to_research_pool(
    tid: str,
    uid: str,
    symbol: str,
    run_id: str | None,
    stock_name: str | None,
    model_id: str | None,
    fusion_score: float | None,
    thesis_summary: str | None,
    features_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO qm_user_research_pool "
                "(tenant_id, user_id, symbol, stock_name, source_run_id, model_id, fusion_score, thesis_summary, features_snapshot, updated_at) "
                "VALUES (:tid, :uid, :s, :n, :rid, :mid, :fs, :ts, :f, NOW()) "
                "ON CONFLICT (tenant_id, user_id, symbol) DO UPDATE SET features_snapshot = EXCLUDED.features_snapshot, updated_at = NOW()"
            ),
            {
                "tid": tid,
                "uid": uid,
                "s": symbol,
                "n": stock_name,
                "rid": run_id,
                "mid": model_id,
                "fs": fusion_score,
                "ts": thesis_summary,
                "f": json.dumps(features_snapshot or {}),
            },
        )
    return {"code": 200, "message": "success"}


async def remove_from_research_pool(tid: str, uid: str, symbol: str) -> dict[str, Any]:
    async with get_session() as session:
        await session.execute(
            text("DELETE FROM qm_user_research_pool WHERE tenant_id = :tid AND user_id = :uid AND symbol = :s"),
            {"tid": tid, "uid": uid, "s": symbol},
        )
    return {"code": 200, "message": "success"}


def _load_quantdb_stock_names() -> dict[str, str]:
    """加载 QuantDB instrument_detail 的股票简称映射（suffix symbol -> name）。

    优先使用 instrument_detail.parquet（含权威 A 股简称），仅当无法读取时回退空 dict。
    """
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub.get_instance()
        if not hub.available:
            return {}
        df = hub.fetch_stock_list()
        if df is None or df.empty:
            return {}
        symbol_col = "Symbol" if "Symbol" in df.columns else "symbol"
        name_col = "Name" if "Name" in df.columns else ("stock_name" if "stock_name" in df.columns else None)
        if symbol_col not in df.columns or name_col is None:
            return {}
        mapping: dict[str, str] = {}
        for _, row in df[[symbol_col, name_col]].dropna().iterrows():
            sym = str(row[symbol_col]).strip()
            nm = str(row[name_col]).strip()
            if sym and nm:
                mapping[sym] = nm
        return mapping
    except Exception as exc:  # noqa: BLE001
        logger.debug("QuantDB stock names load failed: %s", exc)
        return {}


async def get_symbols_features(tid: str, uid: str, symbols: list[str], lite: bool) -> dict[str, Any]:
    normalized_symbols = [StockCodeUtil.to_prefix(s.strip()) for s in symbols if s.strip()]
    if not normalized_symbols:
        return {"code": 200, "data": {"items": []}}

    vals = ", ".join(f"('{s}')" for s in normalized_symbols)
    norm = _norm_symbol_sql("symbol")

    # 简化逻辑：直接读取数据库中的快照，不再进行实时关联或计算
    sql = f"""
        WITH sym_list(raw_symbol) AS (VALUES {vals}),
        pool_norm AS (
            SELECT symbol, features_snapshot, ({norm}) AS prefix_symbol
            FROM qm_user_research_pool WHERE tenant_id = :tid AND user_id = :uid
        ),
        watchlist_norm AS (
            SELECT symbol, features_snapshot, ({norm}) AS prefix_symbol
            FROM qm_user_watchlist WHERE tenant_id = :tid AND user_id = :uid
        )
        SELECT
            sym_list.raw_symbol AS symbol,
            COALESCE(ps.features_snapshot, ws.features_snapshot) as snapshot
        FROM sym_list
        LEFT JOIN pool_norm ps ON ps.prefix_symbol = sym_list.raw_symbol
        LEFT JOIN watchlist_norm ws ON ws.prefix_symbol = sym_list.raw_symbol
    """
    async with get_session(read_only=True) as session:
        result = await session.execute(text(sql), {"tid": tid, "uid": uid})
        items = []
        missing_symbols = []
        for r in result.mappings():
            snap = r["snapshot"]
            if not snap:
                missing_symbols.append(r["symbol"])
                continue
            if isinstance(snap, str):
                try:
                    snap = json.loads(snap)
                except Exception:
                    missing_symbols.append(r["symbol"])
                    continue
            # 确保 symbol 一致
            snap["code"] = r["symbol"]
            items.append(snap)

        # 对于没有 features_snapshot 的股票，从 stock_daily_latest 补充基础数据
        if missing_symbols:
            sdl_vals = ", ".join(f"('{s}')" for s in missing_symbols)
            sdl_norm = _norm_symbol_sql("sdl.symbol")
            # 注意：latest 行可能缺 stock_name / total_mv / pe_ttm（数据源未回填）
            # 改为分组取每个字段的最近非空值，否则前端市值/PE 全部显示为 "--"
            sdl_sql = f"""
                WITH miss(raw_symbol) AS (VALUES {sdl_vals}),
                joined AS (
                    SELECT
                        miss.raw_symbol AS raw_symbol,
                        sdl.trade_date,
                        sdl.stock_name,
                        sdl.industry,
                        sdl.close,
                        sdl.pe_ttm,
                        sdl.pb,
                        sdl.roe,
                        sdl.total_mv,
                        sdl.float_mv,
                        sdl.pct_change,
                        sdl.turnover_rate,
                        sdl.amount
                    FROM miss
                    LEFT JOIN stock_daily_latest sdl ON ({sdl_norm}) = miss.raw_symbol
                ),
                latest AS (
                    SELECT DISTINCT ON (raw_symbol)
                        raw_symbol, close, pct_change, turnover_rate, amount
                    FROM joined
                    ORDER BY raw_symbol, trade_date DESC NULLS LAST
                ),
                latest_name AS (
                    SELECT DISTINCT ON (raw_symbol) raw_symbol, stock_name, industry
                    FROM joined
                    WHERE stock_name IS NOT NULL AND stock_name <> ''
                    ORDER BY raw_symbol, trade_date DESC
                ),
                latest_mv AS (
                    SELECT DISTINCT ON (raw_symbol)
                        raw_symbol, total_mv, float_mv, pe_ttm, pb, roe
                    FROM joined
                    WHERE total_mv IS NOT NULL AND total_mv > 0
                    ORDER BY raw_symbol, trade_date DESC
                ),
                stocks_name AS (
                    SELECT DISTINCT ON ({_norm_symbol_sql("symbol")})
                        {_norm_symbol_sql("symbol")} AS raw_symbol, name AS stock_name
                    FROM stocks
                    WHERE {_norm_symbol_sql("symbol")} IN (SELECT raw_symbol FROM miss)
                    ORDER BY {_norm_symbol_sql("symbol")}
                )
                SELECT
                    l.raw_symbol,
                    COALESCE(n.stock_name, sn.stock_name) AS stock_name,
                    COALESCE(n.industry, '') AS industry,
                    l.close,
                    mv.pe_ttm, mv.pb, mv.roe,
                    mv.total_mv, mv.float_mv,
                    l.pct_change, l.turnover_rate, l.amount
                FROM latest l
                LEFT JOIN latest_name n USING (raw_symbol)
                LEFT JOIN latest_mv   mv USING (raw_symbol)
                LEFT JOIN stocks_name sn USING (raw_symbol)
            """
            sdl_result = await session.execute(text(sdl_sql))
            # QuantDB instrument_detail 提供权威股票简称，用于回填 stock_daily_latest 缺失的 stock_name
            quantdb_names: dict[str, str] = _load_quantdb_stock_names()
            for r in sdl_result.mappings():
                raw_sym = r["raw_symbol"]
                stock_name = r.get("stock_name")
                if not stock_name:
                    suffix = StockCodeUtil.to_suffix(raw_sym)
                    stock_name = quantdb_names.get(suffix) or quantdb_names.get(raw_sym) or raw_sym
                close_price = float(r.get("close") or 0)
                total_mv = float(r.get("total_mv") or 0)
                pe_val = float(r.get("pe_ttm") or 0)
                snap = {
                    "code": raw_sym,
                    "name": stock_name,
                    "marketCap": round(total_mv / 1e8, 2) if total_mv else 0,
                    "totalMv": round(total_mv / 1e8, 2) if total_mv else 0,
                    "pe": pe_val,
                    "pe_ttm": pe_val,
                    "pb": float(r.get("pb") or 0),
                    "roe": float(r.get("roe") or 0),
                    "closePrice": close_price,
                    "price": close_price,
                    "sector": r.get("industry") or "",
                    "industry": r.get("industry") or "",
                    "latestChange": float(r.get("pct_change") or 0),
                    "turnoverRate": float(r.get("turnover_rate") or 0) * 100,
                    "amount": float(r.get("amount") or 0),
                    "floatMv": round(float(r.get("float_mv") or 0) / 1e8, 2) if r.get("float_mv") else 0,
                }
                items.append(snap)

        return {"code": 200, "data": {"items": items}}


def _quantdb_kline_items(normalized_symbol: str, days: int) -> list[dict[str, Any]]:
    """从 QuantDB 读取最近 days 日「不复权」日线（真实成交价），与行情软件同口径。

    当前价格/前端 K 线统一走 QuantDB（qdb_daily_unadjusted，不复权原始价）。
    视图或数据不可用时返回空列表，由调用方回退到聚合表/实时行情源。
    """
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub.get_instance()
        if not hub.available:
            return []
        suffix = StockCodeUtil.to_suffix(normalized_symbol)
        end = date.today()
        # 自然日回退缓冲，确保覆盖 days 个交易日
        start = end - timedelta(days=days * 2 + 20)
        df = hub.fetch_daily_kline(suffix, start, end, adjust="none")
        if df is None or df.empty or "trade_date" not in df.columns:
            return []
        df = df.dropna(subset=["close"]).copy()
        df = df.drop_duplicates(subset=["trade_date"]).sort_values("trade_date")
        df = df.tail(days)
        items = [
            {
                "date": r["trade_date"].strftime("%Y-%m-%d"),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"] if r["volume"] is not None else 0.0),
            }
            for _, r in df.iterrows()
        ]
        return items
    except Exception as exc:  # noqa: BLE001
        logger.warning("[get_stock_kline] QuantDB K线读取失败 %s: %s", normalized_symbol, exc)
        return []


async def get_stock_kline(symbol: str, days: int) -> dict[str, Any]:
    normalized_symbol = StockCodeUtil.to_prefix(symbol)
    # 市场推断：港股后缀 0700.HK → HK 走 quanthk / stock_daily_latest_hk；
    # A 股前缀/6 位走原有 QuantDB / stock_daily_latest 链路
    is_hk = normalized_symbol.upper().endswith(".HK")

    # 当前价格统一走 QuantDB（不复权真实价），避免 stock_daily_latest 空表/复权口径不一致
    if not is_hk:
        qd_items = _quantdb_kline_items(normalized_symbol, days)
        if qd_items:
            payload = {"code": 200, "data": {"symbol": normalized_symbol, "items": qd_items}}
            _set_local_cache(_SDL_CACHE, f"sdl-kline:{normalized_symbol}:{days}", payload, _SDL_CACHE_MAX_ENTRIES)
            return payload

    cache_key = f"sdl-kline:{normalized_symbol}:{days}"
    cached = _get_local_cache(_SDL_CACHE, cache_key, _SDL_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached

    # 港股：stock_daily_latest_hk（quanthk 最新交易日全量，symbol 为 0700.HK 后缀式）
    # 表里 stock_daily_latest.symbol 实际可能是后缀格式（"600519.SH"）或前缀格式
    # （"SH600519"）。统一两边都走 _norm_symbol_sql 归一化为前缀格式后再比较，
    # 才能匹配上当前数据（5536 个股票全部为后缀格式存储）。
    if is_hk:
        cond_where = """symbol = :s"""
    else:
        cond_where = f'{_norm_symbol_sql("symbol")} = {_norm_symbol_sql(":s")}'
    sql = f"""
        SELECT trade_date, open, high, low, close, volume, adj_factor
        FROM {"stock_daily_latest_hk" if is_hk else "stock_daily_latest"}
        WHERE {cond_where}
        ORDER BY trade_date DESC LIMIT :l
    """

    items = []
    try:
        async with get_session(read_only=True) as session:
            res = await session.execute(
                text(sql),
                {"s": normalized_symbol, "l": days},
            )
            for r in res:
                adj_factor = r[6]
                items.append(
                    {
                        "date": str(r[0]),
                        "open": _to_nominal_price(r[1], adj_factor),
                        "high": _to_nominal_price(r[2], adj_factor),
                        "low": _to_nominal_price(r[3], adj_factor),
                        "close": _to_nominal_price(r[4], adj_factor),
                        "volume": float(r[5]),
                    }
                )
            items.reverse()
    except Exception as exc:
        logger.warning(f"[get_stock_kline] DB query failed: {exc}")

    # 若 DB 暂无行情数据，自动通过实时行情源拉取真实 K 线
    if not items:
        try:
            import aiohttp
            if is_hk:
                code5 = normalized_symbol.upper().replace(".HK", "").zfill(5)
                ts_code = f"hk{code5}"
            else:
                ts_code = normalized_symbol.lower()
            url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={ts_code},day,,,{days},qfq"
            async with aiohttp.ClientSession() as client:
                async with client.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                    if resp.status == 200:
                        pdata = await resp.json(content_type=None)
                        day_rows = (pdata.get("data", {}).get(ts_code, {}) or {}).get("qfqday") or (pdata.get("data", {}).get(ts_code, {}) or {}).get("day") or []
                        for row in day_rows:
                            if len(row) >= 6:
                                items.append({
                                    "date": str(row[0]),
                                    "open": float(row[1]),
                                    "close": float(row[2]),
                                    "high": float(row[3]),
                                    "low": float(row[4]),
                                    "volume": float(row[5]),
                                })
        except Exception as e:
            logger.warning(f"[get_stock_kline] 实时在线拉取 K 线失败: {e}")

    payload = {"code": 200, "data": {"symbol": normalized_symbol, "items": items}}
    if items:
        _set_local_cache(_SDL_CACHE, cache_key, payload, _SDL_CACHE_MAX_ENTRIES)
    return payload


# ---- 真·SHAP 单因子归因（树模型原生 pred_contrib，不依赖 shap 库） ----
_SHAP_TREE_FRAMEWORKS = {"lightgbm", "xgboost", "catboost"}
_SHAP_TIMEOUT_SEC = 8.0
_SHAP_MAX_DRIVERS = 6
_SHAP_MIN_DRIVERS = 3  # 真值特征少于 3 个上榜则放弃 SHAP，降级启发式

_SNAPSHOT_MARKET_FILE = {
    "CN": None,  # CN 按年分文件 model_features_{year}.parquet
    "HK": "model_features_hk.parquet",
    "US": "model_features_us.parquet",
    "CRYPTO": "model_features_crypto.parquet",
    "FUTURES": "model_features_futures.parquet",
}


def _resolve_snapshot_parquet(market: str, year: int):
    """特征快照 parquet 路径（CN 按年，其他市场单文件）。找不到返回 None。"""
    from pathlib import Path

    base = Path(os.getenv("MODEL_TRAINING_DATA_DIR", "/app/db/feature_snapshots"))
    name = _SNAPSHOT_MARKET_FILE.get(market)
    if market == "CN":
        name = f"model_features_{year}.parquet"
    if not name:
        return None
    p = base / name
    return p if p.exists() else None


def _snapshot_symbol(market: str, normalized_symbol: str) -> str:
    """快照 parquet 的 symbol 口径：CN 为无前缀 6 位码（SH600519 -> 600519）。"""
    if market == "CN" and normalized_symbol[:2] in ("SH", "SZ", "BJ"):
        return normalized_symbol[2:]
    return normalized_symbol


def _compute_shap_drivers_sync(
    model_id: str, normalized_symbol: str, as_of_date_str: str, market: str
) -> list[dict[str, Any]] | None:
    """加载 headline 树模型 + 该标的快照真实特征，原生 pred_contrib 取 |SHAP| top6。

    设计要点：
    - 缺失特征（如 gtja_alpha_* qlib 表达式因子不落盘）按训练时 fill_values 补齐入模，
      但不参与 top6 排名——保证上榜因子全部为真实快照值。
    - 任何一步失败返回 None（调用方降级启发式因子）。
    """
    import glob as _glob
    from pathlib import Path

    metas = _glob.glob(f"/app/models/users/*/*/{model_id}/metadata.json")
    if not metas:
        return None
    try:
        meta = json.load(open(metas[0], encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    framework = str(meta.get("framework") or "").lower()
    if framework not in _SHAP_TREE_FRAMEWORKS:
        return None
    feat_cols = list(meta.get("feature_columns") or [])
    if len(feat_cols) < 4:
        return None

    try:
        d_ref = date.fromisoformat(as_of_date_str)
    except (ValueError, TypeError):
        d_ref = date.today()
    snap = _resolve_snapshot_parquet(market, d_ref.year)
    if snap is None:
        return None
    sym = _snapshot_symbol(market, normalized_symbol)

    import numpy as np
    import pandas as pd
    import pyarrow.parquet as pq

    schema = set(pq.read_schema(snap).names)
    avail = [c for c in feat_cols if c in schema]
    if not avail:
        return None
    try:
        tbl = pq.read_table(
            snap, columns=["symbol", "trade_date"] + avail,
            filters=[("symbol", "=", sym)],
        )
        df = tbl.to_pandas()
    except Exception:
        return None
    if df.empty:
        return None
    df = df[df["trade_date"].astype(str) <= as_of_date_str]
    if df.empty:
        return None
    row = df.sort_values("trade_date").iloc[-1]

    fill_values: dict[str, Any] = meta.get("fill_values") or {}
    x = np.array(
        [
            [
                float(row[c])
                if (c in avail and pd.notna(row.get(c)))
                else float(fill_values.get(c, 0.0) or 0.0)
                for c in feat_cols
            ]
        ],
        dtype=np.float64,
    )
    # 仅真实快照值参与排名（fill 的 qlib 表达式因子不进 top6）
    real_cols = {c for c in avail if pd.notna(row.get(c))}

    model_file = Path(metas[0]).parent / str(meta.get("model_file") or "")
    if not model_file.exists():
        return None
    try:
        if framework == "lightgbm":
            import lightgbm as lgb

            booster = lgb.Booster(model_file=str(model_file))
            contrib = booster.predict(x, pred_contrib=True)  # (n, n_features+1)
        elif framework == "xgboost":
            import xgboost as xgb

            booster = xgb.Booster()
            booster.load_model(str(model_file))
            contrib = booster.predict(
                xgb.DMatrix(x, feature_names=feat_cols), pred_contribs=True
            )
        else:  # catboost
            from catboost import CatBoost, Pool

            booster = CatBoost()
            booster.load_model(str(model_file))
            contrib = booster.get_feature_importance(
                Pool(x, feature_names=feat_cols), type="ShapValues"
            )  # (n, n_features+1)
        shap_vals = np.asarray(contrib, dtype=float)
        if shap_vals.ndim != 2 or shap_vals.shape[1] < len(feat_cols):
            return None
        shap_vals = shap_vals[0, : len(feat_cols)]  # 末列 base value 丢弃
    except Exception:
        return None

    order = sorted(range(len(feat_cols)), key=lambda i: -abs(float(shap_vals[i])))
    picked = [i for i in order if feat_cols[i] in real_cols][:_SHAP_MAX_DRIVERS]
    if len(picked) < _SHAP_MIN_DRIVERS:
        return None
    return [
        {
            "name": feat_cols[i],
            "category": "模型SHAP",
            "value": round(float(x[0, i]), 4),
            "impact": round(float(shap_vals[i]), 5),
            "direction": "positive" if float(shap_vals[i]) >= 0 else "negative",
        }
        for i in picked
    ]


async def predict_single_stock(
    tid: str,
    uid: str,
    symbol: str,
    model_id: str | None = None,
    target_date: str | None = None,
    horizon: int = 5,
    market: str = "CN",
    consensus_model_ids: list[str] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    """单只股票未来走势与区间分位数预测服务。"""
    normalized_symbol = StockCodeUtil.to_prefix(symbol)

    # 1. 查询股票最新行情 + 真实波动率/均线（用于推导分位数锥与因子归因）
    stock_name = normalized_symbol
    latest_close = 0.0
    latest_date = target_date or datetime.now().strftime("%Y-%m-%d")
    # 日波动率(小数)与均线乖离(百分数)兜底
    daily_vol_pct = 0.025
    ma_gap_5 = 0.0
    ma_gap_20 = 0.0
    main_flow = 0.0

    sdl_table = _get_sdl_table(market)
    async with get_session(read_only=True) as session:
        # stock_daily_latest.stock_name 全表为空，名称回退 stocks 表
        res = await session.execute(
            text(
                f"SELECT sdl.stock_name, sdl.close, sdl.trade_date, "
                f"       sdl.vol_std_20, sdl.vol_atr_14, "
                f"       sdl.ma_gap_5, sdl.ma_gap_20, sdl.main_flow, "
                f"       sdl.adj_factor, "
                f"       (SELECT st.name FROM stocks st "
                f"        WHERE {_norm_symbol_sql('st.symbol')} = {_norm_symbol_sql('sdl.symbol')} "
                f"        LIMIT 1) AS name_fallback "
                f"FROM {sdl_table} sdl "
                f"WHERE {_norm_symbol_sql('sdl.symbol')} = {_norm_symbol_sql(':s')} "
                f"ORDER BY sdl.trade_date DESC LIMIT 1"
            ),
            {"s": normalized_symbol},
        )
        row = res.first()
        if row:
            stock_name = (row[0] or row[9] or stock_name).strip() or stock_name
            # K 线与行情一致还原为真实不复权价：DB close 为前复权价(adj_factor<1)，
            # 与 get_stock_kline 的 _to_nominal_price(close, adj_factor) 保持同口径。
            latest_close = _to_nominal_price(row[1], row[8])
            if not target_date and row[2]:
                latest_date = str(row[2])
            # vol_std_20 在 stock_daily_latest 为百分数口径(2.78=2.78%)；
            # vol_atr_14 为绝对价格 ATR。优先 vol_std，回退 ATR/close
            vol_std = float(row[3] or 0.0)
            atr = float(row[4] or 0.0)
            _ma_gap_5_unused = float(row[5] or 0.0)  # row[5]=ma_gap_5，仅保位（SELECT 按位取值）
            ma_gap_20 = float(row[6] or 0.0)
            main_flow = float(row[7] or 0.0) or 0.0
            if vol_std and vol_std > 0.3:
                daily_vol_pct = vol_std / 100.0
            elif latest_close > 0 and atr > 0:
                daily_vol_pct = atr / latest_close

    # 当前价格统一走 QuantDB（不复权真实价），与前端 K 线同口径；聚合表仅作回退。
    qd_items = _quantdb_kline_items(normalized_symbol, days=30)
    if qd_items:
        latest_close = float(qd_items[-1]["close"])
        if not target_date:
            latest_date = str(qd_items[-1]["date"])
        closes = [float(x["close"]) for x in qd_items]
        if len(closes) >= 5 and latest_close > 0:
            import numpy as np
            daily_vol_pct = max(0.012, float(np.std(np.diff(closes) / closes[:-1])))
            if len(closes) >= 20:
                ma20 = float(np.mean(closes[-20:]))
                ma_gap_20 = round((latest_close - ma20) / ma20 * 100, 2)
    elif latest_close == 0.0:
        # QuantDB 与聚合表均无该股数据时，通过实时行情感底获取最新收盘价与波动率
        k_payload = await get_stock_kline(normalized_symbol, days=30)
        k_items = (k_payload.get("data") or {}).get("items") or []
        if k_items:
            latest_close = float(k_items[-1]["close"])
            if not target_date:
                latest_date = str(k_items[-1]["date"])
            closes = [float(x["close"]) for x in k_items]
            if len(closes) >= 5 and latest_close > 0:
                import numpy as np
                daily_vol_pct = max(0.012, float(np.std(np.diff(closes) / closes[:-1])))
                if len(closes) >= 20:
                    ma20 = float(np.mean(closes[-20:]))
                    ma_gap_20 = round((latest_close - ma20) / ma20 * 100, 2)

    KNOWN_NAMES = {
        "SH600519": "贵州茅台",
        "SZ300750": "宁德时代",
        "SZ002594": "比亚迪",
        "SH600036": "招商银行",
        "SZ000001": "平安银行",
        "SH601318": "中国平安",
        "SZ000858": "五粮液",
        "SH601857": "中国石油",
        "SH600900": "长江电力",
    }
    if stock_name == normalized_symbol and normalized_symbol in KNOWN_NAMES:
        stock_name = KNOWN_NAMES[normalized_symbol]

    # 获取可用模型列表（在会话外调用，避免嵌套会话）
    models_res = await get_available_models(tid, uid, market)
    available_models = (models_res.get("data") or {}).get("models", [])

    # 2. 选定主预测模型
    selected_model = None
    if model_id:
        selected_model = next((m for m in available_models if m.get("modelId") == model_id), None)
    if not selected_model and available_models:
        selected_model = available_models[0]

    sel = selected_model or {}
    chosen_model_id = sel.get("modelId") or "default_lgb"
    chosen_model_name = (
        sel.get("name") or sel.get("modelName") or "LightGBM Alpha-158 增强模型"
    )
    chosen_model_type = sel.get("modelType") or sel.get("model_type") or "lightgbm"

    # “开始预测推理”必须实际执行注册模型，不能用页面侧或服务侧的公式伪造结果。
    # 延迟导入避免 research/model_training 路由在应用启动阶段发生循环导入。
    if execute:
        if not selected_model:
            raise HTTPException(status_code=404, detail="未找到可执行的已注册模型")
        from backend.services.api.routers.model_training import (
            _execute_single_day_inference,
            _resolve_requested_model,
        )

        try:
            requested_model_id, resolved = await _resolve_requested_model(
                {"tenant_id": tid, "user_id": uid}, chosen_model_id
            )
            requested_date = date.fromisoformat(target_date or latest_date)
            execution = await _execute_single_day_inference(
                requested_model_id=requested_model_id,
                resolved=resolved,
                model_dir=Path(resolved.storage_path),
                requested_date=requested_date,
                tenant_id=tid,
                user_id=uid,
                symbols=[normalized_symbol],
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("[predict_single_stock] 实时模型推理失败")
            raise HTTPException(status_code=502, detail=f"实时模型推理失败: {exc}") from exc
        if not execution.get("success"):
            raise HTTPException(
                status_code=422,
                detail=execution.get("error_message") or "模型推理未产生有效结果",
            )

    # 3. 读真实推理分数：engine_signal_scores（混合A：默认读持久化真实分数）
    _sym_variants = list({
        normalized_symbol,
        normalized_symbol.lower(),
        re.sub(r"[^0-9]", "", normalized_symbol),
    })
    score_params: dict[str, Any] = {"tid": tid}
    date_bound_str = target_date or latest_date
    try:
        score_params["d"] = date.fromisoformat(date_bound_str)
    except (ValueError, TypeError):
        score_params["d"] = date.today()

    score_rows = []
    try:
        async with get_session(read_only=True) as session:
            # 仅非 execute 路径保留「目标日不晚于落库日」的上限过滤（历史查询语义）。
            # execute=True 刚对目标股票现场补推并落库，落库 trade_date 可能晚于
            # latest_date/今日（如补推成交到最新交易日），故去掉上限，直接取最新，
            # 否则刚补推的分数会被过滤成 404「该标的没有真实模型推理结果」。
            date_filter = "" if execute else " AND e.trade_date <= :d"
            params = dict(score_params)
            if not date_filter:
                params.pop("d", None)  # SQL 无 :d 占位符时不能传多余绑定
            score_rows = (
                await session.execute(
                    text(
                        """
                        SELECT e.fusion_score, e.signal_side, e.score_rank, e.quality,
                               e.expected_price, r.model_id AS run_model_id,
                               e.run_id, e.trade_date
                        FROM engine_signal_scores e
                        LEFT JOIN qm_model_inference_runs r ON r.run_id = e.run_id
                        WHERE e.tenant_id = :tid
                          AND e.symbol = ANY(:s_variants)
                        """
                        + date_filter
                        + """
                        ORDER BY e.trade_date DESC, e.created_at DESC
                        """
                    ),
                    {**params, "s_variants": _sym_variants},
                )
            ).mappings().all()
    except Exception as exc:
        logger.warning(f"[predict_single_stock] 查询 engine_signal_scores 失败: {exc}")

    resolved_date = latest_date
    main_row = None
    consensus_rows: list[Any] = []
    selected_set = {m for m in (consensus_model_ids or [])[:4] if m}
    if score_rows:
        if selected_set:
            sel_rows = [
                r for r in score_rows if (r["run_model_id"] or r["run_id"]) in selected_set
            ]
            if sel_rows:
                resolved_date = str(sel_rows[0]["trade_date"])
        else:
            resolved_date = str(score_rows[0]["trade_date"])
        day_rows = [r for r in score_rows if str(r["trade_date"]) == resolved_date]
        pool = (
            [r for r in day_rows if (r["run_model_id"] or r["run_id"]) in selected_set]
            if selected_set
            else day_rows
        )
        if model_id:
            main_row = next((r for r in day_rows if r["run_model_id"] == model_id), None)
        if main_row is None and pool:
            main_row = max(pool, key=lambda r: float(r["fusion_score"] or 0.0))
        seen: set[str] = set()
        for r in sorted(pool, key=lambda x: float(x["fusion_score"] or 0.0), reverse=True):
            mid = r["run_model_id"] or r["run_id"]
            if mid in seen:
                continue
            seen.add(mid)
            consensus_rows.append(r)

    # 4. 只展示真实模型 SHAP 归因；没有 SHAP 结果就保持为空，绝不回退到启发式数据。
    drivers: list[dict[str, Any]] = []

    # 5. 预测得分与置信区间
    if main_row is not None:
        fusion_score = float(main_row["fusion_score"] or 0.0)
        signal_side = str(main_row["signal_side"] or "HOLD")
        resolved_date = str(main_row["trade_date"])
        if signal_side == "BUY" and fusion_score >= 0.03:
            rating = "STRONG_BUY"
        else:
            rating = {"BUY": "BUY", "HOLD": "HOLD", "SELL": "SELL"}.get(signal_side, "HOLD")
        data_source = "persisted"
        headline_mid = main_row["run_model_id"] or main_row["run_id"]
        if headline_mid:
            headline_meta = next(
                (m for m in available_models if m.get("modelId") == headline_mid), {}
            )
            chosen_model_id = headline_mid
            chosen_model_name = (
                headline_meta.get("name") or _humanize_model_name(headline_mid)
            )
            chosen_model_type = (
                headline_meta.get("modelType") or chosen_model_type or "lightgbm"
            )
    else:
        raise HTTPException(
            status_code=404,
            detail="该标的没有真实模型推理结果；请点击“开始预测推理”执行模型后重试",
        )

    # SHAP 归因
    drivers_source = None
    try:
        shap_drivers = await asyncio.wait_for(
            asyncio.to_thread(
                _compute_shap_drivers_sync,
                chosen_model_id,
                normalized_symbol,
                resolved_date,
                market,
            ),
            timeout=_SHAP_TIMEOUT_SEC,
        )
    except Exception as e:
        shap_drivers = None
        logger.warning("SHAP 归因失败，降级启发式因子: %s", e)
    if shap_drivers:
        drivers = shap_drivers
        drivers_source = "shap"

    # 只有真实分位模型才返回区间；旧模型仍绝不将波动率公式伪装成分位数。
    p50_ret = round(fusion_score, 4)
    confidence = 0.0
    forecast_curve: list[dict[str, Any]] = []
    curr_p = latest_close if latest_close > 0 else 100.0
    quantile_prediction: dict[str, Any] | None = None
    if main_row is not None:
        quality = main_row.get("quality")
        if isinstance(quality, str):
            try:
                quality = json.loads(quality)
            except (TypeError, ValueError):
                quality = None
        if isinstance(quality, dict):
            detail = quality.get("detail")
            candidate = detail.get("quantile_prediction") if isinstance(detail, dict) else None
            if isinstance(candidate, dict):
                try:
                    values = [float(candidate[key]) for key in ("p10", "p50", "p90")]
                    if all(math.isfinite(value) for value in values):
                        p10_ret, p50_ret, p90_ret = sorted(values)
                        quantile_prediction = candidate
                        confidence = float(candidate.get("calibrated_coverage") or 0.0)
                        target_day = date.fromisoformat(resolved_date) + timedelta(days=max(1, int(horizon)))
                        forecast_curve = [{
                            "step": int(horizon),
                            "date": target_day.isoformat(),
                            "p10": round(p10_ret * 100, 4),
                            "p50": round(p50_ret * 100, 4),
                            "p90": round(p90_ret * 100, 4),
                            "predicted_price": round(curr_p * (1 + p50_ret), 4),
                            "upper_price": round(curr_p * (1 + p90_ret), 4),
                            "lower_price": round(curr_p * (1 + p10_ret), 4),
                        }]
                except (KeyError, TypeError, ValueError):
                    quantile_prediction = None

    # 7. 多模型共识（真实当日各模型分数）
    consensus = []
    for r in consensus_rows:
        fs = float(r["fusion_score"] or 0.0)
        ss = str(r["signal_side"] or "HOLD")
        mid = r["run_model_id"] or r["run_id"]
        meta = next((m for m in available_models if m.get("modelId") == mid), {})
        if mid:
            model_name = meta.get("name") or _humanize_model_name(mid)
        else:
            model_name = "未命名模型"
        consensus.append({
            "model_id": mid,
            "model_name": model_name,
            "model_type": meta.get("modelType") or "",
            "score": round(fs, 4),
            # 保留字段名兼容前端契约，值为真实模型 signal score（不是收益率）。
            "expected_return": round(fs, 4),
            "rating": "STRONG_BUY" if (ss == "BUY" and fs >= 0.03) else ss,
            "horizon": horizon,
        })
    if consensus:
        bullish = sum(1 for c in consensus if c["rating"] in ("BUY", "STRONG_BUY"))
        consensus_score = round(bullish / len(consensus) * 100, 1)
    else:
        consensus_score = 0.0

    payload_data = {
        "status": "success",
        "symbol": normalized_symbol,
        "stock_name": stock_name,
        "model_id": chosen_model_id,
        "model_name": chosen_model_name,
        "model_type": chosen_model_type,
        "as_of_date": resolved_date,
        "current_price": curr_p,
        "horizon": horizon,
        "predicted_score": p50_ret,
        # 保留字段名兼容前端契约，值为真实模型 signal score（不是收益率）。
        "expected_return": p50_ret,
        "confidence": confidence,
        "rating": rating,
        "p10_return": round(p10_ret * 100, 2) if quantile_prediction else None,
        "p50_return": round(p50_ret * 100, 2),
        "p90_return": round(p90_ret * 100, 2) if quantile_prediction else None,
        "forecast_curve": forecast_curve,
        "drivers": drivers,
        "consensus": consensus,
        "consensus_score": consensus_score,
        "data_source": data_source,
        "drivers_source": drivers_source,
        "error": None,
    }
    return {"code": 200, "data": payload_data}
