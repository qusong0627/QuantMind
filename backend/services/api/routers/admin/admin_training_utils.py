import asyncio
import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from docker import DockerClient
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text

from backend.services.api.routers.admin.db import TrainingJobRecord
from backend.services.api.training_explain import normalize_explain
from backend.services.api.user_app.middleware.auth import require_admin
from backend.services.engine.training.orchestrator_base import get_orchestrator, REGISTRY
from backend.services.engine.training.local_docker_orchestrator import LocalDockerOrchestrator
from backend.services.engine.training.training_log_stream import TrainingRunLogStream
from backend.services.engine.data_platform.quantdb_factor_reader import QuantDBFactorReader
from backend.shared.database_manager_v2 import get_session
from backend.shared.model_registry import model_registry_service
from backend.shared.training.request import (
    ALLOWED_TARGET_MODE as _ALLOWED_TARGET_MODE,
    clamp_int as _clamp_int,
    coerce_float as _coerce_float,
    parse_date as _parse_date,
    resolve_market as _resolve_market,
)

router = APIRouter(dependencies=[Depends(require_admin)])  # 路由器级认证兜底
logger = logging.getLogger(__name__)
_FEATURE_CATALOG_FALLBACK = Path(os.getcwd()) / "config" / "features" / "model_training_feature_catalog_v1.json"
_TREE_MODEL_TYPES = {"lightgbm", "xgboost", "catboost", "linear", "random_forest"}
_DL_MODEL_TYPES = {"gru", "lstm", "alstm", "transformer", "tabnet", "tcn", "nativetft", "mlp", "hybrid_gru_tree"}
# 市场 → exchange_calendars 日历名。CRYPTO 为 7x24 无休市，不在此映射中。
_MARKET_TO_XCAL = {"CN": "XSHG", "US": "XNYS", "HK": "XHKG"}


# _clamp_int 下沉至 backend.shared.training.request（单源），此处 import 复用。


def _shift_trading_days_back(anchor: datetime, n_days: int, market: str) -> tuple[datetime, bool]:
    """从 anchor 往前数 n_days 个交易日，返回 (结果日期, 是否用了交易日历)。

    label 是 close(T+N)/close(T)-1，N 计的是交易日，因此 gap 必须按交易日算：
    10 个日历日只夹约 6 个交易日，会让 train 尾部的 label 窗口伸进 valid 区间。
    CRYPTO 或日历不可用时退化为日历日（返回 False，调用方据此提示用户）。
    """
    cal_name = _MARKET_TO_XCAL.get(str(market or "CN").upper())
    if not cal_name:
        return anchor - timedelta(days=n_days), False
    try:
        import exchange_calendars as xcals

        cal = xcals.get_calendar(cal_name)
        # 往前取足够长的窗口（含周末与长假，2 倍 + 30 天足够覆盖春节/国庆）
        lookback = max(n_days * 2 + 30, 45)
        sessions = cal.sessions_in_range(
            (anchor - timedelta(days=lookback)).strftime("%Y-%m-%d"),
            anchor.strftime("%Y-%m-%d"),
        )
        # 只保留严格早于 anchor 的交易日，取倒数第 n_days 个
        prior = [s for s in sessions if s.strftime("%Y-%m-%d") < anchor.strftime("%Y-%m-%d")]
        if len(prior) < n_days:
            return anchor - timedelta(days=n_days), False
        return datetime.strptime(prior[-n_days].strftime("%Y-%m-%d"), "%Y-%m-%d"), True
    except Exception as exc:
        logger.warning("trading-day gap unavailable (market=%s): %s", market, exc)
        return anchor - timedelta(days=n_days), False
_TRAINING_BASE_FEATURES = [
    "mom_ret_1d",
    "mom_ret_5d",
    "mom_ret_20d",
    "liq_volume",
    "liq_amount",
    "fun_turnover_1",
]
_training_log_stream = TrainingRunLogStream()
DEFAULT_TRAINING_IMAGE = (
    os.getenv("TRAINING_IMAGE") or "quantmind-oss:latest"
).strip()


class _SetDefaultModelRequest(BaseModel):
    model_id: str


class _SetStrategyBindingRequest(BaseModel):
    model_id: str


def _resolve_admin_scope(
    *,
    current_user: dict[str, Any],
    tenant_id: str | None,
    user_id: str | None,
) -> tuple[str, str]:
    resolved_tenant = str(tenant_id or current_user.get("tenant_id") or "default").strip() or "default"
    resolved_user = str(user_id or current_user.get("user_id") or current_user.get("sub") or "").strip()
    if not resolved_user:
        raise HTTPException(status_code=422, detail="user_id is required")
    return resolved_tenant, resolved_user


# _parse_date / _coerce_float 下沉至 backend.shared.training.request（单源），此处 import 复用。


def _feature_market_declarations() -> dict[str, list[str]]:
    """从文件目录构建 {feature_key: [market, ...]} 映射，供市场过滤使用。

    catalog 中每个特征带 "markets" 数组；未收录于 catalog 的特征（如 DB 独有）
    返回空列表，表示不限制市场（向后兼容）。
    """
    if not _FEATURE_CATALOG_FALLBACK.exists():
        return {}
    try:
        raw = json.loads(_FEATURE_CATALOG_FALLBACK.read_text(encoding="utf-8"))
    except Exception:
        return {}

    mapping: dict[str, list[str]] = {}
    categories = raw.get("categories") if isinstance(raw, dict) else []
    if not isinstance(categories, list):
        return {}
    for category in categories:
        features = category.get("features") if isinstance(category, dict) else []
        if not isinstance(features, list):
            continue
        for feature in features:
            if not isinstance(feature, dict):
                continue
            key = str(feature.get("key") or "").strip()
            if not key:
                continue
            markets = feature.get("markets")
            if isinstance(markets, list):
                declared = [str(m).upper() for m in markets if str(m).strip()]
            else:
                declared = []
            mapping[key] = declared
    return mapping


