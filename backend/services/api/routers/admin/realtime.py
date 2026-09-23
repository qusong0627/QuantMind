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

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.services.api.user_app.middleware.auth import require_admin
from backend.shared.quantdb_paths import resolve_pinned_data_dir

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)])  # 前缀由 admin 聚合器挂（/realtime）

CONFIG_KEY = "qm:realtime:infer:config"
STATUS_KEY = "qm:realtime:infer:status"
MODELS_ROOT = Path(os.getenv("QM_MODELS_ROOT", "/app/models"))


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


def _model_meta(model_dir: str) -> dict[str, Any]:
    """模型 metadata.json → dict；不可读 → 空 dict（调用方回落默认口径）。"""
    from pathlib import Path

    try:
        return json.loads(
            (Path(str(model_dir or "")) / "metadata.json").read_text(encoding="utf-8")
        )
    except Exception:  # noqa: BLE001
        return {}


def baseline_source_label(model_dir: str) -> str:
    """基线取数面描述（管理面透明化：读哪、按什么口径）。"""
    source = str(_model_meta(model_dir).get("data_source") or "").strip()
    if source == "quantdb_factors":
        return "quantdb_factors 直读（QuantDBFactorReader，与批量推理同源）"
    if source:
        return f"{source} → 遗留快照 parquet"
    return "遗留快照 parquet（model_features_{year}）"


def _quantdb_reader_for_meta(meta: dict[str, Any]) -> tuple[Any, str] | None:
    """quantdb 绑定模型的 ``(reader, 锚源)``；不可判 → None（回落 parquet 口径）。"""
    try:
        from backend.services.engine.data_platform.quantdb_factor_reader import (
            QuantDBFactorReader,
        )

        source = str(meta.get("factor_source") or "l1_l2_factors")
        # 死 pin 直接交给 reader 会静默返回 0 列 → 覆盖率 0% → 配置写入被 400 拒绝
        # （实测：训练节点写死的 /tmp/quantdb_data 让整批 CN 模型都改不了模型）。
        pinned = resolve_pinned_data_dir(meta.get("quantdb_dir"))
        market = (
            str((meta.get("context") or {}).get("market") or "").strip().upper() or None
        )
        reader = QuantDBFactorReader(str(pinned) if pinned else None, market=market)
        return reader, source
    except Exception:  # noqa: BLE001
        return None


def feature_coverage(model_dir: str, feature_columns: list[str]) -> tuple[int, int, float]:
    """模型特征列在**其基线取数面**的覆盖率（0~1）。

    - ``data_source=quantdb_factors`` 绑定 → 按因子源列判定（2026-09-17 迁移：
      运行时直读同一源，校验必须同口径，否则对 quantdb 模型拿遗留 parquet 判定=误导）；
    - 其余 → 遗留快照 parquet（原口径）。

    机构级防线（2026-09-17 实测）：覆盖率过低（如 273 列自定义模型仅 8% 命中）时，
    打分矩阵 92% 走 fill 值 = **垃圾分冒充实时信号** → 配置层直接拒绝。
    """
    meta = _model_meta(model_dir)
    if str(meta.get("data_source") or "").strip() == "quantdb_factors":
        resolved = _quantdb_reader_for_meta(meta)
        if resolved is not None:
            from backend.services.engine.data_platform.quantdb_factor_reader import (
                split_features_by_availability,
            )

            reader, source = resolved
            mapping = {
                str(k): str(v) for k, v in (meta.get("factor_field_sources") or {}).items()
            }
            # 跨库组合（"库:列"）逐库对照：只比锚库列集会把副库特征整片判为未覆盖，
            # 触发"覆盖率过低"误拒。
            valid, _missing = split_features_by_availability(
                reader, feature_columns, mapping, anchor=source
            )
            total = max(1, len(feature_columns))
            return (len(valid), len(feature_columns), len(valid) / total)

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
            f"基线取数面（{baseline_source_label(model_dir)}）未收录大部分特征，"
            "实时打分将大量走 fill 值（疑似错误模型）"
        )


def validate_override_whitelist(whitelist: list[str] | None, feature_columns: list[str]) -> None:
    """白名单必须 ⊆ 模型特征列（防写错列名造成静默错分）。"""
    unknown = [c for c in (whitelist or []) if c not in set(feature_columns)]
    if unknown:
        raise ValueError(f"override 白名单含模型未定义列: {unknown[:6]}（防静默错分，拒绝）")


