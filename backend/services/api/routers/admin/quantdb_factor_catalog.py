"""Admin APIs for the versioned, direct-QuantDB training factor catalog."""

from __future__ import annotations

import asyncio
import re
import uuid
import json
from datetime import date, datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.services.api.user_app.middleware.auth import require_admin
from backend.services.engine.data_platform.quantdb_factor_reader import (
    DEFAULT_FACTOR_SOURCE,
    EXCLUDED_TRAIN_DATASETS,
    FACTOR_SOURCE_DIRS,
    KEY_COLUMNS,
    REQUIRED_COLUMNS,
    QuantDBFactorReader,
    default_source_for,
    market_data_dir,
    normalize_market,
    sources_for_market,
)
from backend.services.engine.data_platform.quantdb_factor_dictionary import definition_for
from backend.shared.database_manager_v2 import get_session

router = APIRouter(dependencies=[Depends(require_admin)])

_VALID_SOURCE = set(FACTOR_SOURCE_DIRS)
_VALID_STATUS = {"draft", "published", "archived"}

# 新建草稿时的默认勾选集（default_selected）。
# 挑选原则（共 48 个，基于 l1_l2_factors 实际发现字段）：
#   1. 每个 子类族 只保留一个代表因子，避免高度冗余拖慢 LightGBM 训练；
#   2. 覆盖 10 大分类，时序+截面、价量+基本面均衡；
#   3. 全部为日线可计算、定义清晰的稳健因子；
#   4. micro_*/flow_*(L2 高频) 与 concept_*(题材噪音) 默认不选，
#      需要时由管理员在草稿中手动勾选。
DEFAULT_SELECTED_FACTORS = frozenset({
    # 动量 (9): 收益阶梯 + 均线偏离 + 趋势/超买超卖代表
    "mom_ret_1d", "mom_ret_5d", "mom_ret_10d", "mom_ret_20d", "mom_ret_60d",
    "mom_ma_gap_5", "mom_ma_gap_20", "mom_macd_hist", "mom_rsi_14",
    # 波动与风险 (6): 短/中标准差、真实波幅、振幅、极值与 OHLC 波动率
    "vol_std_5", "vol_std_20", "vol_atr_14",
    "vol_amp_20", "vol_parkinson_20", "vol_gk_20",
    # 成交量与换手率 (6): 日内量能结构与分布形态
    "vol_tick_density", "vol_gini", "vol_skew",
    "vol_persistence", "vol_up_down_ratio", "vol_weighted_price",
    # 成交额与资金 (5): 活跃度水平、放量、短长额比、资金流强弱
    "amt_log", "amt_ma_20", "amt_z_20", "amt_ratio_5_20", "mfi_14",
    # 换手与流动性 (4): 换手水平、突变与标准化偏离
    "turn_1", "turn_20", "turn_ratio_1_5", "turn_z_20",
    # 技术指标 (4): 布林位置、CCI、趋势强度、回撤路径
    "tech_bb_pos", "tech_cci_20", "tech_adx_14", "tech_max_drawdown_20",
    # 基本面与估值 (5): 估值双雄 + 盈利/成长/规模
    "fun_pe", "fun_pb", "fun_roe", "fun_np_growth", "fun_mv_rank",
    # 截面风格 (3): Beta、特质波动、残差动量
    "style_beta_20", "style_idio_vol_20", "style_residual_ret_20",
    # 行业轮动 (3): 行业动量、强度、拥挤度
    "ind_ret_20", "ind_strength_20", "ind_crowding_20",
    # 筹码分布 (3): 获利盘、集中度、成本带宽
    "chip_profit_ratio_20", "chip_concentration_20", "chip_cost_90_width",
})