def _market_allows_feature(market: str | None, declared: list[str]) -> bool:
    """特征是否适用于目标市场。

    declared 为空（catalog 未声明/未收录）时放行，避免误伤 DB 独有特征。
    """
    market_upper = str(market or "").upper()
    if not market_upper:
        return True
    if not declared:
        return True
    return market_upper in declared


def _load_allowed_features_from_file(market: str | None = None) -> list[str]:
    if not _FEATURE_CATALOG_FALLBACK.exists():
        return []
    try:
        raw = json.loads(_FEATURE_CATALOG_FALLBACK.read_text(encoding="utf-8"))
    except Exception:
        return []

    keys: list[str] = []
    categories = raw.get("categories") if isinstance(raw, dict) else []
    if not isinstance(categories, list):
        return []
    for category in categories:
        features = category.get("features") if isinstance(category, dict) else []
        if not isinstance(features, list):
            continue
        for feature in features:
            if not isinstance(feature, dict):
                continue
            if feature.get("enabled", True) is False:
                continue
            key = str(feature.get("key") or "").strip()
            if not key or key in keys:
                continue
            markets = feature.get("markets")
            declared = (
                [str(m).upper() for m in markets if str(m).strip()]
                if isinstance(markets, list)
                else []
            )
            if not _market_allows_feature(market, declared):
                continue
            keys.append(key)
    return keys


async def _load_allowed_features_from_db(market: str | None = None) -> list[str]:
    sql = text(
        """
        WITH active_version AS (
            SELECT version_id
            FROM qm_feature_set_version
            WHERE status = 'active'
            ORDER BY effective_at DESC, created_at DESC
            LIMIT 1
        )
        SELECT i.feature_key
        FROM qm_feature_set_item i
        JOIN active_version v ON v.version_id = i.version_id
        WHERE COALESCE(i.enabled, TRUE) = TRUE
        ORDER BY i.order_no ASC
        """
    )
    try:
        async with get_session(read_only=True) as session:
            rows = (await session.execute(sql)).mappings().all()
    except Exception:
        return []
    # DB 的 qm_feature_set_item 无 market 字段，用文件 catalog 的 markets 声明过滤
    market_map = _feature_market_declarations()
    keys: list[str] = []
    for row in rows:
        key = str(row.get("feature_key") or "").strip()
        if not key or key in keys:
            continue
        if not _market_allows_feature(market, market_map.get(key, [])):
            continue
        keys.append(key)
    return keys


async def _load_allowed_features(market: str | None = None) -> list[str]:
    db_keys = await _load_allowed_features_from_db(market=market)
    if db_keys:
        return db_keys
    return _load_allowed_features_from_file(market=market)


# _normalize_context / _resolve_market / _normalize_prediction_mode 已下沉至
# backend.shared.training.request（单源），此处 import 复用；context 清洗走 req.context。


