import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.services.engine.auth_context import get_authenticated_identity
from backend.services.engine.inference import InferenceRouterService, InferenceService
from backend.shared.model_registry import model_registry_service

logger = logging.getLogger(__name__)
router = APIRouter()

# Initialize service
inference_service = InferenceService()
inference_router_service = InferenceRouterService(inference_service=inference_service)


# Request/Response Models


class PredictionRequest(BaseModel):
    model_id: str | None = None
    strategy_id: str | None = None
    data: dict[str, Any] | list[dict[str, Any]]
    model_config = {"protected_namespaces": ()}


class ModelLoadRequest(BaseModel):
    model_id: str
    model_config = {"protected_namespaces": ()}


class PredictionResponse(BaseModel):
    status: str
    model_id: str | None = None
    predictions: list[float] | None = None
    input_shape: tuple | None = None
    symbols: list[str] | None = None
    fallback_used: bool | None = None
    fallback_reason: str | None = None
    active_model_id: str | None = None
    effective_model_id: str | None = None
    model_source: str | None = None
    active_data_source: str | None = None
    error: str | None = None
    model_config = {"protected_namespaces": ()}


class ModelInfo(BaseModel):
    status: str
    model_id: str
    metadata: dict[str, Any] | None = None
    error: str | None = None
    model_config = {"protected_namespaces": ()}


def _model_cache_key(tenant_id: str, user_id: str) -> str:
    """模型内存缓存的租户/用户命名空间，与 /predict 的 cache_namespace 口径一致。"""
    return f"{tenant_id}:{user_id}"


async def _assert_model_owned(model_id: str, tenant_id: str, user_id: str) -> dict[str, Any]:
    """校验模型归属，返回注册表记录；不属于该用户一律 404（不泄露存在性）。"""
    record = await model_registry_service.get_model(
        tenant_id=tenant_id, user_id=user_id, model_id=model_id
    )
    if not record:
        raise HTTPException(status_code=404, detail=f"Model {model_id} not found")
    return record


async def _resolve_owned_model_dir(model_id: str, tenant_id: str, user_id: str) -> Path:
    """校验归属并返回模型目录。

    原实现用 InferenceService 的 production_dir 解析，看不到用户模型，
    导致所有用户模型都返回 404；这里改走注册表的 storage_path。
    """
    record = await _assert_model_owned(model_id, tenant_id, user_id)
    storage_path = str(record.get("storage_path") or "").strip()
    if not storage_path:
        raise HTTPException(status_code=409, detail=f"Model {model_id} has no storage_path")
    model_dir = Path(storage_path)
    if not model_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Model directory missing: {storage_path}")
    return model_dir


@router.get("/models")
async def list_models(http_request: Request):
    """列出当前登录用户自己的模型。

    原实现调用不存在的 `InferenceService.list_models()`，恒抛 AttributeError → 500，
    且没有任何归属维度（会暴露全部用户模型）。
    """
    try:
        user_id, tenant_id = get_authenticated_identity(http_request)
        cache_key = _model_cache_key(tenant_id, user_id)
        records = await model_registry_service.list_models(
            tenant_id=tenant_id, user_id=user_id
        )
        models: list[dict[str, Any]] = []
        for record in records:
            mid = str(record.get("model_id") or "").strip()
            if not mid:
                continue
            models.append(
                {
                    "model_id": mid,
                    "status": record.get("status"),
                    "is_default": bool(record.get("is_default")),
                    "storage_path": record.get("storage_path"),
                    "loaded": inference_service.model_loader.get_model(
                        mid, cache_key=cache_key
                    )
                    is not None,
                }
            )
        return {"status": "success", "count": len(models), "models": models}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list models: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/models/{model_id}")
async def get_model_info(model_id: str, http_request: Request):
    """Get detailed information about a specific model."""
    try:
        user_id, tenant_id = get_authenticated_identity(http_request)
        model_dir = await _resolve_owned_model_dir(model_id, tenant_id, user_id)
        info = inference_service.get_model_info(model_id, model_dir=model_dir)
        if info is None:
            raise HTTPException(status_code=404, detail=f"Model {model_id} not found")
        return {"status": "success", "model": info}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get model info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/models/load")
async def load_model(request: ModelLoadRequest, http_request: Request) -> ModelInfo:
    """Load a model into memory."""
    try:
        user_id, tenant_id = get_authenticated_identity(http_request)
        model_dir = await _resolve_owned_model_dir(request.model_id, tenant_id, user_id)
        inference_service.model_loader.load_model(
            request.model_id,
            model_dir=model_dir,
            cache_key=_model_cache_key(tenant_id, user_id),
        )
        info = inference_service.get_model_info(request.model_id, model_dir=model_dir)
        return {"status": "success", "model_id": request.model_id, "metadata": info}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to load model: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/models/{model_id}")
async def unload_model(model_id: str, http_request: Request):
    """Unload a model from memory（仅作用于调用者自己的缓存命名空间）。"""
    try:
        user_id, tenant_id = get_authenticated_identity(http_request)
        await _assert_model_owned(model_id, tenant_id, user_id)
        removed = inference_service.model_loader.unload_model(
            model_id, cache_key=_model_cache_key(tenant_id, user_id)
        )
        return {"status": "success", "model_id": model_id, "unloaded": removed}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to unload model: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/predict")
async def predict(request: PredictionRequest, http_request: Request) -> PredictionResponse:
    """Generate prediction using a loaded model."""
    trace_id = f"predict_{uuid.uuid4().hex[:12]}"
    try:
        auth_user_id, auth_tenant_id = get_authenticated_identity(http_request)
        result = await inference_router_service.predict_with_fallback_async(
            request.model_id or "",
            request.data,
            tenant_id=auth_tenant_id,
            user_id=auth_user_id,
            strategy_id=request.strategy_id,
            trace_id=trace_id,
        )
        if result["status"] == "error":
            raise HTTPException(status_code=400, detail=result.get("error", "Prediction failed"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Prediction failed, trace_id=%s error=%s", trace_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/buffer/stats")
async def buffer_stats():
    """Get history buffer statistics for monitoring."""
    try:
        stats = inference_service.get_buffer_stats()
        return {"status": "success", "buffer": stats}
    except Exception as e:
        logger.error(f"Failed to get buffer stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))
