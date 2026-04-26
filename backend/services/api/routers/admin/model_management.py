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

from .model_management_utils import *
from .model_management_utils import _enrich_feature_catalog_with_data_coverage, _load_feature_catalog_from_db, _load_feature_catalog_from_file

@router.post("", response_model=ModelResponse)
async def create_model(
    data: ModelCreate,
    owner_user_id: str | None = Query(None, alias="user_id", description="归属用户ID(可选)"),
    current_user: dict = Depends(require_admin),
):
    """创建模型（管理员权限）"""
    target_user_id = owner_user_id or current_user.get("user_id", "admin")
    tenant_id = current_user.get("tenant_id", "default")

    async with get_session(read_only=False) as session:
        new_model = ModelRecord(
            tenant_id=tenant_id,
            user_id=target_user_id,
            name=data.name,
            description=data.description,
            source_type=data.source_type,
            start_date=data.start_date,
            end_date=data.end_date,
            config=data.config,
        )
        session.add(new_model)
        await session.commit()
        await session.refresh(new_model)
        return new_model


@router.get("", response_model=list[ModelResponse])
async def get_models(
    current_user: dict = Depends(require_admin),
):
    """获取模型列表（从模型目录读取 JSON 文件）"""
    models = []

    if not os.path.exists(MODELS_ROOT):
        # 如果目录不存在，尝试从数据库回退（兼容旧逻辑）
        async with get_session(read_only=True) as session:
            stmt = select(ModelRecord).where(ModelRecord.tenant_id == current_user.get("tenant_id", "default"))
            result = await session.execute(stmt)
            return result.scalars().all()

    # 扫描 .meta.json 文件
    meta_files = glob.glob(os.path.join(MODELS_ROOT, "*.meta.json"))

    for i, meta_path in enumerate(meta_files):
        try:
            with open(meta_path, encoding="utf-8") as f:
                data = json.load(f)

                # 读取文件修改时间作为更新时间
                mtime = datetime.fromtimestamp(os.path.getmtime(meta_path))
                ctime = datetime.fromtimestamp(os.path.getctime(meta_path))

                # 构建兼容 ModelResponse 的模型对象
                model_entry = {
                    "id": i + 1,
                    "name": data.get("version") or os.path.basename(meta_path).replace(".meta.json", ""),
                    "description": f"Weights: {os.path.basename(data.get('output', ''))} | Instruments: {data.get('instruments', 'N/A')}",
                    "source_type": "qlib_model",
                    "start_date": data.get("date_min"),
                    "end_date": data.get("date_max"),
                    "user_id": "system",
                    "is_active": True,
                    "created_at": ctime,
                    "updated_at": mtime,
                }
                models.append(model_entry)
        except Exception as e:
            print(f"Error parsing model meta {meta_path}: {e}")

    return models


from .model_management_ops import router as model_management_ops_router
router.include_router(model_management_ops_router)