def _normalize_payload(payload: dict[str, Any], allowed_features: list[str]) -> dict[str, Any]:
    from backend.shared.training.request import TrainingRequest

    # 收尾 2：纯输入校验收敛进 TrainingRequest（422 与现状逐字相同）；
    # 本函数只做 DB 耦合校验与推导装配。
    req = TrainingRequest.validate_request(payload)

    if allowed_features:
        allowed_set = set(allowed_features)
        invalid = [feature for feature in req.features if feature not in allowed_set]
        if invalid:
            sample = ", ".join(invalid[:8])
            raise HTTPException(
                status_code=422,
                detail=f"Unknown features: {sample}. Please refresh feature catalog and retry.",
            )

    # 多模型支持：model_types[0] 作为主模型（向后兼容）
    model_type = req.model_type
    model_types = req.model_types
    if model_types:
        model_type = model_types[0]

    # LightGBM max_depth=-1 convention is invalid for XGBoost; strip it
    xgb_params = dict(req.xgb_params)
    if isinstance(xgb_params.get("max_depth"), (int, float)) and xgb_params["max_depth"] < 0:
        xgb_params = {k: v for k, v in xgb_params.items() if k != "max_depth"}

    # ── WFA 稳定性诊断配置（可选） ──
    wfa_config = None
    raw_wfa = req.wfa
    if raw_wfa:
        if not isinstance(raw_wfa, dict):
            raise HTTPException(status_code=422, detail="wfa must be an object")
        wfa_strategy = str(raw_wfa.get("strategy", "rolling")).strip().lower()
        if wfa_strategy not in ("rolling", "expanding"):
            raise HTTPException(status_code=422, detail="wfa.strategy must be one of: rolling, expanding")
        wfa_enabled = bool(raw_wfa.get("enabled", True))
        wfa_config = {
            "enabled": wfa_enabled,
            "strategy": wfa_strategy,
            "n_windows": _clamp_int(raw_wfa.get("n_windows"), 4, 1, 12),
            "train_years": _clamp_int(raw_wfa.get("train_years"), 3, 1, 8),
            "val_months": _clamp_int(raw_wfa.get("val_months"), 12, 1, 36),
            "step_months": _clamp_int(raw_wfa.get("step_months"), 12, 1, 36),
            "start": str(raw_wfa.get("start") or "").strip(),
            "max_train_end": str(raw_wfa.get("max_train_end") or "").strip(),
        }

    # display/日期/数值/特征/params 校验已收敛进 TrainingRequest；多周期推导见下。


    target_horizon_days = int(payload.get("target_horizon_days", 1))
    if not (1 <= target_horizon_days <= 30):
        raise HTTPException(status_code=422, detail="target_horizon_days must be between 1 and 30")

    # 多周期训练：一次训练产出多个周期的模型（T+1/3/5/10…）
    horizons: list[int] | None = None
    raw_horizons = payload.get("horizons")
    if raw_horizons is not None:
        if not isinstance(raw_horizons, list) or not raw_horizons:
            raise HTTPException(status_code=422, detail="horizons must be a non-empty array of integers")
        horizons = []
        for h in raw_horizons:
            try:
                hv = int(h)
            except Exception:
                raise HTTPException(status_code=422, detail=f"horizons contains non-integer value: {h}")
            if not (1 <= hv <= 30):
                raise HTTPException(status_code=422, detail=f"horizons value must be between 1 and 30: {hv}")
            if hv not in horizons:
                horizons.append(hv)
        horizons.sort()
        if len(horizons) < 2:
            raise HTTPException(status_code=422, detail="horizons must contain at least 2 distinct periods")
        # 多周期主显示周期取第一个
        target_horizon_days = horizons[0]

    target_mode = str(payload.get("target_mode", "return")).strip().lower()
    if target_mode not in _ALLOWED_TARGET_MODE:
        raise HTTPException(status_code=422, detail="target_mode must be one of: return, classification")

    label_formula = str(payload.get("label_formula") or "").strip()
    effective_trade_date = str(payload.get("effective_trade_date") or "").strip()
    if effective_trade_date:
        _parse_date(effective_trade_date, "effective_trade_date")

    training_window = str(payload.get("training_window") or "").strip()

    raw_feature_categories = payload.get("feature_categories", []) or []
    feature_categories: list[str] = []
    if isinstance(raw_feature_categories, list):
        for item in raw_feature_categories:
            val = str(item).strip()
            if val and val not in feature_categories:
                feature_categories.append(val)

    context = req.context
    explain = normalize_explain(payload.get("explain"))

    normalized: dict[str, Any] = {
        "job_name": req.job_name,
        "display_name": req.display_name,
        "model_type": model_type,
        "train_start": req.train_start,
        "train_end": req.train_end,
        "val_ratio": req.val_ratio,
        "num_boost_round": req.num_boost_round,
        "early_stopping_rounds": req.early_stopping_rounds,
        "features": req.features,
        "feature_categories": feature_categories,
        "target_horizon_days": target_horizon_days,
        "target_mode": req.target_mode,
        "label_formula": req.label_formula,
        "effective_trade_date": req.effective_trade_date,
        "training_window": req.training_window,
        "context": context,
        "explain": explain,
        "lgb_params": req.lgb_params,
        "xgb_params": xgb_params,
        "catboost_params": req.catboost_params,
        "dl_params": req.dl_params,
        "ensemble": req.ensemble,
        # 分位推理模式透传：此前被白名单剥掉，orchestrator 收不到
        # prediction_mode 永远回落 point，导致训练时选了「收益率分位推理」
        # 但模型 metadata 始终是 point，推理中心提示未启用分位推理。
        "prediction_mode": req.prediction_mode,
    }
    # Stacking 集成参数 + Optuna 超参搜索 + 截面预处理（显式透传）
    if "n_folds" in payload:
        normalized["n_folds"] = _clamp_int(payload.get("n_folds"), 3, 2, 10)
    if "meta_alpha" in payload:
        try:
            normalized["meta_alpha"] = float(payload.get("meta_alpha"))
        except (TypeError, ValueError):
            pass
    if isinstance(payload.get("optuna"), dict):
        normalized["optuna"] = {
            "enabled": bool(payload["optuna"].get("enabled", False)),
            "n_trials": _clamp_int(payload["optuna"].get("n_trials"), 20, 5, 100),
        }
    # 训练时是否停掉其他 Docker 容器（前端开关透传）。
    # 此前未透传，orchestrator 收到 None 回落到环境变量默认 true，导致开关无效。
    if "pause_others" in payload:
        normalized["pause_others"] = bool(payload["pause_others"])
    if isinstance(payload.get("preprocessing"), dict):
        normalized["preprocessing"] = {
            "enabled": bool(payload["preprocessing"].get("enabled", False)),
            "winsor": bool(payload["preprocessing"].get("winsor", True)),
        }
    # 因子筛选配置透传：此前被白名单剥掉，orchestrator 收不到 factor_selection，
    # 永远走默认 top-80 筛选，用户显式指定的 n_top（如 L2 特征集 150）失效
    if isinstance(payload.get("factor_selection"), dict):
        raw_fs = payload["factor_selection"]
        normalized["factor_selection"] = {
            "method": str(raw_fs.get("method") or "").strip().lower(),
            "n_top": _clamp_int(raw_fs.get("n_top"), 150, 10, 300),
            "ic_threshold": float(raw_fs.get("ic_threshold", 0.01)),
            "icir_threshold": float(raw_fs.get("icir_threshold", 0.15)),
            "correlation_threshold": float(raw_fs.get("correlation_threshold", 0.9)),
        }
    if "auto_feature_filter" in payload:
        # 注意：不能用 `payload.get(...) or "true"` —— False 会被 falsy 兜底吞掉
        # 变成 "true"，导致用户关闭筛选后编排器仍注入 factor_selection。
        raw_aff = payload.get("auto_feature_filter")
        normalized["auto_feature_filter"] = str(
            raw_aff if raw_aff is not None else "true"
        ).strip().lower()
    if horizons:
        normalized["horizons"] = horizons
    # 训练时长预算（分钟），默认 120
    normalized["max_time_minutes"] = _clamp_int(payload.get("max_time_minutes"), 120, 10, 1440)
    if wfa_config:
        normalized["wfa"] = wfa_config
    if model_types and len(model_types) > 1:
        normalized["model_types"] = model_types
    if payload.get("factor_source"):
        normalized["factor_source"] = str(payload["factor_source"])
        normalized["factor_catalog_version"] = str(payload.get("factor_catalog_version") or "")
        normalized["factor_field_sources"] = dict(payload.get("factor_field_sources") or {})
        normalized["factor_schema_hash"] = str(payload.get("factor_schema_hash") or "")
        normalized["factor_catalog_published_at"] = str(payload.get("factor_catalog_published_at") or "")
        normalized["factor_coverage"] = dict(payload.get("factor_coverage") or {})

    # 训练起止（split gap 推导用； TrainingRequest 已校验可解析，此处不再抛错）
    dt_train_start = _parse_date(req.train_start, "train_start")
    dt_train_end = _parse_date(req.train_end, "train_end")

    explicit_fields = ["valid_start", "valid_end", "test_start", "test_end"]
    has_explicit_split = any(payload.get(k) for k in explicit_fields)
    if has_explicit_split:
        missing = [k for k in explicit_fields if not payload.get(k)]
        if missing:
            raise HTTPException(status_code=422, detail=f"Explicit split requires fields: {missing}")

        valid_start = str(payload["valid_start"]).strip()
        valid_end = str(payload["valid_end"]).strip()
        test_start = str(payload["test_start"]).strip()
        test_end = str(payload["test_end"]).strip()

        dt_valid_start = _parse_date(valid_start, "valid_start")
        dt_valid_end = _parse_date(valid_end, "valid_end")
        dt_test_start = _parse_date(test_start, "test_start")
        dt_test_end = _parse_date(test_end, "test_end")

        # 自动调整数据间隔(Gap)以防数据泄漏，提升用户体验
        # 信号在 T 日生成、T+1 执行；若预测未来 H 天收益，Train 结束与
        # Val 开始之间至少应留下 H+1 天，避免执行价/未来价格跨入下一分段。
        # 不再阻断(422)，而是由后端自动向后平移日期。
        gap_days = int(normalized.get("target_horizon_days") or 1) + 1

        # 记录修正通知
        adjustment_notices = []

        earliest_valid_start = dt_train_end + timedelta(days=gap_days)
        if dt_valid_start < earliest_valid_start:
            old_val = valid_start
            dt_valid_start = earliest_valid_start
            valid_start = str(dt_valid_start.date())
            adjustment_notices.append(f"valid_start 从 {old_val} 自动修正为 {valid_start} (由于预测跨度 {gap_days}d)")

        earliest_test_start = dt_valid_end + timedelta(days=gap_days)
        if dt_test_start < earliest_test_start:
            old_val = test_start
            dt_test_start = earliest_test_start
            test_start = str(dt_test_start.date())
            adjustment_notices.append(f"test_start 从 {old_val} 自动修正为 {test_start} (由于预测跨度 {gap_days}d)")

        if not (dt_train_start <= dt_train_end < dt_valid_start <= dt_valid_end < dt_test_start <= dt_test_end):
            raise HTTPException(
                status_code=422,
                detail=f"Date order must satisfy train_start <= train_end < valid_start <= valid_end < test_start <= test_end. {' '.join(adjustment_notices)}",
            )

        normalized.update(
            {
                "valid_start": valid_start,
                "valid_end": valid_end,
                "test_start": test_start,
                "test_end": test_end,
                "system_notices": adjustment_notices,
            }
        )

    required_artifacts = payload.get(
        "required_artifacts",
        ["model.lgb", "pred.pkl", "metadata.json", "config.yaml", "result.json"],
    )
    if not isinstance(required_artifacts, list) or not all(isinstance(x, str) for x in required_artifacts):
        raise HTTPException(status_code=422, detail="required_artifacts must be a string array")
    normalized["required_artifacts"] = [x.strip() for x in required_artifacts if x.strip()]

    normalized["deploy_to_production"] = bool(payload.get("deploy_to_production", False))

    generated_at = str(payload.get("generated_at") or "").strip()
    if generated_at:
        normalized["generated_at"] = generated_at

    return normalized


