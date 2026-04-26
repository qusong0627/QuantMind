import json
import logging
import os
import re
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.services.api.routers.admin.admin_training import (
    complete_training_run,
    get_training_run_for_owner,
    submit_training_job,
)
from backend.services.api.routers.admin.model_management import (
    _enrich_feature_catalog_with_data_coverage,
    _load_feature_catalog_from_db,
    _load_feature_catalog_from_file,
)
from backend.services.api.training_shap_summary import read_shap_summary_rows, to_int_or
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.engine.inference.router_service import InferenceRouterService
from backend.services.engine.inference.script_runner import InferenceScriptRunner
from backend.services.engine.services.model_inference_persistence import model_inference_persistence
from backend.shared.database_manager_v2 import get_session
from backend.shared.model_registry import model_registry_service
from backend.shared.redis_sentinel_client import get_redis_sentinel_client
from backend.shared.trading_calendar import calendar_service

router = APIRouter()
logger = logging.getLogger(__name__)

# models/production 目录（相对项目根）
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_PRODUCTION_DIR = Path(os.getenv("MODELS_PRODUCTION_ROOT", str(_PROJECT_ROOT / "models" / "production")))


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
        val_start  = meta.get("val_start")  or meta.get("valid_start")
        val_end    = meta.get("val_end")    or meta.get("valid_end")
        test_start = meta.get("test_start")
        test_end   = meta.get("test_end")

        # display_name 优先取平铺字段，回退旧格式 model_info.name
        display_name = (
            meta.get("display_name")
            or meta.get("model_name")
            or info.get("name")
            or subdir.name
        )

        # label_formula 优先取平铺字段，回退旧格式 training_config.label
        label_formula = (
            meta.get("label_formula")
            or tc.get("label")
            or ""
        )

        # metrics：新格式在 meta.metrics，旧格式在 performance_metrics
        new_metrics = meta.get("metrics", {})
        if new_metrics:
            perf = {
                "train": {"mean_ic": new_metrics.get("train_ic"), "icir": new_metrics.get("train_rank_icir")},
                "valid": {"mean_ic": new_metrics.get("val_ic"),   "icir": new_metrics.get("val_rank_icir")},
                "test":  {"mean_ic": new_metrics.get("test_ic"),  "icir": new_metrics.get("test_rank_icir")},
            }

        results.append({
            "model_id": model_id,
            "dir_name": subdir.name,
            "tenant_id": "system",
            "display_name": display_name,
            "description": meta.get("description") or info.get("description", ""),
            "framework": meta.get("framework", ""),
            "model_type": meta.get("model_type", ""),
            "feature_count": meta.get("feature_count"),
            "feature_columns": meta.get("feature_columns", []),
            "is_neutralized": meta.get("is_neutralized", False),
            "algorithm": info.get("algorithm", ""),
            "version": info.get("version", meta.get("version", "")),
            "created_at": meta.get("generated_at") or info.get("created_at", meta.get("trained_at", "")),
            "training_config": tc,
            # 统一字段名：val_start/val_end（和用户模型 metadata_json 保持一致）
            "train_start": meta.get("train_start"),
            "train_end":   meta.get("train_end"),
            "valid_start": val_start,
            "valid_end":   val_end,
            "test_start":  test_start,
            "test_end":    test_end,
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
        })
    return results


class SetDefaultModelRequest(BaseModel):
    model_id: str


class SetStrategyBindingRequest(BaseModel):
    model_id: str


class InferenceRunRequest(BaseModel):
    model_id: str
    inference_date: date = Field(..., description="推理基准日期 YYYY-MM-DD")


