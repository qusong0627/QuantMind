"""滚动重训（用户态）：读原模型训练任务的完整 payload，整体平移三段窗口后
走标准 submit_training_job 入口，训练完成自动注册为新模型（复用既有回调链路）。

- 参数来源：admin_training_jobs.request_payload（原任务归一化后的完整请求，
  「其他参数不变」有精确保证）；找不到则 404，不做元数据拼凑。
- 日期基准：因子探针 max_date（最新可用交易日）；auto 步长 = 整月差。
- 目录 pin：factor_catalog_version 若已不是当前 published，刷新为最新
  published 版（否则提交必 422），并在 preview/结果中明示。
"""

import copy
import json
import logging
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from backend.services.api.routers.admin.admin_training import submit_training_job
from backend.services.api.routers.admin.db import TrainingJobRecord
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.database_manager_v2 import get_session
from backend.shared.model_registry import model_registry_service
from backend.shared.rolling_window import (
    WINDOW_KEYS,
    RollingWindowError,
    compute_rolled_split,
    parse_ymd,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# 提交时剔除的易变/派生字段（由 submit 链路重新生成）
_DROP_KEYS = (
    "data_window",
    "generated_at",
    "system_notices",
    "tenant_id",
    "user_id",
    "factor_field_sources",
    "factor_schema_hash",
    "factor_catalog_published_at",
    "factor_coverage",
)


class RollingRetrainRequest(BaseModel):
    shift_months: int | None = Field(
        default=None,
        ge=1,
        le=120,
        description="前移月数；为空则 auto（最新数据日期相对原 test_end 的整月差）",
    )


def _read_window(source: dict[str, Any]) -> dict[str, str] | None:
    """从 payload/metadata 读六边界，兼容 val_* 与 valid_* 两种写法。"""
    def pick(*names: str) -> str:
        for name in names:
            value = source.get(name)
            if value:
                return str(value).strip()
        return ""

    window = {
        "train_start": pick("train_start", "trainStart"),
        "train_end": pick("train_end", "trainEnd"),
        "valid_start": pick("valid_start", "validStart", "val_start", "valStart"),
        "valid_end": pick("valid_end", "validEnd", "val_end", "valEnd"),
        "test_start": pick("test_start", "testStart"),
        "test_end": pick("test_end", "testEnd"),
    }
    if not all(window.values()):
        return None
    for key in WINDOW_KEYS:  # 早校验格式，错误归因到原窗口
        parse_ymd(window[key], key)
    return window


def _build_fallback_payload_from_dir(storage_path: str) -> dict[str, Any] | None:
    """集市/导入模型没有任务记录时，用模型目录重建提交载荷。

    来源：metadata.json（窗口/特征/目标/上下文/因子源/名称）+
    config.yaml（超参/轮数/早停/集成/目标/切分/限时/特征筛选）。
    与原提交基本一致；特征固定用目录清单（auto_feature_filter=False）。
    缺关键文件或字段时返回 None。
    """
    model_dir = Path(str(storage_path or "").strip())
    if not model_dir.is_dir():
        return None
    try:
        metadata = json.loads((model_dir / "metadata.json").read_text(encoding="utf-8"))
        config = yaml.safe_load((model_dir / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(metadata, dict) or not isinstance(config, dict):
        return None

    window = _read_window(metadata)
    split_cfg = config.get("split") if isinstance(config.get("split"), dict) else {}
    if window is None and split_cfg:
        window = _read_window(
            {
                "train_start": (split_cfg.get("train") or [None, None])[0],
                "train_end": (split_cfg.get("train") or [None, None])[-1],
                "val_start": (split_cfg.get("valid") or [None, None])[0],
                "val_end": (split_cfg.get("valid") or [None, None])[-1],
                "test_start": (split_cfg.get("test") or [None, None])[0],
                "test_end": (split_cfg.get("test") or [None, None])[-1],
            }
        )
    features = metadata.get("features")
    if window is None or not isinstance(features, list) or not features:
        return None

    model_cfg = config.get("model") if isinstance(config.get("model"), dict) else {}
    label_cfg = config.get("label") if isinstance(config.get("label"), dict) else {}
    model_type = str(metadata.get("model_type") or model_cfg.get("type") or "lightgbm")
    payload: dict[str, Any] = {
        "display_name": str(metadata.get("display_name") or ""),
        "job_name": str(metadata.get("job_name") or metadata.get("display_name") or ""),
        "model_type": model_type,
        **window,
        "features": [str(f) for f in features if str(f).strip()],
        "target_horizon_days": int(
            metadata.get("target_horizon_days")
            or label_cfg.get("target_horizon_days")
            or 1
        ),
        "target_mode": str(
            metadata.get("target_mode") or label_cfg.get("target_mode") or "return"
        ),
        "label_formula": str(
            metadata.get("label_formula") or label_cfg.get("label_formula") or ""
        ),
        "effective_trade_date": str(
            metadata.get("effective_trade_date")
            or label_cfg.get("effective_trade_date")
            or ""
        ),
        "training_window": str(metadata.get("training_window") or ""),
        "num_boost_round": int(model_cfg.get("num_boost_round") or 1000),
        "early_stopping_rounds": int(model_cfg.get("early_stopping_rounds") or 100),
        "ensemble": model_cfg.get("ensemble") or "none",
        "prediction_mode": str(model_cfg.get("prediction_mode") or "point"),
        "max_time_minutes": int(config.get("max_time_minutes") or 120),
        "auto_feature_filter": False,
        "node_id": "local",
        "factor_source": str(metadata.get("factor_source") or ""),
        "context": metadata.get("context")
        if isinstance(metadata.get("context"), dict)
        else {},
    }
    if model_type == "lightgbm" and isinstance(model_cfg.get("params"), dict):
        payload["lgb_params"] = dict(model_cfg["params"])
    for key in ("xgb_params", "catboost_params", "dl_params"):
        if isinstance(model_cfg.get(key), dict) and model_cfg[key]:
            payload[key] = dict(model_cfg[key])
    for key in ("factor_selection", "explain"):
        value = config.get(key)
        if isinstance(value, dict) and value:
            payload[key] = value
        elif isinstance(metadata.get(key), dict) and metadata[key]:
            payload[key] = metadata[key]
    pool_id = str(metadata.get("pool_id") or "").strip()
    if pool_id:
        payload["pool_id"] = pool_id
    return payload


async def _load_rolling_context(
    model_id: str, current_user: dict[str, Any]
) -> dict[str, Any]:
    """加载模型 + 原任务 payload + 探针窗口，返回滚动计算所需的全部上下文。"""
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="用户身份无效")

    model = await model_registry_service.get_model(
        tenant_id=tenant_id, user_id=user_id, model_id=model_id
    )
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    source_run_id = str(model.get("source_run_id") or "").strip()
    if not source_run_id:
        raise HTTPException(
            status_code=422,
            detail="该模型没有训练溯源 run，无法滚动重训",
        )

    async with get_session(read_only=True) as session:
        record = (
            await session.execute(
                select(TrainingJobRecord).where(
                    TrainingJobRecord.id == source_run_id,
                    TrainingJobRecord.tenant_id == tenant_id,
                    TrainingJobRecord.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
    if record is None or not isinstance(record.request_payload, dict):
        # 集市/导入模型：任务记录缺失时用模型目录重建（metadata+config）
        storage_path = str(model.get("storage_path") or "").strip()
        rebuilt = _build_fallback_payload_from_dir(storage_path)
        if rebuilt is None:
            raise HTTPException(
                status_code=404,
                detail=f"找不到原始训练任务 {source_run_id} 的请求记录，"
                "且模型目录缺少可重建的参数（metadata.json/config.yaml），无法保证参数一致",
            )
        payload = rebuilt
        param_source = "dir"
        param_warning = (
            "原训练任务记录缺失，参数由模型目录 metadata.json/config.yaml 重建"
            "（窗口/特征/目标/超参以目录为准，特征筛选已固定为目录清单）"
        )
    else:
        payload = record.request_payload
        param_source = "job"
        param_warning = ""

    metadata = (
        model.get("metadata_json")
        if isinstance(model.get("metadata_json"), dict)
        else {}
    )
    original_window = _read_window(payload) or _read_window(metadata)
    if original_window is None:
        raise HTTPException(
            status_code=422,
            detail="原任务缺少完整的 train/valid/test 窗口，无法滚动",
        )

    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    market = str(context.get("market") or metadata.get("market") or "CN").upper()
    from backend.services.engine.training import window_probe as wp

    source = str(payload.get("factor_source") or metadata.get("factor_source") or "").strip()
    node_id = str(payload.get("node_id") or "local").strip() or "local"
    probe_source = source or wp.DEFAULT_FACTOR_SOURCE
    custom_factor_hint = ""
    if source and source != wp.DEFAULT_FACTOR_SOURCE:
        custom_factor_hint = (
            f"该模型使用自定义因子源 {source}，滚动重训以前移后的日期窗口为准；"
            "如因子口径已变化，结果可能与原模型不可比。"
        )
    try:
        window = await wp.probe_data_window(node_id, probe_source, market=market)
    except Exception as exc:  # noqa: BLE001 - 远程节点失联等，转友好 422
        logger.warning("[RollingRetrain] 探针失败 model=%s node=%s: %s", model_id, node_id, exc)
        if node_id != "local":
            raise HTTPException(
                status_code=422,
                detail=(
                    f"原训练节点 {node_id} 连接失败（{exc}），无法获取最新数据日期。"
                    "请确认该节点在线，或在训练页用本地节点重训。"
                ),
            ) from None
        raise HTTPException(
            status_code=422,
            detail=f"本地数据探针失败（source={probe_source} market={market}）：{exc}",
        ) from None
    if custom_factor_hint:
        warnings_note = custom_factor_hint
    else:
        warnings_note = ""
    if not window.ready or not window.max_date or not window.trading_dates:
        raise HTTPException(
            status_code=422,
            detail=f"数据探针未就绪（node={node_id} source={probe_source} "
            f"market={market}）：{window.reason or '无可用交易日'}",
        )

    catalog_warning = ""
    current_version = ""
    if source:
        # pin 刷新：原 pin 若已不是当前 published，提交必 422，先换新
        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT version_id FROM qm_training_factor_catalog_version "
                        "WHERE status='published' AND source_dataset=:source "
                        "AND market=:market ORDER BY published_at DESC LIMIT 1"
                    ),
                    {"source": source, "market": market},
                )
            ).first()
        if row is None:
            raise HTTPException(
                status_code=422,
                detail=f"因子目录无 published 版本（source={source} market={market}），无法重训",
            )
        current_version = str(row[0])
        old_pin = str(payload.get("factor_catalog_version") or "").strip()
        if old_pin != current_version:
            catalog_warning = (
                f"因子目录 pin 由 {old_pin or '（空）'} 刷新为当前 published "
                f"{current_version}（特征白名单以新版为准）"
            )

    return {
        "model": model,
        "metadata": metadata,
        "payload": payload,
        "custom_factor_hint": warnings_note,
        "param_source": param_source,
        "param_warning": param_warning,
        "source_run_id": source_run_id,
        "market": market,
        "factor_source": source,
        "node_id": node_id,
        "trading_dates": list(window.trading_dates),
        "latest_date": str(window.max_date),
        "original_window": original_window,
        "current_version": current_version,
        "catalog_warning": catalog_warning,
    }


_HYPER_PARAM_KEYS = {
    "lightgbm": "lgb_params",
    "xgboost": "xgb_params",
    "catboost": "catboost_params",
}


def _summarize_params(
    payload: dict[str, Any], window: dict[str, str], catalog_version: str
) -> dict[str, Any]:
    """把完整 payload 折叠为前端可展示的参数摘要（窗口/模型/目标/特征/训练/上下文）。"""
    model_type = str(payload.get("model_type") or "lightgbm")
    hyper: dict[str, Any] = {}
    for key in ("num_boost_round", "early_stopping_rounds"):
        if payload.get(key) is not None:
            hyper[key] = payload.get(key)
    for key in ("lgb_params", "xgb_params", "catboost_params", "dl_params"):
        sub = payload.get(key)
        if isinstance(sub, dict) and sub:
            if key == _HYPER_PARAM_KEYS.get(model_type):
                hyper = {**hyper, **sub}
            else:
                hyper[key] = sub
    preprocessing = payload.get("preprocessing")
    training: dict[str, Any] = {
        "val_ratio": payload.get("val_ratio"),
        "max_time_minutes": payload.get("max_time_minutes"),
        "node_id": payload.get("node_id") or "local",
        "auto_feature_filter": payload.get("auto_feature_filter"),
        "preprocessing": preprocessing,
    }
    ensemble = payload.get("ensemble")
    if ensemble:
        training["ensemble"] = ensemble
    context = payload.get("context")
    features = payload.get("features")
    return {
        "window": dict(window),
        "display_name": str(payload.get("display_name") or ""),
        "job_name": str(payload.get("job_name") or ""),
        "model_type": model_type,
        "hyperparams": hyper,
        "target": {
            "target_horizon_days": payload.get("target_horizon_days"),
            "target_mode": payload.get("target_mode"),
            "label_formula": payload.get("label_formula") or "",
            "effective_trade_date": payload.get("effective_trade_date") or "",
        },
        "features": list(features) if isinstance(features, list) else [],
        "feature_count": len(features) if isinstance(features, list) else 0,
        "training": training,
        "context": dict(context) if isinstance(context, dict) else {},
        "factor": {
            "source": str(payload.get("factor_source") or ""),
            "catalog_version": catalog_version,
        },
    }


def _build_rolling_payload(ctx: dict[str, Any], new_window: dict[str, str]) -> dict[str, Any]:
    """复制原 payload，只换六边界 + 名称 + 目录 pin，其余不动。"""
    payload = copy.deepcopy(ctx["payload"])
    payload.update(new_window)
    for key in _DROP_KEYS:
        payload.pop(key, None)
    if ctx["current_version"]:
        payload["factor_catalog_version"] = ctx["current_version"]

    suffix = f"・滚动重训{new_window['test_end'][:7]}"
    base_display = str(
        payload.get("display_name") or payload.get("job_name") or ctx["model"].get("model_id")
    )
    # display_name/job_name 长度上限 128，超长截断基名保后缀
    payload["display_name"] = (base_display[: 128 - len(suffix)] + suffix)[:128]
    payload["job_name"] = (str(payload.get("job_name") or base_display)[: 128 - len(suffix)] + suffix)[:128]
    return payload


@router.get("/{model_id}/rolling-retrain-preview", summary="滚动重训预览：原窗口→新窗口")
async def rolling_retrain_preview(
    model_id: str,
    shift_months: int | None = None,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    if shift_months is not None and shift_months <= 0:
        raise HTTPException(status_code=422, detail="shift_months 须为正整数")
    ctx = await _load_rolling_context(model_id, current_user)
    try:
        new_window, shift_used, warnings = compute_rolled_split(
            ctx["original_window"],
            ctx["trading_dates"],
            ctx["latest_date"],
            shift_months,
        )
    except RollingWindowError as exc:
        raise HTTPException(status_code=exc.http_status, detail=str(exc)) from None

    payload = ctx["payload"]
    old_pin = str(payload.get("factor_catalog_version") or "").strip()
    original = _summarize_params(payload, ctx["original_window"], old_pin)
    # 目标参数 = 原参数 + 新窗口/新名称/新 pin（其余一字不动）
    target_payload = _build_rolling_payload(ctx, new_window)
    target = _summarize_params(
        target_payload, new_window, target_payload.get("factor_catalog_version", "")
    )
    features = payload.get("features") if isinstance(payload.get("features"), list) else []
    if ctx["param_warning"]:
        warnings.append(ctx["param_warning"])
    if ctx["custom_factor_hint"]:
        warnings.append(ctx["custom_factor_hint"])
    if ctx["catalog_warning"]:
        warnings.append(ctx["catalog_warning"])
    return {
        "model_id": model_id,
        "source_run_id": ctx["source_run_id"],
        "param_source": ctx["param_source"],
        "market": ctx["market"],
        "factor_source": ctx["factor_source"],
        "node_id": ctx["node_id"],
        "model_type": str(payload.get("model_type") or "lightgbm"),
        "feature_count": len(features),
        "latest_date": ctx["latest_date"],
        "shift_months": shift_used,
        "original_window": ctx["original_window"],
        "new_window": new_window,
        "original": original,
        "target": target,
        "warnings": warnings,
    }


@router.post("/{model_id}/rolling-retrain", summary="提交滚动重训（完成后自动注册为新模型）")
async def rolling_retrain(
    model_id: str,
    body: RollingRetrainRequest,
    background_tasks: BackgroundTasks,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    ctx = await _load_rolling_context(model_id, current_user)
    try:
        new_window, shift_used, warnings = compute_rolled_split(
            ctx["original_window"],
            ctx["trading_dates"],
            ctx["latest_date"],
            body.shift_months,
        )
    except RollingWindowError as exc:
        raise HTTPException(status_code=exc.http_status, detail=str(exc)) from None

    if ctx["param_warning"]:
        warnings.append(ctx["param_warning"])
    if ctx["custom_factor_hint"]:
        warnings.append(ctx["custom_factor_hint"])
    if ctx["catalog_warning"]:
        warnings.append(ctx["catalog_warning"])
    new_payload = _build_rolling_payload(ctx, new_window)
    logger.warning(
        "[RollingRetrain] model=%s source_run=%s shift=%s新窗口 test %s~%s",
        model_id,
        ctx["source_run_id"],
        shift_used,
        new_window["test_start"],
        new_window["test_end"],
    )
    result = await submit_training_job(new_payload, background_tasks, current_user)
    result["rolling"] = {
        "base_model_id": model_id,
        "param_source": ctx["param_source"],
        "shift_months": shift_used,
        "original_window": ctx["original_window"],
        "new_window": new_window,
        "target_display_name": str(new_payload.get("display_name") or ""),
        "warnings": warnings,
    }
    return result