async def _resolve_quantdb_factor_payload(payload: dict[str, Any], market: str) -> tuple[dict[str, Any], list[str]]:
    """Validate an immutable published mapping and pin logical fields to raw columns."""
    source = str(payload.get("factor_source") or "").strip()
    if not source:
        return payload, await _load_allowed_features(market=market)
    version_id = str(payload.get("factor_catalog_version") or "").strip()
    if not version_id:
        raise HTTPException(status_code=422, detail="factor_catalog_version is required for QuantDB direct training")
    try:
        status = QuantDBFactorReader(market=market).assert_ready(
            source,
            start=str(payload.get("train_start") or "") or None,
            end=str(payload.get("test_end") or payload.get("valid_end") or payload.get("train_end") or "") or None,
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"QuantDB factor source is not ready: {exc}") from exc

    async with get_session() as session:
        version = (await session.execute(text("""
            SELECT version_id, published_at FROM qm_training_factor_catalog_version
            WHERE version_id = :version_id AND status = 'published'
              AND source_dataset = :source AND market = :market
        """), {"version_id": version_id, "source": source, "market": market})).first()
        if not version:
            raise HTTPException(status_code=422, detail="factor_catalog_version is not the active published source version")
        rows = (await session.execute(text("""
            SELECT feature_key, source_column FROM qm_training_factor_mapping
            WHERE version_id = :version_id AND source_dataset = :source AND enabled
        """), {"version_id": version_id, "source": source})).mappings().all()
    mapping = {str(row["feature_key"]): str(row["source_column"]) for row in rows}
    requested = [str(item).strip() for item in (payload.get("features") or []) if str(item).strip()]
    invalid = [feature for feature in requested if feature not in mapping]
    if invalid:
        raise HTTPException(status_code=422, detail=f"Features are not enabled in pinned QuantDB catalog: {', '.join(invalid[:8])}")
    if not requested:
        raise HTTPException(status_code=422, detail="At least one enabled QuantDB factor must be selected")
    pinned = dict(payload)
    pinned["factor_field_sources"] = {feature: mapping[feature] for feature in requested}
    pinned["factor_schema_hash"] = status.schema_hash
    pinned["factor_catalog_published_at"] = str(version.published_at or "")
    pinned["factor_coverage"] = {"min_date": status.min_date, "max_date": status.max_date}
    return pinned, list(mapping)


