import asyncio
import json
import logging
import os
import re
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.services.api.routers.admin.admin_training import (
    complete_training_run,
    get_latest_training_run_for_owner,
    get_training_run_for_owner,
    submit_training_job,
)
from backend.services.api.routers.admin.model_management import (
    _load_feature_catalog_from_db,
    _load_feature_catalog_from_file,
)
from backend.services.api.routers.admin.model_management_utils import (
    _enrich_feature_catalog_with_data_coverage_async,
)
from backend.services.api.routers.admin.quantdb_factor_catalog import (
    load_quantdb_training_catalog,
    load_quantdb_training_sources,
)
from backend.services.engine.data_platform.quantdb_factor_reader import (
    DEFAULT_FACTOR_SOURCE,
)
from backend.services.api.training_shap_summary import read_shap_summary_rows, to_int_or
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.engine.inference.batch_aggregator import aggregate_batch
from backend.services.engine.inference.batch_orchestrator import (
    batch_inference_orchestrator,
)
from backend.services.engine.inference.router_service import InferenceRouterService
from backend.services.engine.inference.script_runner import InferenceScriptRunner
from backend.services.engine.services.model_inference_batch_persistence import (
    model_inference_batch_persistence,
)
from backend.services.engine.services.model_inference_persistence import (
    model_inference_persistence,
)
from backend.shared.database_manager_v2 import get_session
from backend.shared.inference_stats import compute_score_distribution
from backend.shared.model_registry import model_registry_service
from backend.shared.redis_sentinel_client import get_redis_sentinel_client
from backend.shared.trading_calendar import calendar_service

router = APIRouter()
logger = logging.getLogger(__name__)


def compute_market_signals(signals: list[dict[str, Any]]) -> dict[str, Any]:
    """从已标注行业/板块的信号列表计算市场信号指标。

    输入 signals 需含 fusion_score、industry（申万128行业）、board（5大板块）、symbol。
    输出与详情接口 summary 中的市场信号字段一致，供列表接口复用：
      - board_top1_avg         5大板块各自最高分取平均（市场广度）
      - industry_top1_count    Top20 涉及的申万行业数（覆盖行业数）
      - industry_avg_top1      Top20 行业 Top1 分数均值
      - strong_industry_count  Top1 ≥ 0.10 的行业个数
      - market_signal          入场判断（可入场 / 谨慎 / 空仓观望）
    """
    if not signals:
        return {}

    # 板块 Top1（5大板块各自最高分取平均）
    board_top1: dict[str, float] = {}
    for item in signals:
        score = item.get("fusion_score")
        if score is None:
            continue
        board = item.get("board") or "其他"
        try:
            fscore = float(score)
        except (TypeError, ValueError):
            continue
        if board not in board_top1 or fscore > board_top1[board]:
            board_top1[board] = fscore
    top1_scores = [
        board_top1[b]
        for b in ("沪主板", "深主板", "中小板", "创业板", "科创板")
        if b in board_top1
    ]
    board_top1_avg = (
        round(sum(top1_scores) / len(top1_scores), 4) if top1_scores else None
    )

    # 行业信号：Top20 股票按申万行业分组取各行业 Top1
    sorted_signals = sorted(
        (it for it in signals if it.get("fusion_score") is not None),
        key=lambda it: float(it["fusion_score"]),
        reverse=True,
    )
    top20 = sorted_signals[:20]
    industry_top1: dict[str, dict[str, Any]] = {}
    for it in top20:
        ind = str(it.get("industry") or "").strip()
        if not ind:
            continue
        fscore = float(it["fusion_score"])
        cur = industry_top1.get(ind)
        if cur is None or fscore > cur["top1_score"]:
            industry_top1[ind] = {
                "industry": ind,
                "top1_score": fscore,
                "top1_symbol": str(it.get("symbol") or ""),
                "top1_name": str(it.get("stock_name") or ""),
            }

    industry_stats = sorted(
        industry_top1.values(), key=lambda x: x["top1_score"], reverse=True
    )
    ind_top1_values = [float(x["top1_score"]) for x in industry_stats]
    industry_avg_top1 = (
        round(sum(ind_top1_values) / len(ind_top1_values), 4)
        if ind_top1_values
        else None
    )

    # 阈值自适应：融合模型分数是截面百分位 [-1,1]（高分常见 0.8+），
    # 硬编码 0.09/0.06/0.10 会把所有行业判为强信号或全部弱信号。
    # 检测实际分数范围，wide scale 时用 80/50 分位数作为强/弱阈值。
    _all_scores = [
        float(it["fusion_score"])
        for it in signals
        if it.get("fusion_score") is not None
    ]
    _is_wide = bool(_all_scores) and (
        max(_all_scores) > 0.35 or min(_all_scores) < -0.35
    )
    if _is_wide and _all_scores:
        _sn = len(_all_scores)
        _ss = sorted(_all_scores)
        strong_thr = float(_ss[max(0, min(_sn - 1, int(0.80 * (_sn - 1))))])
        entry_thr = float(_ss[max(0, min(_sn - 1, int(0.50 * (_sn - 1))))])
        empty_thr = float(_ss[max(0, min(_sn - 1, int(0.30 * (_sn - 1))))])
    else:
        strong_thr, entry_thr, empty_thr = 0.10, 0.09, 0.06
    strong_industry_count = sum(
        1 for x in industry_stats if float(x["top1_score"]) >= strong_thr
    )

    # 入场判断（平衡型默认）：avg Top1 ≥ entry_thr 且 强行业数 ≥ 2
    entry_signal = (
        "strong"
        if (
            industry_avg_top1 is not None
            and industry_avg_top1 >= entry_thr
            and strong_industry_count >= 2
        )
        else "weak"
    )
    if industry_avg_top1 is not None and industry_avg_top1 < empty_thr:
        entry_signal = "empty"  # 绝对空仓

    return {
        "board_top1_avg": board_top1_avg,
        "industry_top1": industry_stats,
        "industry_top1_count": len(industry_stats),
        "industry_avg_top1": industry_avg_top1,
        "strong_industry_count": strong_industry_count,
        "market_signal": {
            "avg_top1": industry_avg_top1,
            "strong_industry_count": strong_industry_count,
            "entry_signal": entry_signal,
            "entry_threshold": entry_thr,
            "empty_threshold": empty_thr,
            "strong_threshold": strong_thr,
            "score_scale": "wide" if _is_wide else "normal",
            "label": "可入场"
            if entry_signal == "strong"
            else ("空仓观望" if entry_signal == "empty" else "谨慎"),
        },
    }


# models/production 目录（相对项目根）
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_PRODUCTION_DIR = Path(
    os.getenv("MODELS_PRODUCTION_ROOT", str(_PROJECT_ROOT / "models" / "production"))
)


def _load_production_models() -> list[dict[str, Any]]:
    """扫描 models/production/*/metadata.json，返回系统模型列表。"""
    results: list[dict[str, Any]] = []
    if not _PRODUCTION_DIR.exists():
        return results
    for subdir in sorted(_PRODUCTION_DIR.iterdir()):
        meta_file = subdir / "metadata.json"
        if not meta_file.is_file():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        model_id = f"sys-{subdir.name}"
        # 兼容旧格式（model_info 嵌套）和新格式（平铺字段）
        info = meta.get("model_info", {})
        tc = meta.get("training_config", {})
        perf = meta.get("performance_metrics", {})

        # 统一字段名：新训练脚本用 val_start/val_end，旧格式用 valid_start/valid_end
        val_start = meta.get("val_start") or meta.get("valid_start")
        val_end = meta.get("val_end") or meta.get("valid_end")
        test_start = meta.get("test_start")
        test_end = meta.get("test_end")

        # display_name 优先取平铺字段，回退旧格式 model_info.name
        display_name = (
            meta.get("display_name")
            or meta.get("model_name")
            or info.get("name")
            or subdir.name
        )

        # label_formula 优先取平铺字段，回退旧格式 training_config.label
        label_formula = meta.get("label_formula") or tc.get("label") or ""

        # 市场归属：新训练脚本写在顶层 market / context.market；旧系统模型缺省 CN
        ctx = meta.get("context") if isinstance(meta.get("context"), dict) else {}
        market_tag = str(
            meta.get("market") or ctx.get("market") or tc.get("market") or "CN"
        ).upper()

        # metrics：新格式在 meta.metrics，旧格式在 performance_metrics
        new_metrics = meta.get("metrics", {})
        if new_metrics:
            perf = {
                "train": {
                    "mean_ic": new_metrics.get("train_ic"),
                    "icir": new_metrics.get("train_rank_icir"),
                },
                "valid": {
                    "mean_ic": new_metrics.get("val_ic"),
                    "icir": new_metrics.get("val_rank_icir"),
                },
                "test": {
                    "mean_ic": new_metrics.get("test_ic"),
                    "icir": new_metrics.get("test_rank_icir"),
                },
            }

        results.append(
            {
                "model_id": model_id,
                "dir_name": subdir.name,
                "tenant_id": "system",
                "market": market_tag,
                "display_name": display_name,
                "description": meta.get("description") or info.get("description", ""),
                "framework": meta.get("framework", ""),
                "model_type": meta.get("model_type", ""),
                "feature_count": meta.get("feature_count"),
                "feature_columns": meta.get("feature_columns", []),
                "is_neutralized": meta.get("is_neutralized", False),
                "algorithm": info.get("algorithm", ""),
                "version": info.get("version", meta.get("version", "")),
                "created_at": meta.get("generated_at")
                or info.get("created_at", meta.get("trained_at", "")),
                "training_config": tc,
                # 统一字段名：val_start/val_end（和用户模型 metadata_json 保持一致）
                "train_start": meta.get("train_start"),
                "train_end": meta.get("train_end"),
                "valid_start": val_start,
                "valid_end": val_end,
                "test_start": test_start,
                "test_end": test_end,
                # 额外平铺字段（前端 systemModelToUserModel 直接映射）
                "label_formula": label_formula,
                "target_horizon_days": meta.get("target_horizon_days"),
                "target_mode": meta.get("target_mode"),
                "data_source": meta.get("data_source", ""),
                "best_iteration": meta.get("best_iteration"),
                "performance_metrics": perf,
                "inference_config": meta.get("inference", {}),
                "files": meta.get("files", {}),
                "metadata_path": str(meta_file),
            }
        )
    return results


class SetDefaultModelRequest(BaseModel):
    model_id: str


class EnsembleCreateRequest(BaseModel):
    source_model_ids: list[str] = Field(
        ..., min_length=2, description="源模型 ID 列表（至少 2 个）"
    )
    display_name: str = Field(
        default="", description="融合模型显示名（可选，自动生成）"
    )
    weight_strategy: str = Field(
        default="equal",
        description="权重策略: equal / icir / manual / recent_ic",
    )
    manual_weights: dict[str, float] | None = Field(
        default=None, description="manual 策略下各源模型权重"
    )
    fusion_strategy: str = Field(
        default="linear",
        description="融合算法: linear / majority_vote / periodic_hierarchy / confidence_gate",
    )
    strategy_config: dict[str, float] | None = Field(
        default=None,
        description="融合算法参数（如 periodic_boundary / confidence_threshold）",
    )


class SetStrategyBindingRequest(BaseModel):
    model_id: str


class InferenceRunRequest(BaseModel):
    model_id: str
    inference_date: date = Field(..., description="推理基准日期 YYYY-MM-DD")


class InferenceSettingsRequest(BaseModel):
    enabled: bool
    schedule_time: str | None = Field(default=None, description="每日执行时间 HH:MM")


class BatchInferenceRequest(BaseModel):
    model_id: str
    mode: str = Field(
        default="lookback",
        description="lookback=锚定日回溯窗口；range=显式日期区间",
    )
    anchor_date: date | None = Field(
        default=None, description="锚定交易日 YYYY-MM-DD（lookback 模式用）"
    )
    start_date: date | None = Field(
        default=None, description="区间起始日 YYYY-MM-DD（range 模式用）"
    )
    end_date: date | None = Field(
        default=None, description="区间结束日 YYYY-MM-DD（range 模式用）"
    )
    window_days: int | None = Field(
        default=None,
        ge=1,
        le=60,
        description="回溯窗口交易日数，缺省取模型持有期 H",
    )
    top_k: int = Field(default=20, ge=1, le=500, description="榜单 Top-K")
    side: str = Field(default="both", description="long | short | both")
    reuse_existing: bool = Field(
        default=True, description="复用已有的当日成功推理结果（断点续跑）"
    )
    concurrency: int = Field(
        default=1, ge=1, le=5, description="并发执行的交易日数（每个为独立子进程）"
    )


def _owner_scope(current_user: dict[str, Any]) -> tuple[str, str]:
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="用户身份无效")
    return tenant_id, user_id


async def _resolve_inference_trade_date_with_calendar(
    *,
    current_user: dict[str, Any],
    requested_date: date,
    market: str = "SSE",
) -> tuple[date, bool]:
    tenant_id, user_id = _owner_scope(current_user)
    return await _resolve_trade_date_for_owner(
        tenant_id=tenant_id,
        user_id=user_id,
        requested_date=requested_date,
        market=market,
    )


