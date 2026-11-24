"""训练数据缓存（D3）用户态端点：探测 / 清理。

缓存键与训练侧完全同源（`backend/shared/train_cache.build_cache_key`）：
锚库指纹 + 所有副库指纹 + 窗口 + horizon + 标签口径 + target_hash + 特征 +
field_sources + 股票池 + industry 开关 + code_version。模型类型/超参不在键内——
同特征同窗口换模型可直接命中。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.services.api.user_app.middleware.auth import get_current_user, require_admin
from backend.shared.train_cache import (
    TRAIN_CACHE_CODE_VERSION,
    build_cache_key,
    combine_source_stamps,
    default_cache_dir,
    describe_cache,
    source_stamp,
)

router = APIRouter()
_EXECUTION_LAG_DAYS = 1  # 与 docker/training/data/splits.py 一致（T+1 执行）


class CacheStatusRequest(BaseModel):
    market: str = "CN"
    factor_source: str = Field(..., min_length=1)
    features: list[str] = Field(default_factory=list)
    factor_field_sources: dict[str, str] = Field(default_factory=dict)
    pool_symbols: list[str] | None = None
    train_start: date
    train_end: date
    valid_end: date | None = None
    test_end: date | None = None
    target_horizon_days: int = 5
    target_mode: str = "return"
    label_transform: str = "rank"
    label_top_pct: float = 0.2
    execution_price: str = "close"
    target_hash: str = ""
    industry_as_feature: bool = False
    quantdb_dir: str | None = None


def _build_key(req: CacheStatusRequest) -> str:
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        QuantDBFactorReader,
    )

    reader = QuantDBFactorReader(req.quantdb_dir, market=req.market)
    status = reader.describe(req.factor_source)
    stamp = source_stamp(status, reader.source_path(req.factor_source))
    field_stamps: dict[str, str] = {}
    for lib in sorted({
        str(v).split(":", 1)[0]
        for v in req.factor_field_sources.values()
        if ":" in str(v)
    }):
        if lib == req.factor_source:
            continue
        try:
            st = reader.describe(lib)
            field_stamps[lib] = source_stamp(st, reader.source_path(lib))
        except Exception:  # noqa: BLE001 - 副库不可读 → 保守失效
            field_stamps[lib] = "unavailable"
    stamp = combine_source_stamps(stamp, field_stamps)
    return build_cache_key(
        market=req.market,
        factor_source=req.factor_source,
        source_stamp_value=stamp,
        train_start=req.train_start,
        train_end=req.train_end,
        valid_end=req.valid_end,
        test_end=req.test_end,
        horizon=req.target_horizon_days,
        target_mode=req.target_mode,
        label_transform=req.label_transform,
        label_top_pct=req.label_top_pct,
        execution_price=req.execution_price,
        execution_lag_days=_EXECUTION_LAG_DAYS,
        target_hash=req.target_hash,
        features=req.features,
        factor_field_sources=req.factor_field_sources,
        pool_symbols=req.pool_symbols,
        industry_as_feature=req.industry_as_feature,
        code_version=TRAIN_CACHE_CODE_VERSION,
    )


@router.post("/training-cache/status", summary="探测训练数据缓存是否命中（用户态）")
async def training_cache_status(
    req: CacheStatusRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    key = _build_key(req)
    info = describe_cache(default_cache_dir(), key)
    out: dict[str, Any] = {
        "hit": info is not None,
        "key": key,
        "cache_dir": str(default_cache_dir()),
    }
    if info:
        out.update(info)
    return out


@router.delete("/training-cache/{key}", summary="清理指定训练缓存（管理员）")
async def delete_training_cache(
    key: str,
    current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    if not key or any(ch not in "0123456789abcdef" for ch in key.lower()):
        raise HTTPException(status_code=400, detail="invalid_cache_key")
    base = default_cache_dir()
    parquet = base / f"train_{key}.parquet"
    meta = base / f"train_{key}.json"
    if not parquet.exists() and not meta.exists():
        raise HTTPException(status_code=404, detail="cache_not_found")
    parquet.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)
    return {"deleted": True, "key": key}