def _normalize_artifacts(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, dict):
        raw = raw.get("items") or raw.get("files") or []
    if not isinstance(raw, list):
        return []

    artifacts: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            name = item.strip()
            if name:
                artifacts.append({"name": name})
            continue
        if not isinstance(item, dict):
            continue

        name = str(item.get("name") or item.get("filename") or item.get("file") or "").strip()
        if not name:
            continue
        artifact: dict[str, str] = {"name": name}
        url = str(item.get("url") or "").strip()
        key = str(item.get("key") or item.get("cos_key") or "").strip()
        if url:
            artifact["url"] = url
        if key:
            artifact["key"] = key
        artifacts.append(artifact)

    return artifacts


def _extract_metrics(raw: dict[str, Any]) -> dict[str, dict[str, float]] | None:
    metrics = raw.get("metrics")
    if isinstance(metrics, dict):
        normalized: dict[str, dict[str, float]] = {}
        for stage in ("train", "val", "test"):
            stage_metrics = metrics.get(stage)
            if not isinstance(stage_metrics, dict):
                return None
            rmse = _coerce_float(stage_metrics.get("rmse"))
            auc = _coerce_float(stage_metrics.get("auc"))
            if rmse is None or auc is None:
                return None
            normalized[stage] = {"rmse": rmse, "auc": auc}
        return normalized

    train_rmse = _coerce_float(raw.get("train_rmse", raw.get("rmse")))
    train_auc = _coerce_float(raw.get("train_auc", raw.get("auc")))
    val_rmse = _coerce_float(raw.get("val_rmse"))
    val_auc = _coerce_float(raw.get("val_auc"))
    test_rmse = _coerce_float(raw.get("test_rmse"))
    test_auc = _coerce_float(raw.get("test_auc"))
    if None in (train_rmse, train_auc, val_rmse, val_auc, test_rmse, test_auc):
        return None

    return {
        "train": {"rmse": float(train_rmse), "auc": float(train_auc)},
        "val": {"rmse": float(val_rmse), "auc": float(val_auc)},
        "test": {"rmse": float(test_rmse), "auc": float(test_auc)},
    }


def _build_default_metadata(request_payload: dict[str, Any], run_id: str) -> dict[str, Any]:
    context = request_payload.get("context") if isinstance(request_payload.get("context"), dict) else {}
    lgb_params = request_payload.get("lgb_params") if isinstance(request_payload.get("lgb_params"), dict) else {}
    features = request_payload.get("features") if isinstance(request_payload.get("features"), list) else []
    submitted_features = [str(item).strip() for item in features if str(item).strip()]
    auto_appended_features = [feature for feature in _TRAINING_BASE_FEATURES if feature not in submitted_features]
    feature_categories = (
        request_payload.get("feature_categories")
        if isinstance(request_payload.get("feature_categories"), list)
        else []
    )
    display_name = str(
        request_payload.get("display_name")
        or request_payload.get("job_name")
        or run_id
    ).strip() or run_id

    return {
        "model_id": run_id,
        "model_name": display_name,
        "display_name": display_name,
        "target_horizon_days": int(request_payload.get("target_horizon_days") or 1),
        "target_mode": str(request_payload.get("target_mode") or "return"),
        "label_formula": str(request_payload.get("label_formula") or ""),
        "training_window": str(request_payload.get("training_window") or ""),
        "feature_count": len(features),
        "requested_feature_count": len(submitted_features),
        "requested_features": submitted_features,
        "auto_appended_feature_count": len(auto_appended_features),
        "auto_appended_features": auto_appended_features,
        "feature_categories": [str(x) for x in feature_categories if str(x).strip()],
        "benchmark": str(context.get("benchmark") or "SH000300"),
        "objective": str(lgb_params.get("objective") or "regression"),
        "metric": str(lgb_params.get("metric") or "l2"),
        "generated_at": str(
            request_payload.get("generated_at")
            or datetime.now(timezone.utc).isoformat()
        ),
    }