def _onnx_status(model_dir: str) -> dict[str, Any]:
    """当前模型 ONNX 产物状态（只读；不触发导出——导出由引擎自动链或手动端点负责）。"""
    path = Path(str(model_dir or "").strip())
    onnx_path = path / "model.onnx"
    try:
        if onnx_path.is_file():
            stat = onnx_path.stat()
            return {
                "ready": True,
                "path": str(onnx_path),
                "size_bytes": int(stat.st_size),
                "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        return {"ready": False, "path": str(onnx_path), "size_bytes": None, "mtime": None}
    except OSError:
        return {"ready": False, "path": str(onnx_path), "size_bytes": None, "mtime": None}


def _ensure_under_models_root(model_dir: str) -> Path:
    """导出目标必须位于模型根目录内（防路径穿越）；返回 resolve 后路径。"""
    root = MODELS_ROOT.resolve()
    p = Path(str(model_dir or "").strip()).resolve()
    if p != root and root not in p.parents:
        raise HTTPException(status_code=400, detail="model_dir 必须位于模型根目录内")
    return p


async def _load_model_display_names(model_ids: list[str]) -> dict[str, str]:
    """model_id(=目录名) → 中文显示名（qm_user_models.metadata_json.display_name）。

    显示名权威源是 PG 注册表（模型目录 metadata.json 里没有）；缺失不猜，回落空串。
    """
    ids = sorted({str(m).strip() for m in model_ids if str(m).strip()})
    if not ids:
        return {}
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    out: dict[str, str] = {}
    try:
        async with get_session(read_only=True) as db:
            rows = (
                await db.execute(
                    _text(
                        "SELECT model_id, metadata_json FROM qm_user_models "
                        "WHERE model_id = ANY(:ids)"
                    ),
                    {"ids": ids},
                )
            ).fetchall()
        for model_id, meta_raw in rows:
            meta = meta_raw if isinstance(meta_raw, dict) else json.loads(meta_raw or "{}")
            meta = meta if isinstance(meta, dict) else {}
            name = str(meta.get("display_name") or meta.get("model_name") or "").strip()
            if name:
                out[str(model_id)] = name
    except Exception as exc:  # noqa: BLE001 - 显示名缺失不判失败
        logger.warning("[RealtimeAdmin] 模型显示名查询失败: %s", exc)
    return out


async def list_infer_models(limit: int = 60) -> list[dict[str, Any]]:
    """实时推理候选模型（CN，轻量扫描）：users/{tenant}/{user}/mdl_cn*/metadata.json。

    只报目录/显示名/ONNX 有无/更新时间——不做特征覆盖等重校验（保存时由 POST 校验兜底）。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pattern in ("users/*/*/mdl_cn*/metadata.json", "mdl_cn*/metadata.json"):
        for meta_path in MODELS_ROOT.glob(pattern):
            d = meta_path.parent
            key = str(d)
            if key in seen:
                continue
            seen.add(key)
            meta = _model_meta(key)
            try:
                mtime = datetime.fromtimestamp(
                    meta_path.stat().st_mtime, tz=timezone.utc
                ).isoformat()
            except OSError:
                mtime = ""
            out.append(
                {
                    "model_dir": key,
                    "name": str(
                        meta.get("display_name") or meta.get("model_name") or d.name
                    ),
                    "display_name": str(meta.get("display_name") or "").strip(),
                    "dir_name": d.name,
                    "has_onnx": (d / "model.onnx").is_file(),
                    "feature_count": len(meta.get("feature_columns") or []),
                    "updated_at": mtime,
                }
            )
    names = await _load_model_display_names([item["dir_name"] for item in out])
    for item in out:
        dn = names.get(item["dir_name"], "")
        if dn:
            item["display_name"] = dn
            item["name"] = dn
    out.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    return out[:limit]


@router.get("/infer/models")
async def get_infer_models() -> dict[str, Any]:
    """实时推理候选模型列表（CN；供前端选择 model_dir，含中文显示名）。"""
    return {
        "success": True,
        "data": {"models": await list_infer_models()},
    }


class ExportOnnxRequest(BaseModel):
    model_dir: str | None = None


@router.post("/infer/export-onnx")
async def export_onnx(payload: ExportOnnxRequest) -> dict[str, Any]:
    """手动导出/重建 ONNX（修复缺失/损坏的 model.onnx；含原模型对照校验，结果如实返回）。

    不传 model_dir 时导出当前配置的模型。导出产物替换模型目录内 model.onnx；
    已加载的 ONNX session 保持旧值直到下次会话加载（停/启用或进程重启后生效）。
    """
    target = str(payload.model_dir or "").strip()
    if not target:
        try:
            client = _redis()
            try:
                target = str((client.hgetall(CONFIG_KEY) or {}).get("model_dir") or "").strip()
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"Redis 不可用: {exc}") from exc
    if not target:
        raise HTTPException(status_code=400, detail="未指定 model_dir 且当前配置无模型")
    path = _ensure_under_models_root(target)
    try:
        validate_model_dir(str(path))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    from backend.services.engine.inference.onnx_exporter import export_model_to_onnx

    try:
        report = await asyncio.to_thread(
            export_model_to_onnx, str(path), output_path=path / "model.onnx"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[RealtimeAdmin] ONNX 导出异常 %s: %s", path, exc)
        report = {"ok": False, "reason": str(exc)[:300]}
    return {
        "success": True,
        "data": {
            "model_dir": str(path),
            "report": report,
            "model_onnx": _onnx_status(str(path)),
        },
    }


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
    # 治理器快照（引擎进程内状态的镜像）：近窗 p95 时延 / 降级阶梯 / 生效节拍。
    # 坏 JSON 或缺失都当 None —— 面板少一格，不能整卡 500。
    governor: dict[str, Any] | None = None
    if status.get("governor"):
        try:
            parsed = json.loads(status["governor"])
            governor = parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            governor = None
    model_dir = str(config.get("model_dir") or "").strip()
    display_names = await _load_model_display_names(
        [Path(model_dir).name] if model_dir else []
    )
    return {
        "success": True,
        "data": {
            "config": config,
            "baseline_source": baseline_source_label(model_dir),
            "model_onnx": _onnx_status(model_dir),
            "model_display_name": display_names.get(Path(model_dir).name, "") if model_dir else "",
            "status": {
                "updated_at": status.get("updated_at"),
                "counters": counters,
                "governor": governor,
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