@router.post("/run-inference", summary="手动触发每日推理（管理员）")
async def run_inference(
    model_file: str = Query("model.bin", description="模型文件名（保留兼容，实际执行 inference.py）"),
    current_user: dict = Depends(require_admin),
):
    """
    手动执行模型目录中的 inference.py 脚本，完成当日推理并生成信号。

    执行流程：
    1. 获取分布式锁（防止与 Celery Beat 并发执行）
    2. 调用 InferenceScriptRunner 执行 inference.py（超时 10 分钟）
    3. 解析 stdout JSON 信号 → 写入 engine_signal_scores → 发布 Redis Stream
    4. 设置 Redis 完成标记（供自动任务判断是否跳过）
    5. 返回执行结果（exit_code / stdout / stderr / signals_count）

    前置条件：应先调用 GET /precheck-inference 确认通过后再执行。
    """
    tz = ZoneInfo("Asia/Shanghai")
    now_local = datetime.now(tz)
    requested_data_trade_date, data_trade_date, prediction_trade_date, calendar_adjusted = (
        await _resolve_inference_dates_with_calendar(current_user=current_user, now_local=now_local)
    )
    lock_key = f"{_INFERENCE_LOCK_KEY_PREFIX}:{prediction_trade_date}"

    # --- 1. 获取分布式锁（SET NX EX），防止并发 ---
    try:
        redis = get_redis_sentinel_client()
        acquired = redis.set(lock_key, "admin_manual", ex=_INFERENCE_LOCK_TTL_SEC, nx=True)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis 不可用，无法获取并发锁: {e}")

    if not acquired:
        locked_by = ""
        try:
            locked_by = redis.get(lock_key) or ""
        except Exception:
            pass
        raise HTTPException(
            status_code=409,
            detail=(
                f"目标预测日任务（{prediction_trade_date}）已在运行中，请稍后重试。"
                f"（锁持有者: {locked_by}，TTL: {_INFERENCE_LOCK_TTL_SEC}s）"
            ),
        )

    # --- 2. 执行 inference.py ---
    tenant_id = str(current_user.get("tenant_id", "default"))
    user_id = str(current_user.get("user_id", "admin"))
    router_service = InferenceRouterService()
    resolved_model = await router_service.resolve_effective_model(
        tenant_id=tenant_id,
        user_id=user_id,
        model_id=None,
    )

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: router_service.run_daily_inference_script(
                date=data_trade_date,
                tenant_id=tenant_id,
                user_id=user_id,
                redis_client=redis,
                resolved_model=resolved_model,
            ),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"推理脚本执行异常: {e}")
    finally:
        try:
            redis.delete(lock_key)
        except Exception:
            pass

    if not result.success:
        return {
            "success": False,
            "trade_date": prediction_trade_date,
            "requested_inference_date": requested_data_trade_date,
            "calendar_adjusted": calendar_adjusted,
            "data_trade_date": data_trade_date,
            "prediction_trade_date": prediction_trade_date,
            "run_id": result.run_id,
            "exit_code": result.exit_code,
            "signals_count": 0,
            "error": result.error,
            "failure_stage": result.failure_stage,
            "fallback_used": result.fallback_used,
            "fallback_reason": result.fallback_reason,
            "execution_mode": result.execution_mode,
            "model_switch_used": result.model_switch_used,
            "model_switch_reason": result.model_switch_reason,
            "stdout": result.stdout[-2000:] if result.stdout else "",
            "stderr": result.stderr[-2000:] if result.stderr else "",
            "active_model_id": result.active_model_id,
            "active_data_source": result.active_data_source,
        }

    return {
        "success": True,
        "message": f"推理已完成（预测日 {prediction_trade_date}，数据日 {data_trade_date}），共生成 {result.signals_count} 条信号",
        "trade_date": prediction_trade_date,
        "requested_inference_date": requested_data_trade_date,
        "calendar_adjusted": calendar_adjusted,
        "data_trade_date": data_trade_date,
        "prediction_trade_date": prediction_trade_date,
        "run_id": result.run_id,
        "exit_code": result.exit_code,
        "signals_count": result.signals_count,
        "fallback_used": result.fallback_used,
        "fallback_reason": result.fallback_reason,
        "execution_mode": result.execution_mode,
        "model_switch_used": result.model_switch_used,
        "model_switch_reason": result.model_switch_reason,
        "failure_stage": result.failure_stage,
        "stdout": result.stdout[-2000:] if result.stdout else "",
        "stderr": result.stderr[-2000:] if result.stderr else "",
        "active_model_id": result.active_model_id,
        "active_data_source": result.active_data_source,
        "lock_key": lock_key,
        "lock_ttl_sec": _INFERENCE_LOCK_TTL_SEC,
    }