class InferenceSettingsRequest(BaseModel):
    enabled: bool
    schedule_time: str | None = Field(default=None, description="每日执行时间 HH:MM")


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
        default_model = await model_registry_service.get_default_model(tenant_id=tenant_id, user_id=user_id)
        if not default_model:
            raise HTTPException(status_code=404, detail="未找到默认模型")
        requested_model_id = str(default_model.get("model_id") or "")
    resolved = await model_registry_service.resolve_effective_model(
        tenant_id=tenant_id,
        user_id=user_id,
        model_id=requested_model_id,
    )
    if requested_model_id and resolved.fallback_used and resolved.model_source in {"user_default", "system_fallback"}:
        raise HTTPException(status_code=404, detail=f"模型不可用或未就绪: {requested_model_id}")
    if not resolved.storage_path:
        raise HTTPException(status_code=404, detail=f"模型路径不可用: {requested_model_id}")
    return requested_model_id, resolved


def _get_model_data_dir(model_dir: Path, metadata: dict | None = None) -> str:
    """
    从模型配置中获取推理数据目录。

    优先级：
    1. metadata.json 中的 qlib_data_path 字段（绝对路径）
    2. metadata.json 中的 data_source 字段判断：
       - "qlib" -> db/qlib_data
       - "parquet" 或其他 -> db/feature_snapshots
    3. 默认值 -> db/feature_snapshots
    """
    # 优先读取 metadata 中的 qlib_data_path
    if metadata:
        qlib_data_path = metadata.get("qlib_data_path")
        if qlib_data_path:
            return qlib_data_path

        # 根据 data_source 判断
        data_source = str(metadata.get("data_source", "")).lower()
        if data_source == "qlib":
            return "db/qlib_data"

    # 尝试从模型目录读取 metadata.json
    meta_file = model_dir / "metadata.json"
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            qlib_data_path = meta.get("qlib_data_path")
            if qlib_data_path:
                return qlib_data_path

            data_source = str(meta.get("data_source", "")).lower()
            if data_source == "qlib":
                return "db/qlib_data"
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