def _normalize_training_result_payload(
    result: dict[str, Any],
    request_payload: dict[str, Any],
    run_id: str,
    status: str,
) -> tuple[dict[str, Any], str | None]:
    raw = result if isinstance(result, dict) else {}
    metadata = _build_default_metadata(request_payload, run_id)
    incoming_meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    metadata.update({k: v for k, v in incoming_meta.items() if v is not None})

    default_summary_status = "进行中"
    default_summary_message = "训练任务执行中。"
    if status == "completed":
        default_summary_status = "训练完成"
        default_summary_message = "训练流程执行完成"
    elif status == "failed":
        default_summary_status = "训练失败"
        default_summary_message = "训练任务失败"

    summary_raw = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
    summary_status = str(summary_raw.get("status") or default_summary_status)
    summary_message = str(
        summary_raw.get("message")
        or summary_raw.get("notes")
        or raw.get("message")
        or default_summary_message
    )

    metrics = _extract_metrics(raw)
    artifacts = _normalize_artifacts(raw.get("artifacts") or raw.get("files") or raw.get("required_artifacts"))

    error_text = str(raw.get("error") or "").strip()
    validation_error: str | None = None
    model_registration = raw.get("model_registration") if isinstance(raw.get("model_registration"), dict) else {}

    if status == "completed":
        missing_fields: list[str] = []
        if metrics is None:
            missing_fields.append("metrics")
        if not artifacts:
            missing_fields.append("artifacts")
        if not summary_status or not summary_message:
            missing_fields.append("summary")
        if not isinstance(metadata, dict):
            missing_fields.append("metadata")

        if missing_fields:
            validation_error = f"Training result incomplete: missing {', '.join(missing_fields)}"
            error_text = validation_error
            summary_status = "结果不完整"
            summary_message = "训练回调缺少关键字段，任务已标记失败。"

    if status == "failed" and not error_text:
        error_text = "训练任务失败"

    normalized: dict[str, Any] = {
        "metrics": metrics,
        "artifacts": artifacts,
        "summary": {
            "status": summary_status,
            "message": summary_message,
        },
        "metadata": metadata,
        "model_registration": model_registration,
        "error": error_text or None,
        "logs": str(raw.get("logs") or ""),
    }

    # WFA 稳定性诊断：透传到顶层，供训练结果页展示
    raw_wfa = raw.get("wfa")
    if isinstance(raw_wfa, dict) and raw_wfa.get("enabled"):
        normalized["wfa"] = raw_wfa
    # 若顶层缺失但 metadata 里存在（兼容旧回调），补到顶层
    elif isinstance(metadata.get("wfa"), dict):
        normalized["wfa"] = metadata["wfa"]

    # 数据漂移检测（PSI）：透传到顶层，供训练结果页展示
    raw_drift = raw.get("drift")
    if isinstance(raw_drift, dict) and raw_drift.get("enabled"):
        normalized["drift"] = raw_drift
    elif isinstance(metadata.get("drift"), dict):
        normalized["drift"] = metadata["drift"]

    # 多周期训练结果：透传到顶层，供前端训练结果页展示周期明细 + 融合模型
    if isinstance(raw.get("multi_horizon"), dict):
        normalized["multi_horizon"] = raw["multi_horizon"]
    elif isinstance(metadata.get("multi_horizon"), dict):
        normalized["multi_horizon"] = metadata["multi_horizon"]

    return normalized, validation_error


def _merge_log_text(*parts: str, max_lines: int = 600) -> str:
    seen: set[str] = set()
    merged_lines: list[str] = []
    for part in parts:
        text = str(part or "").strip()
        if not text:
            continue
        for line in text.splitlines():
            normalized_line = line.rstrip()
            if not normalized_line or normalized_line in seen:
                continue
            seen.add(normalized_line)
            merged_lines.append(normalized_line)
    if max_lines > 0 and len(merged_lines) > max_lines:
        merged_lines = merged_lines[-max_lines:]
    return "\n".join(merged_lines).strip()


async def submit_training_job(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    current_user: dict[str, Any],
) -> dict[str, Any]:
    # 先解析目标市场（显式字段或 benchmark 推断），再按市场过滤可用特征
    context_raw = payload.get("context", {})
    context = context_raw if isinstance(context_raw, dict) else {}
    benchmark_hint = str(context.get("benchmark") or "SH000300").strip()
    market = _resolve_market(context.get("market"), benchmark_hint)
    payload, allowed_features = await _resolve_quantdb_factor_payload(payload, market)
    normalized_payload = _normalize_payload(payload, allowed_features)
    run_id = f"train_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"

    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "unknown")

    # ── 多周期训练：创建 parent job + 每周期一个 child job ──
    horizons = normalized_payload.get("horizons")
    multi_horizon = bool(horizons and isinstance(horizons, list) and len(horizons) >= 2)

    async with get_session() as session:
        parent_record = TrainingJobRecord(
            id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            status="pending",
            request_payload={**normalized_payload, "_parent": True},
            progress=0,
        )
        session.add(parent_record)

        if multi_horizon:
            child_run_ids: list[str] = []
            for i, h in enumerate(horizons):
                child_run_id = f"{run_id}_t{h}"
                # 子任务固定单周期，display_name 追加 _T{h}
                child_payload = {
                    **normalized_payload,
                    "target_horizon_days": int(h),
                    "display_name": f"{normalized_payload.get('display_name', 'unnamed')}_T{h}",
                    "horizons": None,
                    "_parent_run_id": run_id,
                    "_multi_horizon_index": i,
                    "max_time_minutes": max(
                        30,
                        int(normalized_payload.get("max_time_minutes") or 120)
                        // max(1, len(horizons)),
                    ),
                }
                child_payload.pop("wfa", None)
                child_record = TrainingJobRecord(
                    id=child_run_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    status="pending",
                    request_payload=child_payload,
                    progress=0,
                )
                session.add(child_record)
                child_run_ids.append(child_run_id)
            parent_record.request_payload = {
                **parent_record.request_payload,
                "_child_run_ids": child_run_ids,
                "_tenant_id": tenant_id,
                "_user_id": user_id,
            }
        await session.commit()

    _training_log_stream.append_log(
        run_id=run_id,
        tenant_id=tenant_id,
        user_id=user_id,
        line=f"[SYSTEM] 训练任务已创建: {run_id}"
        + (f"（多周期 ×{len(horizons)}: " + ", ".join(f"T{h}" for h in horizons) + "）" if multi_horizon else ""),
        status="pending",
        progress=0,
    )

    # 训练节点选择（payload.node_id: "local" 或 "autodl-xxx"，默认本地）
    node_id = str(normalized_payload.get("node_id") or payload.get("node_id") or "local")
    orchestrator = get_orchestrator(node_id=node_id)
    logger.warning(f"[SYSTEM] Dispatching training job {run_id}. node={node_id} payload_keys={list(normalized_payload.keys())}")
    if multi_horizon:
        # 多周期：编排器串行跑各 child，全部成功后自动创建融合模型
        REGISTRY.register(
            orchestrator.launch_multi_horizon_job(
                parent_run_id=run_id,
                child_run_ids=child_run_ids,
                payload=normalized_payload,
            )
        )
    else:
        # 单周期：直接跑
        REGISTRY.register(
            orchestrator.launch_training_job(run_id=run_id, payload=normalized_payload)
        )

    # 预检特征可用性，告知前端哪些特征在 parquet 中不存在
    valid_features, missing_features = LocalDockerOrchestrator._filter_features_by_parquet(
        run_id, normalized_payload.get("features", [])
    )

    return {
        "runId": run_id,
        "status": "pending",
        "multiHorizon": multi_horizon,
        "payload": normalized_payload,
        "validFeatureCount": len(valid_features),
        "missingFeatureCount": len(missing_features),
        "missingFeatures": missing_features[:30],
    }