@router.get("/predictions", summary="管理员查询模型预测批次")
async def list_prediction_runs(
    prediction_date: date | None = Query(None, description="预测交易日 YYYY-MM-DD"),
    tenant_id: str | None = Query(None, description="租户ID，可选"),
    user_id: str | None = Query(None, description="用户ID，可选"),
    run_id: str | None = Query(None, description="运行批次ID，可选"),
    model_version: str | None = Query("inference_script", description="模型版本过滤（默认 inference_script）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    current_user: dict = Depends(require_admin),
):
    tenant_scope = str(tenant_id or current_user.get("tenant_id") or "default")
    where_clauses = ["1=1"]
    params: dict[str, Any] = {"tenant_scope": tenant_scope}
    where_clauses.append("s.tenant_id = :tenant_scope")
    if prediction_date:
        where_clauses.append("s.trade_date = :prediction_date")
        params["prediction_date"] = prediction_date
    if user_id:
        where_clauses.append("s.user_id = :user_id")
        params["user_id"] = user_id
    if run_id:
        where_clauses.append("s.run_id = :run_id")
        params["run_id"] = run_id
    if model_version:
        where_clauses.append("s.model_version = :model_version")
        params["model_version"] = model_version

    where_sql = " AND ".join(where_clauses)
    offset = (page - 1) * page_size
    params.update({"limit": page_size, "offset": offset})

    async with get_session(read_only=True) as session:
        total_row = (
            (
                await session.execute(
                    text(
                        f"""
                        SELECT COUNT(*) AS total
                        FROM (
                            SELECT s.run_id, s.trade_date, s.tenant_id, s.user_id, s.model_version
                            FROM engine_signal_scores s
                            WHERE {where_sql}
                            GROUP BY s.run_id, s.trade_date, s.tenant_id, s.user_id, s.model_version
                        ) t
                        """
                    ),
                    params,
                )
            )
            .mappings()
            .first()
        )
        total = int((total_row or {}).get("total") or 0)

        rows = (
            (
                await session.execute(
                    text(
                        f"""
                        SELECT
                            s.run_id,
                            s.trade_date,
                            s.tenant_id,
                            s.user_id,
                            s.model_version,
                            COUNT(*) AS rows_count,
                            COUNT(DISTINCT s.symbol) AS symbols_count,
                            MIN(s.fusion_score) AS min_fusion_score,
                            MAX(s.fusion_score) AS max_fusion_score,
                            MIN(s.created_at) AS first_created_at,
                            MAX(s.created_at) AS last_created_at
                        FROM engine_signal_scores s
                        WHERE {where_sql}
                        GROUP BY s.run_id, s.trade_date, s.tenant_id, s.user_id, s.model_version
                        ORDER BY MAX(s.created_at) DESC
                        LIMIT :limit OFFSET :offset
                        """
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )

        items = []
        for row in rows:
            item = dict(row or {})
            for dt_key in ("first_created_at", "last_created_at"):
                if item.get(dt_key) is not None:
                    item[dt_key] = item[dt_key].isoformat()
            items.append(item)

    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "items": items,
    }


@router.get("/predictions/{run_id}", summary="管理员查看预测批次明细")
async def get_prediction_run_detail(
    run_id: str,
    prediction_date: date | None = Query(None, description="预测交易日 YYYY-MM-DD，可选"),
    tenant_id: str | None = Query(None, description="租户ID，可选"),
    user_id: str | None = Query(None, description="用户ID，可选"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    current_user: dict = Depends(require_admin),
):
    tenant_scope = str(tenant_id or current_user.get("tenant_id") or "default")
    where_clauses = ["run_id = :run_id"]
    params: dict[str, Any] = {"run_id": run_id, "tenant_scope": tenant_scope}
    where_clauses.append("tenant_id = :tenant_scope")
    if prediction_date:
        where_clauses.append("trade_date = :prediction_date")
        params["prediction_date"] = prediction_date
    if user_id:
        where_clauses.append("user_id = :user_id")
        params["user_id"] = user_id
    where_sql = " AND ".join(where_clauses)
    offset = (page - 1) * page_size
    params.update({"limit": page_size, "offset": offset})

    async with get_session(read_only=True) as session:
        total_row = (
            (
                await session.execute(
                    text(f"SELECT COUNT(*) AS total FROM engine_signal_scores WHERE {where_sql}"),
                    params,
                )
            )
            .mappings()
            .first()
        )
        total = int((total_row or {}).get("total") or 0)
        if total <= 0:
            raise HTTPException(status_code=404, detail=f"预测批次不存在: {run_id}")

        summary_row = (
            (
                await session.execute(
                    text(
                        f"""
                        SELECT
                            run_id,
                            MAX(trade_date) AS trade_date,
                            MAX(tenant_id) AS tenant_id,
                            MAX(user_id) AS user_id,
                            MAX(model_version) AS model_version,
                            COUNT(*) AS rows_count,
                            COUNT(DISTINCT symbol) AS symbols_count,
                            MIN(fusion_score) AS min_fusion_score,
                            MAX(fusion_score) AS max_fusion_score,
                            MIN(created_at) AS first_created_at,
                            MAX(created_at) AS last_created_at
                        FROM engine_signal_scores
                        WHERE {where_sql}
                        GROUP BY run_id
                        """
                    ),
                    params,
                )
            )
            .mappings()
            .first()
        )
        summary = dict(summary_row or {})
        for dt_key in ("first_created_at", "last_created_at"):
            if summary.get(dt_key) is not None:
                summary[dt_key] = summary[dt_key].isoformat()
        if summary.get("trade_date") is not None:
            summary["trade_date"] = str(summary["trade_date"])

        detail_rows = (
            (
                await session.execute(
                    text(
                        f"""
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
                        WHERE {where_sql}
                        ORDER BY fusion_score DESC NULLS LAST, symbol ASC
                        LIMIT :limit OFFSET :offset
                        """
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        items = []
        for row in detail_rows:
            item = dict(row or {})
            if item.get("created_at") is not None:
                item["created_at"] = item["created_at"].isoformat()
            items.append(item)

    return {
        "summary": summary,
        "page": page,
        "page_size": page_size,
        "total": total,
        "items": items,
    }


@router.get("/predictions/{run_id}/export", summary="管理员导出预测批次CSV")
async def export_prediction_run_csv(
    run_id: str,
    prediction_date: date | None = Query(None),
    tenant_id: str | None = Query(None),
    user_id: str | None = Query(None),
    current_user: dict = Depends(require_admin),
):
    from io import StringIO
    import csv
    from fastapi.responses import StreamingResponse

    tenant_scope = str(tenant_id or current_user.get("tenant_id") or "default")
    where_clauses = ["run_id = :run_id", "tenant_id = :tenant_scope"]
    params: dict[str, Any] = {"run_id": run_id, "tenant_scope": tenant_scope}

    if prediction_date:
        where_clauses.append("trade_date = :prediction_date")
        params["prediction_date"] = prediction_date
    if user_id:
        where_clauses.append("user_id = :user_id")
        params["user_id"] = user_id

    where_sql = " AND ".join(where_clauses)

    async with get_session(read_only=True) as session:
        # Fetch ALL items for export (no pagination)
        stmt = text(f"""
            SELECT 
                symbol, fusion_score, light_score, tft_score, 
                score_rank, signal_side, expected_price, created_at
            FROM engine_signal_scores
            WHERE {where_sql}
            ORDER BY fusion_score DESC NULLS LAST, symbol ASC
        """)
        result = await session.execute(stmt, params)
        rows = result.mappings().all()

    if not rows:
        raise HTTPException(status_code=404, detail="该批次无预测数据")

    # Generate CSV in memory
    output = StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow([
        "symbol", "fusion_score", "light_score", "tft_score",
        "rank", "side", "expected_price", "created_at"
    ])

    # Data rows
    for row in rows:
        writer.writerow([
            row["symbol"],
            row["fusion_score"],
            row["light_score"],
            row["tft_score"],
            row["score_rank"],
            row["signal_side"],
            row["expected_price"],
            row["created_at"].isoformat() if row["created_at"] else ""
        ])

    output.seek(0)
    filename = f"prediction_{run_id}_{prediction_date or 'export'}.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.get("/{model_id}", response_model=ModelResponse)
async def get_model(
    model_id: int,
    current_user: dict = Depends(require_admin),
):
    """获取单个模型（管理员权限）"""
    tenant_id = current_user.get("tenant_id", "default")
    async with get_session(read_only=True) as session:
        stmt = select(ModelRecord).where(ModelRecord.id == model_id, ModelRecord.tenant_id == tenant_id)
        result = await session.execute(stmt)
        model = result.scalar_one_or_none()
        if not model:
            raise HTTPException(status_code=404, detail="模型不存在")
        return model


@router.get("/directory/{model_path:path}", summary="获取指定模型目录详情")
async def get_model_directory_detail(
    model_path: str,
    current_user: dict = Depends(require_admin),
):
    """
    返回指定模型目录（相对 models/ 的路径，如 production/model_qlib）的完整元数据。
    """
    abs_path = os.path.abspath(os.path.join(MODELS_ROOT, model_path))
    # 防止路径穿越
    if not abs_path.startswith(MODELS_ROOT):
        raise HTTPException(status_code=400, detail="非法路径")
    if not os.path.isdir(abs_path):
        raise HTTPException(status_code=404, detail=f"目录不存在: {model_path}")

    try:
        return _scan_model_directory(abs_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取模型目录失败: {e}")


async def ensure_admin_tables():
    """确保管理员相关表存在 - 已禁用自动建表"""
    # 表结构由 quantmind_init.sql 初始化
    pass
