import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from docker import DockerClient
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text

from backend.services.api.routers.admin.db import TrainingJobRecord
from backend.services.api.training_explain import normalize_explain
from backend.services.api.user_app.middleware.auth import require_admin
from backend.services.engine.training.local_docker_orchestrator import LocalDockerOrchestrator
from backend.services.engine.training.training_log_stream import TrainingRunLogStream
from backend.shared.database_manager_v2 import get_session
from backend.shared.model_registry import model_registry_service

router = APIRouter()
logger = logging.getLogger(__name__)
_FEATURE_CATALOG_FALLBACK = Path(os.getcwd()) / "config" / "features" / "model_training_feature_catalog_v1.json"
_ALLOWED_TARGET_MODE = {"return", "classification"}
_ALLOWED_DEAL_PRICE = {"open", "close"}
_TRAINING_BASE_FEATURES = [
    "mom_ret_1d",
    "mom_ret_5d",
    "mom_ret_20d",
    "liq_volume",
    "liq_amount",
    "liq_turnover_os",
]
_training_log_stream = TrainingRunLogStream()
DEFAULT_TRAINING_IMAGE = (
    os.getenv("TRAINING_IMAGE") or "quantmind-ml-runtime:latest"
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


def _parse_date(date_str: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(date_str)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Invalid date for {field}: {date_str}") from exc


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _load_allowed_features_from_file() -> list[str]:
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
            if key and key not in keys:
                keys.append(key)
    return keys


async def _load_allowed_features_from_db() -> list[str]:
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
    keys: list[str] = []
    for row in rows:
        key = str(row.get("feature_key") or "").strip()
        if key and key not in keys:
            keys.append(key)
    return keys


async def _load_allowed_features() -> list[str]:
    db_keys = await _load_allowed_features_from_db()
    if db_keys:
        return db_keys
    return _load_allowed_features_from_file()


def _normalize_context(context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(context, dict):
        raise HTTPException(status_code=422, detail="context must be an object")

    initial_capital = _coerce_float(context.get("initial_capital"))
    if initial_capital is None:
        initial_capital = _coerce_float(context.get("initialCapital"))
    initial_capital = initial_capital if initial_capital is not None else 1_000_000.0
    if initial_capital <= 0:
        raise HTTPException(status_code=422, detail="context.initial_capital must be > 0")

    benchmark = str(context.get("benchmark") or "SH000300").strip() or "SH000300"

    commission_rate = _coerce_float(context.get("commission_rate"))
    if commission_rate is None:
        commission_rate = _coerce_float(context.get("commissionRate"))
    commission_rate = commission_rate if commission_rate is not None else 0.00025
    if commission_rate < 0:
        raise HTTPException(status_code=422, detail="context.commission_rate must be >= 0")

    slippage = _coerce_float(context.get("slippage"))
    slippage = slippage if slippage is not None else 0.0005
    if slippage < 0:
        raise HTTPException(status_code=422, detail="context.slippage must be >= 0")

    deal_price = str(context.get("deal_price") or context.get("dealPrice") or "close").strip().lower()
    if deal_price not in _ALLOWED_DEAL_PRICE:
        raise HTTPException(status_code=422, detail="context.deal_price must be one of: open, close")

    return {
        "initial_capital": initial_capital,
        "benchmark": benchmark,
        "commission_rate": commission_rate,
        "slippage": slippage,
        "deal_price": deal_price,
    }


def _normalize_payload(payload: dict[str, Any], allowed_features: list[str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Payload must be a JSON object")

    model_type = str(payload.get("model_type", "lightgbm")).strip().lower()
    if model_type != "lightgbm":
        raise HTTPException(status_code=422, detail="Only lightgbm is supported for training")

    display_name = str(payload.get("display_name") or payload.get("job_name") or "unnamed").strip() or "unnamed"
    if len(display_name) > 128:
        raise HTTPException(status_code=422, detail="display_name must be at most 128 characters")

    train_start = str(payload.get("train_start", "2022-01-01")).strip()
    train_end = str(payload.get("train_end", "2024-12-31")).strip()
    dt_train_start = _parse_date(train_start, "train_start")
    dt_train_end = _parse_date(train_end, "train_end")
    if dt_train_start >= dt_train_end:
        raise HTTPException(status_code=422, detail="train_start must be earlier than train_end")

    val_ratio = float(payload.get("val_ratio", 0.15))
    if not (0.01 <= val_ratio <= 0.5):
        raise HTTPException(status_code=422, detail="val_ratio must be between 0.01 and 0.5")

    num_boost_round = int(payload.get("num_boost_round", 1000))
    if not (10 <= num_boost_round <= 20000):
        raise HTTPException(status_code=422, detail="num_boost_round must be between 10 and 20000")

    early_stopping_rounds = int(payload.get("early_stopping_rounds", 100))
    if not (1 <= early_stopping_rounds <= 5000):
        raise HTTPException(status_code=422, detail="early_stopping_rounds must be between 1 and 5000")

    raw_features = payload.get("features", []) or []
    if not isinstance(raw_features, list):
        raise HTTPException(status_code=422, detail="features must be a string array")
    features: list[str] = []
    for item in raw_features:
        val = str(item).strip()
        if val and val not in features:
            features.append(val)
    if len(features) > 300:
        raise HTTPException(status_code=422, detail="features length cannot exceed 300")
    if allowed_features:
        allowed_set = set(allowed_features)
        invalid = [feature for feature in features if feature not in allowed_set]
        if invalid:
            sample = ", ".join(invalid[:8])
            raise HTTPException(
                status_code=422,
                detail=f"Unknown features: {sample}. Please refresh feature catalog and retry.",
            )

    lgb_params = payload.get("lgb_params", {}) or {}
    if not isinstance(lgb_params, dict):
        raise HTTPException(status_code=422, detail="lgb_params must be an object")

    target_horizon_days = int(payload.get("target_horizon_days", 1))
    if not (1 <= target_horizon_days <= 30):
        raise HTTPException(status_code=422, detail="target_horizon_days must be between 1 and 30")

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

    context = _normalize_context(payload.get("context", {}) or {})
    explain = normalize_explain(payload.get("explain"))

    normalized: dict[str, Any] = {
        "job_name": str(payload.get("job_name", "unnamed")).strip() or "unnamed",
        "display_name": display_name,
        "model_type": model_type,
        "train_start": train_start,
        "train_end": train_end,
        "val_ratio": val_ratio,
        "num_boost_round": num_boost_round,
        "early_stopping_rounds": early_stopping_rounds,
        "features": features,
        "feature_categories": feature_categories,
        "target_horizon_days": target_horizon_days,
        "target_mode": target_mode,
        "label_formula": label_formula,
        "effective_trade_date": effective_trade_date,
        "training_window": training_window,
        "context": context,
        "explain": explain,
        "lgb_params": lgb_params,
    }

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
        # 例如: 如果预测未来3天收益率(H=3)，Train 结束与 Val 开始之间必须至少有 3 天间隔。
        # 不再阻断(422)，而是由后端自动向后平移日期。
        gap_days = int(normalized.get("target_horizon_days") or 1)

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

    generated_at = str(payload.get("generated_at") or "").strip()
    if generated_at:
        normalized["generated_at"] = generated_at

    return normalized


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
        "generated_at": str(request_payload.get("generated_at") or datetime.utcnow().isoformat()),
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
    allowed_features = await _load_allowed_features()
    normalized_payload = _normalize_payload(payload, allowed_features)
    run_id = f"train_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"

    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "unknown")

    async with get_session() as session:
        record = TrainingJobRecord(
            id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            status="pending",
            request_payload=normalized_payload,
            progress=0,
        )
        session.add(record)
        await session.commit()
    _training_log_stream.append_log(
        run_id=run_id,
        tenant_id=tenant_id,
        user_id=user_id,
        line=f"[SYSTEM] 训练任务已创建: {run_id}",
        status="pending",
        progress=0,
    )

    orchestrator = LocalDockerOrchestrator()
    logger.warning(f"[SYSTEM] Dispatching training job {run_id}. payload_keys={list(normalized_payload.keys())}")
    # 使用 asyncio.create_task 代替 BackgroundTasks，避免 anyio cancel scope 在同步阻塞调用
    # （COS 上传、Docker API）期间静默取消任务
    asyncio.create_task(
        orchestrator.launch_training_job(run_id=run_id, payload=normalized_payload),
        name=f"training-{run_id}",
    )

    return {"runId": run_id, "status": "pending", "payload": normalized_payload}


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

    live_snapshot = _training_log_stream.fetch_snapshot(run_id, line_limit=220) or {}
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


async def complete_training_run(
    run_id: str,
    result: dict[str, Any],
    x_internal_call_secret: str,
) -> dict[str, Any]:
    expected = os.getenv("INTERNAL_CALL_SECRET", "")
    if not expected or x_internal_call_secret != expected:
        raise HTTPException(status_code=403, detail="Invalid internal call secret")

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
                if str(registration.get("status") or "") != "ready":
                    status = "failed"
                    reg_error = str(registration.get("error") or "model registration failed").strip()
                    normalized_result["error"] = reg_error
                    normalized_result["summary"] = {
                        "status": "模型注册失败",
                        "message": reg_error,
                    }
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