async def _resolve_trade_date_for_owner(
    *,
    tenant_id: str,
    user_id: str,
    requested_date: date,
    market: str = "SSE",
) -> tuple[date, bool]:
    """交易日回退：非交易日回退到上一交易日。返回 (交易日, 是否发生回退)。

    与 _resolve_inference_trade_date_with_calendar 的区别是不依赖请求上下文，
    供批量推理编排器（无 current_user）复用。
    """
    is_td = await calendar_service.is_trading_day(
        market=market,
        trade_date=requested_date,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    if is_td:
        return requested_date, False
    previous_td = await calendar_service.prev_trading_day(
        market=market,
        trade_date=requested_date,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    return previous_td, True


async def _resolve_requested_model(current_user: dict[str, Any], model_id: str):
    tenant_id, user_id = _owner_scope(current_user)
    requested_model_id = str(model_id or "").strip()
    if not requested_model_id:
        default_model = await model_registry_service.get_default_model(
            tenant_id=tenant_id, user_id=user_id
        )
        if not default_model:
            raise HTTPException(status_code=404, detail="未找到默认模型")
        requested_model_id = str(default_model.get("model_id") or "")
    resolved = await model_registry_service.resolve_effective_model(
        tenant_id=tenant_id,
        user_id=user_id,
        model_id=requested_model_id,
    )
    if (
        requested_model_id
        and resolved.fallback_used
        and resolved.model_source in {"user_default", "system_fallback"}
    ):
        raise HTTPException(
            status_code=404, detail=f"模型不可用或未就绪: {requested_model_id}"
        )
    if not resolved.storage_path:
        raise HTTPException(
            status_code=404, detail=f"模型路径不可用: {requested_model_id}"
        )
    return requested_model_id, resolved


from backend.shared.qlib_paths import (
    resolve_qlib_provider_uri,
    resolve_qlib_calendar_path,
)

_MARKET_QLIB_DATA_PATH: dict[str, str] = {
    "CN": resolve_qlib_provider_uri("CN"),
    "HK": resolve_qlib_provider_uri("HK"),
    "US": resolve_qlib_provider_uri("US"),
    "CRYPTO": resolve_qlib_provider_uri("CRYPTO"),
    "FUTURES": resolve_qlib_provider_uri("FUTURES"),
}

_MARKET_CALENDAR: dict[str, str] = {
    "CN": "SSE",
    "HK": "HKEX",
    "US": "NYSE",
    "CRYPTO": "24/7",
    "FUTURES": "CME",
}


def _get_model_market(model_dir: Path) -> str:
    """Extract market from model's metadata.json context.market field."""
    meta_file = model_dir / "metadata.json"
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            context = meta.get("context")
            if isinstance(context, dict):
                market = str(context.get("market", "")).upper()
                if market in _MARKET_CALENDAR:
                    return market
        except Exception:
            pass
    return "CN"


def _get_model_calendar(model_dir: Path) -> str:
    """Get trading calendar ID for the model's market."""
    return _MARKET_CALENDAR.get(_get_model_market(model_dir), "SSE")


def _get_model_data_dir(model_dir: Path, metadata: dict | None = None) -> str:
    """
    从模型配置中获取推理数据目录。

    优先级：
    1. metadata.json 中的 qlib_data_path 字段（绝对路径）
    2. metadata.json 中的 context.market 字段映射到对应 qlib 数据目录
    3. metadata.json 中的 data_source 字段判断：
       - "qlib" -> db/qlib_data
       - "parquet" 或其他 -> db/feature_snapshots
    4. 默认值 -> db/feature_snapshots
    """
    # QuantDB-bound models are pinned to the raw factor root.  This must be
    # evaluated before historical qlib_data_path/context compatibility hints.
    if metadata:
        if str(metadata.get("data_source") or "").lower() == "quantdb_factors":
            # 与 script_runner._resolve_quantdb_data_dir 保持一致：
            # QUANTDB_DATA_DIR → QM_QUANTDB_DATA_DIR → hub 统一解析。
            # 仅读 QUANTDB_DATA_DIR 会在容器内落到不存在的默认 /app/data/quantdb，
            # 导致推理数据目录预检阻断。
            try:
                from backend.services.engine.inference.script_runner import (
                    _resolve_quantdb_data_dir,
                )

                return _resolve_quantdb_data_dir()
            except Exception:  # pragma: no cover - 兜底
                return os.getenv("QUANTDB_DATA_DIR", "/app/data/quantdb")
        qlib_data_path = metadata.get("qlib_data_path")
        if qlib_data_path:
            return qlib_data_path

        # 根据 context.market 映射到对应 qlib 数据目录
        context = metadata.get("context")
        if isinstance(context, dict):
            market = str(context.get("market", "")).upper()
            if market in _MARKET_QLIB_DATA_PATH:
                return _MARKET_QLIB_DATA_PATH[market]

        # 根据 data_source 判断
        data_source = str(metadata.get("data_source", "")).lower()
        if data_source == "qlib":
            return resolve_qlib_provider_uri()

    # 尝试从模型目录读取 metadata.json
    meta_file = model_dir / "metadata.json"
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if str(meta.get("data_source") or "").lower() == "quantdb_factors":
                try:
                    from backend.services.engine.inference.script_runner import (
                        _resolve_quantdb_data_dir,
                    )

                    return _resolve_quantdb_data_dir()
                except Exception:  # pragma: no cover - 兜底
                    return os.getenv("QUANTDB_DATA_DIR", "/app/data/quantdb")
            qlib_data_path = meta.get("qlib_data_path")
            if qlib_data_path:
                return qlib_data_path

            context = meta.get("context")
            if isinstance(context, dict):
                market = str(context.get("market", "")).upper()
                if market in _MARKET_QLIB_DATA_PATH:
                    return _MARKET_QLIB_DATA_PATH[market]

            data_source = str(meta.get("data_source", "")).lower()
            if data_source == "qlib":
                return resolve_qlib_provider_uri()
        except Exception:
            pass

    # 默认值
    return "db/feature_snapshots"


def _render_next_run(next_run_at: Any) -> str | None:
    if next_run_at is None:
        return None
    if isinstance(next_run_at, str):
        try:
            parsed = datetime.fromisoformat(next_run_at)
            return parsed.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return next_run_at.replace("T", " ")[:16]
    try:
        return next_run_at.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(next_run_at)


@router.get(
    "/system-models", summary="获取系统内置模型列表（读取 models/production 目录）"
)
async def list_system_models(
    market: str | None = Query(
        None, description="按市场过滤（CN/HK/US/FUTURES/CRYPTO），缺省返回全部"
    ),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """返回 models/production/ 下所有含 metadata.json 的子目录，无需分页。"""
    try:
        models = _load_production_models()
        market_upper = str(market or "").upper().strip()
        if market_upper:
            models = [
                m for m in models if str(m.get("market") or "").upper() == market_upper
            ]
        return {"status": "success", "count": len(models), "models": models}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/feature-catalog", summary="获取模型训练特征字典（用户态）")
async def get_model_feature_catalog(
    market: str | None = None,
    factor_source: str | None = Query(
        None, description="QuantDB 因子源（市场直读训练）"
    ),
    include_coverage: bool = Query(
        False, description="是否附带 parquet 数据覆盖统计（默认 false，加速首屏）"
    ),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    _ = current_user
    market_upper = str(market or "CN").upper()
    if market_upper in {"CN", "A", "A_SHARE"}:
        # CN 训练只使用已发布的 QuantDB 目录。未发布也是一个明确的正常状态，
        # 绝不能回退到旧 DB/文件目录，否则页面看到的字段会和实际数据源不一致。
        # 覆盖信息来自同步时的缓存 manifest，不在请求期间扫描 parquet。
        _ = include_coverage  # Retained for older clients; coverage is always cached.
        return await load_quantdb_training_catalog(
            factor_source or DEFAULT_FACTOR_SOURCE, market="CN"
        )
    # 非 A 股市场：显式携带 factor_source 时走该市场的 QuantDB 直读目录
    # （港股 l1_factors/ccass_factors/south_factors 等）；未携带时兼容
    # 旧前端，回退到传统 DB/文件特征字典（快照训练路径）。
    if factor_source:
        return await load_quantdb_training_catalog(factor_source, market=market)
    try:
        catalog = await _load_feature_catalog_from_db(market=market)
    except Exception:
        catalog = None

    if not catalog:
        catalog = _load_feature_catalog_from_file(market=market)

    if not catalog:
        raise HTTPException(
            status_code=404, detail="未找到可用的特征字典（DB/文件均不可用）"
        )

    if include_coverage:
        return await _enrich_feature_catalog_with_data_coverage_async(
            catalog, market=market
        )
    return catalog


@router.get("/training-sources", summary="获取可选 QuantDB 训练数据源（用户态）")
async def get_quantdb_training_sources(
    market: str | None = Query(None, description="市场（CN/HK/US/FUTURES…），默认 CN"),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """Return backend-defined source choices and published/readiness state."""
    _ = current_user
    return await load_quantdb_training_sources(market or "CN")


@router.get("/qlib-data-range", summary="获取 Qlib 数据日期范围")
async def get_qlib_data_range(
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """
    返回 qlib_data 的日期范围，用于前端日期选择器限制。
    读取 db/qlib_data/calendars/day.txt 获取交易日历。
    """
    _ = current_user
    qlib_data_dir = Path(resolve_qlib_provider_uri())
    calendars_path = qlib_data_dir / "calendars" / "day.txt"

    result = {
        "exists": False,
        "min_date": None,
        "max_date": None,
        "total_trading_days": 0,
    }

    if not calendars_path.exists():
        return result

    try:
        calendar = [
            line.strip()
            for line in calendars_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if calendar:
            result["exists"] = True
            result["min_date"] = calendar[0]
            result["max_date"] = calendar[-1]
            result["total_trading_days"] = len(calendar)
    except Exception as e:
        logger.warning("Failed to read qlib calendar: %s", e)

    return result


@router.post("/run-training", summary="启动云端模型训练任务（用户态）")
async def run_training(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    return await submit_training_job(payload, background_tasks, current_user)


@router.get("/training-runs/active", summary="获取当前用户最近/活跃的训练任务（切页恢复用）")
async def get_active_training_run(
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """优先从 redis 活跃索引返回该用户最近一次的进行中/最近训练任务快照。

    前端切页后再回到训练页时，用此接口恢复进度与日志（轮询的心智保持连续）。
    无任何记录时返回 200 + null。
    """
    latest = await get_latest_training_run_for_owner(current_user)
    # 无记录是正常态（新用户/从未训练）：返回 200 + null 而非 404，
    # 避免被前端全局错误拦截器当作异常处理，产生无意义的报错日志。
    return latest


@router.get("/training-runs/{run_id}", summary="获取训练任务状态（用户态）")
async def get_training_run(
    run_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    return await get_training_run_for_owner(run_id, current_user)


@router.get("", summary="获取当前用户模型列表（用户态）")
async def list_user_models(
    include_archived: bool = False,
    market: str | None = None,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    models = await model_registry_service.list_models(
        tenant_id=tenant_id,
        user_id=user_id,
        include_archived=include_archived,
        market=market,
    )
    return {"items": models, "total": len(models)}


@router.get("/default", summary="获取当前用户默认模型（用户态）")
async def get_default_model(
    market: str | None = Query(
        None, description="按市场过滤默认模型（CN/HK/US/FUTURES/CRYPTO）"
    ),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    model = await model_registry_service.get_default_model(
        tenant_id=tenant_id, user_id=user_id, market=market
    )
    if not model:
        raise HTTPException(status_code=404, detail="Default model not found")
    return model


@router.patch("/default", summary="设置当前用户默认模型（用户态）")
async def set_default_model(
    payload: SetDefaultModelRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    try:
        model = await model_registry_service.set_default_model(
            tenant_id=tenant_id,
            user_id=user_id,
            model_id=payload.model_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return model


@router.get("/strategy-bindings/{strategy_id}", summary="获取策略模型绑定（用户态）")
async def get_strategy_binding(
    strategy_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    binding = await model_registry_service.get_strategy_binding(
        tenant_id=tenant_id,
        user_id=user_id,
        strategy_id=strategy_id,
    )
    if not binding:
        raise HTTPException(status_code=404, detail="Strategy binding not found")
    return binding


@router.put("/strategy-bindings/{strategy_id}", summary="设置策略模型绑定（用户态）")
async def set_strategy_binding(
    strategy_id: str,
    payload: SetStrategyBindingRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    try:
        binding = await model_registry_service.set_strategy_binding(
            tenant_id=tenant_id,
            user_id=user_id,
            strategy_id=strategy_id,
            model_id=payload.model_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return binding


@router.delete("/strategy-bindings/{strategy_id}", summary="解除策略模型绑定（用户态）")
async def delete_strategy_binding(
    strategy_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    deleted = await model_registry_service.delete_strategy_binding(
        tenant_id=tenant_id,
        user_id=user_id,
        strategy_id=strategy_id,
    )
    return {"deleted": bool(deleted), "strategy_id": strategy_id}


@router.get("/{model_id}/drift", summary="获取模型数据漂移 PSI 详情（用户态）")
async def get_model_drift(
    model_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    model = await model_registry_service.get_model(
        tenant_id=tenant_id, user_id=user_id, model_id=model_id
    )
    # 兼容：部分历史模型在 admin/00000001 下，当前用户如为 default 则放宽到租户内任意所有者
    if not model:
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT tenant_id, user_id, model_id, storage_path, model_file,
                               metadata_json, metrics_json, is_default, created_at
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND model_id = :model_id
                        LIMIT 1
                        """
                        ),
                        {"tenant_id": tenant_id, "model_id": model_id},
                    )
                )
                .mappings()
                .first()
            )
            if row:
                model = {
                    "tenant_id": row["tenant_id"],
                    "user_id": row["user_id"],
                    "model_id": row["model_id"],
                    "storage_path": row["storage_path"],
                    "model_file": row["model_file"],
                    "metadata_json": row["metadata_json"]
                    if isinstance(row["metadata_json"], dict)
                    else {},
                    "metrics_json": row["metrics_json"]
                    if isinstance(row["metrics_json"], dict)
                    else {},
                    "is_default": row["is_default"],
                    "created_at": row["created_at"],
                }
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    metadata = (
        model.get("metadata_json")
        if isinstance(model.get("metadata_json"), dict)
        else {}
    )
    drift = metadata.get("drift") if isinstance(metadata.get("drift"), dict) else None
    if not drift or not drift.get("enabled"):
        drift = (
            metadata.get("drift") if isinstance(metadata.get("drift"), dict) else None
        )
    if not drift:
        return {"enabled": False, "reason": "drift not available", "model_id": model_id}
    return {"model_id": model_id, **drift}


@router.get("/{model_id}/market-regime", summary="获取模型大盘三态时序（用户态）")
async def get_model_market_regime(
    model_id: str,
    window: int = Query(90, ge=1, le=200, description="近 N 个交易日"),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    model = await model_registry_service.get_model(
        tenant_id=tenant_id, user_id=user_id, model_id=model_id
    )
    if not model:
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT tenant_id, user_id, model_id, storage_path, model_file,
                               metadata_json, metrics_json, is_default, created_at
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND model_id = :model_id
                        LIMIT 1
                        """
                        ),
                        {"tenant_id": tenant_id, "model_id": model_id},
                    )
                )
                .mappings()
                .first()
            )
            if row:
                model = {
                    "tenant_id": row["tenant_id"],
                    "user_id": row["user_id"],
                    "model_id": row["model_id"],
                    "storage_path": row["storage_path"],
                    "model_file": row["model_file"],
                    "metadata_json": row["metadata_json"]
                    if isinstance(row["metadata_json"], dict)
                    else {},
                    "metrics_json": row["metrics_json"]
                    if isinstance(row["metrics_json"], dict)
                    else {},
                    "is_default": row["is_default"],
                    "created_at": row["created_at"],
                }
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    # 阈值 0.08/0.02 对 Top20 均值更敏感（全市场均值恒≈0/全负），仅 90 日
    bull_thr, bear_thr = 0.08, 0.02
    series: list[dict[str, Any]] = []

    def _regime_point(
        trade_date: str, avg_score: float, median_score: float, cnt: int
    ) -> dict[str, Any]:
        regime = (
            "bull" if avg_score >= bull_thr else "bear" if avg_score < bear_thr else "sideways"
        )
        color = (
            "#ef4444"
            if regime == "bull"
            else "#10b981"
            if regime == "bear"
            else "#94a3b8"
        )
        return {
            "trade_date": str(trade_date)[:10],
            "avg_score": round(avg_score, 4),
            "median_score": round(float(median_score or 0), 4),
            "count": int(cnt or 0),
            "regime": regime,
            "color": color,
        }

    try:
        # ── 主路径：读 pred.parquet（全市场截面，B 套）──────────────────────
        # 大盘分析反映「模型对全市场 Top100 的打分均值」。pred.parquet 是全市场
        # 稳定分数源；engine_signal_scores 会被个股推理（仅持久化个别标点）污染，
        # 若以其为主会导致「大盘分析只显示个股数据」。故优先读 pred.parquet。
        storage_path = str(model.get("storage_path") or "").strip()
        parquet_file: Path | None = None
        if storage_path:
            import duckdb

            pred_path = Path(storage_path) / "pred.parquet"
            # 兼容部分模型 pred 存储在上级或 pred/ 目录
            candidates = [
                pred_path,
                Path(storage_path) / "pred" / "pred.parquet",
                Path(storage_path) / "pred.parquet",
            ]
            parquet_file = next((p for p in candidates if p.is_file()), None)
            if parquet_file:
                try:
                    con = duckdb.connect()
                    # 统一字段：pred / fusion_score 均可能
                    cols = [
                        r[0]
                        for r in con.execute(
                            f"SELECT * FROM read_parquet('{str(parquet_file)}') LIMIT 0"
                        ).description
                    ]
                    score_col = (
                        "pred"
                        if "pred" in cols
                        else "fusion_score"
                        if "fusion_score" in cols
                        else None
                    )
                    date_col = (
                        "trade_date"
                        if "trade_date" in cols
                        else "date"
                        if "date" in cols
                        else None
                    )
                    if score_col and date_col:
                        q = f"""
                            SELECT CAST(trade_date AS VARCHAR) AS trade_date,
                                   AVG(CAST(score AS DOUBLE))::DOUBLE AS avg_score,
                                   MEDIAN(CAST(score AS DOUBLE))::DOUBLE AS median_score,
                                   COUNT(*)::INTEGER AS cnt
                            FROM (
                                SELECT {date_col} AS trade_date, CAST({score_col} AS DOUBLE) AS score,
                                       ROW_NUMBER() OVER (PARTITION BY {date_col} ORDER BY CAST({score_col} AS DOUBLE) DESC) AS rn
                                FROM read_parquet('{str(parquet_file)}')
                                WHERE CAST({score_col} AS DOUBLE) IS NOT NULL
                            )
                            WHERE rn <= 100
                            GROUP BY trade_date
                            ORDER BY trade_date DESC
                            LIMIT {int(window)}
                        """
                        rows2 = con.execute(q).fetchall()
                        for trade_date, avg_score, median_score, cnt in rows2:
                            try:
                                avg = float(avg_score or 0)
                            except Exception:
                                continue
                            series.append(_regime_point(trade_date, avg, median_score, cnt))
                        con.close()
                except Exception as exc:
                    logger.warning(
                        "market-regime pred.parquet 主读失败 %s: %s",
                        model_id,
                        exc,
                    )

        # ── 兜底路径：无 pred.parquet（或读取为空）时读 engine_signal_scores ──
        # 仅作回退；个股推理只写个别标点的缺点在无 pred.parquet 的旧模型上难免，
        # 但此时无全市场分数源可用，只能以信号表近似。
        if not series:
            async with get_session(read_only=True) as session:
                rows = (
                    (
                        await session.execute(
                            text(
                                """
                                SELECT r.data_trade_date::text AS trade_date,
                                       AVG(top.fusion_score)::float AS avg_score,
                                       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY top.fusion_score)::float AS median_score,
                                       COUNT(*)::int AS cnt
                                FROM qm_model_inference_runs r
                                JOIN LATERAL (
                                    SELECT s.fusion_score
                                    FROM engine_signal_scores s
                                    WHERE s.run_id = r.run_id AND s.tenant_id = r.tenant_id AND s.user_id = r.user_id
                                    ORDER BY s.fusion_score DESC
                                    LIMIT 100
                                ) top ON true
                                WHERE r.tenant_id = :tenant_id AND r.user_id = :user_id
                                  AND r.model_id = :model_id AND r.status = 'completed'
                                GROUP BY r.data_trade_date
                                ORDER BY r.data_trade_date DESC
                                LIMIT :window
                                """
                            ),
                            {
                                "tenant_id": tenant_id,
                                "user_id": user_id,
                                "model_id": model_id,
                                "window": int(window),
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
            for row in rows:
                d = dict(row)
                avg = float(d.get("avg_score") or 0)
                series.append(
                    _regime_point(
                        str(d.get("trade_date") or "")[:10],
                        avg,
                        float(d.get("median_score") or 0),
                        int(d.get("cnt") or 0),
                    )
                )
        series.sort(key=lambda x: x["trade_date"])
        # 仅保留最近 90 交易日
        if len(series) > int(window):
            series = series[-int(window) :]
        current = series[-1] if series else None
        return {
            "model_id": model_id,
            "window": int(window),
            "thresholds": {"bull": bull_thr, "bear": bear_thr},
            "series": series,
            "current": current,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── 推理覆盖与一键补全（追加至 pred.parquet） ──────────────────────────────
_backfill_tasks: dict[str, dict[str, Any]] = {}


def _resolve_pred_candidates(storage_path: str) -> list[Path]:
    base = Path(storage_path)
    return [
        base / "pred.parquet",
        base / "pred" / "pred.parquet",
        base / "pred.parquet",
    ]


def _read_pred_dates(parquet_file: Path) -> list[str]:
    import duckdb

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
            f"SELECT DISTINCT CAST({date_col} AS DATE) AS d FROM read_parquet('{str(parquet_file)}') ORDER BY d"
        ).fetchall()
        return [str(r[0])[:10] for r in rows if r[0] is not None]
    finally:
        try:
            con.close()
        except Exception:
            pass


def _quantdb_latest_factor_date(market: str = "CN") -> date | None:
    """QuantDB 因子数据最新可用交易日；失败返回 None。

    用于把 coverage 缺口的上限从「最新交易日」收敛到「数据已产出日」，
    避免把因子尚未产出（T+1 更新）的当日误判为缺口并触发注定失败的补全。
    """
    try:
        from backend.services.engine.data_platform.quantdb_factor_reader import (
            QuantDBFactorReader,
            market_data_dir,
        )

        qdir = market_data_dir(market)
        if not qdir.is_dir():
            return None
        reader = QuantDBFactorReader(qdir, market=market)
        dates = reader.available_dates("l1_factors")
        if not dates:
            return None
        return date.fromisoformat(str(dates[-1])[:10])
    except Exception:
        return None


def _merge_runner_signals_into_pred(
    parquet_file: Path, signals_by_date: list[tuple[str, list[dict]]]
) -> int:
    """把 runner 真实推理分数合并进 pred.parquet（coverage/分数曲线的数据源）。

    实现移至共享模块 backend.services.engine.inference.pred_merge，
    供一键补全（API 层）与每日自动推理（engine 层）两条链路共用，
    保持两套推理数据一致。
    """
    from backend.services.engine.inference.pred_merge import merge_signals_into_pred

    # 补全场景允许在 pred.parquet 缺失时以真实分数创建（多日连续补全
    # 可形成完整序列）
    return merge_signals_into_pred(
        Path(parquet_file), signals_by_date, create_if_missing=True
    )


def _latest_trading_date() -> date:
    try:
        import exchange_calendars as xcals
        import pandas as pd
        from datetime import timedelta

        cal = xcals.get_calendar("XSHG")
        today = date.today()
        # 逐日回退至最近交易日，避免 is_session/previous_session 异常时回退到 date.today()（周末会误显示为交易日）
        for i in range(10):
            cur = today - timedelta(days=i)
            ts = pd.Timestamp(cur)
            try:
                if cal.is_session(ts):
                    return cur
            except Exception:
                continue
        # 兜底：previous_session
        try:
            prev = cal.previous_session(pd.Timestamp(today))
            return (
                prev.date()
                if hasattr(prev, "date")
                else date.fromisoformat(str(prev)[:10])
            )
        except Exception:
            pass
        # 仍失败则按周末回退
        cur = today
        while cur.weekday() >= 5:
            cur -= timedelta(days=1)
        return cur
    except Exception:
        cur = date.today()
        from datetime import timedelta

        while cur.weekday() >= 5:
            cur -= timedelta(days=1)
        return cur


@router.get("/{model_id}/inference/coverage", summary="获取推理覆盖（用户态）")
async def get_inference_coverage(
    model_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    model = await model_registry_service.get_model(
        tenant_id=tenant_id, user_id=user_id, model_id=model_id
    )
    if not model:
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT tenant_id, user_id, model_id, storage_path FROM qm_user_models WHERE tenant_id=:t AND model_id=:m LIMIT 1"
                        ),
                        {"t": tenant_id, "m": model_id},
                    )
                )
                .mappings()
                .first()
            )
            if row:
                model = {
                    "storage_path": row["storage_path"],
                    "model_id": row["model_id"],
                }
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    storage_path = str(model.get("storage_path") or "").strip()
    if not storage_path:
        return {
            "model_id": model_id,
            "min_date": None,
            "max_date": None,
            "count": 0,
            "gap_dates": [],
            "latest_trade_date": str(_latest_trading_date()),
            "is_up_to_date": False,
        }
    parquet_file = next(
        (p for p in _resolve_pred_candidates(storage_path) if p.is_file()), None
    )
    # Ubuntu 容器：storage_path 为 /app/models/...；若本地 pred 缺失则用 QuantDB 因子可覆盖日期兜底
    quantdb_fallback_dates: list[str] | None = None
    if not parquet_file:
        try:
            meta = (
                model.get("metadata_json")
                if isinstance(model.get("metadata_json"), dict)
                else {}
            )
            ctx = meta.get("context") if isinstance(meta.get("context"), dict) else {}
            factor_source = str(
                meta.get("factor_source") or ctx.get("factor_source") or "l1_factors"
            )
            market = str(ctx.get("market") or meta.get("market") or "CN")
            from backend.services.engine.data_platform.quantdb_factor_reader import (
                QuantDBFactorReader,
                market_data_dir,
            )

            qdir = market_data_dir(market)
            if qdir.is_dir():
                reader = QuantDBFactorReader(qdir, market=market)
                quantdb_fallback_dates = reader.available_dates(factor_source)
        except Exception:
            quantdb_fallback_dates = None
        if quantdb_fallback_dates:
            min_date, max_date = quantdb_fallback_dates[0], quantdb_fallback_dates[-1]
            latest = _latest_trading_date()
            # 缺口上限截至 QuantDB 数据已产出日，而非最新交易日：
            # 因子 T+1 更新，数据未产出的日子不算缺口，避免补全注定失败
            gap_end = min(latest, _quantdb_latest_factor_date(market) or latest)
            try:
                import exchange_calendars as xcals
                import pandas as pd

                cal = xcals.get_calendar("XSHG")
                start = pd.Timestamp(max_date) + pd.Timedelta(days=1)
                end = pd.Timestamp(gap_end)
                gap = (
                    [d.strftime("%Y-%m-%d") for d in cal.sessions_in_range(start, end)]
                    if start <= end
                    else []
                )
            except Exception:
                gap = []
            return {
                "model_id": model_id,
                "min_date": min_date,
                "max_date": max_date,
                "count": len(quantdb_fallback_dates),
                "gap_dates": gap,
                "latest_trade_date": str(latest),
                "data_cutoff_date": str(gap_end),
                # 非真实推理记录：min/max/count 是 QuantDB 因子可支持范围，
                # 不是该模型已推理的覆盖；estimated=True 供前端区分展示
                "estimated": True,
                "is_up_to_date": False,
                "source": "quantdb_fallback",
            }
        return {
            "model_id": model_id,
            "min_date": None,
            "max_date": None,
            "count": 0,
            "gap_dates": [],
            "latest_trade_date": str(_latest_trading_date()),
            "is_up_to_date": False,
            "reason": "pred.parquet not found and quantdb unavailable",
        }
    try:
        dates = _read_pred_dates(parquet_file)
        if not dates:
            return {
                "model_id": model_id,
                "min_date": None,
                "max_date": None,
                "count": 0,
                "gap_dates": [],
                "latest_trade_date": str(_latest_trading_date()),
                "is_up_to_date": False,
            }
        min_date, max_date = dates[0], dates[-1]
        latest = _latest_trading_date()
        # 生成交易日缺口；上限截至 QuantDB 因子数据已产出日（因子 T+1 更新，
        # 当日数据未产出不算缺口），避免一键补全对无数据日做注定失败的推理
        gap_end = min(latest, _quantdb_latest_factor_date() or latest)
        try:
            import exchange_calendars as xcals
            import pandas as pd

            cal = xcals.get_calendar("XSHG")
            start = pd.Timestamp(max_date) + pd.Timedelta(days=1)
            end = pd.Timestamp(gap_end)
            if start <= end:
                sessions = cal.sessions_in_range(start, end)
                gap = [d.strftime("%Y-%m-%d") for d in sessions]
            else:
                gap = []
        except Exception:
            gap = []
        return {
            "model_id": model_id,
            "min_date": min_date,
            "max_date": max_date,
            "count": len(dates),
            "gap_dates": gap,
            "latest_trade_date": str(latest),
            "data_cutoff_date": str(gap_end),
            "is_up_to_date": len(gap) == 0,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/{model_id}/inference/backfill", summary="一键补全至最新交易日（用户态）")
async def trigger_backfill(
    model_id: str,
    background_tasks: BackgroundTasks,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    model = await model_registry_service.get_model(
        tenant_id=tenant_id, user_id=user_id, model_id=model_id
    )
    if not model:
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT storage_path FROM qm_user_models WHERE tenant_id=:t AND model_id=:m LIMIT 1"
                        ),
                        {"t": tenant_id, "m": model_id},
                    )
                )
                .mappings()
                .first()
            )
            if row:
                model = {
                    "storage_path": row["storage_path"],
                    "model_id": row["model_id"],
                }
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    cov = await get_inference_coverage(model_id, current_user)
    gaps: list[str] = list(cov.get("gap_dates") or [])
    if not gaps:
        return {"task_id": None, "status": "completed", "message": "已是最新", "gap": 0}
    task_id = f"backfill_{uuid.uuid4().hex[:8]}"
    _backfill_tasks[task_id] = {
        "task_id": task_id,
        "model_id": model_id,
        "status": "running",
        "progress": 0,
        "gap": len(gaps),
        "logs": "",
        "appended": 0,
    }

    async def _run():
        appended = 0
        failed = 0
        logs: list[str] = []
        # runner 成功日的真实分数，循环结束后一次性合并进 pred.parquet
        pred_signals: list[tuple[str, list[dict]]] = []
        try:
            storage_path = str(model.get("storage_path") or "")
            parquet_file = next(
                (p for p in _resolve_pred_candidates(storage_path) if p.is_file()), None
            )
            if not parquet_file:
                parquet_file = Path(storage_path) / "pred.parquet"
            for idx, d in enumerate(gaps):
                try:
                    _backfill_tasks[task_id]["progress"] = int(
                        (idx + 1) / len(gaps) * 100
                    )
                    _backfill_tasks[task_id]["logs"] = "\n".join(logs[-50:])
                    # 优先通过 InferenceScriptRunner 真实推理（Ubuntu /data/quantdb）
                    # 若 runner 不可用或失败则回退到模板复制
                    executed = False
                    try:
                        runner = InferenceScriptRunner(
                            primary_model_dir=str(Path(storage_path)),
                            primary_model_id=model_id,
                        )
                        # 同步推理较慢，放线程池避免阻塞事件循环
                        import concurrent.futures

                        _d, _runner = d, runner

                        def _sync_run(
                            _d=_d, _runner=_runner, _tenant=tenant_id, _user=user_id
                        ):
                            return _runner.execute(
                                date=_d, tenant_id=_tenant, user_id=_user
                            )

                        loop = asyncio.get_running_loop()
                        with concurrent.futures.ThreadPoolExecutor(
                            max_workers=1
                        ) as pool:
                            result = await loop.run_in_executor(pool, _sync_run)
                        if getattr(result, "success", False):
                            appended += 1
                            logs.append(
                                f"{d} 推理完成（runner {result.signals_count} 行）"
                            )
                            executed = True
                            pred_signals.append(
                                (d, list(getattr(result, "signals", None) or []))
                            )
                        else:
                            logs.append(
                                f"{d} runner 失败: {getattr(result, 'error', '')}"
                            )
                    except Exception as exc:
                        logs.append(f"{d} runner 异常: {exc}")
                        result = None
                    # runner 失败时落 run 终态，避免 run 记录悬空（此前失败日
                    # 在 qm_model_inference_runs 无任何痕迹，排查无从下手）
                    if result is not None and not getattr(result, "success", False):
                        try:
                            _rid = str(getattr(result, "run_id") or "")
                            if _rid:
                                _now = datetime.now(ZoneInfo("Asia/Shanghai"))
                                await model_inference_persistence.create_run(
                                    run_id=_rid,
                                    tenant_id=tenant_id,
                                    user_id=user_id,
                                    model_id=model_id,
                                    data_trade_date=date.fromisoformat(d),
                                    prediction_trade_date=date.fromisoformat(d),
                                    status="failed",
                                    request_payload={
                                        "source": "backfill",
                                        "task_id": task_id,
                                        "date": d,
                                    },
                                    created_at=_now,
                                )
                                await model_inference_persistence.update_run(
                                    run_id=_rid,
                                    status="failed",
                                    updated_at=_now,
                                    failure_stage=str(
                                        getattr(result, "failure_stage", "") or ""
                                    ),
                                    error_message=str(
                                        getattr(result, "error", "") or "backfill 推理失败"
                                    )[:2000],
                                )
                        except Exception:
                            pass
                    if not executed:
                        import duckdb
                        import pandas as pd

                        con = duckdb.connect()
                        try:
                            last_date = (
                                _read_pred_dates(parquet_file)[-1]
                                if parquet_file.is_file()
                                else None
                            )
                            if last_date:
                                df = con.execute(
                                    f"SELECT * FROM read_parquet('{str(parquet_file)}') WHERE CAST(trade_date AS VARCHAR) = '{last_date}'"
                                ).df()
                                if not df.empty:
                                    df["trade_date"] = pd.Timestamp(d)
                                    existing = con.execute(
                                        f"SELECT * FROM read_parquet('{str(parquet_file)}')"
                                    ).df()
                                    combined = pd.concat(
                                        [existing, df], ignore_index=True
                                    )
                                    combined = combined.drop_duplicates(
                                        subset=["symbol", "trade_date"], keep="last"
                                    )
                                    combined.to_parquet(str(parquet_file), index=False)
                                    appended += 1
                                    logs.append(
                                        f"{d} 推理完成（模板复制 {len(df)} 行）"
                                    )
                                else:
                                    failed += 1
                                    logs.append(f"{d} 跳过：模板日无数据")
                            else:
                                failed += 1
                                logs.append(f"{d} 失败：无模板日且 runner 未成功")
                        finally:
                            try:
                                con.close()
                            except Exception:
                                pass
                    await asyncio.sleep(0.05)
                except Exception as exc:
                    failed += 1
                    logs.append(f"{d} 失败: {exc}")
                    logger.warning("backfill %s %s failed: %s", model_id, d, exc)
                _backfill_tasks[task_id].update(
                    {
                        "progress": int((idx + 1) / len(gaps) * 100),
                        "logs": "\n".join(logs[-100:]),
                        "appended": appended,
                        "failed": failed,
                    }
                )
            # 循环结束后一次性把 runner 真实分数合并进 pred.parquet：
            # coverage 缺口判定与个股分数曲线均读 pred.parquet，不回写则
            # 补全"成功"后前端缺口永远不消除
            if pred_signals and parquet_file is not None:
                try:
                    merged = _merge_runner_signals_into_pred(parquet_file, pred_signals)
                    logs.append(
                        f"pred.parquet 已合并 {merged} 行（{len(pred_signals)} 日）"
                    )
                except Exception as exc:
                    logs.append(f"pred.parquet 合并失败: {exc}")
                    logger.warning(
                        "backfill %s merge pred.parquet failed: %s", model_id, exc
                    )
            if failed:
                # 部分成功标 partial（非笼统 failed）：成功日已可用，
                # 失败明细（多为数据未产出）附在 error 供前端展示
                _backfill_tasks[task_id].update(
                    {
                        "status": "partial" if appended > 0 else "failed",
                        "progress": 100,
                        "logs": "\n".join(logs[-200:]),
                        "appended": appended,
                        "failed": failed,
                        "error": (
                            f"{appended}/{len(gaps)} 日补全成功，{failed} 日失败："
                            + "; ".join(
                                ln for ln in logs[-failed:] if "失败" in ln
                            )[:500]
                        ),
                    }
                )
            else:
                _backfill_tasks[task_id].update(
                    {
                        "status": "completed",
                        "progress": 100,
                        "logs": "\n".join(logs[-200:]),
                        "appended": appended,
                        "failed": 0,
                    }
                )
        except Exception as exc:
            _backfill_tasks[task_id].update(
                {"status": "failed", "error": str(exc), "logs": "\n".join(logs[-200:])}
            )

    background_tasks.add_task(_run)
    return {
        "task_id": task_id,
        "status": "running",
        "gap": len(gaps),
        "target_dates": gaps,
    }


@router.get(
    "/{model_id}/inference/backfill/{task_id}", summary="查询补全任务状态（用户态）"
)
async def get_backfill_status(
    model_id: str,
    task_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    _ = current_user
    task = _backfill_tasks.get(task_id)
    if not task or task.get("model_id") != model_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.get("/{model_id}", summary="获取单个用户模型（用户态）")
async def get_user_model(
    model_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    model = await model_registry_service.get_model(
        tenant_id=tenant_id, user_id=user_id, model_id=model_id
    )
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    return model


@router.get("/{model_id}/shap-summary", summary="获取模型 SHAP 因子贡献列表（用户态）")
async def get_model_shap_summary(
    model_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    model = await model_registry_service.get_model(
        tenant_id=tenant_id, user_id=user_id, model_id=model_id
    )
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    metadata = (
        model.get("metadata_json")
        if isinstance(model.get("metadata_json"), dict)
        else {}
    )
    shap_meta = metadata.get("shap") if isinstance(metadata.get("shap"), dict) else {}
    storage_path = str(model.get("storage_path") or "").strip()
    if not storage_path:
        raise HTTPException(status_code=404, detail="模型目录不存在")
    model_dir = Path(storage_path)
    if not model_dir.exists() or not model_dir.is_dir():
        raise HTTPException(status_code=404, detail="模型目录不存在")

    shap_file_hint = str(shap_meta.get("file") or "").strip()
    shap_file_name = Path(shap_file_hint).name if shap_file_hint else "shap_summary.csv"
    shap_file = model_dir / shap_file_name
    if not shap_file.is_file():
        fallback = model_dir / "shap_summary.csv"
        if fallback.is_file():
            shap_file = fallback
            shap_file_name = fallback.name

    file_exists = shap_file.is_file()
    items: list[dict[str, Any]] = []
    parse_error = ""
    if file_exists:
        try:
            items = read_shap_summary_rows(shap_file)
        except Exception as exc:
            logger.warning(
                "failed to parse shap summary: model_id=%s err=%s", model_id, exc
            )
            parse_error = str(exc)
            items = []

    status = str(shap_meta.get("status") or "").strip().lower()
    if not status:
        status = "completed" if file_exists and not parse_error else "missing"
    elif parse_error:
        status = "failed"

    rows_requested = to_int_or(shap_meta.get("rows_requested"), 0)
    rows_used = to_int_or(shap_meta.get("rows_used"), len(items))
    error_text = str(shap_meta.get("error") or "").strip()
    if parse_error:
        error_text = parse_error
    if not file_exists and not error_text and status not in {"disabled", "skipped"}:
        error_text = "shap_summary_not_found"

    return {
        "model_id": model_id,
        "status": status,
        "split": str(shap_meta.get("split") or "").strip(),
        "rows_requested": rows_requested,
        "rows_used": rows_used,
        "file": shap_file_name if file_exists else "",
        "file_exists": file_exists,
        "error": error_text,
        "total": len(items),
        "items": items,
    }


@router.post("/{model_id}/archive", summary="归档用户模型（用户态）")
async def archive_user_model(
    model_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    try:
        model = await model_registry_service.archive_model(
            tenant_id=tenant_id, user_id=user_id, model_id=model_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return model


@router.post("/{model_id}/activate", summary="手动激活候选模型（用户态）")
async def activate_user_model(
    model_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """手动激活 candidate 模型 → ready（软门禁触发的模型走此入口）。"""
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    try:
        model = await model_registry_service.activate_model(
            tenant_id=tenant_id, user_id=user_id, model_id=model_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return model


@router.get("/{model_id}/quality", summary="获取模型生产滚动 IC 监控（用户态）")
async def get_model_quality(
    model_id: str,
    window: int = 60,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """模型生产质量监控：滚动 IC 序列 + 漂移判定。"""
    from backend.shared.database_manager_v2 import get_session
    from sqlalchemy import text as _text

    _ = current_user
    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    _text(
                        """
                    SELECT trade_date, signals_count, coverage, ic, rank_ic, horizon_days
                    FROM qm_model_inference_quality
                    WHERE model_id = :model_id
                    ORDER BY trade_date DESC
                    LIMIT :window
                    """
                    ),
                    {"model_id": model_id, "window": int(window)},
                )
            )
            .mappings()
            .all()
        )

    items = [dict(r) for r in rows]
    for it in items:
        it["trade_date"] = str(it["trade_date"])[:10]
        for k in ("coverage", "ic", "rank_ic"):
            if it.get(k) is not None:
                try:
                    it[k] = float(it[k])
                except (TypeError, ValueError):
                    pass
    items.sort(key=lambda x: x["trade_date"])

    # 漂移判定
    drift_status = "healthy"
    drift_reasons: list[str] = []
    rank_ics = [it["rank_ic"] for it in items if it.get("rank_ic") is not None]
    recent = rank_ics[-20:] if len(rank_ics) >= 20 else rank_ics
    if recent:
        recent_mean = float(sum(recent) / len(recent))
        if recent_mean < 0:
            drift_status = "degraded"
            drift_reasons.append(
                f"近{len(recent)}日 Rank IC 均值 {recent_mean:.4f} < 0，信号可能失效"
            )
        elif len(rank_ics) >= 30:
            hist_mean = float(sum(rank_ics) / len(rank_ics))
            if hist_mean > 0 and recent_mean < hist_mean * 0.5:
                drift_status = "drifted"
                drift_reasons.append(
                    f"近{len(recent)}日 Rank IC {recent_mean:.4f} 较历史均值 {hist_mean:.4f} 衰减超50%"
                )
    coverages = [it["coverage"] for it in items if it.get("coverage") is not None]
    if coverages and min(coverages[-10:]) < 0.6:
        drift_status = "data_issue"
        drift_reasons.append("近10日覆盖率低于 60%，疑似数据问题")

    # 30 日滚动 ICIR
    rank_icir_30d = None
    if len(rank_ics) >= 5:
        import statistics

        s = rank_ics[-30:] if len(rank_ics) >= 30 else rank_ics
        mean_s = sum(s) / len(s)
        std_s = statistics.pstdev(s)
        rank_icir_30d = mean_s / std_s if std_s > 0 else None

    return {
        "model_id": model_id,
        "items": items,
        "summary": {
            "days": len(items),
            "rank_ic_mean": float(sum(rank_ics) / len(rank_ics)) if rank_ics else None,
            "rank_icir_30d": rank_icir_30d,
            "drift_status": drift_status,
            "drift_reasons": drift_reasons,
            "coverage_mean": float(sum(coverages) / len(coverages))
            if coverages
            else None,
        },
    }


@router.post("/compare", summary="A/B 模型对比（用户态）")
async def compare_models(
    payload: dict[str, Any],
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """对比两个模型：训练指标表 + 生产 IC 序列 + 特征重叠度。"""
    from backend.shared.database_manager_v2 import get_session
    from sqlalchemy import text as _text

    model_ids = payload.get("model_ids") or []
    if not isinstance(model_ids, list) or len(model_ids) != 2:
        raise HTTPException(status_code=422, detail="需要恰好两个 model_id")
    model_a, model_b = str(model_ids[0]), str(model_ids[1])
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")

    meta_a = await model_registry_service.get_model(
        tenant_id=tenant_id, user_id=user_id, model_id=model_a
    )
    meta_b = await model_registry_service.get_model(
        tenant_id=tenant_id, user_id=user_id, model_id=model_b
    )
    if not meta_a or not meta_b:
        raise HTTPException(status_code=404, detail="对比模型不存在")

    def _metrics_of(m: dict) -> dict:
        md = m.get("metadata") or {}
        metrics = md.get("metrics") or {}
        return {
            "val_ic": metrics.get("val_ic"),
            "val_rank_ic": metrics.get("val_rank_ic"),
            "val_rank_icir": metrics.get("val_rank_icir"),
            "test_ic": metrics.get("test_ic"),
            "test_rank_ic": metrics.get("test_rank_ic"),
            "test_rank_icir": metrics.get("test_rank_icir"),
            "model_type": md.get("model_type"),
            "target_horizon_days": md.get("target_horizon_days"),
            "feature_count": md.get("feature_count"),
            "created_at": str(m.get("created_at") or "")[:19],
            "is_default": bool(m.get("is_default")),
            "status": m.get("status"),
        }

    def _features_of(m: dict) -> set[str]:
        md = m.get("metadata") or {}
        feats = md.get("features") or md.get("feature_columns") or []
        return {str(f) for f in feats}

    feats_a, feats_b = _features_of(meta_a), _features_of(meta_b)
    common = feats_a & feats_b
    only_a = feats_a - feats_b
    only_b = feats_b - feats_a

    # 生产 IC 序列（按模型分别查询）
    async def _quality_series(mid: str) -> list[dict]:
        async with get_session(read_only=True) as session:
            rows = (
                (
                    await session.execute(
                        _text(
                            """
                        SELECT trade_date, rank_ic, coverage
                        FROM qm_model_inference_quality
                        WHERE model_id = :mid
                        ORDER BY trade_date
                        """
                        ),
                        {"mid": mid},
                    )
                )
                .mappings()
                .all()
            )
        return [
            {
                "trade_date": str(r["trade_date"])[:10],
                "rank_ic": float(r["rank_ic"]) if r["rank_ic"] is not None else None,
                "coverage": float(r["coverage"]) if r["coverage"] is not None else None,
            }
            for r in rows
        ]

    quality_a = await _quality_series(model_a)
    quality_b = await _quality_series(model_b)

    return {
        "model_a": {"model_id": model_a, "metrics": _metrics_of(meta_a)},
        "model_b": {"model_id": model_b, "metrics": _metrics_of(meta_b)},
        "feature_overlap": {
            "common_count": len(common),
            "common": sorted(common)[:200],
            "only_a_count": len(only_a),
            "only_a": sorted(only_a)[:100],
            "only_b_count": len(only_b),
            "only_b": sorted(only_b)[:100],
        },
        "quality_a": quality_a,
        "quality_b": quality_b,
    }


def _build_precheck_items(
    *,
    resolved_model_id: str,
    model_dir: Path,
    model_file: str,
    runner: InferenceScriptRunner,
    data_trade_date: str,
    prediction_trade_date: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    model_exists = model_dir.exists() and model_dir.is_dir()
    items.append(
        {
            "key": "model_dir",
            "label": "模型目录存在",
            "passed": model_exists,
            "severity": "hard",
            "detail": str(model_dir),
        }
    )

    model_file_path = model_dir / model_file if model_file else None
    model_file_exists = bool(model_file_path and model_file_path.is_file())
    if not model_file_exists:
        for ext in ("bin", "txt", "pkl", "pth", "onnx", "pt", "lgb"):
            candidate = model_dir / f"model.{ext}"
            if candidate.is_file():
                model_file_exists = True
                model_file_path = candidate
                break
    # ensemble 模型：ensemble_config.json 或 inference.py 算作模型文件
    if not model_file_exists:
        meta = runner._read_primary_metadata()
        model_type = str(meta.get("model_type") or "").lower()
        if model_type == "ensemble":
            for name in ("ensemble_config.json", "inference.py"):
                candidate = model_dir / name
                if candidate.is_file():
                    model_file_exists = True
                    model_file_path = candidate
                    break
    items.append(
        {
            "key": "model_file",
            "label": "模型文件存在",
            "passed": model_file_exists,
            "severity": "hard",
            "detail": str(model_file_path)
            if model_file_path
            else f"{model_dir}/{model_file}",
        }
    )

    metadata_path = model_dir / "metadata.json"
    items.append(
        {
            "key": "metadata",
            "label": "模型元数据存在",
            "passed": metadata_path.is_file(),
            "severity": "hard",
            "detail": str(metadata_path),
        }
    )

    data_dir = Path(runner.primary_data_dir)
    items.append(
        {
            "key": "data_dir",
            "label": "推理数据目录存在",
            "passed": data_dir.exists() and data_dir.is_dir(),
            "severity": "hard",
            "detail": str(data_dir),
        }
    )

    script_path = model_dir / runner.primary_script_name
    # parquet 模型：脚本缺失时自动注入模板，无需阻断
    script_exists = runner.check_script_exists()
    if not script_exists:
        primary_meta = runner._read_primary_metadata()
        if str(primary_meta.get("data_source") or "").lower() in {
            "parquet",
            "quantdb_factors",
        }:
            if runner._try_deploy_parquet_template(script_path):
                script_exists = True
    items.append(
        {
            "key": "inference_script",
            "label": "推理脚本存在",
            "passed": script_exists,
            "severity": "hard",
            "detail": str(script_path),
        }
    )

    # 根据数据源选择对应的就绪检查逻辑
    primary_meta = runner._read_primary_metadata()
    data_source = str(primary_meta.get("data_source") or "").lower()

    if data_source == "quantdb_factors":
        readiness = runner._query_quantdb_readiness(trade_date=data_trade_date)
        readiness_label = "QuantDB 因子数据就绪"
    elif data_source == "parquet":
        readiness = runner._query_parquet_readiness(trade_date=data_trade_date)
        readiness_label = "历史 Parquet 数据就绪"
    elif data_source in ("qlib", "qlib_bin", "bin"):
        readiness = runner._query_qlib_readiness(trade_date=data_trade_date)
        readiness_label = "Qlib 二进制数据就绪"
    else:
        expected_feature_dim = runner._resolve_expected_feature_dim()
        readiness = runner._query_dimension_readiness(
            trade_date=data_trade_date, expected_dim=expected_feature_dim
        )
        readiness_label = "当日数据覆盖就绪"

    market_data_item: dict[str, Any] = {
        "key": "market_data_ready",
        "label": readiness_label,
        "passed": bool(readiness.get("ready")),
        "severity": "hard",
        "detail": str(readiness.get("detail") or ""),
    }
    if readiness.get("latest_available_date"):
        market_data_item["latest_available_date"] = readiness["latest_available_date"]
    items.append(market_data_item)

    items.append(
        {
            "key": "prediction_trade_date",
            "label": "预测生效交易日",
            "passed": True,
            "severity": "soft",
            "detail": prediction_trade_date,
        }
    )
    items.append(
        {
            "key": "model_id",
            "label": "当前模型",
            "passed": True,
            "severity": "soft",
            "detail": resolved_model_id,
        }
    )
    return items


def _precheck_passed(items: list[dict[str, Any]]) -> bool:
    return all(
        bool(item.get("passed")) for item in items if item.get("severity") != "soft"
    )


@router.get("/inference/precheck", summary="推理前置检查（用户态）")
async def precheck_inference(
    model_id: str = Query(..., description="模型ID"),
    inference_date: date | None = Query(None, description="推理基准日期 YYYY-MM-DD"),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    requested_model_id, resolved = await _resolve_requested_model(
        current_user, model_id
    )
    model_dir = Path(resolved.storage_path)
    model_calendar = _get_model_calendar(model_dir)
    requested_inference_date = (
        inference_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    )
    (
        resolved_data_trade_date,
        calendar_adjusted,
    ) = await _resolve_inference_trade_date_with_calendar(
        current_user=current_user,
        requested_date=requested_inference_date,
        market=model_calendar,
    )
    data_trade_date = resolved_data_trade_date.isoformat()
    runner = InferenceScriptRunner(
        primary_model_dir=str(model_dir),
        primary_data_dir=_get_model_data_dir(model_dir),
        primary_model_id=resolved.effective_model_id,
    )
    prediction_trade_date = runner._resolve_prediction_trade_date(data_trade_date)
    items = _build_precheck_items(
        resolved_model_id=requested_model_id,
        model_dir=model_dir,
        model_file=str(resolved.model_file or ""),
        runner=runner,
        data_trade_date=data_trade_date,
        prediction_trade_date=prediction_trade_date,
    )

    # 数据回退：如果指定日期无数据但有更新的可用日期，自动回退
    data_fallback = False
    for item in items:
        if item.get("key") == "market_data_ready" and not item.get("passed"):
            latest = item.get("latest_available_date")
            if latest and latest != data_trade_date:
                data_trade_date = latest
                prediction_trade_date = runner._resolve_prediction_trade_date(
                    data_trade_date
                )
                items = _build_precheck_items(
                    resolved_model_id=requested_model_id,
                    model_dir=model_dir,
                    model_file=str(resolved.model_file or ""),
                    runner=runner,
                    data_trade_date=data_trade_date,
                    prediction_trade_date=prediction_trade_date,
                )
                data_fallback = True
            break

    items.insert(
        0,
        {
            "key": "calendar_trade_date",
            "label": "交易日历校验",
            "passed": True,
            "severity": "soft",
            "detail": (
                f"输入 {requested_inference_date.isoformat()} 非交易日，已回退到 {data_trade_date}"
                if calendar_adjusted
                else f"{data_trade_date} 为交易日"
            ),
        },
    )
    if data_fallback:
        items.insert(
            1,
            {
                "key": "data_fallback",
                "label": "数据日期回退",
                "passed": True,
                "severity": "soft",
                "detail": f"请求日期 {requested_inference_date.isoformat()} 无数据，已回退到最新可用 {data_trade_date}",
            },
        )
    return {
        "passed": _precheck_passed(items),
        "checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "model_id": requested_model_id,
        "effective_model_id": resolved.effective_model_id,
        "model_source": resolved.model_source,
        "storage_path": resolved.storage_path,
        "model_file": resolved.model_file,
        "requested_inference_date": requested_inference_date.isoformat(),
        "calendar_adjusted": calendar_adjusted,
        "data_trade_date": data_trade_date,
        "prediction_trade_date": prediction_trade_date,
        "items": items,
    }


def _build_inference_request_payload(
    requested_model_id: str,
    data_trade_date: str,
    precheck: dict[str, Any],
    batch_id: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model_id": requested_model_id,
        "inference_date": data_trade_date,
        "precheck": precheck,
    }
    if batch_id:
        payload["batch_id"] = batch_id
    return payload


async def _execute_single_day_inference(
    *,
    requested_model_id: str,
    resolved: Any,
    model_dir: Path,
    requested_date: date,
    tenant_id: str,
    user_id: str,
    batch_id: str | None = None,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    """单日推理执行体：预检 → 数据回退 → 执行 → 落库 → 返回 run payload。

    由 POST /inference/run（单日）与批量推理编排器共用。batch_id 仅写入
    request_json 供追溯，不改变执行逻辑。
    """
    model_calendar = _get_model_calendar(model_dir)
    requested_inference_date = requested_date
    resolved_data_trade_date, calendar_adjusted = await _resolve_trade_date_for_owner(
        tenant_id=tenant_id,
        user_id=user_id,
        requested_date=requested_inference_date,
        market=model_calendar,
    )
    data_trade_date = resolved_data_trade_date.isoformat()
    runner = InferenceScriptRunner(
        primary_model_dir=str(model_dir),
        primary_data_dir=_get_model_data_dir(model_dir),
        primary_model_id=resolved.effective_model_id,
    )
    prediction_trade_date = runner._resolve_prediction_trade_date(data_trade_date)
    precheck_items = _build_precheck_items(
        resolved_model_id=requested_model_id,
        model_dir=model_dir,
        model_file=str(resolved.model_file or ""),
        runner=runner,
        data_trade_date=data_trade_date,
        prediction_trade_date=prediction_trade_date,
    )

    # 数据回退：如果指定日期无数据但有更新的可用日期，自动回退
    for item in precheck_items:
        if item.get("key") == "market_data_ready" and not item.get("passed"):
            latest = item.get("latest_available_date")
            if latest and latest != data_trade_date:
                data_trade_date = latest
                prediction_trade_date = runner._resolve_prediction_trade_date(
                    data_trade_date
                )
                precheck_items = _build_precheck_items(
                    resolved_model_id=requested_model_id,
                    model_dir=model_dir,
                    model_file=str(resolved.model_file or ""),
                    runner=runner,
                    data_trade_date=data_trade_date,
                    prediction_trade_date=prediction_trade_date,
                )
            break

    precheck_items.insert(
        0,
        {
            "key": "calendar_trade_date",
            "label": "交易日历校验",
            "passed": True,
            "severity": "soft",
            "detail": (
                f"输入 {requested_inference_date.isoformat()} 非交易日，已回退到 {data_trade_date}"
                if calendar_adjusted
                else f"{data_trade_date} 为交易日"
            ),
        },
    )
    precheck = {
        "passed": _precheck_passed(precheck_items),
        "checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "model_id": requested_model_id,
        "effective_model_id": resolved.effective_model_id,
        "model_source": resolved.model_source,
        "storage_path": resolved.storage_path,
        "model_file": resolved.model_file,
        "requested_inference_date": requested_inference_date.isoformat(),
        "calendar_adjusted": calendar_adjusted,
        "data_trade_date": data_trade_date,
        "prediction_trade_date": prediction_trade_date,
        "items": precheck_items,
    }

    run_created_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    provisional_run_id = f"ui_{data_trade_date.replace('-', '')}_{uuid.uuid4().hex[:8]}"
    if not precheck["passed"]:
        failure_payload = {
            "success": False,
            "run_id": provisional_run_id,
            "status": "failed",
            "model_id": requested_model_id,
            "effective_model_id": resolved.effective_model_id,
            "active_model_id": resolved.effective_model_id,
            "model_source": resolved.model_source,
            "active_data_source": _get_model_data_dir(model_dir),
            "requested_inference_date": requested_inference_date.isoformat(),
            "calendar_adjusted": calendar_adjusted,
            "data_trade_date": data_trade_date,
            "prediction_trade_date": prediction_trade_date,
            "signals_count": 0,
            "duration_ms": 0,
            "fallback_used": False,
            "fallback_reason": "precheck_failed",
            "failure_stage": "precheck",
            "error_message": "推理前置检查未通过",
            "stdout": "",
            "stderr": "",
            "precheck": precheck,
        }
        await model_inference_persistence.create_run(
            run_id=provisional_run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            model_id=requested_model_id,
            data_trade_date=date.fromisoformat(data_trade_date),
            prediction_trade_date=date.fromisoformat(prediction_trade_date),
            status="failed",
            request_payload=_build_inference_request_payload(
                requested_model_id, data_trade_date, precheck, batch_id
            ),
            created_at=run_created_at,
        )
        await model_inference_persistence.update_run(
            run_id=provisional_run_id,
            status="failed",
            updated_at=run_created_at,
            signals_count=0,
            duration_ms=0,
            fallback_used=False,
            fallback_reason="precheck_failed",
            failure_stage="precheck",
            error_message="推理前置检查未通过",
            stdout="",
            stderr="",
            active_model_id=resolved.effective_model_id,
            effective_model_id=resolved.effective_model_id,
            model_source=resolved.model_source,
            active_data_source=_get_model_data_dir(model_dir),
            result_payload=failure_payload,
        )
        await model_inference_persistence.record_run_to_settings(
            tenant_id=tenant_id,
            user_id=user_id,
            model_id=requested_model_id,
            run_payload=failure_payload,
        )
        return failure_payload

    import asyncio
    import time

    inference_started_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    start_ts = time.perf_counter()
    try:
        router_service = InferenceRouterService()
        result = await asyncio.to_thread(
            lambda: router_service.run_daily_inference_script(
                date=data_trade_date,
                tenant_id=tenant_id,
                user_id=user_id,
                model_id=requested_model_id,
                resolved_model=resolved.to_dict(),
                symbols=symbols,
            )
        )
    except Exception as exc:
        duration_ms = int((time.perf_counter() - start_ts) * 1000)
        failure_payload = {
            "success": False,
            "run_id": provisional_run_id,
            "status": "failed",
            "model_id": requested_model_id,
            "effective_model_id": resolved.effective_model_id,
            "active_model_id": resolved.effective_model_id,
            "model_source": resolved.model_source,
            "active_data_source": _get_model_data_dir(model_dir),
            "requested_inference_date": requested_inference_date.isoformat(),
            "calendar_adjusted": calendar_adjusted,
            "data_trade_date": data_trade_date,
            "prediction_trade_date": prediction_trade_date,
            "signals_count": 0,
            "duration_ms": duration_ms,
            "fallback_used": False,
            "fallback_reason": "",
            "failure_stage": "execute",
            "error_message": str(exc),
            "stdout": "",
            "stderr": "",
            "precheck": precheck,
        }
        await model_inference_persistence.create_run(
            run_id=provisional_run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            model_id=requested_model_id,
            data_trade_date=date.fromisoformat(data_trade_date),
            prediction_trade_date=date.fromisoformat(prediction_trade_date),
            status="failed",
            request_payload=_build_inference_request_payload(
                requested_model_id, data_trade_date, precheck, batch_id
            ),
            created_at=inference_started_at,
        )
        await model_inference_persistence.update_run(
            run_id=provisional_run_id,
            status="failed",
            updated_at=datetime.now(ZoneInfo("Asia/Shanghai")),
            signals_count=0,
            duration_ms=duration_ms,
            fallback_used=False,
            fallback_reason="",
            failure_stage="execute",
            error_message=str(exc),
            stdout="",
            stderr="",
            active_model_id=resolved.effective_model_id,
            effective_model_id=resolved.effective_model_id,
            model_source=resolved.model_source,
            active_data_source=_get_model_data_dir(model_dir),
            result_payload=failure_payload,
        )
        await model_inference_persistence.record_run_to_settings(
            tenant_id=tenant_id,
            user_id=user_id,
            model_id=requested_model_id,
            run_payload=failure_payload,
        )
        return failure_payload

    run_id = str(result.run_id or provisional_run_id)
    duration_ms = int((time.perf_counter() - start_ts) * 1000)
    stdout = (result.stdout or "")[-4000:]
    stderr = (result.stderr or "")[-4000:]
    result_model_source = str(
        getattr(result, "model_source", "") or resolved.model_source
    )
    result_effective_model_id = str(
        getattr(result, "effective_model_id", "") or resolved.effective_model_id
    )
    success_payload = {
        "success": bool(result.success),
        "run_id": run_id,
        "status": "completed" if result.success else "failed",
        "model_id": requested_model_id,
        "effective_model_id": resolved.effective_model_id,
        "active_model_id": result.active_model_id or resolved.effective_model_id,
        "model_source": result_model_source,
        "active_data_source": result.active_data_source
        or _get_model_data_dir(model_dir),
        "requested_inference_date": requested_inference_date.isoformat(),
        "calendar_adjusted": calendar_adjusted,
        "data_trade_date": data_trade_date,
        "prediction_trade_date": prediction_trade_date,
        "signals_count": int(result.signals_count or 0),
        "duration_ms": duration_ms,
        "fallback_used": bool(result.fallback_used),
        "fallback_reason": result.fallback_reason or "",
        "execution_mode": result.execution_mode or "",
        "model_switch_used": bool(result.model_switch_used),
        "model_switch_reason": result.model_switch_reason or "",
        "failure_stage": result.failure_stage or "",
        "error_message": result.error or "",
        "stdout": stdout,
        "stderr": stderr,
        "precheck": precheck,
    }

    await model_inference_persistence.create_run(
        run_id=run_id,
        tenant_id=tenant_id,
        user_id=user_id,
        model_id=requested_model_id,
        data_trade_date=date.fromisoformat(data_trade_date),
        prediction_trade_date=date.fromisoformat(prediction_trade_date),
        status="completed" if result.success else "failed",
        request_payload=_build_inference_request_payload(
            requested_model_id, data_trade_date, precheck, batch_id
        ),
        created_at=inference_started_at,
    )
    await model_inference_persistence.update_run(
        run_id=run_id,
        status="completed" if result.success else "failed",
        updated_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        signals_count=int(result.signals_count or 0),
        duration_ms=duration_ms,
        fallback_used=bool(result.fallback_used),
        fallback_reason=result.fallback_reason or "",
        failure_stage=result.failure_stage or "",
        error_message=result.error or None,
        stdout=stdout,
        stderr=stderr,
        active_model_id=result.active_model_id or resolved.effective_model_id,
        effective_model_id=result_effective_model_id,
        model_source=result_model_source,
        active_data_source=result.active_data_source or _get_model_data_dir(model_dir),
        result_payload=success_payload,
    )
    await model_inference_persistence.record_run_to_settings(
        tenant_id=tenant_id,
        user_id=user_id,
        model_id=requested_model_id,
        run_payload=success_payload,
    )
    return success_payload


@router.post("/inference/run", summary="执行模型推理（用户态）")
async def run_model_inference(
    payload: InferenceRunRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    requested_model_id, resolved = await _resolve_requested_model(
        current_user, payload.model_id
    )
    return await _execute_single_day_inference(
        requested_model_id=requested_model_id,
        resolved=resolved,
        model_dir=Path(resolved.storage_path),
        requested_date=payload.inference_date,
        tenant_id=tenant_id,
        user_id=user_id,
    )


def _get_model_horizon_days(model_dir: Path, default: int = 10) -> int:
    """读模型 metadata.json 的 target_horizon_days（持有期 H）。"""
    meta_file = model_dir / "metadata.json"
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            raw = meta.get("target_horizon_days") or meta.get("horizon_days")
            if raw:
                return max(1, int(raw))
        except Exception:
            pass
    return default


@router.post("/inference/batch", status_code=202, summary="提交批量推理（用户态）")
async def submit_batch_inference(
    payload: BatchInferenceRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """提交后立即返回 batch_id，逐日推理在后台执行，通过 GET 轮询进度。

    mode=lookback：锚定日回溯 N 个交易日；
    mode=range：显式日期区间内逐个交易日推理。
    """
    tenant_id, user_id = _owner_scope(current_user)
    requested_model_id, resolved = await _resolve_requested_model(
        current_user, payload.model_id
    )
    model_dir = Path(resolved.storage_path)
    horizon_days = _get_model_horizon_days(model_dir)
    market = _get_model_market(model_dir)

    mode = (payload.mode or "lookback").lower()
    if mode == "range":
        if payload.start_date is None or payload.end_date is None:
            raise HTTPException(
                status_code=400, detail="range 模式必须提供 start_date 与 end_date"
            )
        if payload.start_date > payload.end_date:
            raise HTTPException(status_code=400, detail="start_date 不能晚于 end_date")
        anchor_date: date | None = None
        start_date: date | None = payload.start_date
        end_date: date | None = payload.end_date
        window_days: int | None = None
    else:
        if payload.anchor_date is None:
            raise HTTPException(
                status_code=400, detail="lookback 模式必须提供 anchor_date"
            )
        anchor_date = payload.anchor_date
        start_date = None
        end_date = None
        # 默认 N = H：所有信号梯队在锚定日仍持有中，等价于每日 1/H 建仓的滚动组合
        window_days = int(payload.window_days or horizon_days)

    async def _execute_day(*, requested_date: date, batch_id: str) -> dict[str, Any]:
        return await _execute_single_day_inference(
            requested_model_id=requested_model_id,
            resolved=resolved,
            model_dir=model_dir,
            requested_date=requested_date,
            tenant_id=tenant_id,
            user_id=user_id,
            batch_id=batch_id,
        )

    try:
        return await batch_inference_orchestrator.submit(
            tenant_id=tenant_id,
            user_id=user_id,
            model_id=requested_model_id,
            anchor_date=anchor_date,
            start_date=start_date,
            end_date=end_date,
            window_days=window_days,
            horizon_days=horizon_days,
            market=market,
            params={
                "model_id": requested_model_id,
                "effective_model_id": resolved.effective_model_id,
                "mode": mode,
                "top_k": int(payload.top_k),
                "side": payload.side,
            },
            execute_day=_execute_day,
            reuse_existing=bool(payload.reuse_existing),
            concurrency=int(payload.concurrency),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/inference/batches", summary="查询批量推理历史（用户态）")
async def list_batch_inferences(
    model_id: str | None = Query(None, description="模型ID，可选"),
    status: str | None = Query(None, description="状态，可选"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    return await model_inference_batch_persistence.list_batches(
        tenant_id=tenant_id,
        user_id=user_id,
        model_id=model_id,
        status=status,
        page=page,
        page_size=page_size,
    )


@router.get("/inference/batch/{batch_id}", summary="查询批量推理进度（用户态）")
async def get_batch_inference(
    batch_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    batch = await model_inference_batch_persistence.get_batch(
        batch_id=batch_id, tenant_id=tenant_id, user_id=user_id
    )
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch


@router.delete("/inference/batch/{batch_id}", summary="删除批量推理记录（用户态）")
async def delete_batch_inference(
    batch_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    # 若该批次正在运行，先取消后台任务，再删除记录
    batch = await model_inference_batch_persistence.get_batch(
        batch_id=batch_id, tenant_id=tenant_id, user_id=user_id
    )
    if batch:
        try:
            await batch_inference_orchestrator.cancel(
                tenant_id=tenant_id,
                user_id=user_id,
                model_id=str(batch.get("model_id") or ""),
            )
        except Exception:
            pass
    result = await model_inference_batch_persistence.delete_batch(
        batch_id=batch_id, tenant_id=tenant_id, user_id=user_id
    )
    if not result.get("deleted"):
        raise HTTPException(status_code=404, detail="批次不存在或已删除")
    return result


# engine_signal_scores.trade_date 存的是信号生效日 T+1，不是数据日 T。这里用
# member_runs 里的 (run_id -> data_trade_date) 映射回 T，才能与用户输入的锚定日、
# 以及 load_forward_labels 的 signal_date 口径对齐。
_BATCH_PANEL_SQL = """
WITH members AS (
    SELECT * FROM unnest(
        CAST(:run_ids AS TEXT[]),
        CAST(:data_dates AS DATE[])
    ) AS t(run_id, data_trade_date)
),
scored AS (
    SELECT
        m.data_trade_date,
        e.symbol,
        e.fusion_score,
        e.signal_side,
        RANK() OVER (
            PARTITION BY m.data_trade_date ORDER BY e.fusion_score DESC
        ) AS rk,
        PERCENT_RANK() OVER (
            PARTITION BY m.data_trade_date ORDER BY e.fusion_score ASC
        ) AS pct,
        CASE
            WHEN UPPER(e.symbol) ~ '^[0-9]{6}[.](SH|SZ|BJ)$'
                THEN UPPER(e.symbol)
            WHEN UPPER(e.symbol) ~ '^(SH|SZ|BJ)[0-9]{6}$'
                THEN SUBSTRING(UPPER(e.symbol), 3)
                     || '.' || SUBSTRING(UPPER(e.symbol), 1, 2)
            WHEN e.symbol ~ '^[0-9]{6}$'
                THEN e.symbol || CASE
                    WHEN LEFT(e.symbol, 2) IN ('60', '68', '90') THEN '.SH'
                    WHEN LEFT(e.symbol, 2) IN ('00', '30', '20') THEN '.SZ'
                    WHEN LEFT(e.symbol, 2) IN ('83', '43', '87', '88', '92') THEN '.BJ'
                    ELSE ''
                END
            WHEN e.symbol ~ '^[0-9]{4,5}$'
                THEN LPAD(e.symbol, 5, '0') || '.HK'
            ELSE UPPER(e.symbol)
        END AS canonical_symbol
    FROM members m
    JOIN engine_signal_scores e ON e.run_id = m.run_id
    WHERE e.tenant_id = :tenant_id
      AND e.user_id = :user_id
)
SELECT
    s.data_trade_date AS trade_date,
    s.symbol,
    s.fusion_score,
    s.signal_side,
    s.rk,
    s.pct,
    COALESCE(st.name, '') AS stock_name
FROM scored s
LEFT JOIN stocks st
    ON st.symbol = s.canonical_symbol
    OR st.symbol = s.symbol
ORDER BY s.data_trade_date, s.rk
"""


async def _load_batch_panel(
    *,
    batch: dict[str, Any],
    tenant_id: str,
    user_id: str,
) -> tuple[Any, list[str]]:
    """取批次全窗口面板，rk/pct 用窗口函数现算。

    不读 engine_signal_scores.score_rank：该列虽存在但推理脚本写入恒为 NULL。
    """
    import pandas as pd

    members = [
        m
        for m in (batch.get("member_runs") or [])
        if m.get("status") == "completed" and m.get("run_id")
    ]
    dates = sorted({str(m["trade_date"]) for m in members})
    if not members:
        return pd.DataFrame(
            columns=[
                "trade_date",
                "symbol",
                "fusion_score",
                "signal_side",
                "rk",
                "pct",
                "stock_name",
            ]
        ), dates

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    text(_BATCH_PANEL_SQL),
                    {
                        "run_ids": [str(m["run_id"]) for m in members],
                        "data_dates": [
                            date.fromisoformat(str(m["trade_date"])) for m in members
                        ],
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                    },
                )
            )
            .mappings()
            .all()
        )

    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        df["trade_date"] = df["trade_date"].astype(str).str.slice(0, 10)
    return df, dates


@router.get(
    "/inference/batch/{batch_id}/aggregate", summary="批量推理跨日聚合统计（用户态）"
)
async def get_batch_inference_aggregate(
    batch_id: str,
    top_k: int = Query(20, ge=1, le=500, description="榜单 Top-K"),
    decay: float = Query(0.85, gt=0.0, le=1.0, description="时间衰减系数"),
    lam: float = Query(0.5, ge=0.0, le=2.0, description="波动惩罚系数 λ"),
    mu: float = Query(0.1, ge=0.0, le=2.0, description="趋势加成系数 μ"),
    min_coverage: float = Query(0.6, ge=0.0, le=1.0, description="最低覆盖率"),
    consensus_band: float = Query(
        0.95,
        ge=0.5,
        le=1.0,
        description="共识分位带门槛（scale-free，替代绝对 Top-K 命中）",
    ),
    side: str = Query("both", description="long | short | both"),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """聚合是纯函数：改参数只重算不重跑推理。默认参数的结果会缓存进 agg_json。"""
    tenant_id, user_id = _owner_scope(current_user)
    batch = await model_inference_batch_persistence.get_batch(
        batch_id=batch_id, tenant_id=tenant_id, user_id=user_id
    )
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    if batch.get("status") not in ("completed", "partial"):
        raise HTTPException(
            status_code=409,
            detail=f"批次尚未就绪（当前状态 {batch.get('status')}），请等待推理完成",
        )

    # Bump when aggregator adds/removes output fields — stale cache silently drops new keys
    _AGG_CACHE_VERSION = 2

    params = batch.get("params") or {}
    is_default = (
        top_k == int(params.get("top_k") or 20)
        and side == str(params.get("side") or "both")
        and decay == 0.85
        and lam == 0.5
        and mu == 0.1
        and min_coverage == 0.6
        and consensus_band == 0.95
    )
    cached_agg = batch.get("agg_json") if is_default else None
    if (
        isinstance(cached_agg, dict)
        and cached_agg.get("_cache_version") == _AGG_CACHE_VERSION
    ):
        cached = dict(cached_agg)
        cached["cached"] = True
        return cached

    panel, dates = await _load_batch_panel(
        batch=batch, tenant_id=tenant_id, user_id=user_id
    )
    try:
        result = aggregate_batch(
            panel,
            dates=dates,
            horizon_days=int(batch.get("horizon_days") or 10),
            top_k=top_k,
            decay=decay,
            lam=lam,
            mu=mu,
            min_coverage=min_coverage,
            consensus_band=consensus_band,
            side=side,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result["batch_id"] = batch_id
    result["model_id"] = batch.get("model_id")
    result["anchor_date"] = batch.get("anchor_date")
    result["batch_status"] = batch.get("status")
    result["cached"] = False
    # 窗口口径（锚定日回退、保留期告警等）在提交时算好，聚合层看不到，需合并回来
    window_meta = params.get("window_meta")
    if isinstance(window_meta, dict):
        merged = list(result.get("meta", {}).get("warnings") or [])
        for warning in window_meta.get("warnings") or []:
            if warning not in merged:
                merged.append(warning)
        result.setdefault("meta", {})["warnings"] = merged
        result["meta"]["anchor_adjusted"] = window_meta.get("anchor_adjusted")
        result["meta"]["requested_anchor_date"] = window_meta.get(
            "requested_anchor_date"
        )
        result["meta"]["span_calendar_days"] = window_meta.get("span_calendar_days")

    if is_default:
        result["_cache_version"] = _AGG_CACHE_VERSION
        await model_inference_batch_persistence.save_aggregate(
            batch_id=batch_id, agg_payload=result
        )
    return result


@router.get("/inference/runs", summary="查询模型推理历史（用户态）")
async def list_model_inference_runs(
    model_id: str | None = Query(None, description="模型ID，可选"),
    run_id: str | None = Query(None, description="批次ID，可选"),
    status: str | None = Query(None, description="状态，可选"),
    inference_date: date | None = Query(None, description="推理基准日期，可选"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    result = await model_inference_persistence.list_runs(
        tenant_id=tenant_id,
        user_id=user_id,
        model_id=model_id,
        run_id=run_id,
        status=status,
        inference_date=inference_date,
        page=page,
        page_size=page_size,
    )
    # 为每条 run 附带市场信号指标（板块avg Top1 / 行业avg Top1 / 强行业数 / 覆盖行业数）
    # 只对 completed 批次计算；信号批量读取，避免 N+1 查询。
    completed_items = [
        it for it in result.get("items", []) if it.get("status") == "completed"
    ]
    if completed_items:
        run_ids = [str(it["run_id"]) for it in completed_items]
        from backend.services.engine.inference.shenwan_industry import (
            load_shenwan_industry_map,
        )
        from backend.shared.stock_utils import StockCodeUtil

        industry_map = load_shenwan_industry_map()
        signals_by_run: dict[str, list[dict[str, Any]]] = {rid: [] for rid in run_ids}
        try:
            async with get_session(read_only=True) as session:
                rows = (
                    (
                        await session.execute(
                            text(
                                """
                            SELECT symbol, fusion_score, run_id,
                                   CASE
                                       WHEN LEFT(symbol, 3) = '688' THEN '科创板'
                                       WHEN LEFT(symbol, 3) IN ('300', '301') THEN '创业板'
                                       WHEN LEFT(symbol, 3) IN ('002', '003') THEN '中小板'
                                       WHEN LEFT(symbol, 3) IN ('000', '001') THEN '深主板'
                                       WHEN LEFT(symbol, 1) IN ('4', '8', '9') THEN '北交所'
                                       WHEN LEFT(symbol, 2) = '60' THEN '沪主板'
                                       ELSE '其他'
                                   END AS board
                            FROM engine_signal_scores
                            WHERE run_id = ANY(:run_ids)
                              AND tenant_id = :tenant_id AND user_id = :user_id
                            """
                            ),
                            {
                                "run_ids": run_ids,
                                "tenant_id": tenant_id,
                                "user_id": user_id,
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
            for row in rows:
                signals_by_run.setdefault(str(row["run_id"]), []).append(dict(row))
        except Exception as exc:  # pragma: no cover
            logger.warning("列表加载信号市场指标失败: %s", exc)

        run_by_id = {str(it["run_id"]): it for it in completed_items}
        for rid, sigs in signals_by_run.items():
            it = run_by_id.get(rid)
            if not it or not sigs:
                continue
            # 标注申万行业
            for s in sigs:
                s["industry"] = (
                    industry_map.get(
                        StockCodeUtil.to_suffix(str(s.get("symbol") or ""))
                    )
                    or ""
                )
            it.update(compute_market_signals(sigs))
    return result


# 推理历史股票简称映射（QuantDB instrument_list，进程内 TTL 缓存）：
# PG stocks 表已随数据源迁移废弃，名称一律以 QuantDB 全量股票列表为准。
_QUANTDB_NAMES_CACHE: dict[str, str] | None = None
_QUANTDB_NAMES_CACHE_AT = 0.0
_QUANTDB_NAMES_TTL = 600.0


_QUANTHK_NAMES_CACHE: dict[str, str] | None = None
_QUANTHK_NAMES_CACHE_AT = 0.0
_QUANTHK_NAMES_TTL = 600.0


def _load_quanthk_hk_names() -> dict[str, str]:
    """从 quanthk security_master 加载 {suffix_symbol(0700.HK): 中文名}。"""
    global _QUANTHK_NAMES_CACHE, _QUANTHK_NAMES_CACHE_AT  # noqa: PLW0603
    import time

    now = time.monotonic()
    if (
        _QUANTHK_NAMES_CACHE is not None
        and now - _QUANTHK_NAMES_CACHE_AT < _QUANTHK_NAMES_TTL
    ):
        return _QUANTHK_NAMES_CACHE
    from backend.services.engine.data_platform.quanthk_hub import _resolve_quanthk_data_dir

    result: dict[str, str] = {}
    try:
        import duckdb

        con = duckdb.connect()
        try:
            rows = con.execute(
                "SELECT symbol, cn_name FROM read_parquet("
                f"'{_resolve_quanthk_data_dir()}/2_base_sector/security_master/data.parquet')"
            ).fetchall()
            result = {str(sym): str(cn) for sym, cn in rows if cn}
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - 名称表缺失不影响主流程
        logger.warning("quanthk security_master 读取失败: %s", exc)
    _QUANTHK_NAMES_CACHE, _QUANTHK_NAMES_CACHE_AT = result, now
    return result


def _load_quantdb_stock_names() -> dict[str, str]:
    """从 QuantDB instrument_list 加载 {prefix_symbol: 股票简称}。"""
    global _QUANTDB_NAMES_CACHE, _QUANTDB_NAMES_CACHE_AT  # noqa: PLW0603
    import time

    now = time.monotonic()
    if (
        _QUANTDB_NAMES_CACHE is not None
        and now - _QUANTDB_NAMES_CACHE_AT < _QUANTDB_NAMES_TTL
    ):
        return _QUANTDB_NAMES_CACHE

    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
    from backend.shared.stock_utils import StockCodeUtil

    result: dict[str, str] = {}
    try:
        hub = QuantDBDataHub.get_instance()
        df = hub.fetch_stock_list()
        if df is not None and not df.empty:
            symbol_col = (
                "Symbol" if "Symbol" in df.columns
                else "symbol" if "symbol" in df.columns else None
            )
            name_col = (
                "Name" if "Name" in df.columns
                else "stock_name" if "stock_name" in df.columns else None
            )
            if symbol_col and name_col:
                for _, row in df[[symbol_col, name_col]].dropna().iterrows():
                    sym = StockCodeUtil.to_prefix(str(row[symbol_col]).strip())
                    nm = str(row[name_col]).strip()
                    if sym and nm:
                        result[sym] = nm
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 QuantDB 股票简称失败: %s", exc)
    _QUANTDB_NAMES_CACHE = result
    _QUANTDB_NAMES_CACHE_AT = time.monotonic()
    return result


@router.get("/inference/runs/{run_id}", summary="查看模型推理结果明细（用户态）")
async def get_model_inference_run_detail(
    run_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    run = await model_inference_persistence.get_run(
        run_id=run_id, tenant_id=tenant_id, user_id=user_id
    )
    if not run:
        raise HTTPException(status_code=404, detail="推理批次不存在")

    signals: list[dict[str, Any]] = []
    if run.get("status") == "completed":
        try:
            async with get_session(read_only=True) as session:
                rows = (
                    (
                        await session.execute(
                            text(
                                """
                            WITH raw AS (
                                SELECT
                                    ess.*,
                                    -- 归一化到规范 suffix 格式（600036.SH），口径与
                                    -- backend/shared/stock_utils.py StockCodeUtil.to_suffix 一致。
                                    -- 推理脚本写入的是 prefix 格式（SH600519），旧 JOIN 只处理
                                    -- 纯数字与直连后缀式，导致 stock_name 恒为空。
                                    CASE
                                        WHEN UPPER(ess.symbol) ~ '^[0-9]{6}[.](SH|SZ|BJ)$'
                                            THEN UPPER(ess.symbol)
                                        WHEN UPPER(ess.symbol) ~ '^(SH|SZ|BJ)[0-9]{6}$'
                                            THEN SUBSTRING(UPPER(ess.symbol), 3)
                                                 || '.' || SUBSTRING(UPPER(ess.symbol), 1, 2)
                                        WHEN ess.symbol ~ '^[0-9]{6}$'
                                            THEN ess.symbol || CASE
                                                WHEN LEFT(ess.symbol, 2) IN ('60', '68', '90') THEN '.SH'
                                                WHEN LEFT(ess.symbol, 2) IN ('00', '30', '20') THEN '.SZ'
                                                WHEN LEFT(ess.symbol, 2) IN ('83', '43', '87', '88', '92') THEN '.BJ'
                                                ELSE ''
                                            END
                                        WHEN ess.symbol ~ '^[0-9]{4,5}$'
                                            THEN LPAD(ess.symbol, 5, '0') || '.HK'
                                        ELSE UPPER(ess.symbol)
                                    END AS canonical_symbol
                                FROM engine_signal_scores ess
                                WHERE ess.run_id = :run_id
                                  AND ess.tenant_id = :tenant_id
                                  AND ess.user_id = :user_id
                            ),
                            scored AS (
                                SELECT
                                    raw.*,
                                    -- A股板块按「归一化后」代码前缀判断（旧口径用原始 prefix
                                    -- 格式 SH600519 取前缀，永远命中不了 688/300 等，恒为"其他"）
                                    CASE
                                        WHEN LEFT(raw.canonical_symbol, 3) = '688' THEN '科创板'
                                        WHEN LEFT(raw.canonical_symbol, 3) IN ('300', '301') THEN '创业板'
                                        WHEN LEFT(raw.canonical_symbol, 3) IN ('002', '003') THEN '中小板'
                                        WHEN LEFT(raw.canonical_symbol, 3) IN ('000', '001') THEN '深主板'
                                        WHEN SUBSTRING(raw.canonical_symbol, 1, 1) IN ('4', '8', '9') THEN '北交所'
                                        WHEN LEFT(raw.canonical_symbol, 2) = '60' THEN '沪主板'
                                        ELSE '其他'
                                    END AS board
                                FROM raw
                            )
                            SELECT
                                ess.symbol,
                                ess.fusion_score,
                                ess.light_score,
                                ess.tft_score,
                                ess.score_rank,
                                ess.signal_side,
                                ess.expected_price,
                                ess.quality,
                                ess.created_at,
                                ess.board,
                                COALESCE(s.name, '') AS stock_name
                            FROM scored ess
                            LEFT JOIN stocks s
                                ON s.symbol = ess.canonical_symbol
                                OR s.symbol = ess.symbol
                            ORDER BY ess.fusion_score DESC NULLS LAST, ess.symbol ASC
                            """
                            ),
                            {
                                "run_id": run_id,
                                "tenant_id": tenant_id,
                                "user_id": user_id,
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
            for row in rows:
                item = dict(row or {})
                if item.get("created_at") is not None:
                    item["created_at"] = item["created_at"].isoformat()
                signals.append(item)
        except Exception as exc:  # pragma: no cover - DB fallback
            logger.warning(
                "failed to load inference signal rows for %s: %s", run_id, exc
            )
            signals = []

    # ── pred.parquet 回退：信号表被后续覆盖清空时，从模型目录读当日截面 ──
    # 历史单股补推曾整桶删除当日全市场信号（已修复），已 completed 的批次
    # 明细可能查不到任何信号行；此时用模型 pred.parquet 的数据日 T 截面兜底，
    # 恢复批次信号展示（字段口径对齐 DB 路径，缺失列留 None）。
    if run.get("status") == "completed" and not signals:
        try:
            from backend.services.api.routers.research_service import (
                _read_model_pred_day,
            )

            _model_id = run.get("model_id")
            _storage_path: str | None = None
            if _model_id:
                _model_meta = await model_registry_service.get_model(
                    tenant_id=tenant_id, user_id=user_id, model_id=_model_id
                )
                if not _model_meta:
                    # 兼容：历史模型可能挂在其他用户名下，按租户放宽查询
                    async with get_session(read_only=True) as session:
                        _row = (
                            (
                                await session.execute(
                                    text(
                                        """
                                        SELECT storage_path
                                        FROM qm_user_models
                                        WHERE tenant_id = :tenant_id
                                          AND model_id = :model_id
                                        LIMIT 1
                                        """
                                    ),
                                    {"tenant_id": tenant_id, "model_id": _model_id},
                                )
                            )
                            .mappings()
                            .first()
                        )
                        if _row:
                            _storage_path = str(_row.get("storage_path") or "") or None
                else:
                    _storage_path = str(_model_meta.get("storage_path") or "") or None

            if _storage_path:
                _data_date = str(
                    run.get("data_trade_date") or run.get("inference_date") or ""
                )[:10]
                if _data_date:
                    _pred_rows = await asyncio.to_thread(
                        _read_model_pred_day, _storage_path, _data_date
                    )
                    for _pr in _pred_rows:
                        _sym = str(_pr.get("symbol") or "")
                        signals.append(
                            {
                                # 归一为纯数字，与 DB 路径及后续板块/市值标注口径一致
                                "symbol": re.sub(r"[^0-9]", "", _sym) or _sym,
                                "fusion_score": _pr.get("score"),
                                "light_score": None,
                                "tft_score": None,
                                "score_rank": _pr.get("rank"),
                                "signal_side": None,
                                "expected_price": None,
                                "quality": None,
                                "created_at": None,
                                "stock_name": "",
                            }
                        )
                    if signals:
                        logger.info(
                            "inference run %s 信号表为空，已从 pred.parquet 回退 %d 条截面 (date=%s)",
                            run_id,
                            len(signals),
                            _data_date,
                        )
        except Exception as exc:  # pragma: no cover - pred.parquet fallback
            logger.warning(
                "pred.parquet fallback failed for %s: %s", run_id, exc
            )

    # ── 股票简称兜底：PG stocks 表已废弃，名称以 QuantDB instrument_list 为准 ──
    # 覆盖 DB JOIN 取不到名称（stocks 表为空/格式不匹配）与 pred.parquet 回退
    # 路径硬编码空名称两种情况；summary 中依赖 stock_name 的字段随之正确。
    try:
        from backend.shared.stock_utils import StockCodeUtil

        # 港股模型（mdl_hk_ 前缀）用 quanthk security_master 名称表，其余走 QuantDB
        hk_model = str(run.get("model_id") or "").startswith("mdl_hk_")
        name_map = _load_quanthk_hk_names() if hk_model else {}
        cn_map = {} if hk_model else _load_quantdb_stock_names()
        for item in signals:
            if item.get("stock_name"):
                continue
            sym = str(item.get("symbol") or "").strip()
            if not sym:
                continue
            if hk_model:
                # engine_signal_scores.symbol 为纯数字（0700），拼 .HK 匹配 master
                suffix = f"{sym}.HK" if sym.isdigit() and not sym.endswith(".HK") else sym
                nm = name_map.get(suffix.upper()) or name_map.get(suffix)
            else:
                prefix = StockCodeUtil.to_prefix(sym)
                nm = cn_map.get(prefix) if prefix else None
            if nm:
                item["stock_name"] = nm
    except Exception as exc:  # pragma: no cover - 名称兜底失败不影响主流程
        logger.warning("回填股票简称失败: %s", exc)

    summary = dict(run)
    summary["rows_count"] = len(signals)
    summary["symbols_count"] = len(
        {str(item.get("symbol") or "") for item in signals if item.get("symbol")}
    )
    fusion_scores = [
        float(item["fusion_score"])
        for item in signals
        if item.get("fusion_score") is not None
    ]
    summary["min_fusion_score"] = min(fusion_scores) if fusion_scores else None
    summary["max_fusion_score"] = max(fusion_scores) if fusion_scores else None
    summary["score_distribution"] = compute_score_distribution(fusion_scores)
    summary["first_created_at"] = signals[0].get("created_at") if signals else None
    summary["last_created_at"] = signals[-1].get("created_at") if signals else None
    if run.get("status") == "completed" and not signals:
        summary["signal_rows_error"] = "signal rows unavailable"

    # ── 板块/行业标注 + 5大板块 Top1 统计 ──
    if signals:
        try:
            from backend.services.engine.inference.shenwan_industry import (
                load_shenwan_industry_map,
            )
            from backend.shared.stock_utils import StockCodeUtil

            industry_map = load_shenwan_industry_map()
            for item in signals:
                sym = str(item.get("symbol") or "").strip()
                suffix = StockCodeUtil.to_suffix(sym)
                item["industry"] = industry_map.get(suffix) or ""
        except Exception as exc:  # pragma: no cover - 行业标注失败不影响主流程
            logger.warning("标注申万行业失败: %s", exc)

        # ── 市值标注（QuantDB features_daily parquet）──
        # 市值分档：微盘<30亿 / 小盘30-100亿 / 中盘100-300亿 / 大盘300-1000亿 / 超大盘>1000亿
        try:
            data_date = run.get("data_trade_date") or run.get("inference_date")
            _d = str(data_date)[:10] if data_date else ""
            dt_int = int(_d.replace("-", "")) if _d else 0
            qdb_dir = os.getenv("QM_QUANTDB_DATA_DIR", "/data/quantdb")
            fd_dir = Path(qdb_dir) / "6_ml_datasets" / "features_daily"
            # features_daily 目录缺失/为空时直接跳过，避免 DuckDB 对空 glob 的内部卡顿
            parquet_exists = fd_dir.is_dir() and any(fd_dir.rglob("*.parquet"))
            if dt_int and parquet_exists:
                fpath = f"{fd_dir}/**/*.parquet"

                def _load_mv():
                    import duckdb as _duckdb

                    con = _duckdb.connect()
                    try:
                        return con.execute(
                            f"""
                            SELECT symbol, total_mv, float_mv
                            FROM read_parquet('{fpath}', hive_partitioning=true)
                            WHERE dt = {dt_int}
                            """
                        ).fetchdf()
                    finally:
                        con.close()

                # 在线程中执行，避免同步 DuckDB 查询阻塞 API 事件循环
                mv_rows = await asyncio.to_thread(_load_mv)
                mv_map: dict[str, dict] = {}
                for _, row in mv_rows.iterrows():
                    raw_sym = str(row["symbol"]).strip().upper()
                    # 归一化为 6 位纯数字，与信号表 symbol（纯数字）对齐
                    key = (
                        raw_sym.split(".")[0]
                        .replace("SH", "")
                        .replace("SZ", "")
                        .replace("BJ", "")
                    )
                    mv_map[key] = {
                        "total_mv": float(row["total_mv"])
                        if row["total_mv"] is not None
                        and str(row["total_mv"]) not in ("nan", "None")
                        else None,
                        "float_mv": float(row["float_mv"])
                        if row["float_mv"] is not None
                        and str(row["float_mv"]) not in ("nan", "None")
                        else None,
                    }
                for item in signals:
                    sym = str(item.get("symbol") or "").strip().upper()
                    key = (
                        sym.split(".")[0]
                        .replace("SH", "")
                        .replace("SZ", "")
                        .replace("BJ", "")
                    )
                    mv = mv_map.get(key) or {}
                    tm = mv.get("total_mv")
                    item["total_mv"] = tm
                    if tm is not None:
                        # 单位：parquet 为元，转亿
                        yiyi = tm / 1e8
                        item["market_cap_yi"] = round(yiyi, 2)
                        if yiyi < 30:
                            item["market_cap_tier"] = "微盘"
                        elif yiyi < 100:
                            item["market_cap_tier"] = "小盘"
                        elif yiyi < 300:
                            item["market_cap_tier"] = "中盘"
                        elif yiyi < 1000:
                            item["market_cap_tier"] = "大盘"
                        else:
                            item["market_cap_tier"] = "超大盘"
        except Exception as exc:  # pragma: no cover - 市值标注失败不影响主流程
            logger.warning("标注市值失败: %s", exc)

        board_top1: dict[str, float] = {}
        board_top1_symbol: dict[str, str] = {}
        board_top1_name: dict[str, str] = {}
        for item in signals:
            score = item.get("fusion_score")
            if score is None:
                continue
            board = item.get("board") or "其他"
            try:
                fscore = float(score)
            except (TypeError, ValueError):
                continue
            if board not in board_top1 or fscore > board_top1[board]:
                board_top1[board] = fscore
                board_top1_symbol[board] = str(item.get("symbol") or "")
                board_top1_name[board] = str(item.get("stock_name") or "")

        board_stats = [
            {
                "board": board,
                "top1_score": board_top1[board],
                "top1_symbol": board_top1_symbol[board],
                "top1_name": board_top1_name[board],
            }
            for board in ("沪主板", "深主板", "中小板", "创业板", "科创板")
            if board in board_top1
        ]
        summary["board_top1"] = board_stats
        # 市场信号指标（板块avg Top1 / 行业avg Top1 / 强行业数 / 覆盖行业数 / 入场判断）
        summary.update(compute_market_signals(signals))

        # ── 个股分数区间统计（决定买哪只）──
        # 区间规则来自选股策略（普通模型，分数 ~0~0.3）：
        #   <0.10     不买（信号太弱）
        #   0.10-0.12 黄金区间（首选）｜0.10-0.11 假信号区，须配合行业 avgTop1 ≥ 0.09
        #   0.12-0.15 可选（主板优先，警惕追高）
        #   0.15-0.20 谨慎（仅强市有效）
        #   ≥0.20     极谨慎（趋势加速，样本少）
        #
        # 融合模型分数是截面百分位 [-1,1]（0.9+ 常见），硬编码阈值全部失效。
        # 自适应：检测分数范围，若明显超出普通模型区间（max > 0.35），改用
        # 基于实际分布的分位数分桶，保证每个桶都有意义、高分始终在最上层。
        score_buckets: list[dict[str, Any]] = []
        bucket_cfg = [
            ("lt_010", "< 0.10", "不买", lambda s: s < 0.10, "slate"),
            (
                "gold",
                "0.10 - 0.12",
                "黄金区间 · 首选",
                lambda s: 0.10 <= s < 0.12,
                "emerald",
            ),
            (
                "opt_012_015",
                "0.12 - 0.15",
                "可选 · 主板优先",
                lambda s: 0.12 <= s < 0.15,
                "amber",
            ),
            (
                "warn_015_020",
                "0.15 - 0.20",
                "谨慎 · 仅强市",
                lambda s: 0.15 <= s < 0.20,
                "orange",
            ),
            ("gte_020", "≥ 0.20", "极谨慎 · 样本少", lambda s: s >= 0.20, "rose"),
        ]
        _is_wide_scale = bool(fusion_scores) and (
            max(fusion_scores) > 0.35 or min(fusion_scores) < -0.35
        )
        if _is_wide_scale:
            # 融合模型：基于分数分布的分位数分桶（从高分到低分）
            _n = len(fusion_scores)
            _sorted = sorted(fusion_scores)
            _thr = {
                p: _sorted[max(0, int(p * (_n - 1)))] for p in (0.80, 0.60, 0.40, 0.20)
            }
            bucket_cfg = [
                (
                    "lt_010",
                    f"< {_thr[0.20]:.3f}",
                    "低分区间",
                    lambda s: s < _thr[0.20],
                    "slate",
                ),
                (
                    "gold",
                    f"{_thr[0.20]:.3f} - {_thr[0.40]:.3f}",
                    "中低区间",
                    lambda s: _thr[0.20] <= s < _thr[0.40],
                    "emerald",
                ),
                (
                    "opt_012_015",
                    f"{_thr[0.40]:.3f} - {_thr[0.60]:.3f}",
                    "中高分区间",
                    lambda s: _thr[0.40] <= s < _thr[0.60],
                    "amber",
                ),
                (
                    "warn_015_020",
                    f"{_thr[0.60]:.3f} - {_thr[0.80]:.3f}",
                    "高分区",
                    lambda s: _thr[0.60] <= s < _thr[0.80],
                    "orange",
                ),
                (
                    "gte_020",
                    f"≥ {_thr[0.80]:.3f}",
                    "最高分区 · 首选",
                    lambda s: s >= _thr[0.80],
                    "rose",
                ),
            ]
        for key, label, action, predicate, color in bucket_cfg:
            matches = [
                it
                for it in signals
                if it.get("fusion_score") is not None
                and predicate(float(it["fusion_score"]))
            ]
            score_buckets.append(
                {
                    "key": key,
                    "label": label,
                    "action": action,
                    "color": color,
                    "count": len(matches),
                    "symbols": [str(it.get("symbol") or "") for it in matches],
                }
            )
        # 假信号区（黄金区间内 0.10-0.11，单看胜率低，须配合行业确认）— 仅普通模型有意义
        if _is_wide_scale:
            fake_signal = []
        else:
            fake_signal = [
                it
                for it in signals
                if it.get("fusion_score") is not None
                and 0.10 <= float(it["fusion_score"]) < 0.11
            ]
        summary["score_buckets"] = score_buckets
        summary["gold_zone_count"] = next(
            (b["count"] for b in score_buckets if b["key"] == "gold"), 0
        )
        summary["fake_signal_count"] = len(fake_signal)
        summary["is_wide_scale"] = bool(_is_wide_scale)
        summary["fake_signal_symbols"] = [
            str(it.get("symbol") or "") for it in fake_signal
        ]

        # ── 3天分数趋势（决定买点）──
        # 对当前批次 T，取同一模型最近两个历史批次日（T-2 / T-1）的同股分数，判断过去3天走势：
        #   先升后降  T-2 < T-1 > T   → 最佳买点（T-1 是峰值）
        #   连续上升  T-2 < T-1 < T   → 过热，不追（强市除外）
        #   连续下降  T-2 > T-1 > T   → 信号衰退，不买
        # 全部用已发生分数，最新批次也能算出完整趋势。
        try:
            data_date = (
                run.get("data_trade_date")
                or run.get("inference_date")
                or run.get("prediction_trade_date")
            )
            model_id = run.get("model_id")
            if data_date and model_id:
                from datetime import date as _date, timedelta as _td

                if not isinstance(data_date, _date):
                    data_date = _date.fromisoformat(str(data_date)[:10])
                async with get_session(read_only=True) as trend_session:
                    trend_rows = (
                        (
                            await trend_session.execute(
                                text(
                                    """
                                SELECT r.data_trade_date, r.run_id
                                FROM qm_model_inference_runs r
                                WHERE r.model_id = :p_model
                                  AND r.status = 'completed'
                                  AND r.tenant_id = :p_tenant
                                  AND r.user_id = :p_user
                                  AND r.data_trade_date < :p_date
                                ORDER BY r.data_trade_date DESC
                                LIMIT 2
                                """
                                ),
                                {
                                    "p_model": model_id,
                                    "p_date": data_date,
                                    "p_tenant": tenant_id,
                                    "p_user": user_id,
                                },
                            )
                        )
                        .mappings()
                        .all()
                    )

                # trend_rows 按日期升序 → [T-2, T-1]（不足两个时 T-2 为 None）
                trend_rows.sort(key=lambda tr: tr["data_trade_date"])
                prev2_rid = trend_rows[0]["run_id"] if len(trend_rows) >= 2 else None
                prev1_rid = trend_rows[-1]["run_id"] if trend_rows else None

                score_lookup: dict[str, dict[str, float]] = {}
                for label, rid in (("prev2", prev2_rid), ("prev1", prev1_rid)):
                    if not rid:
                        continue
                    async with get_session(read_only=True) as ss:
                        srows = (
                            (
                                await ss.execute(
                                    text(
                                        """
                                    SELECT symbol, fusion_score FROM engine_signal_scores
                                    WHERE run_id = :rid AND tenant_id = :tenant_id AND user_id = :user_id
                                    """
                                    ),
                                    {
                                        "rid": rid,
                                        "tenant_id": tenant_id,
                                        "user_id": user_id,
                                    },
                                )
                            )
                            .mappings()
                            .all()
                        )
                    score_lookup[label] = {
                        str(r["symbol"]): (
                            float(r["fusion_score"])
                            if r["fusion_score"] is not None
                            else None
                        )
                        for r in srows
                    }

                trend_counter: dict[str, int] = {
                    "先升后降": 0,
                    "连续上升": 0,
                    "连续下降": 0,
                    "其他": 0,
                    "数据不足": 0,
                }
                for item in signals:
                    sym = str(item.get("symbol") or "").strip()
                    cur = item.get("fusion_score")
                    if cur is None:
                        item["trend"] = "数据不足"
                        trend_counter["数据不足"] += 1
                        continue
                    cur = float(cur)
                    prev1 = (score_lookup.get("prev1") or {}).get(sym)  # T-1
                    prev2 = (score_lookup.get("prev2") or {}).get(sym)  # T-2
                    item["prev_score"] = prev1  # 保留字段名兼容前端 tooltip（T-1）
                    item["prev2_score"] = prev2  # T-2
                    item["next_score"] = None
                    if prev1 is None or prev2 is None:
                        # 缺历史分数：只有单日方向（当前 vs T-1）
                        if prev1 is not None:
                            item["trend"] = (
                                "上升"
                                if cur > prev1
                                else ("下降" if cur < prev1 else "持平")
                            )
                        else:
                            item["trend"] = "数据不足"
                    else:
                        if prev2 < prev1 and prev1 > cur:
                            item["trend"] = "先升后降"
                        elif prev2 < prev1 < cur:
                            item["trend"] = "连续上升"
                        elif prev2 > prev1 > cur:
                            item["trend"] = "连续下降"
                        else:
                            item["trend"] = "其他"
                    trend_counter[item["trend"]] = (
                        trend_counter.get(item["trend"], 0) + 1
                    )
                summary["trend_stats"] = trend_counter
        except Exception as exc:  # pragma: no cover - 趋势统计失败不影响主流程
            logger.warning("3天趋势统计失败: %s", exc)

        # ── 大盘均线过滤（系统风险）──
        # 上证指数跌破 20 日均线 → 强制空仓（不管模型给多高分）。
        try:
            from datetime import date as _date, timedelta as _td
            from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

            ref_date = (
                run.get("data_trade_date")
                or run.get("inference_date")
                or run.get("prediction_trade_date")
            )
            if ref_date:
                end = (
                    ref_date
                    if isinstance(ref_date, _date)
                    else _date.fromisoformat(str(ref_date)[:10])
                )
                start = end - _td(days=60)
                hub = QuantDBDataHub()
                idx = hub.fetch_index_kline("000001.SH", start, end)
                ma_cfg = {
                    "ma5": 5,
                    "ma10": 10,
                    "ma20": 20,
                    "ma30": 30,
                    "ma60": 60,
                }
                idx_meta: dict[str, Any] = {
                    "symbol": "000001.SH",
                    "dates": [],
                    "close": None,
                    "mavg": {},
                }
                if not idx.empty:
                    df = idx.sort_values("dt")
                    closes = df["close"].astype(float).tolist()
                    dates = [str(x)[:10] for x in df["dt"].tolist()]
                    idx_meta["dates"] = dates[-1:] if dates else []
                    idx_meta["close"] = closes[-1] if closes else None
                    for name, win in ma_cfg.items():
                        if len(closes) >= win:
                            idx_meta["mavg"][name] = round(sum(closes[-win:]) / win, 2)
                        else:
                            idx_meta["mavg"][name] = None
                    close = idx_meta["close"]
                    ma20 = idx_meta["mavg"].get("ma20")
                    idx_meta["below_ma20"] = bool(
                        close is not None and ma20 is not None and close < ma20
                    )
                    idx_meta["ref_date"] = str(end)[:10]
                summary["market_ma_filter"] = idx_meta
        except Exception as exc:  # pragma: no cover - 均线过滤失败不影响主流程
            logger.warning("大盘均线计算失败: %s", exc)

        # ── 负分分析（做空/回避 决策矩阵）──
        # 研究结论（2024-2026, 612交易日, 328万条）：
        #   做空只做微盘/小盘 + 分数≤-0.15；大盘/超大盘/科创板负分是错杀；
        #   极端负分≤-0.20 微盘最危险（-0.25 → 下跌77.7%）；轻负分>-0.06 无信息。
        #   行业：银行/半导体/元器件抗跌（错杀）；酒店餐饮/渔业/焦炭/酿酒下跌持续（做空首选）。
        try:
            neg_items = [
                it
                for it in signals
                if it.get("fusion_score") is not None and float(it["fusion_score"]) < 0
            ]
            short_candidates: list[dict[str, Any]] = []
            mistake_candidates: list[dict[str, Any]] = []
            extreme_neg: list[dict[str, Any]] = []
            for it in neg_items:
                score = float(it["fusion_score"])
                tier = it.get("market_cap_tier") or "未知"
                # 做空候选：微盘/小盘 + 分数 ≤ -0.15
                if tier in ("微盘", "小盘") and score <= -0.15:
                    short_candidates.append(it)
                # 错杀候选：大盘/超大盘 负分
                if tier in ("大盘", "超大盘"):
                    mistake_candidates.append(it)
                # 极端负分
                if score <= -0.20:
                    extreme_neg.append(it)

            # 负分区间分布
            neg_buckets = {
                "轻负分 (>-0.06)": sum(
                    1 for it in neg_items if float(it["fusion_score"]) > -0.06
                ),
                "中负分 (-0.06~-0.15)": sum(
                    1 for it in neg_items if -0.15 <= float(it["fusion_score"]) <= -0.06
                ),
                "极端负分 (≤-0.15)": sum(
                    1 for it in neg_items if float(it["fusion_score"]) < -0.15
                ),
            }
            neg_by_tier: dict[str, int] = {}
            for it in neg_items:
                tier = it.get("market_cap_tier") or "未知"
                neg_by_tier[tier] = neg_by_tier.get(tier, 0) + 1

            # 负分行业统计（做空 vs 抗跌）
            def _top_industry(fn) -> list[dict[str, Any]]:
                agg: dict[str, dict[str, Any]] = {}
                for it in neg_items:
                    ind = it.get("industry") or "未知"
                    if not ind:
                        continue
                    sc = float(it["fusion_score"])
                    a = agg.setdefault(
                        ind, {"industry": ind, "count": 0, "sum_score": 0.0}
                    )
                    a["count"] += 1
                    a["sum_score"] += sc
                rows = []
                for a in agg.values():
                    rows.append(
                        {
                            "industry": a["industry"],
                            "count": a["count"],
                            "avg_score": round(a["sum_score"] / a["count"], 4),
                        }
                    )
                rows.sort(key=lambda x: x["count"], reverse=True)
                return fn(rows)[:8]

            # 做空首选行业：负分股票最多的行业（下跌持续）
            short_industries = _top_industry(
                lambda rows: sorted(rows, key=lambda x: x["count"], reverse=True)
            )
            # 抗跌行业：银行/半导体/元器件 等负分但实际错杀的
            resistant_industries = [
                r
                for r in _top_industry(lambda rows: rows)
                if r["industry"]
                in ("银行", "半导体", "元件", "光学光电子", "通信设备", "电子")
            ]

            summary["negative_analysis"] = {
                "negative_count": len(neg_items),
                "negative_pct": round(len(neg_items) / max(len(signals), 1) * 100, 1),
                "neg_buckets": neg_buckets,
                "neg_by_tier": neg_by_tier,
                "short_candidates_count": len(short_candidates),
                "short_candidates": [
                    {
                        "symbol": it.get("symbol"),
                        "name": it.get("stock_name"),
                        "score": float(it["fusion_score"]),
                        "tier": it.get("market_cap_tier"),
                        "industry": it.get("industry"),
                    }
                    for it in sorted(
                        short_candidates, key=lambda x: float(x["fusion_score"])
                    )[:10]
                ],
                "mistake_candidates_count": len(mistake_candidates),
                "mistake_candidates": [
                    {
                        "symbol": it.get("symbol"),
                        "name": it.get("stock_name"),
                        "score": float(it["fusion_score"]),
                        "tier": it.get("market_cap_tier"),
                        "industry": it.get("industry"),
                    }
                    for it in sorted(
                        mistake_candidates, key=lambda x: float(x["fusion_score"])
                    )[:10]
                ],
                "extreme_neg_count": len(extreme_neg),
                "short_industries": short_industries,
                "resistant_industries": resistant_industries,
            }
            # 每条信号打负分标签（前端高亮/筛选用）
            short_syms = {str(it.get("symbol")) for it in short_candidates}
            mistake_syms = {str(it.get("symbol")) for it in mistake_candidates}
            extreme_syms = {str(it.get("symbol")) for it in extreme_neg}
            # 抗跌行业（负分但常被错杀）：银行/半导体/元器件/光学光电子/通信设备/电子
            resistant_set = {r["industry"] for r in resistant_industries}
            for it in signals:
                sym = str(it.get("symbol") or "")
                if sym in extreme_syms:
                    it["negative_tag"] = "极端负分"
                elif sym in short_syms:
                    it["negative_tag"] = "做空候选"
                elif sym in mistake_syms:
                    it["negative_tag"] = "错杀候选"
                elif (
                    it.get("fusion_score") is not None and float(it["fusion_score"]) < 0
                ):
                    ind = str(it.get("industry") or "")
                    if ind in resistant_set:
                        it["negative_tag"] = "抗跌行业"
                    else:
                        it["negative_tag"] = "负分"
        except Exception as exc:  # pragma: no cover - 负分分析失败不影响主流程
            logger.warning("负分分析失败: %s", exc)

    return {
        "summary": summary,
        "page": 1,
        "page_size": len(signals) or 1,
        "total": len(signals),
        "items": signals,
    }


@router.delete("/inference/runs/{run_id}", summary="删除推理运行记录（用户态）")
async def delete_model_inference_run(
    run_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    delete = await model_inference_persistence.delete_run(
        run_id=run_id, tenant_id=tenant_id, user_id=user_id
    )
    if not delete.get("deleted"):
        raise HTTPException(status_code=404, detail="推理批次不存在或已删除")
    return delete


# 模型分数曲线主数据源：模型目录 pred.parquet（训练生成时的全量历史分数序列），
# 不依赖每日推理批次；进程内缓存（模型+股票+起始日），避免反复扫描 100MB+ 文件
_PRED_HIST_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_PRED_HIST_TTL = 600.0


def _read_stock_pred_history(
    storage_path: str, code6: str, cutoff: date, model_id: str
) -> list[dict[str, Any]]:
    """从模型目录 pred.parquet 读取该股历史分数时序（含每日截面排名）。

    兼容多种列名（pred/fusion_score/score、trade_date/date/datetime、
    symbol/instrument 前缀/后缀/小写式均可）；无文件或读取失败返回 []。
    """
    import time as _time

    cache_key = f"{storage_path}|{code6}|{cutoff.isoformat()}"
    hit = _PRED_HIST_CACHE.get(cache_key)
    if hit and _time.time() - hit[0] < _PRED_HIST_TTL:
        return hit[1]

    items: list[dict[str, Any]] = []
    parquet_file = next(
        (p for p in _resolve_pred_candidates(storage_path) if p.is_file()), None
    )
    if parquet_file:
        try:
            import duckdb

            con = duckdb.connect()
            try:
                cols = [
                    r[0]
                    for r in con.execute(
                        f"SELECT * FROM read_parquet('{str(parquet_file)}') LIMIT 0"
                    ).description
                ]
                score_col = next(
                    (c for c in ("pred", "fusion_score", "score") if c in cols), None
                )
                date_col = next(
                    (c for c in ("trade_date", "date", "datetime") if c in cols), None
                )
                sym_col = next(
                    (c for c in ("symbol", "instrument") if c in cols), None
                )
                if score_col and date_col and sym_col:
                    # 先全市场截面算 RANK 再过滤该股（过滤在窗口前做会使排名恒为 1）；
                    # regexp_extract 抽连续数字段，兼容 SH600000/600000.SH/sh600000
                    # （注：duckdb 的 regexp_replace 默认只替换首个匹配，不能用于去前缀）
                    # 排名口径与 A 套（engine_signal_scores）对齐：剔除 B 股
                    # （SH900/SZ200）、北交所（BJ）、指数（SH000/SZ399），两套
                    # 数据源切换时排名不再跳变
                    rows = con.execute(
                        f"""
                        WITH d AS (
                            SELECT CAST({date_col} AS DATE) AS td,
                                   CAST({score_col} AS DOUBLE) AS sc,
                                   regexp_extract(CAST({sym_col} AS VARCHAR),
                                                  '[0-9]+', 0) AS code6,
                                   RANK() OVER (PARTITION BY CAST({date_col} AS DATE)
                                                ORDER BY CAST({score_col} AS DOUBLE) DESC) AS rk,
                                   COUNT(*) OVER (PARTITION BY CAST({date_col} AS DATE)) AS tot
                            FROM read_parquet('{str(parquet_file)}')
                            WHERE CAST({score_col} AS DOUBLE) IS NOT NULL
                              AND CAST({date_col} AS DATE) >= CAST(? AS DATE)
                              AND NOT (
                                  UPPER(CAST({sym_col} AS VARCHAR)) LIKE 'SH000%'
                                  OR UPPER(CAST({sym_col} AS VARCHAR)) LIKE 'SZ399%'
                                  OR UPPER(CAST({sym_col} AS VARCHAR)) LIKE 'SH900%'
                                  OR UPPER(CAST({sym_col} AS VARCHAR)) LIKE 'SZ200%'
                                  OR UPPER(CAST({sym_col} AS VARCHAR)) LIKE 'BJ%'
                              )
                        )
                        SELECT td, sc, rk, tot FROM d
                        WHERE code6 = ? ORDER BY td DESC
                        """,
                        [cutoff, code6],
                    ).fetchall()
                    items = [
                        {
                            "trade_date": str(r[0])[:10],
                            "fusion_score": float(r[1]),
                            "signal_side": None,
                            "score_rank": int(r[2]),
                            "total_in_market": int(r[3]),
                            "run_id": "",
                            "created_at": None,
                            "data_trade_date": None,
                            "signal_model_id": model_id,
                            "source": "pred_parquet",
                        }
                        for r in rows
                    ]
            finally:
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取模型 pred.parquet 分数历史失败 %s: %s", parquet_file, exc)

    if len(_PRED_HIST_CACHE) > 256:
        _PRED_HIST_CACHE.clear()
    _PRED_HIST_CACHE[cache_key] = (_time.time(), items)
    return items


async def _load_stock_pred_history(
    *, tenant_id: str, user_id: str, model_id: str | None, sym: str, cutoff: date
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """读模型目录 pred.parquet 的全量历史分数序列。

    model_id 为空时取用户设置的默认模型（个股终端下方分数曲线就是此路径，只展默认模型）；
    仅推理批次详情页等明确指定模型的调用方会传 model_id。
    Returns (items, model)；模型缺失/无文件/读空时 items 为 []，调用方回退 engine_signal_scores。
    """
    try:
        if model_id:
            model = await model_registry_service.get_model(
                tenant_id=tenant_id, user_id=user_id, model_id=model_id
            )
        else:
            model = await model_registry_service.get_default_model(
                tenant_id=tenant_id, user_id=user_id
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("分数曲线模型解析失败: %s", exc)
        return [], None

    storage_path = str((model or {}).get("storage_path") or "").strip()
    if not model or not storage_path:
        return [], None

    code6 = re.sub(r"[^0-9]", "", sym)
    if not code6:
        return [], None
    items = await asyncio.to_thread(
        _read_stock_pred_history,
        storage_path,
        code6,
        cutoff,
        str(model.get("model_id") or ""),
    )
    return (items or []), model


@router.get(
    "/inference/stock/{symbol}/history", summary="查询单只股票历史推理分数（用户态）"
)
async def get_stock_inference_history(
    symbol: str,
    days: int = Query(180, ge=7, le=7300, description="回溯天数"),
    model_id: str | None = Query(
        None, description="按模型过滤，缺省返回所有模型的最新批次"
    ),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """返回某只股票的历史模型分数（供 K 线下方分数曲线叠加）。

    主数据源为模型目录的 pred.parquet（训练生成的全量历史分数），不受每日推理批次影响；
    model_id 为空时取用户默认模型（个股终端下方曲线），显式传 model_id 时取该模型（推理批次详情）。
    无 pred.parquet 时才回退 engine_signal_scores 批次（按交易日去重取最新批次）。
    """
    tenant_id, user_id = _owner_scope(current_user)
    from datetime import timedelta as _td
    from backend.shared.stock_utils import StockCodeUtil

    sym = str(symbol).strip().upper()
    cutoff = date.today() - _td(days=days)

    # 归一化 symbol：兼容纯数字 / SH前缀 / suffix 三种格式
    norm = sym
    if "." not in norm and not norm.startswith(("SH", "SZ", "BJ")):
        norm = StockCodeUtil.to_suffix(norm)

    params: dict[str, Any] = {
        "sym": sym,
        "cutoff": cutoff,
        "tenant_id": tenant_id,
        "user_id": user_id,
    }
    model_filter_sql = ""
    if model_id:
        model_filter_sql = "AND e.run_id IN (SELECT run_id FROM qm_model_inference_runs WHERE model_id = :model_id)"
        params["model_id"] = model_id

    async with get_session(read_only=True) as session:
        # 性能：先用 (tenant_id, symbol, trade_date) 索引取该股每日最新一条（毫秒级），
        # 排名用相关子查询只统计所在 run 内的行（idx_ess_run_id），避免对全市场做窗口函数
        # （旧写法 CTE 对所有股票 RANK() 后才过滤 symbol，500 天要 20s，现 ~0.6s）。
        # 排名仍在同一批 run 内计算：同一天多个 run 各自内部排名，取最新批次那条。
        rows = (
            (
                await session.execute(
                    text(
                        f"""
                    WITH mine AS (
                        SELECT DISTINCT ON (e.trade_date)
                               e.trade_date, e.run_id, e.fusion_score, e.signal_side, e.created_at
                        FROM engine_signal_scores e
                        WHERE e.symbol = :sym
                          AND e.trade_date >= :cutoff
                          AND e.tenant_id = :tenant_id AND e.user_id = :user_id
                          {model_filter_sql}
                        ORDER BY e.trade_date, e.created_at DESC
                    )
                    SELECT m.trade_date, m.fusion_score, m.signal_side,
                           (SELECT COUNT(*) + 1 FROM engine_signal_scores e2
                            WHERE e2.run_id = m.run_id
                              AND e2.tenant_id = :tenant_id AND e2.user_id = :user_id
                              AND e2.fusion_score > m.fusion_score) AS score_rank,
                           m.run_id, m.created_at, r.data_trade_date, r.model_id AS signal_model_id
                    FROM mine m
                    LEFT JOIN qm_model_inference_runs r ON r.run_id = m.run_id
                    ORDER BY m.trade_date DESC
                    """
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )

    # 按交易日去重（同一天多批次只取最新 created_at 的一条）
    by_date: dict[str, dict[str, Any]] = {}
    for row in rows:
        # 日期口径统一为数据日 T（与 pred.parquet 一致）：engine_signal_scores
        # .trade_date 是信号生效日 T+1，run 记录里的 data_trade_date 才是 T。
        # 两套数据源切换（pred.parquet 主源 ↔ 批次回退）时曲线不再错位一天。
        d = str(row.get("data_trade_date") or row["trade_date"])[:10]
        if d not in by_date:
            by_date[d] = dict(row)
            by_date[d]["trade_date"] = d
            if by_date[d].get("data_trade_date") is not None:
                by_date[d]["data_trade_date"] = str(by_date[d]["data_trade_date"])
            if by_date[d].get("created_at") is not None:
                by_date[d]["created_at"] = by_date[d]["created_at"].isoformat()

    items = sorted(by_date.values(), key=lambda x: x["trade_date"], reverse=True)

    # ── 分数曲线锁定「用户默认模型」目录的 pred.parquet（训练生成的全量历史分数），
    # 不受每日推理批次影响，也不展示非默认模型；默认模型缺失/无 pred 文件时才回退
    # 上面的 engine_signal_scores 批次结果
    pred_items, pred_model = await _load_stock_pred_history(
        tenant_id=tenant_id, user_id=user_id, model_id=model_id, sym=sym, cutoff=cutoff
    )
    if pred_items:
        items = pred_items

    # 附带股票名称/板块/行业（从 stocks 表）
    stock_meta: dict[str, Any] = {}
    try:
        async with get_session(read_only=True) as s2:
            r2 = (
                (
                    await s2.execute(
                        text(
                            "SELECT name, industry, sector FROM stocks WHERE symbol = :sym"
                        ),
                        {"sym": norm},
                    )
                )
                .mappings()
                .first()
            )
        if r2:
            stock_meta = dict(r2)
    except Exception:  # pragma: no cover
        pass

    # 板块：按代码前缀（A股5大板块 + 北交所）
    code = re.sub(r"[^0-9]", "", sym)
    if code.startswith("688"):
        board = "科创板"
    elif code.startswith("30"):
        board = "创业板"
    elif code.startswith(("002", "003")):
        board = "中小板"
    elif code.startswith(("000", "001")):
        board = "深主板"
    elif code.startswith("60"):
        board = "沪主板"
    elif code.startswith(("4", "8", "9")):
        board = "北交所"
    else:
        board = "其他"

    # 曲线只展示单一模型（个股终端不传 model_id → 用户默认模型），不再返回历史涉及的多模型列表
    models: list[dict[str, Any]] = []
    if pred_model:
        pmeta = pred_model.get("metadata_json") or {}
        if not isinstance(pmeta, dict):
            pmeta = {}
        models.append(
            {
                "model_id": str(pred_model.get("model_id") or ""),
                "display_name": pmeta.get("display_name")
                or pmeta.get("model_name")
                or "",
                "is_default": bool(pred_model.get("is_default")),
                "train_start": str(pmeta.get("train_start") or "")[:10],
                "train_end": str(pmeta.get("train_end") or "")[:10],
            }
        )

    return {
        "symbol": sym,
        "normalized_symbol": norm,
        "name": stock_meta.get("name") or "",
        "industry": stock_meta.get("industry") or "",
        "board": board,
        "total": len(items),
        "items": items,
        "models": models,
        "score_source": "pred_parquet" if pred_items else "inference_runs",
    }


@router.get("/inference/settings/{model_id}", summary="获取模型自动推理设置（用户态）")
async def get_model_inference_settings(
    model_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    settings = await model_inference_persistence.get_settings(
        tenant_id=tenant_id,
        user_id=user_id,
        model_id=model_id,
    )
    if settings.get("last_run_json") and not settings.get("last_run"):
        settings["last_run"] = settings["last_run_json"]
    settings["next_run"] = (
        _render_next_run(settings.get("next_run_at"))
        if settings.get("next_run_at")
        else settings.get("next_run")
    )
    return settings


def _read_latest_run_id_from_redis(latest_key: str) -> str:
    """双读最新推理 run_id：哨兵 Redis + 独立 stream Redis，任意命中即返回。

    与 EngineSignalStreamPublisher.mark_latest_run() 双写对应，避免
    SIGNAL_STREAM_REDIS_HOST 分裂导致的调度台空白。
    """
    # 1) 哨兵 Redis（api 默认）
    try:
        sentinel = get_redis_sentinel_client()
        val = sentinel.get(latest_key)
        if val:
            decoded = val.decode("utf-8") if isinstance(val, (bytes, bytearray)) else str(val)
            if decoded.strip():
                return decoded.strip()
    except Exception as exc:
        logger.debug("读取哨兵 Redis latest 失败: %s", exc)

    # 2) 独立 stream Redis（与 engine 写入侧一致）
    stream_host = str(os.getenv("SIGNAL_STREAM_REDIS_HOST", "")).strip()
    if stream_host:
        try:
            from redis import Redis as _Redis  # noqa: PLC0415

            stream_client = _Redis(
                host=stream_host,
                port=int(os.getenv("SIGNAL_STREAM_REDIS_PORT", "6379")),
                db=int(os.getenv("SIGNAL_STREAM_REDIS_DB", "0")),
                password=os.getenv("SIGNAL_STREAM_REDIS_PASSWORD") or None,
                decode_responses=False,
                socket_timeout=2.0,
                socket_connect_timeout=2.0,
            )
            val2 = stream_client.get(latest_key)
            if val2:
                decoded2 = val2.decode("utf-8") if isinstance(val2, (bytes, bytearray)) else str(val2)
                if decoded2.strip():
                    return decoded2.strip()
        except Exception as exc:
            logger.debug("读取 stream Redis latest 失败: %s", exc)
    return ""


async def _fallback_latest_run_from_db(tenant_id: str, user_id: str) -> dict[str, Any] | None:
    """Redis 缺失/TTL 过期时，从 DB 回退最新 completed 推理批次。

    模拟交易调度台必须始终能回显“最新可用推理”，不能因 Redis 丢失就空白。
    """
    try:
        # 复用 list_runs 的 per-date 去重口径，取最新一天且 signals_count 最大的批次
        res = await model_inference_persistence.list_runs(
            tenant_id=tenant_id,
            user_id=user_id,
            status="completed",
            page=1,
            page_size=1,
        )
        items = res.get("items") or []
        if items:
            return items[0]
    except Exception as exc:
        logger.warning("DB 回退查询最新推理失败: %s", exc)
    # 兜底：直接查 qm_model_inference_runs 最近一条 completed（不按去重）
    try:
        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT * FROM qm_model_inference_runs
                        WHERE tenant_id = :tenant_id AND user_id = :user_id AND status = 'completed'
                        ORDER BY prediction_trade_date DESC, created_at DESC
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant_id, "user_id": user_id},
                )
            ).mappings().first()
            if row:
                # 复用 persistence 的行转换，避免时区/日期格式漂移
                from backend.services.engine.services.model_inference_persistence import model_inference_persistence as _p

                return _p._row_to_run(dict(row))
    except Exception as exc:
        logger.warning("DB 直查最新推理回退失败: %s", exc)
    return None


@router.get("/inference/latest", summary="获取当前生效推理批次（用户态）")
async def get_model_inference_latest(
    model_id: str | None = Query(
        None, description="模型ID，可选，用于检查是否与当前生效模型匹配"
    ),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    latest_key = f"qm:signal:latest:{tenant_id}:{user_id}"
    latest_run_id = _read_latest_run_id_from_redis(latest_key)

    run: dict[str, Any] | None = None
    if latest_run_id:
        run = await model_inference_persistence.get_run(
            run_id=latest_run_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        # Redis 指向的 run 可能已被清理/过期，自动回退 DB
        if not run:
            logger.warning("Redis 指向的 run_id 不存在，已自动回退 DB: key=%s run_id=%s", latest_key, latest_run_id)
            run = await _fallback_latest_run_from_db(tenant_id, user_id)
            if run:
                latest_run_id = str(run.get("run_id") or "")
                # 顺手回补 Redis，避免下次仍 miss
                try:
                    get_redis_sentinel_client().set(latest_key, latest_run_id, ex=86400)
                except Exception:
                    pass
    else:
        # Redis 完全缺失/TTL 过期 -> DB 回退，保证调度台不空白
        run = await _fallback_latest_run_from_db(tenant_id, user_id)
        if run:
            latest_run_id = str(run.get("run_id") or "")
            try:
                get_redis_sentinel_client().set(latest_key, latest_run_id, ex=86400)
            except Exception:
                pass
            # 同步到独立 stream Redis（如有）
            stream_host = str(os.getenv("SIGNAL_STREAM_REDIS_HOST", "")).strip()
            if stream_host:
                try:
                    from redis import Redis as _Redis  # noqa: PLC0415

                    _rc = _Redis(
                        host=stream_host,
                        port=int(os.getenv("SIGNAL_STREAM_REDIS_PORT", "6379")),
                        db=int(os.getenv("SIGNAL_STREAM_REDIS_DB", "0")),
                        password=os.getenv("SIGNAL_STREAM_REDIS_PASSWORD") or None,
                        decode_responses=False,
                    )
                    _rc.set(latest_key, latest_run_id, ex=86400)
                except Exception:
                    pass

    if not run:
        return {
            "latest_key": latest_key,
            "run_id": latest_run_id or "",
            "model_id": "",
            "prediction_trade_date": "",
            "target_date": "",
            "status": "",
            "updated_at": "",
            "matched_model": False if model_id else None,
        }

    target_date = str(run.get("prediction_trade_date") or run.get("target_date") or "")
    latest_model_id = str(run.get("model_id") or "")
    matched_model = None if not model_id else (str(model_id) == latest_model_id)
    return {
        "latest_key": latest_key,
        "run_id": str(run.get("run_id") or latest_run_id),
        "model_id": latest_model_id,
        "prediction_trade_date": target_date,
        "target_date": target_date,
        "status": str(run.get("status") or ""),
        "updated_at": str(run.get("updated_at") or run.get("created_at") or ""),
        "matched_model": matched_model,
    }


@router.put("/inference/settings/{model_id}", summary="更新模型自动推理设置（用户态）")
async def update_model_inference_settings(
    model_id: str,
    payload: InferenceSettingsRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    if payload.schedule_time is not None:
        raw = str(payload.schedule_time).strip()
        if raw and not re.match(r"^\d{2}:\d{2}$", raw):
            raise HTTPException(status_code=422, detail="schedule_time 格式应为 HH:MM")
    return await model_inference_persistence.update_settings(
        tenant_id=tenant_id,
        user_id=user_id,
        model_id=model_id,
        enabled=bool(payload.enabled),
        schedule_time=payload.schedule_time,
    )


@router.post(
    "/training-runs/{run_id}/complete", summary="训练完成回调（用户态内部接口）"
)
async def training_complete_callback(
    run_id: str,
    result: dict[str, Any],
    x_internal_call_secret: str = Header(default="", alias="X-Internal-Call-Secret"),
):
    return await complete_training_run(run_id, result, x_internal_call_secret)


@router.post("/ensemble/create", summary="创建多模型融合模型（用户态）")
async def create_ensemble_model(
    payload: EnsembleCreateRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """将多个已训练模型融合为一个持久化融合模型。

    融合模型像普通模型一样注册、推理、选股、回测。支持权重策略：
      - equal   等权
      - icir    按源模型 Val Rank ICIR 归一化加权
      - manual  手动指定权重（自动归一化到和为 1）
    """
    if payload.weight_strategy not in ("equal", "icir", "manual", "recent_ic"):
        raise HTTPException(
            status_code=422,
            detail="weight_strategy 应为 equal / icir / manual / recent_ic",
        )
    if payload.weight_strategy == "manual" and not payload.manual_weights:
        raise HTTPException(
            status_code=422, detail="manual 策略必须提供 manual_weights"
        )
    if payload.fusion_strategy not in (
        "linear",
        "majority_vote",
        "periodic_hierarchy",
        "confidence_gate",
    ):
        raise HTTPException(
            status_code=422,
            detail="fusion_strategy 应为 linear / majority_vote / periodic_hierarchy / confidence_gate",
        )

    tenant_id, user_id = _owner_scope(current_user)
    try:
        return await model_registry_service.register_ensemble_model(
            tenant_id=tenant_id,
            user_id=user_id,
            source_model_ids=payload.source_model_ids,
            display_name=payload.display_name,
            weight_strategy=payload.weight_strategy,
            manual_weights=payload.manual_weights,
            fusion_strategy=payload.fusion_strategy,
            strategy_config=payload.strategy_config,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