async def get_training_run_for_owner(run_id: str, current_user: dict[str, Any]) -> dict[str, Any]:
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "unknown")

    async with get_session(read_only=True) as session:
        stmt = select(TrainingJobRecord).where(
            TrainingJobRecord.id == run_id,
            TrainingJobRecord.tenant_id == tenant_id,
            TrainingJobRecord.user_id == user_id,
        )
        record = (await session.execute(stmt)).scalar_one_or_none()

    if not record:
        raise HTTPException(status_code=404, detail="Training run not found")

    effective_status = str(record.status or "")
    raw_result = record.result if isinstance(record.result, dict) else {}
    normalized_result, normalize_error = _normalize_training_result_payload(
        raw_result,
        record.request_payload if isinstance(record.request_payload, dict) else {},
        record.id,
        effective_status,
    )

    if effective_status == "completed" and normalize_error:
        effective_status = "failed"
        normalized_result["error"] = normalize_error

    live_snapshot = _training_log_stream.fetch_snapshot(run_id, line_limit=600) or {}
    live_status = str(live_snapshot.get("status") or "").strip()
    live_progress_raw = live_snapshot.get("progress")
    live_logs = str(live_snapshot.get("logs") or "").strip()

    progress = int(record.progress or 0)
    if live_progress_raw is not None:
        try:
            progress = max(progress, int(live_progress_raw))
        except Exception:
            pass

    if effective_status not in {"completed", "failed"} and live_status in {
        "pending",
        "provisioning",
        "running",
        "waiting_callback",
    }:
        effective_status = live_status

    merged_logs = _merge_log_text(record.logs or "", live_logs)

    return {
        "runId": record.id,
        "status": effective_status,
        "progress": progress,
        "logs": merged_logs,
        "result": normalized_result,
        "isCompleted": effective_status in ["completed", "failed"],
    }


async def get_latest_training_run_for_owner(
    current_user: dict[str, Any],
) -> dict[str, Any] | None:
    """查询当前用户「最近/活跃」的训练主任务，用于前端切页后恢复进度。

    优先从 redis 的用户活跃索引读（训练实时流会持续维护该 key，TTL 与状态一致）；
    索引失效/无缓存时回退 DB：先找进行中主任务，再回退最近创建主任务。
    跳过多周期子任务与父占位。都没有时返回 None。
    """
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "unknown")

    # 1) 优先 redis 活跃索引 -> run_id -> 复用单任务查询（含实时快照合并）
    idx = _training_log_stream.fetch_active_run_id(tenant_id, user_id)
    if idx and idx.get("run_id"):
        try:
            return await get_training_run_for_owner(str(idx["run_id"]), current_user)
        except HTTPException:
            # redis 里的 run 在 DB 已不存在（被清理），落到 DB 兜底
            pass

    active_statuses = ("pending", "provisioning", "running", "waiting_callback")

    def _is_root(rec: TrainingJobRecord) -> bool:
        payload = rec.request_payload if isinstance(rec.request_payload, dict) else {}
        return not (payload.get("_parent") or payload.get("_parent_run_id"))

    async with get_session(read_only=True) as session:
        # 先找进行中的主任务（倒序最新一条）
        stmt = (
            select(TrainingJobRecord)
            .where(
                TrainingJobRecord.tenant_id == tenant_id,
                TrainingJobRecord.user_id == user_id,
                TrainingJobRecord.status.in_(active_statuses),
            )
            .order_by(TrainingJobRecord.updated_at.desc(), TrainingJobRecord.created_at.desc())
        )
        rows = (await session.execute(stmt)).scalars().all()

        root_active = next((r for r in rows if _is_root(r)), None)

        # 无进行中任务时，回退最近创建的主任务
        if root_active is None:
            stmt_all = (
                select(TrainingJobRecord)
                .where(
                    TrainingJobRecord.tenant_id == tenant_id,
                    TrainingJobRecord.user_id == user_id,
                )
                .order_by(TrainingJobRecord.created_at.desc())
            )
            all_rows = (await session.execute(stmt_all)).scalars().all()
            root_recent = next((r for r in all_rows if _is_root(r)), None)
            candidate = root_recent
        else:
            candidate = root_active

    if candidate is None:
        return None
    return await get_training_run_for_owner(candidate.id, current_user)


def _verify_internal_call_secret(provided: str) -> None:
    """P0-3: 强制 fail-closed 的 internal secret 校验。

    三个独立失败路径均返回 401：
    1. env 缺失（INTERNAL_CALL_SECRET 未配置）→ 401 + 详细日志
    2. header 缺失或空字符串 → 401
    3. header 值不匹配 → 401

    用 secrets.compare_digest 替代 ==，避免 timing attack。
    """
    expected = os.getenv("INTERNAL_CALL_SECRET", "")
    if not expected:
        logger.error(
            "INTERNAL_CALL_SECRET env not set; refusing internal callback"
        )
        raise HTTPException(
            status_code=401,
            detail="Internal call secret not configured",
        )
    if not provided:
        raise HTTPException(
            status_code=401,
            detail="Missing X-Internal-Call-Secret",
        )
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid internal call secret",
        )