# 数据源元信息属于后端训练数据契约；前端不得硬编码来源或默认项。
FACTOR_SOURCE_LABELS = {
    "l1_factors": "L1 因子",
    "l2_factors": "L2 因子",
    "l1_l2_factors": "L1 + L2 合并宽表",
    "ccass_factors": "CCASS 持仓结构",
    "south_factors": "南向资金结构",
    "alpha_library": "Alpha 库因子",
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS qm_quantdb_factor_field (
    market VARCHAR(16) NOT NULL DEFAULT 'CN',
    dataset_id VARCHAR(64) NOT NULL,
    column_name VARCHAR(128) NOT NULL,
    data_type VARCHAR(64),
    schema_hash VARCHAR(128) NOT NULL DEFAULT '',
    min_date DATE,
    max_date DATE,
    is_present BOOLEAN NOT NULL DEFAULT TRUE,
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (market, dataset_id, column_name)
);
CREATE TABLE IF NOT EXISTS qm_training_factor_catalog_version (
    version_id VARCHAR(64) PRIMARY KEY,
    market VARCHAR(16) NOT NULL DEFAULT 'CN',
    version_name VARCHAR(128) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'draft',
    source_dataset VARCHAR(64),
    created_by VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ,
    CHECK (status IN ('draft', 'published', 'archived'))
);
CREATE TABLE IF NOT EXISTS qm_training_factor_mapping (
    mapping_id VARCHAR(64) PRIMARY KEY,
    version_id VARCHAR(64) NOT NULL REFERENCES qm_training_factor_catalog_version(version_id) ON DELETE CASCADE,
    source_dataset VARCHAR(64) NOT NULL,
    source_column VARCHAR(128) NOT NULL,
    feature_key VARCHAR(128) NOT NULL,
    display_name VARCHAR(256) NOT NULL,
    category_id VARCHAR(64) NOT NULL,
    category_name VARCHAR(128) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    default_selected BOOLEAN NOT NULL DEFAULT FALSE,
    required BOOLEAN NOT NULL DEFAULT FALSE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE(version_id, source_dataset, feature_key),
    UNIQUE(version_id, source_dataset, source_column)
);
CREATE INDEX IF NOT EXISTS idx_qm_training_factor_mapping_version
    ON qm_training_factor_mapping(version_id, source_dataset, category_id, sort_order);
CREATE TABLE IF NOT EXISTS qm_quantdb_factor_source_status (
    market VARCHAR(16) NOT NULL DEFAULT 'CN',
    dataset_id VARCHAR(64) NOT NULL,
    files INTEGER NOT NULL DEFAULT 0,
    column_count INTEGER NOT NULL DEFAULT 0,
    schema_hash VARCHAR(128) NOT NULL DEFAULT '',
    min_date DATE,
    max_date DATE,
    ready BOOLEAN NOT NULL DEFAULT FALSE,
    missing_required TEXT NOT NULL DEFAULT '[]',
    reason TEXT,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (market, dataset_id)
);
"""


class CatalogVersionCreate(BaseModel):
    version_name: str = Field(min_length=1, max_length=128)
    source_dataset: str = DEFAULT_FACTOR_SOURCE


class CatalogVersionClone(BaseModel):
    version_name: str = Field(min_length=1, max_length=128)


class FactorMappingInput(BaseModel):
    mapping_id: str | None = None
    source_dataset: str
    source_column: str = Field(min_length=1, max_length=128)
    feature_key: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=256)
    category_id: str = Field(min_length=1, max_length=64)
    category_name: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    default_selected: bool = False
    required: bool = False
    sort_order: int = 0


class MappingUpdate(BaseModel):
    mapping: FactorMappingInput


def _validate_source(source: str) -> str:
    """因子源校验：静态注册源直接放行；其余按命名规则放行动态数据集。

    动态目录（未来新增的 6_ml_datasets/xxx_factors）由“刷新字段”扫描注册进
    qm_quantdb_factor_source_status 后即可建目录/发布；读取层
    QuantDBFactorReader.validate_source 会二次校验目录真实存在与排除清单。
    """
    if source in _VALID_SOURCE:
        return source
    if source in EXCLUDED_TRAIN_DATASETS or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", source
    ):
        raise HTTPException(status_code=400, detail=f"Unknown factor source: {source}")
    return source


def _category_for(column: str) -> tuple[str, str]:
    prefix = column.split("_", 1)[0].lower()
    categories = {
        "turn": ("turnover", "换手与流动性"), "amt": ("amount", "成交额与资金"),
        "mom": ("momentum", "动量"), "vol": ("volatility", "波动率"),
        "tech": ("technical", "技术指标"), "fun": ("fundamental", "基本面"),
        "chip": ("chip", "筹码"), "style": ("style", "风格"),
        "ind": ("industry", "行业"), "concept": ("concept", "概念"),
        "micro": ("microstructure", "微观结构"), "flow": ("money_flow", "资金流"),
    }
    return categories.get(prefix, ("other", "其他因子"))


async def _ensure_schema(session) -> None:
    for statement in _SCHEMA_SQL.split(";"):
        if statement.strip():
            await session.execute(text(statement))
    # 存量库迁移：版本表补 market 列（既有 CN 版本默认归属 CN）
    await session.execute(text(
        "ALTER TABLE qm_training_factor_catalog_version "
        "ADD COLUMN IF NOT EXISTS market VARCHAR(16) NOT NULL DEFAULT 'CN'"
    ))


async def _active_version(
    session, source_dataset: str | None = None, market: str = "CN"
) -> dict[str, Any] | None:
    source_clause = " AND source_dataset = :source_dataset" if source_dataset else ""
    result = await session.execute(text(
        "SELECT version_id, version_name, status, source_dataset, market, created_at, published_at "
        "FROM qm_training_factor_catalog_version WHERE status = 'published' AND market = :market"
        f"{source_clause} ORDER BY published_at DESC NULLS LAST, created_at DESC LIMIT 1"
    ), {"market": normalize_market(market),
        "source_dataset": source_dataset} if source_dataset else {"market": normalize_market(market)})
    row = result.mappings().first()
    return dict(row) if row else None


def _unrefreshed_source_status(source: str, market: str = "CN") -> dict[str, Any]:
    """Fast explicit state for a source that has not been scanned yet."""
    return {
        "dataset_id": source,
        "path": str(QuantDBFactorReader(market=market).source_path(source)),
        "files": 0,
        "column_count": 0,
        "columns": [],
        "column_types": {},
        "schema_hash": "",
        "min_date": None,
        "max_date": None,
        "ready": False,
        "missing_required": list(REQUIRED_COLUMNS),
        "reason": "字段尚未刷新，请点击“刷新字段”执行数据扫描",
        "refreshed_at": None,
    }


async def _cached_factor_sources(
    session, market: str = "CN"
) -> dict[str, dict[str, Any]]:
    """Read readiness from the registry, never by scanning parquet on page load."""
    market = normalize_market(market)
    rows = (await session.execute(text("""
        SELECT dataset_id, files, column_count, schema_hash, min_date, max_date,
               ready, missing_required, reason, refreshed_at
        FROM qm_quantdb_factor_source_status
        WHERE market = :market
    """), {"market": market})).mappings().all()
    cached = {str(row["dataset_id"]): dict(row) for row in rows}
    sources: dict[str, dict[str, Any]] = {}
    reader = QuantDBFactorReader(market=market)
    # 静态注册源恒展示；已扫描过的动态数据集（含目录后来移除的）按目录真实
    # 存在过滤，避免历史行残留导致页面出现幽灵源
    known = list(sources_for_market(market))
    extras = [
        dataset
        for dataset in cached
        if dataset not in known
        and dataset not in EXCLUDED_TRAIN_DATASETS
        and (reader.data_dir / "6_ml_datasets" / dataset).is_dir()
    ]
    for source in known + extras:
        row = cached.get(source)
        if not row:
            sources[source] = _unrefreshed_source_status(source, market)
            continue
        try:
            missing_required = json.loads(str(row["missing_required"] or "[]"))
        except (TypeError, json.JSONDecodeError):
            missing_required = list(REQUIRED_COLUMNS)
        sources[source] = {
            "dataset_id": source,
            "path": str(reader.source_path(source)),
            "files": int(row["files"] or 0),
            "column_count": int(row["column_count"] or 0),
            "columns": [],
            "column_types": {},
            "schema_hash": str(row["schema_hash"] or ""),
            "min_date": str(row["min_date"]) if row["min_date"] else None,
            "max_date": str(row["max_date"]) if row["max_date"] else None,
            "ready": bool(row["ready"]),
            "missing_required": missing_required,
            "reason": row["reason"],
            "refreshed_at": str(row["refreshed_at"]) if row["refreshed_at"] else None,
        }
    return sources


async def _store_discovered_sources(
    session, discovered: dict[str, dict[str, Any]], market: str = "CN"
) -> None:
    market = normalize_market(market)
    for source, status in discovered.items():
        await session.execute(text("""
            INSERT INTO qm_quantdb_factor_source_status
              (market, dataset_id, files, column_count, schema_hash, min_date,
               max_date, ready, missing_required, reason, refreshed_at)
            VALUES (:market, :dataset_id, :files, :column_count, :schema_hash,
                    :min_date, :max_date, :ready, :missing_required, :reason, NOW())
            ON CONFLICT (market, dataset_id) DO UPDATE SET
              files = EXCLUDED.files, column_count = EXCLUDED.column_count,
              schema_hash = EXCLUDED.schema_hash, min_date = EXCLUDED.min_date,
              max_date = EXCLUDED.max_date, ready = EXCLUDED.ready,
              missing_required = EXCLUDED.missing_required, reason = EXCLUDED.reason,
              refreshed_at = NOW()
        """), {
            "market": market,
            "dataset_id": source,
            "files": status["files"],
            "column_count": len(status["columns"]),
            "schema_hash": status["schema_hash"],
            "min_date": date.fromisoformat(status["min_date"]) if status["min_date"] else None,
            "max_date": date.fromisoformat(status["max_date"]) if status["max_date"] else None,
            "ready": status["ready"],
            "missing_required": json.dumps(status["missing_required"]),
            "reason": status["reason"],
        })


async def _catalog_payload(session, version: dict[str, Any], source_dataset: str) -> dict[str, Any]:
    rows = (await session.execute(text("""
        SELECT mapping_id, source_dataset, source_column, feature_key, display_name,
               category_id, category_name, enabled, default_selected, required, sort_order
        FROM qm_training_factor_mapping
        WHERE version_id = :version_id AND source_dataset = :source_dataset
        ORDER BY category_name, sort_order, feature_key
    """), {"version_id": version["version_id"], "source_dataset": source_dataset})).mappings().all()
    categories: dict[str, dict[str, Any]] = {}
    for row in rows:
        # 兼容早期草稿：曾错误地把 dictionary.explanation 存入
        # display_name。训练页卡片只应显示短名称，完整说明留给后台编辑。
        stored_display_name = str(row["display_name"])
        dictionary = definition_for(str(row["source_column"]))
        display_name = (
            str(dictionary["display_name"])
            if "具体计算口径" in stored_display_name
            else stored_display_name
        )
        category = categories.setdefault(str(row["category_id"]), {
            "id": str(row["category_id"]), "name": str(row["category_name"]),
            "order": len(categories), "feature_count": 0, "features": [],
        })
        category["features"].append({
            "feature_id": str(row["mapping_id"]), "key": str(row["feature_key"]),
            "feature_name": display_name, "source_dataset": str(row["source_dataset"]),
            "source_column": str(row["source_column"]), "enabled": bool(row["enabled"]),
            "default_selected": bool(row["default_selected"]), "required": bool(row["required"]),
            "category_id": str(row["category_id"]), "category_name": str(row["category_name"]),
            "order_no": int(row["sort_order"]),
        })
        category["feature_count"] += 1
    return {
        "version_id": version["version_id"], "version_name": version["version_name"],
        "source_dataset": source_dataset, "status": version["status"],
        "feature_count": sum(c["feature_count"] for c in categories.values()),
        "categories": list(categories.values()), "source": "quantdb_factor_catalog",
    }


async def load_active_factor_catalog(source_dataset: str = DEFAULT_FACTOR_SOURCE) -> dict[str, Any] | None:
    """Public compatibility helper used by the user-facing training catalog API."""
    source_dataset = _validate_source(source_dataset)
    async with get_session() as session:
        await _ensure_schema(session)
        version = await _active_version(session, source_dataset)
        return await _catalog_payload(session, version, source_dataset) if version else None


def _training_coverage(source: str, status: dict[str, Any]) -> dict[str, Any]:
    """Convert the cached discovery manifest to the user training API shape."""
    suggested_periods = None
    if status["min_date"] and status["max_date"]:
        start = date.fromisoformat(str(status["min_date"]))
        end = date.fromisoformat(str(status["max_date"]))
        span_days = max((end - start).days, 1)
        # 70% / 15% / 15% chronological split.  The reader filters to actual
        # trading dates, so boundaries may fall on a non-trading day safely.
        train_end = start.fromordinal(start.toordinal() + int(span_days * 0.70))
        val_end = start.fromordinal(start.toordinal() + int(span_days * 0.85))
        suggested_periods = {
            "train": [start.isoformat(), train_end.isoformat()],
            "val": [(train_end.fromordinal(train_end.toordinal() + 1)).isoformat(), val_end.isoformat()],
            "test": [(val_end.fromordinal(val_end.toordinal() + 1)).isoformat(), end.isoformat()],
        }
    return {
        "source": "quantdb_factors",
        "dataset_id": source,
        "snapshot_dir": status["path"],
        "file_count": int(status["files"]),
        "scanned_files": int(status["files"]),
        "failed_files": 0,
        "total_rows": 0,
        "min_date": status["min_date"],
        "max_date": status["max_date"],
        "schema_hash": status["schema_hash"],
        "ready": bool(status["ready"]),
        "missing_required": list(status["missing_required"]),
        "reason": status["reason"],
        "refreshed_at": status["refreshed_at"],
        "suggested_periods": suggested_periods,
    }


async def load_quantdb_training_sources(market: str = "CN") -> dict[str, Any]:
    """Return all trainable-source choices from the cached market manifest.

    This is deliberately database/manifest-only: opening the training page must
    never scan parquet partitions.
    """
    market = normalize_market(market)
    async with get_session() as session:
        await _ensure_schema(session)
        statuses = await _cached_factor_sources(session, market)
        rows = (await session.execute(text("""
            SELECT version_id, source_dataset, published_at
            FROM qm_training_factor_catalog_version
            WHERE status = 'published' AND market = :market
        """), {"market": market})).mappings().all()
    published = {str(row["source_dataset"]): dict(row) for row in rows}
    sources = []
    for source in sources_for_market(market):
        status = statuses[source]
        version = published.get(source)
        sources.append({
            "id": source,
            "name": FACTOR_SOURCE_LABELS[source],
            "default": source == default_source_for(market),
            "ready": bool(status["ready"]),
            "published": version is not None,
            "trainable": bool(status["ready"]) and version is not None,
            "feature_count": 0,
            "catalog_version": version["version_id"] if version else None,
            "schema_hash": status["schema_hash"],
            "reason": status["reason"] if not status["ready"] else (
                None if version else "尚未发布因子目录"
            ),
        })
    return {
        "default_source": default_source_for(market),
        "market": market,
        "sources": sources,
    }


async def load_quantdb_training_catalog(
    source_dataset: str, market: str = "CN"
) -> dict[str, Any]:
    """Return the sole user-facing QuantDB training catalog for one source.

    An unpublished source is a valid empty state.  It must never fall back to
    old parquet/database feature dictionaries, because that would show fields
    unrelated to the selected QuantDB source.
    """
    source_dataset = _validate_source(source_dataset)
    market = normalize_market(market)
    if source_dataset not in sources_for_market(market):
        raise HTTPException(
            status_code=422,
            detail=f"因子源 {source_dataset} 不属于市场 {market}",
        )
    async with get_session() as session:
        await _ensure_schema(session)
        statuses = await _cached_factor_sources(session, market)
        status = statuses[source_dataset]
        version = await _active_version(session, source_dataset, market)
        coverage = _training_coverage(source_dataset, status)
        if not version:
            return {
                "catalog_status": "unpublished",
                "version_id": "",
                "version_name": "",
                "market": market,
                "source_dataset": source_dataset,
                "status": "unpublished",
                "feature_count": 0,
                "categories": [],
                "source": "quantdb_factor_catalog",
                "data_coverage": coverage,
                "message": "尚未发布因子目录",
            }
        catalog = await _catalog_payload(session, version, source_dataset)
        catalog["catalog_status"] = "ready" if status["ready"] else "source_not_ready"
        catalog["market"] = market
        catalog["data_coverage"] = coverage
        catalog["message"] = None if status["ready"] else status["reason"]
        return catalog


@router.get("/sources")
async def get_factor_sources(
    market: str = Query("CN"),
    current_user: dict = Depends(require_admin),
):
    """Return cached direct-read readiness for the market's factor sources."""
    _ = current_user
    market = normalize_market(market)
    async with get_session() as session:
        await _ensure_schema(session)
        sources = await _cached_factor_sources(session, market)
    return {
        "sources": sources,
        "labels": {s: FACTOR_SOURCE_LABELS.get(s, s) for s in sources},
        "market": market,
        "default_source": default_source_for(market),
    }


@router.post("/sources/refresh")
async def refresh_factor_sources(
    market: str = Query("CN"),
    current_user: dict = Depends(require_admin),
):
    """Scan local factor schemas for the market and upsert the raw field registry."""
    _ = current_user
    market = normalize_market(market)
    discovered = await asyncio.to_thread(
        QuantDBFactorReader(market=market).discover, market
    )
    async with get_session() as session:
        await _ensure_schema(session)
        await _store_discovered_sources(session, discovered, market)
        for source, status in discovered.items():
            await session.execute(text("""
                UPDATE qm_quantdb_factor_field SET is_present = FALSE, discovered_at = NOW()
                WHERE market = :market AND dataset_id = :source
            """), {"market": market, "source": source})
            for column in status["columns"]:
                await session.execute(text("""
                    INSERT INTO qm_quantdb_factor_field
                      (market, dataset_id, column_name, data_type, schema_hash, min_date, max_date, is_present, discovered_at)
                    VALUES (:market, :dataset_id, :column_name, :data_type, :schema_hash, :min_date, :max_date, TRUE, NOW())
                    ON CONFLICT (market, dataset_id, column_name) DO UPDATE SET
                      data_type = EXCLUDED.data_type, schema_hash = EXCLUDED.schema_hash, min_date = EXCLUDED.min_date,
                      max_date = EXCLUDED.max_date, is_present = TRUE, discovered_at = NOW()
                """), {
                    "market": market, "dataset_id": source, "column_name": column,
                    "data_type": status["column_types"].get(column), "schema_hash": status["schema_hash"],
                    "min_date": date.fromisoformat(status["min_date"]) if status["min_date"] else None,
                    "max_date": date.fromisoformat(status["max_date"]) if status["max_date"] else None,
                })
    return {"sources": discovered, "market": market}


@router.get("/fields")
async def list_factor_fields(
    market: str = Query("CN"),
    source_dataset: str = Query(DEFAULT_FACTOR_SOURCE),
    include_keys: bool = Query(False),
    current_user: dict = Depends(require_admin),
):
    _ = current_user
    market = normalize_market(market)
    source_dataset = _validate_source(source_dataset)
    async with get_session() as session:
        await _ensure_schema(session)
        rows = (await session.execute(text("""
            SELECT column_name, data_type, schema_hash, min_date, max_date, is_present, discovered_at
            FROM qm_quantdb_factor_field
            WHERE market = :market AND dataset_id = :source_dataset
            ORDER BY column_name
        """), {"market": market, "source_dataset": source_dataset})).mappings().all()
    fields = [
        {
            **dict(row),
            "dictionary": definition_for(str(row["column_name"])),
        }
        for row in rows
    ]
    if not include_keys:
        fields = [row for row in fields if row["column_name"] not in KEY_COLUMNS | set(REQUIRED_COLUMNS)]
    return {"source_dataset": source_dataset, "fields": fields}


@router.post("/versions")
async def create_draft_version(
    payload: CatalogVersionCreate,
    market: str = Query("CN"),
    current_user: dict = Depends(require_admin),
):
    """Create an empty draft. Mapping rows are explicitly added by the admin UI."""
    market = normalize_market(market)
    source_dataset = _validate_source(payload.source_dataset)
    version_id = f"qdb-{market.lower()}-{source_dataset}-{uuid.uuid4().hex[:12]}"
    async with get_session() as session:
        await _ensure_schema(session)
        await session.execute(text("""
            INSERT INTO qm_training_factor_catalog_version
              (version_id, market, version_name, status, source_dataset, created_by)
            VALUES (:version_id, :market, :version_name, 'draft', :source_dataset, :created_by)
        """), {
            "version_id": version_id, "market": market,
            "version_name": payload.version_name,
            "source_dataset": source_dataset,
            "created_by": str(current_user.get("user_id") or current_user.get("sub") or "admin"),
        })
    return {"version_id": version_id, "status": "draft", "source_dataset": source_dataset, "market": market}


@router.get("/catalog")
async def get_factor_catalog(
    market: str = Query("CN"),
    source_dataset: str = Query(DEFAULT_FACTOR_SOURCE),
    version_id: str | None = Query(None),
    current_user: dict = Depends(require_admin),
):
    _ = current_user
    market = normalize_market(market)
    source_dataset = _validate_source(source_dataset)
    async with get_session() as session:
        await _ensure_schema(session)
        if version_id:
            row = (await session.execute(text("""
                SELECT version_id, version_name, status, source_dataset, market, created_at, published_at
                FROM qm_training_factor_catalog_version WHERE version_id = :version_id
            """), {"version_id": version_id})).mappings().first()
            version = dict(row) if row else None
        else:
            version = await _active_version(session, source_dataset, market)
        # 没有活动发布版本是管理员首次配置时的正常状态，而不是资源路由
        # 不存在或权限异常。返回 200 可以让前端安静地显示“未发布”，避免被
        # 全局鉴权拦截器误报为 Auth Error。
        if not version and not version_id:
            return {
                "catalog": None,
                "market": market,
                "source_dataset": source_dataset,
                "message": "No published factor catalog for this source",
            }
        if not version:
            raise HTTPException(status_code=404, detail="Catalog version not found")
        if version["source_dataset"] != source_dataset:
            raise HTTPException(status_code=400, detail="Catalog version belongs to a different factor source")
        if version["market"] != market:
            raise HTTPException(status_code=400, detail="Catalog version belongs to a different market")
        return await _catalog_payload(session, version, source_dataset)


@router.put("/versions/{version_id}/mappings")
async def upsert_factor_mapping(version_id: str, payload: MappingUpdate, current_user: dict = Depends(require_admin)):
    _ = current_user
    mapping = payload.mapping
    source_dataset = _validate_source(mapping.source_dataset)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", mapping.source_column):
        raise HTTPException(status_code=400, detail="Invalid source_column")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", mapping.feature_key):
        raise HTTPException(status_code=400, detail="Invalid feature_key")
    if mapping.feature_key in set(REQUIRED_COLUMNS) | {"trade_date", "symbol", "dt"}:
        raise HTTPException(status_code=400, detail="feature_key cannot overwrite key or OHLCV columns")
    async with get_session() as session:
        await _ensure_schema(session)
        version = (await session.execute(text("""
            SELECT status, source_dataset, market FROM qm_training_factor_catalog_version WHERE version_id = :version_id
        """), {"version_id": version_id})).mappings().first()
        if not version:
            raise HTTPException(status_code=404, detail="Catalog version not found")
        if version["status"] != "draft":
            raise HTTPException(status_code=409, detail="Only draft catalogs can be edited")
        if version["source_dataset"] != source_dataset:
            raise HTTPException(status_code=400, detail="Mapping source must match draft source")
        field = (await session.execute(text("""
            SELECT 1 FROM qm_quantdb_factor_field
            WHERE market = :market AND dataset_id = :dataset_id AND column_name = :column_name AND is_present
        """), {"market": version["market"], "dataset_id": source_dataset,
               "column_name": mapping.source_column})).first()
        if not field:
            raise HTTPException(status_code=400, detail="Source field is not present in the discovered schema")
        mapping_id = mapping.mapping_id or uuid.uuid4().hex
        await session.execute(text("""
            INSERT INTO qm_training_factor_mapping
             (mapping_id, version_id, source_dataset, source_column, feature_key, display_name,
              category_id, category_name, enabled, default_selected, required, sort_order)
            VALUES (:mapping_id, :version_id, :source_dataset, :source_column, :feature_key, :display_name,
                    :category_id, :category_name, :enabled, :default_selected, :required, :sort_order)
            ON CONFLICT (version_id, source_dataset, feature_key) DO UPDATE SET
              mapping_id = EXCLUDED.mapping_id, source_column = EXCLUDED.source_column,
              display_name = EXCLUDED.display_name, category_id = EXCLUDED.category_id,
              category_name = EXCLUDED.category_name, enabled = EXCLUDED.enabled,
              default_selected = EXCLUDED.default_selected, required = EXCLUDED.required,
              sort_order = EXCLUDED.sort_order
        """), {
            "mapping_id": mapping_id,
            "version_id": version_id,
            **mapping.model_dump(exclude={"mapping_id"}),
        })
    return {"mapping_id": mapping_id, "version_id": version_id}


@router.post("/versions/{version_id}/publish")
async def publish_factor_catalog(version_id: str, current_user: dict = Depends(require_admin)):
    _ = current_user
    async with get_session() as session:
        await _ensure_schema(session)
        version = (await session.execute(text("""
            SELECT version_id, source_dataset, market, status FROM qm_training_factor_catalog_version
            WHERE version_id = :version_id
        """), {"version_id": version_id})).mappings().first()
        if not version:
            raise HTTPException(status_code=404, detail="Catalog version not found")
        if version["status"] != "draft":
            raise HTTPException(status_code=409, detail="Only draft catalogs can be published")
        count = (await session.execute(text("""
            SELECT count(*) FROM qm_training_factor_mapping
            WHERE version_id = :version_id AND enabled
        """), {"version_id": version_id})).scalar_one()
        if not count:
            raise HTTPException(status_code=400, detail="A published catalog needs at least one enabled factor")
        await session.execute(text("""
            UPDATE qm_training_factor_catalog_version SET status = 'archived'
            WHERE source_dataset = :source_dataset AND market = :market AND status = 'published'
        """), {"source_dataset": version["source_dataset"], "market": version["market"]})
        await session.execute(text("""
            UPDATE qm_training_factor_catalog_version
            SET status = 'published', published_at = :published_at WHERE version_id = :version_id
        """), {"version_id": version_id, "published_at": datetime.now(timezone.utc)})
    return {"version_id": version_id, "status": "published"}


@router.post("/versions/{version_id}/clone")
async def clone_factor_catalog(version_id: str, payload: CatalogVersionClone, current_user: dict = Depends(require_admin)):
    """Copy an immutable published version into an independently editable draft."""
    _ = current_user
    async with get_session() as session:
        await _ensure_schema(session)
        source = (await session.execute(text("""
            SELECT source_dataset, market FROM qm_training_factor_catalog_version WHERE version_id = :version_id
        """), {"version_id": version_id})).mappings().first()
        if not source:
            raise HTTPException(status_code=404, detail="Catalog version not found")
        clone_id = f"qdb-{str(source['market']).lower()}-{source['source_dataset']}-{uuid.uuid4().hex[:12]}"
        await session.execute(text("""
            INSERT INTO qm_training_factor_catalog_version
              (version_id, market, version_name, status, source_dataset, created_by)
            VALUES (:clone_id, :market, :version_name, 'draft', :source_dataset, :created_by)
        """), {
            "clone_id": clone_id, "market": source["market"],
            "version_name": payload.version_name, "source_dataset": source["source_dataset"],
            "created_by": str(current_user.get("user_id") or current_user.get("sub") or "admin"),
        })
        await session.execute(text("""
            INSERT INTO qm_training_factor_mapping
              (mapping_id, version_id, source_dataset, source_column, feature_key, display_name,
               category_id, category_name, enabled, default_selected, required, sort_order)
            SELECT :prefix || mapping_id, :clone_id, source_dataset, source_column, feature_key, display_name,
                   category_id, category_name, enabled, default_selected, required, sort_order
            FROM qm_training_factor_mapping WHERE version_id = :version_id
        """), {"prefix": f"{uuid.uuid4().hex[:8]}-", "clone_id": clone_id, "version_id": version_id})
    return {"version_id": clone_id, "source_dataset": source["source_dataset"],
            "market": source["market"], "status": "draft"}


@router.post("/versions/{version_id}/seed")
async def seed_draft_mappings(version_id: str, current_user: dict = Depends(require_admin)):
    """Convenience endpoint: add all discovered factor columns to a draft as mappings.

    新建草稿后：全部发现字段默认启用（enabled=True），
    其中 DEFAULT_SELECTED_FACTORS 里的 48 个核心因子额外默认勾选
    （default_selected=True），其余因子由管理员手动勾选。
    """
    _ = current_user
    async with get_session() as session:
        await _ensure_schema(session)
        version = (await session.execute(text("""
            SELECT status, source_dataset, market FROM qm_training_factor_catalog_version WHERE version_id = :version_id
        """), {"version_id": version_id})).mappings().first()
        if not version:
            raise HTTPException(status_code=404, detail="Catalog version not found")
        if version["status"] != "draft":
            raise HTTPException(status_code=409, detail="Only draft catalogs can be seeded")
        fields = (await session.execute(text("""
            SELECT column_name FROM qm_quantdb_factor_field
            WHERE market = :market AND dataset_id = :dataset_id AND is_present
            ORDER BY column_name
        """), {"market": version["market"], "dataset_id": version["source_dataset"]})).scalars().all()
        count = 0
        default_selected_count = 0
        for column in fields:
            if column in KEY_COLUMNS or column in REQUIRED_COLUMNS:
                continue
            definition = definition_for(str(column))
            cat_id = str(definition["category_id"])
            cat_name = str(definition["category_name"])
            is_default_selected = str(column) in DEFAULT_SELECTED_FACTORS
            await session.execute(text("""
                INSERT INTO qm_training_factor_mapping
                 (mapping_id, version_id, source_dataset, source_column, feature_key, display_name,
                  category_id, category_name, enabled, default_selected, required, sort_order)
                VALUES (:mapping_id, :version_id, :source_dataset, :source_column, :feature_key, :display_name,
                        :category_id, :category_name, TRUE, :default_selected, FALSE, :sort_order)
                ON CONFLICT (version_id, source_dataset, source_column) DO NOTHING
            """), {
                "mapping_id": uuid.uuid4().hex, "version_id": version_id,
                "source_dataset": version["source_dataset"], "source_column": column,
                "feature_key": column,
                "category_name": cat_name,
                "category_id": cat_id,
                "display_name": str(definition["display_name"]),
                "default_selected": is_default_selected,
                "sort_order": int(definition["sort_order"]) + count,
            })
            count += 1
            default_selected_count += int(is_default_selected)
    return {
        "version_id": version_id, "seeded_fields": count,
        "enabled_fields": count, "default_selected_fields": default_selected_count,
    }
