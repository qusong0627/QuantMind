"""实时推理管理 API（T-P6-08 收口）：配置读写 + 状态（admin）。

- `GET  /api/v1/admin/realtime/infer/config` —— 当前配置 + 服务状态镜像（Redis
  ``qm:realtime:infer:status``，引擎侧每周期写）；
- `POST /api/v1/admin/realtime/infer/config` —— 保存配置（热生效，无需重启）：
  ``enabled / model_dir / cadence_s / override_whitelist / min_live_coverage``。

**机构级校验（fail-fast，资金相关不静默降级）**：
- ``model_dir`` 必须存在且含 metadata.json、feature_columns 非空（启用时必填）；
- ``override_whitelist`` 必须 ⊆ 模型 feature_columns（写错列名=静默错分险，直接 400）；
- ``cadence_s ∈ [3, 600]``、``min_live_coverage ∈ [0, 1]``。

值守口径：覆盖率闸门默认 0.5——行情未到达时不发布"伪实时"信号；显式下调该值即运维
确认接受 T-1 基线条目（面板会标注现值的含义）。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.services.api.user_app.middleware.auth import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)])  # 前缀由 admin 聚合器挂（/realtime）

CONFIG_KEY = "qm:realtime:infer:config"
STATUS_KEY = "qm:realtime:infer:status"


class InferConfigRequest(BaseModel):
    enabled: bool | None = None
    model_dir: str | None = None
    cadence_s: float | None = Field(default=None, ge=3.0, le=600.0)
    override_whitelist: list[str] | None = None
    min_live_coverage: float | None = Field(default=None, ge=0.0, le=1.0)


def _redis():
    import redis as _redis

    return _redis.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=int(os.getenv("REDIS_DB", "0")),
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


def validate_model_dir(model_dir: str) -> list[str]:
    """模型目录校验；返回 feature_columns（目录非法抛 ValueError）。"""
    from pathlib import Path

    path = Path(str(model_dir or "").strip())
    if not path.is_dir():
        raise ValueError(f"模型目录不存在: {model_dir!r}")
    meta_path = path / "metadata.json"
    if not meta_path.is_file():
        raise ValueError(f"模型目录缺 metadata.json: {model_dir!r}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"metadata.json 解析失败: {exc}") from exc
    cols = [str(c) for c in (meta.get("feature_columns") or []) if str(c).strip()]
    if not cols:
        raise ValueError(f"metadata.feature_columns 为空: {model_dir!r}")
    return cols


BASELINE_PARQUET_TMPL = "/app/db/feature_snapshots/model_features_{year}.parquet"
MIN_COLUMN_COVERAGE = 0.5


def feature_coverage(model_dir: str, feature_columns: list[str]) -> tuple[int, int, float]:
    """模型特征列在实时基线 parquet 中的覆盖率（0~1）。

    机构级防线（2026-09-17 实测）：覆盖率过低（如 273 列自定义模型仅 8% 命中）时，
    打分矩阵 92% 走 fill 值 = **垃圾分冒充实时信号** → 配置层直接拒绝。
    """
    import datetime as _dt
    from pathlib import Path

    path = Path(BASELINE_PARQUET_TMPL.format(year=_dt.datetime.now().year))
    if not path.is_file():
        return (0, len(feature_columns), 0.0)
    try:
        import pyarrow.parquet as _pq

        available = set(_pq.ParquetFile(path).schema_arrow.names)
    except Exception:  # noqa: BLE001
        return (0, len(feature_columns), 0.0)
    hit = sum(1 for c in feature_columns if c in available)
    total = max(1, len(feature_columns))
    return (hit, len(feature_columns), hit / total)


def validate_feature_coverage(model_dir: str, feature_columns: list[str]) -> None:
    hit, total, ratio = feature_coverage(model_dir, feature_columns)
    if ratio < MIN_COLUMN_COVERAGE:
        raise ValueError(
            f"模型特征覆盖率过低（{hit}/{total}={ratio:.0%} < {MIN_COLUMN_COVERAGE:.0%}）："
            "基线 parquet 未收录大部分特征，实时打分将大量走 fill 值（疑似错误模型）"
        )


def validate_override_whitelist(whitelist: list[str] | None, feature_columns: list[str]) -> None:
    """白名单必须 ⊆ 模型特征列（防写错列名造成静默错分）。"""
    unknown = [c for c in (whitelist or []) if c not in set(feature_columns)]
    if unknown:
        raise ValueError(f"override 白名单含模型未定义列: {unknown[:6]}（防静默错分，拒绝）")


@router.get("/infer/config")
async def get_infer_config() -> dict[str, Any]:
    """当前配置 + 服务状态镜像（只读）。"""
    try:
        client = _redis()
        try:
            config = client.hgetall(CONFIG_KEY) or {}
            status = client.hgetall(STATUS_KEY) or {}
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"Redis 不可读: {exc}"}
    counters: dict[str, Any] = {}
    if status.get("counters"):
        try:
            counters = json.loads(status["counters"])
        except (TypeError, ValueError):
            counters = {}
    return {
        "success": True,
        "data": {
            "config": config,
            "status": {
                "updated_at": status.get("updated_at"),
                "counters": counters,
                "source": "redis:qm:realtime:infer:status（引擎侧每周期镜像）",
            },
        },
    }


@router.post("/infer/config")
async def set_infer_config(payload: InferConfigRequest) -> dict[str, Any]:
    """保存配置（热生效）。校验失败 400（含模型目录与白名单列名校验）。"""
    try:
        client = _redis()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Redis 不可用: {exc}") from exc
    try:
        current = client.hgetall(CONFIG_KEY) or {}
        updates: dict[str, str] = {}
        if payload.enabled is not None:
            updates["enabled"] = "true" if payload.enabled else "false"
        if payload.cadence_s is not None:
            updates["cadence_s"] = str(float(payload.cadence_s))
        if payload.min_live_coverage is not None:
            updates["min_live_coverage"] = str(float(payload.min_live_coverage))

        model_dir = (
            str(payload.model_dir).strip()
            if payload.model_dir is not None
            else str(current.get("model_dir") or "").strip()
        )
        feature_columns: list[str] | None = None
        if model_dir:
            try:
                feature_columns = validate_model_dir(model_dir)
                validate_feature_coverage(model_dir, feature_columns)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if payload.model_dir is not None:
            updates["model_dir"] = model_dir

        # 白名单：显式传入则对（新/现）模型列校验；未传但模型目录变更 → 清空（防旧列残留）
        if payload.override_whitelist is not None:
            if feature_columns is None:
                raise HTTPException(status_code=400, detail="override_whitelist 需先配置有效 model_dir")
            try:
                validate_override_whitelist(payload.override_whitelist, feature_columns)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            updates["override_whitelist"] = ",".join(
                c.strip() for c in payload.override_whitelist if c.strip()
            )
        elif payload.model_dir is not None and (current.get("override_whitelist") or "").strip():
            updates["override_whitelist"] = ""

        enabling = updates.get("enabled") == "true" or (
            "enabled" not in updates and str(current.get("enabled") or "").lower() in {"1", "true"}
        )
        if enabling and not model_dir:
            raise HTTPException(status_code=400, detail="启用实时推理必须先配置有效 model_dir")

        if updates:
            client.hset(CONFIG_KEY, mapping=updates)
        return {
            "success": True,
            "data": {"config": client.hgetall(CONFIG_KEY) or {}, "updated": sorted(updates)},
        }
    finally:
        client.close()