def _registration_outcome(registration: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    """由注册结果判定训练 run 状态（纯函数，可单测）。

    - ready → completed。
    - candidate + gate_reasons（软门禁暂留）：训练本身成功，模型待手动激活，
      run 保持 completed，summary 如实提示，不再误报“模型注册失败”。
    - 其余（failed / 无原因的 candidate）→ failed。
    返回 (run_status, summary, error)。
    """
    status = str(registration.get("status") or "")
    if status == "ready":
        return "completed", {}, ""
    gate_reasons = [
        str(r).strip() for r in (registration.get("gate_reasons") or []) if str(r).strip()
    ]
    if status == "candidate" and gate_reasons:
        message = str(registration.get("message") or "").strip() or (
            f"样本外质量门禁：{'；'.join(gate_reasons)}，未自动激活。"
            "请人工评估后在模型管理页手动激活。"
        )
        return (
            "completed",
            {"status": "质量门禁暂留候选", "message": message},
            "",
        )
    reg_error = str(registration.get("error") or "model registration failed").strip()
    return (
        "failed",
        {"status": "模型注册失败", "message": reg_error},
        reg_error,
    )


async def complete_training_run(
    run_id: str,
    result: dict[str, Any],
    x_internal_call_secret: str,
) -> dict[str, Any]:
    _verify_internal_call_secret(x_internal_call_secret)

    incoming_status = str(result.get("status", "completed"))
    status = incoming_status if incoming_status in ("completed", "failed") else "completed"

    async with get_session() as session:
        record = (
            await session.execute(select(TrainingJobRecord).where(TrainingJobRecord.id == run_id))
        ).scalar_one_or_none()
        if not record:
            raise HTTPException(status_code=404, detail="Training run not found")

        normalized_result, validation_error = _normalize_training_result_payload(
            result,
            record.request_payload if isinstance(record.request_payload, dict) else {},
            run_id,
            status,
        )

        if status == "completed" and validation_error:
            status = "failed"
            normalized_result["error"] = validation_error
            normalized_result["summary"] = {
                "status": "结果不完整",
                "message": "训练回调缺少关键字段，任务已标记失败。",
            }

        if status == "completed":
            try:
                registration = await model_registry_service.register_model_from_training_run(
                    tenant_id=str(record.tenant_id or "default"),
                    user_id=str(record.user_id or ""),
                    run_id=run_id,
                    request_payload=record.request_payload if isinstance(record.request_payload, dict) else {},
                    result_payload=normalized_result,
                )
                normalized_result["model_registration"] = registration
                outcome, summary, reg_error = _registration_outcome(registration)
                if outcome == "failed":
                    status = "failed"
                    normalized_result["error"] = reg_error
                    normalized_result["summary"] = summary
                elif summary:
                    normalized_result["summary"] = summary
            except Exception as exc:
                status = "failed"
                normalized_result["model_registration"] = {
                    "model_id": "",
                    "status": "failed",
                    "error": str(exc),
                }
                normalized_result["error"] = str(exc)
                normalized_result["summary"] = {
                    "status": "模型注册失败",
                    "message": f"模型注册与同步失败: {exc}",
                }

        record.status = status
        record.progress = 100
        record.result = normalized_result

        callback_logs = str(result.get("logs") or "").strip()
        merged_logs = "\n".join([x for x in [record.logs or "", callback_logs] if x]).strip()
        # Redis 日志流 TTL 48h 后会消失：完成时把去重后的流日志尾部持久化进 DB，
        # 保证训练详情页"为什么选这些特征"的筛选日志长期可查。
        try:
            stream_logs = str(
                (_training_log_stream.fetch_snapshot(run_id, line_limit=600) or {}).get("logs") or ""
            ).strip()
            existing_lines = set((record.logs or "").splitlines()) | set(
                merged_logs.splitlines()
            )
            extra = [
                line for line in stream_logs.splitlines()
                if line and line not in existing_lines
            ]
            if extra:
                merged_logs = "\n".join(
                    [x for x in [merged_logs, "\n".join(extra)] if x]
                ).strip()
        except Exception:
            pass
        record.logs = merged_logs
        await session.commit()
        _training_log_stream.update_state(
            run_id=run_id,
            tenant_id=str(record.tenant_id or "default"),
            user_id=str(record.user_id or ""),
            status=status,
            progress=100,
            last_line=f"[{status.upper()}] callback completed",
        )
        if callback_logs:
            for line in callback_logs.splitlines()[-30:]:
                text = str(line).strip()
                if not text:
                    continue
                _training_log_stream.append_log(
                    run_id=run_id,
                    tenant_id=str(record.tenant_id or "default"),
                    user_id=str(record.user_id or ""),
                    line=text,
                    status=status,
                    progress=100,
                )

    # 训练完成后立即清理容器，避免面板长期堆积 Exited 的 qm-train-* 容器
    container_name = f"qm-train-{run_id}"
    try:
        docker = DockerClient.from_env()
        try:
            container = docker.containers.get(container_name)
        except Exception:
            container = None
        if container is not None:
            container.remove(force=True, v=True)
            logger.info("[%s] removed training container: %s", run_id, container_name)
    except Exception as exc:
        logger.warning("[%s] failed to remove container %s: %s", run_id, container_name, exc)

    return {"ok": True, "runId": run_id, "status": status}
