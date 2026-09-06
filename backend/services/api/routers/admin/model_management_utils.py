import asyncio
import glob
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time as time_module
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
try:
    import exchange_calendars as xcals
except Exception:
    xcals = None

from backend.services.api.user_app.middleware.auth import require_admin
from backend.services.engine.inference.router_service import InferenceRouterService
from backend.services.engine.inference.script_runner import InferenceScriptRunner
from backend.shared.auth import get_internal_call_secret
from backend.shared.database_manager_v2 import get_session
from backend.shared.redis_sentinel_client import get_redis_sentinel_client
from backend.shared.trading_calendar import calendar_service
try:
    from backend.services.engine.qlib_app.celery_config import celery_app
except ImportError:
    celery_app = None

from .db import Base, DataFileRecord, ModelRecord, TrainingJobRecord  # noqa: F401 — ensure all models are registered in Base.metadata before create_all

router = APIRouter(dependencies=[Depends(require_admin)])  # 路由器级认证兜底

# 模型存放根目录（扫描所有子目录）
MODELS_ROOT = os.path.abspath(os.path.join(os.getcwd(), "models"))

# Engine 服务地址（与 engine_proxy 保持一致）
_ENGINE_BASE_URL = os.getenv("ENGINE_SERVICE_URL", "http://127.0.0.1:8001").rstrip("/")
_ENGINE_INTERNAL_SECRET = get_internal_call_secret()
# 每日推理分布式锁：TTL 30 分钟，防止 Admin 手动触发与 Celery Beat 09:15 并发冲突
_INFERENCE_LOCK_TTL_SEC = int(os.getenv("INFERENCE_LOCK_TTL_SEC", "1800"))
_INFERENCE_LOCK_KEY_PREFIX = "qm:lock:inference:daily"
# 生产模型目录（兼容旧逻辑）
MODELS_PRODUCTION = os.path.join(MODELS_ROOT, "production", "model_qlib")
FEATURE_CATALOG_FALLBACK = os.path.join(os.getcwd(), "config", "features", "model_training_feature_catalog_v1.json")
FEATURE_SNAPSHOT_DIR = Path(
    os.getenv("TRAINING_LOCAL_DATA_PATH", str(Path(os.getcwd()) / "db" / "feature_snapshots"))
)
FEATURE_COVERAGE_CACHE_TTL_SEC = max(5, int(os.getenv("FEATURE_COVERAGE_CACHE_TTL_SEC", "300")))
_feature_coverage_cache_data: dict[str, Any] | None = None
_feature_coverage_cache_expires_at: float = 0.0


# ---------- 目录扫描工具函数 ----------


def _read_yaml_safe(path: str) -> dict | None:
    """安全读取 YAML 文件，失败返回 None"""
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return None