@router.get("/system-models", summary="获取系统内置模型列表（读取 models/production 目录）")
async def list_system_models(
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """返回 models/production/ 下所有含 metadata.json 的子目录，无需分页。"""
    try:
        models = _load_production_models()
        return {"status": "success", "count": len(models), "models": models}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/feature-catalog", summary="获取模型训练特征字典（用户态）")
async def get_model_feature_catalog(
    current_user: dict[str, Any] = Depends(get_current_user),
):
    _ = current_user
    try:
        catalog = await _load_feature_catalog_from_db()
    except Exception:
        catalog = None

    if catalog:
        return _enrich_feature_catalog_with_data_coverage(catalog)

    fallback = _load_feature_catalog_from_file()
    if fallback:
        return _enrich_feature_catalog_with_data_coverage(fallback)

    raise HTTPException(status_code=404, detail="未找到可用的特征字典（DB/文件均不可用）")


@router.get("/qlib-data-range", summary="获取 Qlib 数据日期范围")
async def get_qlib_data_range(
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """
    返回 qlib_data 的日期范围，用于前端日期选择器限制。
    读取 db/qlib_data/calendars/day.txt 获取交易日历。
    """
    _ = current_user
    qlib_data_dir = Path(os.getcwd()) / "db" / "qlib_data"
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


@router.get("/training-runs/{run_id}", summary="获取训练任务状态（用户态）")
async def get_training_run(
    run_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    return await get_training_run_for_owner(run_id, current_user)


@router.get("", summary="获取当前用户模型列表（用户态）")
async def list_user_models(
    include_archived: bool = False,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    models = await model_registry_service.list_models(
        tenant_id=tenant_id,
        user_id=user_id,
        include_archived=include_archived,
    )
    return {"items": models, "total": len(models)}


@router.get("/default", summary="获取当前用户默认模型（用户态）")
async def get_default_model(
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    model = await model_registry_service.get_default_model(tenant_id=tenant_id, user_id=user_id)
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


@router.get("/{model_id}", summary="获取单个用户模型（用户态）")
async def get_user_model(
    model_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    model = await model_registry_service.get_model(tenant_id=tenant_id, user_id=user_id, model_id=model_id)
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
    model = await model_registry_service.get_model(tenant_id=tenant_id, user_id=user_id, model_id=model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    metadata = model.get("metadata_json") if isinstance(model.get("metadata_json"), dict) else {}
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
            logger.warning("failed to parse shap summary: model_id=%s err=%s", model_id, exc)
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
        model = await model_registry_service.archive_model(tenant_id=tenant_id, user_id=user_id, model_id=model_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return model


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
    items.append(
        {
            "key": "model_file",
            "label": "模型文件存在",
            "passed": model_file_exists,
            "severity": "hard",
            "detail": str(model_file_path) if model_file_path else f"{model_dir}/{model_file}",
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
        if str(primary_meta.get("data_source") or "").lower() == "parquet":
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

    expected_feature_dim = runner._resolve_expected_feature_dim()
    items.append(
        {
            "key": "expected_feature_dim",
            "label": "期望特征维度",
            "passed": True,
            "severity": "soft",
            "detail": str(expected_feature_dim),
        }
    )

    # 根据数据源选择对应的就绪检查逻辑
    primary_meta = runner._read_primary_metadata()
    data_source = str(primary_meta.get("data_source") or "").lower()

    if data_source == "parquet":
        readiness = runner._query_parquet_readiness(trade_date=data_trade_date)
        readiness_label = "历史 Parquet 数据就绪"
    elif data_source in ("qlib", "qlib_bin", "bin"):
        readiness = runner._query_qlib_readiness(trade_date=data_trade_date)
        readiness_label = "Qlib 二进制数据就绪"
    else:
        readiness = runner._query_dimension_readiness(trade_date=data_trade_date, expected_dim=expected_feature_dim)
        readiness_label = "当日数据覆盖就绪"

    items.append(
        {
            "key": "market_data_ready",
            "label": readiness_label,
            "passed": bool(readiness.get("ready")),
            "severity": "hard",
            "detail": str(readiness.get("detail") or ""),
        }
    )

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
    return all(bool(item.get("passed")) for item in items if item.get("severity") != "soft")


@router.get("/inference/precheck", summary="推理前置检查（用户态）")
async def precheck_inference(
    model_id: str = Query(..., description="模型ID"),
    inference_date: date | None = Query(None, description="推理基准日期 YYYY-MM-DD"),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    requested_model_id, resolved = await _resolve_requested_model(current_user, model_id)
    model_dir = Path(resolved.storage_path)
    requested_inference_date = inference_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    resolved_data_trade_date, calendar_adjusted = await _resolve_inference_trade_date_with_calendar(
        current_user=current_user,
        requested_date=requested_inference_date,
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


@router.post("/inference/run", summary="执行模型推理（用户态）")
async def run_model_inference(
    payload: InferenceRunRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    requested_model_id, resolved = await _resolve_requested_model(current_user, payload.model_id)
    model_dir = Path(resolved.storage_path)
    requested_inference_date = payload.inference_date
    resolved_data_trade_date, calendar_adjusted = await _resolve_inference_trade_date_with_calendar(
        current_user=current_user,
        requested_date=requested_inference_date,
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
            request_payload={"model_id": requested_model_id, "inference_date": data_trade_date, "precheck": precheck},
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
            request_payload={"model_id": requested_model_id, "inference_date": data_trade_date, "precheck": precheck},
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
    result_model_source = str(getattr(result, "model_source", "") or resolved.model_source)
    result_effective_model_id = str(getattr(result, "effective_model_id", "") or resolved.effective_model_id)
    success_payload = {
        "success": bool(result.success),
        "run_id": run_id,
        "status": "completed" if result.success else "failed",
        "model_id": requested_model_id,
        "effective_model_id": resolved.effective_model_id,
        "active_model_id": result.active_model_id or resolved.effective_model_id,
        "model_source": result_model_source,
        "active_data_source": result.active_data_source or _get_model_data_dir(model_dir),
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
        request_payload={"model_id": requested_model_id, "inference_date": data_trade_date, "precheck": precheck},
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
    return await model_inference_persistence.list_runs(
        tenant_id=tenant_id,
        user_id=user_id,
        model_id=model_id,
        run_id=run_id,
        status=status,
        inference_date=inference_date,
        page=page,
        page_size=page_size,
    )


@router.get("/inference/runs/{run_id}", summary="查看模型推理结果明细（用户态）")
async def get_model_inference_run_detail(
    run_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    run = await model_inference_persistence.get_run(run_id=run_id, tenant_id=tenant_id, user_id=user_id)
    if not run:
        raise HTTPException(status_code=404, detail="推理批次不存在")

    signals: list[dict[str, Any]] = []
    if run.get("status") == "completed":
        try:
            async with get_session(read_only=True) as session:
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT
                                symbol,
                                fusion_score,
                                light_score,
                                tft_score,
                                score_rank,
                                signal_side,
                                expected_price,
                                quality,
                                created_at
                            FROM engine_signal_scores
                            WHERE run_id = :run_id
                              AND tenant_id = :tenant_id
                              AND user_id = :user_id
                            ORDER BY fusion_score DESC NULLS LAST, symbol ASC
                            """
                        ),
                        {"run_id": run_id, "tenant_id": tenant_id, "user_id": user_id},
                    )
                ).mappings().all()
            for row in rows:
                item = dict(row or {})
                if item.get("created_at") is not None:
                    item["created_at"] = item["created_at"].isoformat()
                signals.append(item)
        except Exception as exc:  # pragma: no cover - DB fallback
            logger.warning("failed to load inference signal rows for %s: %s", run_id, exc)
            signals = []

    summary = dict(run)
    summary["rows_count"] = len(signals)
    summary["symbols_count"] = len({str(item.get("symbol") or "") for item in signals if item.get("symbol")})
    fusion_scores = [float(item["fusion_score"]) for item in signals if item.get("fusion_score") is not None]
    summary["min_fusion_score"] = min(fusion_scores) if fusion_scores else None
    summary["max_fusion_score"] = max(fusion_scores) if fusion_scores else None
    summary["first_created_at"] = signals[0].get("created_at") if signals else None
    summary["last_created_at"] = signals[-1].get("created_at") if signals else None
    if run.get("status") == "completed" and not signals:
        summary["signal_rows_error"] = "signal rows unavailable"

    return {
        "summary": summary,
        "page": 1,
        "page_size": len(signals) or 1,
        "total": len(signals),
        "items": signals,
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
    settings["next_run"] = _render_next_run(settings.get("next_run_at")) if settings.get("next_run_at") else settings.get("next_run")
    return settings


@router.get("/inference/latest", summary="获取当前生效推理批次（用户态）")
async def get_model_inference_latest(
    model_id: str | None = Query(None, description="模型ID，可选，用于检查是否与当前生效模型匹配"),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner_scope(current_user)
    redis_client = get_redis_sentinel_client()
    latest_key = f"qm:signal:latest:{tenant_id}:{user_id}"
    try:
        val = redis_client.get(latest_key)
        latest_run_id = val.decode("utf-8") if val else ""
    except Exception as exc:
        logger.warning("读取最新推理版本失败: %s", exc)

    if not latest_run_id:
        return {
            "latest_key": latest_key,
            "run_id": "",
            "model_id": "",
            "prediction_trade_date": "",
            "target_date": "",
            "status": "",
            "updated_at": "",
            "matched_model": False if model_id else None,
        }

    run = await model_inference_persistence.get_run(
        run_id=latest_run_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    if not run:
        return {
            "latest_key": latest_key,
            "run_id": latest_run_id,
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
        "run_id": latest_run_id,
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


@router.post("/training-runs/{run_id}/complete", summary="训练完成回调（用户态内部接口）")
async def training_complete_callback(
    run_id: str,
    result: dict[str, Any],
    x_internal_call_secret: str = Header(default="", alias="X-Internal-Call-Secret"),
):
    return await complete_training_run(run_id, result, x_internal_call_secret)