def _read_json_safe(path: str) -> dict | None:
    """安全读取 JSON 文件，失败返回 None"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _file_sha256(path: str) -> str | None:
    """计算文件 SHA256（用于完整性核验）"""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _feature_market_declarations(path: str = FEATURE_CATALOG_FALLBACK) -> dict[str, list[str]]:
    """从文件目录构建 {feature_key: [market,...]} 映射，供市场过滤使用。"""
    raw = _read_json_safe(path)
    if not isinstance(raw, dict):
        return {}
    mapping: dict[str, list[str]] = {}
    categories_raw = raw.get("categories")
    if not isinstance(categories_raw, list):
        return {}
    for cat in categories_raw:
        if not isinstance(cat, dict):
            continue
        features_raw = cat.get("features") if isinstance(cat.get("features"), list) else []
        for feat in features_raw:
            if not isinstance(feat, dict):
                continue
            key = str(feat.get("key") or "").strip()
            if not key:
                continue
            markets = feat.get("markets")
            mapping[key] = (
                [str(m).upper() for m in markets if str(m).strip()]
                if isinstance(markets, list)
                else []
            )
    return mapping


def _market_allows_feature(market: str | None, declared: list[str]) -> bool:
    """特征是否适用于目标市场。declared 为空（未声明/未收录）时放行，避免误伤。"""
    market_upper = str(market or "").upper()
    if not market_upper:
        return True
    if not declared:
        return True
    return market_upper in declared


# ---------- 统一分类口径（B 特征字典 → A 训练目录）-----------------------------
# 训练消费侧（用户训练页）展示用：把 B 的 15 分类归一到 A 的 17 分类，避免同屏两套名字。
# 仅重组 categories 分组，不改动任何特征字段；admin 读写回路保持原生口径（见函数注释）。
_UNIFIED_CATEGORY_ORDER: dict[str, tuple[str, int]] = {
    "momentum": ("动量", 100),
    "volatility": ("波动与风险", 200),
    "volume_turnover": ("成交量与换手率", 220),
    "money_flow": ("成交额与资金", 300),
    "turnover": ("换手与流动性", 400),
    "technical": ("技术指标", 500),
    "fundamental": ("基本面与估值", 600),
    "style": ("截面风格", 700),
    "industry": ("行业轮动", 800),
    "chip": ("筹码分布", 900),
    "concept": ("概念板块", 1000),
    "money_flow_l2": ("逐笔资金流", 1100),
    "order_flow": ("撤单与委托流", 1200),
    "toxicity": ("信息不对称与毒性", 1300),
    "microstructure": ("价差与微观结构", 1400),
    "holding_structure": ("持仓结构", 1500),
    "other": ("其他因子", 9999),
}

_B_TO_A_CATEGORY: dict[str, str] = {
    "momentum": "momentum",
    "volatility": "volatility",
    "fund_flow": "money_flow",
    "technical": "technical",
    "fundamental": "fundamental",
    "style": "style",
    "industry": "industry",
    "chip": "chip",
    "concept": "concept",
    "microstructure": "microstructure",
    "holding": "holding_structure",
    "gtja": "technical",
}


def unify_feature_catalog_categories(catalog: dict[str, Any] | None) -> dict[str, Any] | None:
    """把 B 特征字典目录按 A 训练目录口径重组，仅训练消费侧调用。

    - gtja→technical、holding→holding_structure、fund_flow→money_flow 等静态映射
    - liquidity 按因子 key 经 quantdb 字典前缀规则逐个归位（未知前缀兜底 turnover）
    - ohlcv / custom 为 B 专属分组，原样透出（保留原名与原排序）
    """
    if not isinstance(catalog, dict):
        return catalog
    categories = catalog.get("categories")
    if not isinstance(categories, list):
        return catalog
    try:
        from backend.services.engine.data_platform.quantdb_factor_dictionary import (
            _group as _dict_group,
        )
    except Exception:
        _dict_group = None  # type: ignore[assignment]

    grouped: dict[str, dict[str, Any]] = {}
    for cat in categories:
        if not isinstance(cat, dict):
            continue
        orig_id = str(cat.get("id") or "")
        orig_order = int(cat.get("order") or 0)
        for feat in cat.get("features", []) or []:
            if not isinstance(feat, dict):
                continue
            fkey = str(feat.get("key") or "")
            if orig_id == "liquidity" and fkey and _dict_group is not None:
                try:
                    gid, _gname, _gorder = _dict_group(fkey)
                except Exception:
                    gid = "other"
                target = gid if gid in _UNIFIED_CATEGORY_ORDER and gid != "other" else "turnover"
            elif orig_id in ("ohlcv", "custom"):
                target = orig_id
            else:
                target = _B_TO_A_CATEGORY.get(orig_id, "other")
            if target in _UNIFIED_CATEGORY_ORDER:
                tname, torder = _UNIFIED_CATEGORY_ORDER[target]
            elif target == "custom":
                tname, torder = str(cat.get("name") or target), 9998
            else:
                tname, torder = str(cat.get("name") or target), orig_order
            node = grouped.get(target)
            if node is None:
                node = grouped[target] = {
                    "id": target, "name": tname, "order": torder,
                    "feature_count": 0, "features": [],
                }
            node["features"].append(feat)
            node["feature_count"] += 1
    out = dict(catalog)
    out["categories"] = sorted(grouped.values(), key=lambda x: x["order"])
    return out


def _load_feature_catalog_from_file(path: str = FEATURE_CATALOG_FALLBACK, market: str | None = None) -> dict[str, Any] | None:
    """从本地 JSON 回退加载特征字典（用于 DB 未初始化场景）。

    market 提供时按特征 markets 声明过滤；未提供返回全量。
    """
    raw = _read_json_safe(path)
    if not isinstance(raw, dict):
        return None
    categories_raw = raw.get("categories")
    if not isinstance(categories_raw, list) or not categories_raw:
        return None

    categories: list[dict[str, Any]] = []
    market_map = _feature_market_declarations(path)
    for cat in categories_raw:
        if not isinstance(cat, dict):
            continue
        cid = str(cat.get("id") or "").strip()
        if not cid:
            continue
        c_name = str(cat.get("name") or cid).strip()
        c_order = int(cat.get("order") or 0)
        features_raw = cat.get("features") if isinstance(cat.get("features"), list) else []
        features: list[dict[str, Any]] = []
        for feat in features_raw:
            if not isinstance(feat, dict):
                continue
            f_key = str(feat.get("key") or "").strip()
            if not f_key:
                continue
            if not _market_allows_feature(market, market_map.get(f_key, [])):
                continue
            features.append(
                {
                    "feature_id": str(feat.get("feature_id") or ""),
                    "key": f_key,
                    "feature_name": str(feat.get("description") or feat.get("feature_name") or f_key),
                    "explanation": str(feat.get("explanation") or feat.get("detail") or ""),
                    "formula": str(feat.get("formula") or ""),
                    "source_table_fields": str(feat.get("source") or feat.get("source_table_fields") or ""),
                    "enabled": bool(feat.get("enabled", True)),
                    "order_no": int(feat.get("order_no") or len(features) + 1),
                    "default_selected": bool(feat.get("default_selected", False)),
                    "markets": market_map.get(f_key, []),
                }
            )
        if not features:
            continue
        categories.append(
            {
                "id": cid,
                "name": c_name,
                "order": c_order if c_order > 0 else len(categories) + 1,
                "feature_count": len(features),
                "features": features,
            }
        )

    feature_count = sum(len(c["features"]) for c in categories)
    return {
        "version_id": str(raw.get("version_id") or "file_fallback"),
        "version_name": str(raw.get("name") or "Feature Catalog (File)"),
        "feature_count": feature_count,
        "categories": categories,
        "source": "file",
        "fallback_path": path,
    }


async def _load_feature_catalog_from_db(market: str | None = None) -> dict[str, Any] | None:
    """从特征注册表读取当前生效版本。表不存在或无数据时返回 None。

    market 提供时按文件 catalog 的 markets 声明过滤（qm_feature_definition 无 market 字段）。
    """
    async with get_session(read_only=True) as session:
        version_row = (
            await session.execute(
                text(
                    """
                    SELECT version_id, version_name, feature_count
                    FROM qm_feature_set_version
                    WHERE status = 'active'
                    ORDER BY effective_at DESC, created_at DESC
                    LIMIT 1
                    """
                )
            )
        ).mappings().first()

        if not version_row:
            return None

        try:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT
                            c.category_id,
                            c.category_name,
                            c.sort_order,
                            i.order_no,
                            i.enabled,
                            d.feature_id,
                            d.feature_key,
                            d.feature_name,
                            d.formula,
                            d.source_table_fields,
                            d.explanation
                        FROM qm_feature_set_item i
                        JOIN qm_feature_definition d ON d.feature_key = i.feature_key
                        JOIN qm_feature_category c ON c.category_id = i.category_id
                        WHERE i.version_id = :version_id
                        ORDER BY c.sort_order ASC, i.order_no ASC
                        """
                    ),
                    {"version_id": version_row["version_id"]},
                )
            ).mappings().all()
        except Exception:
            # 迁移前老库无 explanation 列，回退到无描述查询
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT
                            c.category_id,
                            c.category_name,
                            c.sort_order,
                            i.order_no,
                            i.enabled,
                            d.feature_id,
                            d.feature_key,
                            d.feature_name,
                            d.formula,
                            d.source_table_fields
                        FROM qm_feature_set_item i
                        JOIN qm_feature_definition d ON d.feature_key = i.feature_key
                        JOIN qm_feature_category c ON c.category_id = i.category_id
                        WHERE i.version_id = :version_id
                        ORDER BY c.sort_order ASC, i.order_no ASC
                        """
                    ),
                    {"version_id": version_row["version_id"]},
                )
            ).mappings().all()

        if not rows:
            return None

        market_map = _feature_market_declarations()
        cat_map: dict[str, dict[str, Any]] = {}
        for r in rows:
            cid = str(r["category_id"])
            f_key = str(r["feature_key"] or "").strip()
            if not _market_allows_feature(market, market_map.get(f_key, [])):
                continue
            if cid not in cat_map:
                cat_map[cid] = {
                    "id": cid,
                    "name": str(r["category_name"] or cid),
                    "order": int(r["sort_order"] or 0),
                    "feature_count": 0,
                    "features": [],
                }
            cat_map[cid]["features"].append(
                {
                    "feature_id": str(r["feature_id"] or ""),
                    "key": f_key,
                    "feature_name": str(r["feature_name"] or f_key or ""),
                    "explanation": str((r["explanation"] if "explanation" in r else "") or ""),
                    "formula": str(r["formula"] or ""),
                    "source_table_fields": str(r["source_table_fields"] or ""),
                    "enabled": bool(r["enabled"]),
                    "order_no": int(r["order_no"] or 0),
                    "markets": market_map.get(f_key, []),
                }
            )
            cat_map[cid]["feature_count"] += 1

        categories = sorted(cat_map.values(), key=lambda x: x["order"])
        return {
            "version_id": str(version_row["version_id"]),
            "version_name": str(version_row["version_name"] or version_row["version_id"]),
            "feature_count": int(version_row["feature_count"] or sum(c["feature_count"] for c in categories)),
            "categories": categories,
            "source": "database",
        }


def _build_suggested_periods(min_date: date, max_date: date) -> dict[str, list[str]] | None:
    total_days = (max_date - min_date).days + 1
    if total_days < 3:
        return None

    train_days = max(1, int(total_days * 0.7))
    val_days = max(1, int(total_days * 0.15))
    test_days = total_days - train_days - val_days

    if test_days < 1 and train_days > 1:
        train_days -= 1
        test_days += 1
    if test_days < 1 and val_days > 1:
        val_days -= 1
        test_days += 1
    if test_days < 1:
        return None

    train_end = min_date + timedelta(days=train_days - 1)
    val_start = train_end + timedelta(days=1)
    val_end = val_start + timedelta(days=val_days - 1)
    test_start = val_end + timedelta(days=1)
    test_end = max_date
    if test_start > test_end:
        return None

    return {
        "train": [min_date.isoformat(), train_end.isoformat()],
        "val": [val_start.isoformat(), val_end.isoformat()],
        "test": [test_start.isoformat(), test_end.isoformat()],
    }


def _normalize_market_key(market: str | None) -> str:
    """把市场名归一化为标准 key（a_share / hong_kong / us_stock / crypto / futures）。

    兼容前端 AppMarket 枚举（CN/US/HK/CRYPTO/FUTURES）与内部命名（a_share 等）。
    """
    raw = (market or "").strip().upper()
    mapping = {
        "CN": "a_share", "A": "a_share", "A_SHARE": "a_share",
        "HK": "hong_kong", "HONG_KONG": "hong_kong",
        "US": "us_stock", "US_STOCK": "us_stock",
        "CRYPTO": "crypto", "BC": "crypto",
        "FUTURES": "futures",
        "CUSTOM": "custom",
    }
    if raw in mapping:
        return mapping[raw]
    lower = (market or "").strip().lower()
    return lower if lower else "a_share"


def _scan_feature_snapshot_coverage(market: str | None = None) -> dict[str, Any] | None:
    if not FEATURE_SNAPSHOT_DIR.exists() or not FEATURE_SNAPSHOT_DIR.is_dir():
        return None

    market_key = _normalize_market_key(market)
    if market_key == "a_share":
        # A 股：年份文件 model_features_YYYY.parquet（+ core）
        files = sorted(FEATURE_SNAPSHOT_DIR.glob("model_features_20[0-9][0-9].parquet"))
        core = FEATURE_SNAPSHOT_DIR / "model_features_core.parquet"
        if core.exists():
            files.append(core)
    else:
        # 非 A 股：单体 model_features_{market}.parquet（用市场→文件名映射）
        parquet_name = _MARKET_SNAPSHOT_PARQUET.get(market_key)
        f = FEATURE_SNAPSHOT_DIR / parquet_name if parquet_name else None
        files = [f] if f and f.exists() else []

    if not files:
        return None

    try:
        import pandas as pd
    except Exception:
        return None

    min_date: date | None = None
    max_date: date | None = None
    scanned_files = 0
    total_rows = 0
    failed_files = 0

    for file_path in files:
        try:
            date_series = pd.read_parquet(file_path, columns=["trade_date"], engine="pyarrow")["trade_date"]
            if date_series.empty:
                continue
            file_min = pd.to_datetime(date_series.min(), errors="coerce")
            file_max = pd.to_datetime(date_series.max(), errors="coerce")
            if pd.isna(file_min) or pd.isna(file_max):
                continue

            file_min_date = file_min.date()
            file_max_date = file_max.date()
            min_date = file_min_date if min_date is None else min(min_date, file_min_date)
            max_date = file_max_date if max_date is None else max(max_date, file_max_date)
            scanned_files += 1
            total_rows += int(date_series.shape[0])
        except Exception:
            failed_files += 1

    if min_date is None or max_date is None:
        return None

    return {
        "source": "local_parquet",
        "snapshot_dir": str(FEATURE_SNAPSHOT_DIR),
        "market": market_key,
        "file_count": len(files),
        "scanned_files": scanned_files,
        "failed_files": failed_files,
        "total_rows": total_rows,
        "min_date": min_date.isoformat(),
        "max_date": max_date.isoformat(),
        "suggested_periods": _build_suggested_periods(min_date, max_date),
    }


def _get_feature_snapshot_coverage_cached(market: str | None = None) -> dict[str, Any] | None:
    global _feature_coverage_cache_data
    global _feature_coverage_cache_expires_at

    market_key = _normalize_market_key(market)
    now_ts = time_module.time()
    cached = _feature_coverage_cache_data or {}
    if market_key in cached and now_ts < _feature_coverage_cache_expires_at:
        return cached[market_key]

    payload = _scan_feature_snapshot_coverage(market)
    cached[market_key] = payload
    _feature_coverage_cache_data = cached
    _feature_coverage_cache_expires_at = now_ts + FEATURE_COVERAGE_CACHE_TTL_SEC
    return payload


async def _get_feature_snapshot_coverage_cached_async(market: str | None = None) -> dict[str, Any] | None:
    """异步版：把同步 parquet 扫描放到线程池执行，避免阻塞事件循环。

    15GB+ 的 train_ready_*.parquet 冷扫描耗时数十秒，同步执行会让
    API 健康检查超时被误杀（api service unresponsive -> restart）。"""
    global _feature_coverage_cache_data
    global _feature_coverage_cache_expires_at

    market_key = _normalize_market_key(market)
    now_ts = time_module.time()
    cached = _feature_coverage_cache_data or {}
    if market_key in cached and now_ts < _feature_coverage_cache_expires_at:
        return cached[market_key]

    payload = await asyncio.to_thread(_scan_feature_snapshot_coverage, market)
    cached[market_key] = payload
    _feature_coverage_cache_data = cached
    _feature_coverage_cache_expires_at = now_ts + FEATURE_COVERAGE_CACHE_TTL_SEC
    return payload


# 市场 → 单体快照 parquet 文件名（非 A 股，由 update_market_features.py 生成）
_MARKET_SNAPSHOT_PARQUET: dict[str, str] = {
    "crypto": "model_features_crypto.parquet",
    "hong_kong": "model_features_hk.parquet",
    "us_stock": "model_features_us.parquet",
    "futures": "model_features_futures.parquet",
}


def _scan_single_market_snapshot(parquet_name: str) -> dict[str, Any] | None:
    """读取非 A 股单体快照 parquet 的元数据（行数/标的/日期范围/特征数）。"""
    p = FEATURE_SNAPSHOT_DIR / parquet_name
    if not p.exists():
        return None
    try:
        import pyarrow.parquet as pq

        pf = pq.ParquetFile(str(p))
        total_rows = pf.metadata.num_rows
        # 读取日期范围与标的大致统计：仅读最小/最大分区避免全量加载
        schema = pf.schema_arrow
        date_col = "trade_date" if "trade_date" in schema.names else ("date" if "date" in schema.names else None)
        sym_col = "instrument" if "instrument" in schema.names else ("symbol" if "symbol" in schema.names else None)

        min_date = max_date = None
        symbol_count = 0
        if date_col:
            import pyarrow as pa

            dates = pf.read(columns=[date_col], use_threads=True).column(0).combine_chunks()
            try:
                d_arr = dates.cast(pa.string())
                min_date = str(pa.compute.min(d_arr).as_py())[:10]
                max_date = str(pa.compute.max(d_arr).as_py())[:10]
            except Exception:
                pass
            del dates
        if sym_col:
            syms = pf.read(columns=[sym_col], use_threads=True).column(0)
            symbol_count = int(len(syms.unique()))
            del syms

        return {
            "year": None,
            "start_date": min_date,
            "end_date": max_date,
            "row_count": int(total_rows),
            "feature_dim": len(schema.names),
            "symbol_count": symbol_count,
            "filename": p.name,
        }
    except Exception:
        return None


def _scan_feature_snapshots_status(
    target_date: str | None = None,
    topn: int = 20,
    market: str | None = None,
) -> dict[str, Any]:
    """
    按市场扫描特征快照：
    - a_share / 默认: 11 个年份的 model_features_YYYY.metadata.json
    - crypto/hong_kong/us_stock/futures: 单体 model_features_{market}.parquet
    """
    result: dict[str, Any] = {
        "exists": False,
        "snapshot_dir": str(FEATURE_SNAPSHOT_DIR),
        "file_count": 0,
        "scanned_files": 0,
        "failed_files": 0,
        "total_rows": 0,
        "min_date": None,
        "max_date": None,
        "metadata_files": [],
        "latest_date_coverage": {
            "target_date": target_date,
            "at_target_count": 0,
            "older_count": 0,
            "invalid_count": 0,
        },
        "topn_samples": {
            "sample_size": topn,
            "older_samples": [],
            "invalid_samples": [],
        },
        "suggested_periods": None,
    }

    if not FEATURE_SNAPSHOT_DIR.exists() or not FEATURE_SNAPSHOT_DIR.is_dir():
        return result

    result["exists"] = True

    # 非 A 股市场：读取单体快照 parquet
    market_key = (market or "a_share").lower()
    if market_key != "a_share":
        parquet_name = _MARKET_SNAPSHOT_PARQUET.get(market_key)
        if parquet_name:
            result["file_count"] = 1
            result["scanned_files"] = 1
            snapshot = _scan_single_market_snapshot(parquet_name)
            if snapshot:
                result["metadata_files"] = [snapshot]
                result["total_rows"] = snapshot["row_count"]
                result["min_date"] = snapshot["start_date"]
                result["max_date"] = snapshot["end_date"]
                if snapshot["start_date"] and snapshot["end_date"]:
                    try:
                        min_d = date.fromisoformat(snapshot["start_date"])
                        max_d = date.fromisoformat(snapshot["end_date"])
                        result["suggested_periods"] = _build_suggested_periods(min_d, max_d)
                    except Exception:
                        pass
                if target_date and snapshot["end_date"]:
                    if snapshot["end_date"] >= target_date:
                        result["latest_date_coverage"]["at_target_count"] = snapshot["symbol_count"]
                    else:
                        result["latest_date_coverage"]["older_count"] = snapshot["symbol_count"]
        return result

    # A 股：扫描所有的 metadata.json 文件
    json_files = sorted(FEATURE_SNAPSHOT_DIR.glob("*.metadata.json"), reverse=True)
    result["file_count"] = len(json_files)

    metadata_list = []
    total_rows = 0
    all_min_date = None
    all_max_date = None

    for jf in json_files:
        data = _read_json_safe(str(jf))
        if not data:
            continue

        m_year = data.get("year")
        s_date = data.get("output_start_date")
        e_date = data.get("output_end_date")
        r_count = data.get("row_count") or 0
        f_count = data.get("implemented_feature_count") or 0
        sym_count = data.get("symbol_count") or 0

        total_rows += r_count

        # 更新全局日期范围
        if s_date:
            all_min_date = s_date if all_min_date is None else min(all_min_date, s_date)
        if e_date:
            all_max_date = e_date if all_max_date is None else max(all_max_date, e_date)

        metadata_list.append({
            "year": m_year,
            "start_date": s_date,
            "end_date": e_date,
            "row_count": r_count,
            "feature_dim": f_count,
            "symbol_count": sym_count,
            "filename": jf.name
        })

    result["metadata_files"] = metadata_list
    result["scanned_files"] = len(metadata_list)
    result["total_rows"] = total_rows
    result["min_date"] = all_min_date
    result["max_date"] = all_max_date

    # 2. 计算建议的训练/验证/测试划分
    if all_min_date and all_max_date:
        try:
            min_d = date.fromisoformat(all_min_date)
            max_d = date.fromisoformat(all_max_date)
            result["suggested_periods"] = _build_suggested_periods(min_d, max_d)

            # 计算简单的覆盖率（基于最新年份数据）
            if target_date and metadata_list:
                latest = metadata_list[0]
                if latest["end_date"] >= target_date:
                    result["latest_date_coverage"]["at_target_count"] = latest["symbol_count"]
                else:
                    result["latest_date_coverage"]["older_count"] = latest["symbol_count"]
        except Exception:
            pass

    return result


def _enrich_feature_catalog_with_data_coverage(catalog: dict[str, Any] | None, market: str | None = None) -> dict[str, Any] | None:
    if not isinstance(catalog, dict):
        return catalog

    coverage = _get_feature_snapshot_coverage_cached(market)
    if not coverage:
        return catalog

    enriched = dict(catalog)
    enriched["data_coverage"] = coverage
    return enriched


async def _enrich_feature_catalog_with_data_coverage_async(catalog: dict[str, Any] | None, market: str | None = None) -> dict[str, Any] | None:
    """异步版 enrichment：coverage 扫描在 to_thread 中执行，不阻塞事件循环。"""
    if not isinstance(catalog, dict):
        return catalog

    coverage = await _get_feature_snapshot_coverage_cached_async(market)
    if not coverage:
        return catalog

    enriched = dict(catalog)
    enriched["data_coverage"] = coverage
    return enriched


def _resolve_expected_feature_dim(model_dir: Path) -> int:
    """解析生产模型推理期望维度，失败时回退到 48。"""
    default_dim = int(os.getenv("INFERENCE_DEFAULT_FEATURE_DIM", "48"))

    metadata = _read_json_safe(str(model_dir / "metadata.json")) or {}
    if isinstance(metadata, dict):
        for key in ("feature_count", "feature_dim", "input_dim"):
            val = metadata.get(key)
            if isinstance(val, int) and val > 0:
                return val
        feature_columns = metadata.get("feature_columns")
        if isinstance(feature_columns, list) and feature_columns:
            return len(feature_columns)
        input_spec = metadata.get("input_spec")
        if isinstance(input_spec, dict):
            tensor_shape = input_spec.get("tensor_shape")
            if isinstance(tensor_shape, list) and len(tensor_shape) >= 3:
                try:
                    dim = int(tensor_shape[2] or 0)
                    if dim > 0:
                        return dim
                except Exception:
                    pass
        model_info = metadata.get("model_info")
        if isinstance(model_info, dict):
            for key in ("feature_count", "feature_dim", "input_dim"):
                val = model_info.get(key)
                if isinstance(val, int) and val > 0:
                    return val
            feature_columns = model_info.get("feature_columns")
            if isinstance(feature_columns, list) and feature_columns:
                return len(feature_columns)

    schema = _read_json_safe(str(model_dir / "feature_schema.json")) or {}
    if isinstance(schema, dict):
        for key in ("features", "feature_columns", "columns"):
            cols = schema.get(key)
            if isinstance(cols, list) and cols:
                return len(cols)

    script_path = model_dir / "inference.py"
    if script_path.exists():
        try:
            import re

            text_part = script_path.read_text(encoding="utf-8", errors="ignore")[:4000]
            match = re.search(r"(\d+)\s*特征", text_part)
            if match:
                dim = int(match.group(1))
                if dim > 0:
                    return dim
        except Exception:
            pass

    return default_dim


def _resolve_ready_threshold(total_rows: int) -> tuple[int, int, float, int]:
    """
    计算“数据覆盖达标”阈值。
    - min_ready_symbols: 绝对上限阈值（默认 3000）
    - min_ready_ratio:   相对覆盖比例（默认 0.9）
    - min_ready_floor:   最小下限（默认 100）
    最终阈值按 min(绝对上限, 相对比例) 并受 floor 约束，且不超过 total_rows。
    """
    min_ready_symbols = int(os.getenv("INFERENCE_MIN_READY_SYMBOLS", "3000"))
    min_ready_ratio = float(os.getenv("INFERENCE_MIN_READY_RATIO", "0.9"))
    min_ready_floor = int(os.getenv("INFERENCE_MIN_READY_FLOOR", "100"))

    if total_rows <= 0:
        return min_ready_symbols, min_ready_symbols, min_ready_ratio, min_ready_floor

    ratio = min(max(min_ready_ratio, 0.0), 1.0)
    abs_target = min(min_ready_symbols, total_rows)
    ratio_target = int(math.ceil(total_rows * ratio))
    required = min(abs_target, ratio_target)
    required = max(min_ready_floor, required)
    required = min(required, total_rows)
    return required, min_ready_symbols, ratio, min_ready_floor


def _read_bin_start_index(bin_path: Path) -> int | None:
    """读取 qlib .day.bin 首 4 字节起始索引（float32 编码），失败返回 None。"""
    try:
        with bin_path.open("rb") as f:
            head = f.read(4)
        if len(head) < 4:
            return None
        import struct

        return int(struct.unpack("<f", head)[0])
    except Exception:
        return None


async def _resolve_inference_dates_with_calendar(
    *,
    current_user: dict,
    now_local: datetime,
    market: str = "SSE",
) -> tuple[str, str, str, bool]:
    """
    返回 (requested_data_trade_date, data_trade_date, prediction_trade_date, calendar_adjusted)。
    规则：
    - 09:30 前：候选数据日=上一交易日；
    - 09:30 后：候选数据日=当天（若当天非交易日则回退上一交易日）。
    """
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "admin")

    if now_local.time() < time(9, 30):
        candidate = await calendar_service.prev_trading_day(
            market=market,
            trade_date=now_local.date(),
            tenant_id=tenant_id,
            user_id=user_id,
        )
    else:
        candidate = now_local.date()

    requested_data_trade_date = candidate.isoformat()
    is_td = await calendar_service.is_trading_day(
        market=market,
        trade_date=candidate,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    if is_td:
        data_trade_date = candidate
        adjusted = False
    else:
        data_trade_date = await calendar_service.prev_trading_day(
            market=market,
            trade_date=candidate,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        adjusted = True

    prediction_trade_date = await calendar_service.next_trading_day(
        market=market,
        trade_date=data_trade_date,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    return (
        requested_data_trade_date,
        data_trade_date.isoformat(),
        prediction_trade_date.isoformat(),
        adjusted,
    )


def _scan_model_directory(model_dir: str) -> dict[str, Any]:
    """
    扫描单个模型目录，聚合所有元数据文件。
    返回包含 metadata / workflow_config / best_params / files 的结构化字典。
    """
    dir_path = Path(model_dir)
    model_id = dir_path.name

    # 收集目录内所有文件信息
    files = []
    for f in sorted(dir_path.iterdir()):
        if f.is_file():
            stat = f.stat()
            files.append(
                {
                    "name": f.name,
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )

    metadata = _read_json_safe(str(dir_path / "metadata.json"))
    workflow_config = _read_yaml_safe(str(dir_path / "workflow_config.yaml"))
    # 增加对 Qlib v10 config.yaml 的支持
    qlib_config = _read_yaml_safe(str(dir_path / "config.yaml"))
    best_params = _read_yaml_safe(str(dir_path / "best_params.yaml"))
    feature_schema = _read_json_safe(str(dir_path / "feature_schema.json"))

    # 读取特征描述文档
    feature_description_path = dir_path / "feature_description.md"
    feature_description = None
    if feature_description_path.exists():
        try:
            with open(feature_description_path, encoding="utf-8") as f:
                feature_description = f.read(10000)  # 读取前10k字符，防止过大阻塞
        except Exception:
            pass

    # 从 metadata 提取关键字段
    performance_metrics = (metadata or {}).get("performance_metrics")
    feature_count = (metadata or {}).get("feature_count")
    model_format = (metadata or {}).get("model_format")
    resolved_class = (metadata or {}).get("resolved_class")
    sha256 = (metadata or {}).get("sha256")

    # 从 workflow_config 或 config.yaml 提取训练时间范围
    train_start = train_end = test_start = test_end = None
    if workflow_config:
        handler_cfg = workflow_config.get("data_handler_config") or {}
        if isinstance(handler_cfg, dict):
            train_start = handler_cfg.get("start_time")
            train_end = handler_cfg.get("end_time")

    # 适配 Qlib 任务配置中的 segments (如 v10 版本)
    if not train_start and qlib_config:
        try:
            # 常见路径: task -> dataset -> kwargs -> segments
            segments = qlib_config.get("task", {}).get("dataset", {}).get("kwargs", {}).get("segments", {})
            if segments:
                train_seg = segments.get("train")
                if isinstance(train_seg, list) and len(train_seg) >= 2:
                    train_start, train_end = train_seg[0], train_seg[1]

                test_seg = segments.get("test")
                if isinstance(test_seg, list) and len(test_seg) >= 2:
                    test_start, test_end = test_seg[0], test_seg[1]
        except Exception:
            pass

    # 增加兜底逻辑：从 metadata 直接读取日期区间，支持非 Qlib 结构的纯 Json 模型
    if isinstance(metadata, dict):
        train_start = train_start or metadata.get("train_start")
        train_end = train_end or metadata.get("train_end")
        test_start = test_start or metadata.get("test_start")
        test_end = test_end or metadata.get("test_end")

    # 判断是否为生产模型
    is_production = "production" in model_dir

    # 目录修改时间
    dir_stat = dir_path.stat()
    updated_at = datetime.fromtimestamp(dir_stat.st_mtime).isoformat()

    return {
        "model_id": model_id,
        "dir_path": model_dir,
        "is_production": is_production,
        "feature_count": feature_count,
        "model_format": model_format,
        "resolved_class": resolved_class,
        "sha256": sha256,
        "train_start": str(train_start) if train_start else None,
        "train_end": str(train_end) if train_end else None,
        "test_start": str(test_start) if test_start else None,
        "test_end": str(test_end) if test_end else None,
        "updated_at": updated_at,
        "metadata": metadata,
        "performance_metrics": performance_metrics,
        "workflow_config": workflow_config,
        "qlib_config": qlib_config,
        "best_params": best_params,
        "feature_schema": feature_schema,
        "feature_description": feature_description,
        "files": files,
    }


def _find_model_directories(root: str) -> list[str]:
    """
    递归扫描 models/ 下包含 metadata.json 或模型文件的目录，
    跳过 archive/candidates 等存档目录。
    """
    skip_dirs = {"archive", "candidates", "__pycache__"}
    result = []
    for entry in sorted(Path(root).rglob("*")):
        if not entry.is_dir():
            continue
        if entry.name in skip_dirs:
            continue
        # 判断是否是有效模型目录
        has_metadata = (entry / "metadata.json").exists()
        has_model = any((entry / f"model.{ext}").exists() for ext in ("bin", "txt", "pkl", "pth", "onnx", "pt"))
        if has_metadata or has_model:
            result.append(str(entry))
    return result


# ---------- Schemas (内联定义，避免额外文件) ----------


class ModelCreate(BaseModel):
    name: str = Field(..., description="模型名称")
    description: str | None = Field(None, description="描述")
    source_type: str = Field("ai_model", description="模型类型: ai_model, hybrid, external")
    start_date: datetime | None = Field(None, description="模型数据开始日期")
    end_date: datetime | None = Field(None, description="模型数据结束日期")
    config: dict[str, Any] | None = Field(None, description="配置参数")


class ModelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    source_type: str
    start_date: datetime | None = None
    end_date: datetime | None = None
    user_id: str
    is_active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DataFileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    data_source_id: int
    filename: str
    file_size: int | None = None
    status: str = "uploaded"
    created_at: datetime | None = None


# ---------- Endpoints ----------


