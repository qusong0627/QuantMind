from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.shared.model_registry import model_registry_service

from .script_runner import ExecutionResult, InferenceScriptRunner
from .service import InferenceService

logger = logging.getLogger(__name__)

_NON_FEATURE_KEYS = {"symbol", "instrument", "timestamp", "datetime", "date"}


def _get_model_data_dir(model_dir: Path) -> str:
    """
    从模型配置中获取推理数据目录。

    优先级：
    1. metadata.json 中的 qlib_data_path 字段（绝对路径）
    2. metadata.json 中的 data_source 字段判断：
       - "qlib" -> db/qlib_data
       - "parquet" 或其他 -> db/feature_snapshots
    3. 默认值 -> db/feature_snapshots
    """
    meta_file = Path(model_dir) / "metadata.json"
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

    return "db/feature_snapshots"


class InferenceRouterService:
    """统一推理编排层：显式模型/策略绑定/默认模型 + 系统兜底。"""

    def __init__(self, inference_service: InferenceService | None = None):
        self.inference_service = inference_service or InferenceService()
        self.primary_model_id = os.getenv("PRIMARY_MODEL_ID", "model_qlib")
        self.fallback_model_id = os.getenv("FALLBACK_MODEL_ID", "alpha158")
        self.primary_model_dir = os.getenv("MODELS_PRODUCTION", "/app/models/production/model_qlib")
        self.fallback_model_dir = os.getenv("MODELS_FALLBACK_PRODUCTION", "/app/models/production/alpha158")
        self.primary_data_source = os.getenv("QLIB_PRIMARY_DATA_PATH", "db/feature_snapshots")
        self.fallback_data_source = os.getenv("QLIB_FALLBACK_DATA_PATH", "db/feature_snapshots")

    def _resolve_data_source(self, model_id: str, model_source: str = "") -> str:
        if model_id == self.fallback_model_id:
            return self.fallback_data_source
        if model_source in {"explicit_model_id", "strategy_binding", "user_default"}:
            return "user_model_registry"
        return self.primary_data_source

    async def resolve_effective_model(
        self,
        *,
        tenant_id: str,
        user_id: str,
        strategy_id: str | None = None,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        resolved = await model_registry_service.resolve_effective_model(
            tenant_id=tenant_id,
            user_id=user_id,
            strategy_id=strategy_id,
            model_id=model_id,
        )
        return resolved.to_dict()

    def _finalize(
        self,
        *,
        raw: dict[str, Any],
        fallback_used: bool,
        fallback_reason: str,
        active_model_id: str,
        active_data_source: str,
        trace_id: str | None,
        model_source: str,
        effective_model_id: str,
    ) -> dict[str, Any]:
        result = self._enrich_result(
            raw,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            active_model_id=active_model_id,
            active_data_source=active_data_source,
            model_source=model_source,
            effective_model_id=effective_model_id,
        )
        logger.info(
            "[InferenceRouter] trace_id=%s active_model_id=%s effective_model_id=%s source=%s fallback_used=%s fallback_reason=%s status=%s",
            trace_id or "n/a",
            result.get("active_model_id"),
            result.get("effective_model_id"),
            result.get("model_source"),
            result.get("fallback_used"),
            result.get("fallback_reason") or "",
            result.get("status"),
        )
        return result

    def _expected_feature_dim(self, model_id: str, *, model_dir: Path | None = None) -> int:
        meta = self.inference_service.model_loader.get_model_metadata(model_id, model_dir=model_dir) or {}
        feature_cols = meta.get("feature_columns")
        if isinstance(feature_cols, list) and feature_cols:
            return len(feature_cols)
        input_spec = meta.get("input_spec")
        if isinstance(input_spec, dict):
            tensor_shape = input_spec.get("tensor_shape")
            if isinstance(tensor_shape, list) and len(tensor_shape) >= 3:
                try:
                    return int(tensor_shape[2] or 0)
                except Exception:
                    return 0
        return 0

    @staticmethod
    def _input_feature_dim(data: dict[str, Any] | list[dict[str, Any]]) -> int:
        if isinstance(data, dict) and isinstance(data.get("sequences"), list):
            seqs = data.get("sequences") or []
            if seqs and isinstance(seqs[0], list) and seqs[0] and isinstance(seqs[0][0], list):
                return int(len(seqs[0][0]))
            return 0

        rows: list[dict[str, Any]]
        if isinstance(data, dict):
            rows = [data]
        elif isinstance(data, list):
            rows = [item for item in data if isinstance(item, dict)]
        else:
            return 0

        feature_keys = set()
        for row in rows:
            feature_keys.update(str(k) for k in row.keys())
        feature_keys -= _NON_FEATURE_KEYS
        return len(feature_keys)

    def _dimension_gate(
        self,
        model_id: str,
        data: dict[str, Any] | list[dict[str, Any]],
        *,
        model_dir: Path | None = None,
    ) -> tuple[bool, str]:
        expected_dim = self._expected_feature_dim(model_id, model_dir=model_dir)
        if expected_dim <= 0:
            return True, "expected_dim_unknown"
        actual_dim = self._input_feature_dim(data)
        passed = actual_dim >= expected_dim
        return passed, f"expected_dim={expected_dim}, actual_dim={actual_dim}"

    @staticmethod
    def _enrich_result(
        raw: dict[str, Any],
        *,
        fallback_used: bool,
        fallback_reason: str,
        active_model_id: str,
        active_data_source: str,
        model_source: str,
        effective_model_id: str,
    ) -> dict[str, Any]:
        result = dict(raw or {})
        result["fallback_used"] = bool(fallback_used)
        result["fallback_reason"] = str(fallback_reason or "")
        result["active_model_id"] = str(active_model_id)
        result["active_data_source"] = str(active_data_source)
        result["model_source"] = str(model_source or "")
        result["effective_model_id"] = str(effective_model_id or active_model_id)
        return result

    def _predict_single_model(
        self,
        *,
        model_id: str,
        data: dict[str, Any] | list[dict[str, Any]],
        model_dir: str | None = None,
        cache_namespace: str | None = None,
    ) -> dict[str, Any]:
        model_path = Path(model_dir) if model_dir else None
        return self.inference_service.predict(
            model_id,
            data,
            model_dir=model_path,
            cache_namespace=cache_namespace,
        )

    def _predict_system_chain(
        self,
        *,
        model_id: str,
        data: dict[str, Any] | list[dict[str, Any]],
        trace_id: str | None = None,
        model_source: str = "system_fallback",
        effective_model_id: str | None = None,
    ) -> dict[str, Any]:
        effective_mid = str(effective_model_id or model_id or self.primary_model_id)
        requested_model = str(model_id or self.primary_model_id)

        if requested_model != self.primary_model_id:
            primary = self._predict_single_model(model_id=requested_model, data=data)
            return self._finalize(
                raw=primary,
                fallback_used=False,
                fallback_reason="",
                active_model_id=requested_model,
                active_data_source=self._resolve_data_source(requested_model, model_source=model_source),
                trace_id=trace_id,
                model_source=model_source,
                effective_model_id=effective_mid,
            )

        dim_ready, dim_detail = self._dimension_gate(requested_model, data)
        if not dim_ready:
            fallback = self._predict_single_model(model_id=self.fallback_model_id, data=data)
            if fallback.get("status") == "success":
                return self._finalize(
                    raw=fallback,
                    fallback_used=True,
                    fallback_reason=f"主模型维度门禁未通过: {dim_detail}",
                    active_model_id=self.fallback_model_id,
                    active_data_source=self.fallback_data_source,
                    trace_id=trace_id,
                    model_source=model_source,
                    effective_model_id=effective_mid,
                )
            return self._finalize(
                raw=fallback,
                fallback_used=True,
                fallback_reason=f"主模型维度门禁未通过且兜底失败: {dim_detail}",
                active_model_id=self.fallback_model_id,
                active_data_source=self.fallback_data_source,
                trace_id=trace_id,
                model_source=model_source,
                effective_model_id=effective_mid,
            )

        primary = self._predict_single_model(model_id=requested_model, data=data)
        if primary.get("status") == "success":
            return self._finalize(
                raw=primary,
                fallback_used=False,
                fallback_reason="",
                active_model_id=self.primary_model_id,
                active_data_source=self.primary_data_source,
                trace_id=trace_id,
                model_source=model_source,
                effective_model_id=effective_mid,
            )

        fallback = self._predict_single_model(model_id=self.fallback_model_id, data=data)
        if fallback.get("status") == "success":
            return self._finalize(
                raw=fallback,
                fallback_used=True,
                fallback_reason=f"主模型推理失败: {primary.get('error', 'unknown')}",
                active_model_id=self.fallback_model_id,
                active_data_source=self.fallback_data_source,
                trace_id=trace_id,
                model_source=model_source,
                effective_model_id=effective_mid,
            )

        return self._finalize(
            raw=fallback,
            fallback_used=True,
            fallback_reason=(
                f"主模型推理失败且兜底失败: primary={primary.get('error', 'unknown')}; "
                f"fallback={fallback.get('error', 'unknown')}"
            ),
            active_model_id=self.fallback_model_id,
            active_data_source=self.fallback_data_source,
            trace_id=trace_id,
            model_source=model_source,
            effective_model_id=effective_mid,
        )

    def predict_with_fallback(
        self,
        model_id: str,
        data: dict[str, Any] | list[dict[str, Any]],
        *,
        trace_id: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        strategy_id: str | None = None,
    ) -> dict[str, Any]:
        requested_model_id = str(model_id or "").strip() or self.primary_model_id
        if tenant_id and user_id:
            try:
                asyncio.get_running_loop()
                raise RuntimeError("predict_with_fallback must use async API when tenant_id/user_id are provided")
            except RuntimeError as loop_err:
                if "must use async API" in str(loop_err):
                    raise
            resolved = model_registry_service.resolve_effective_model_sync(
                tenant_id=str(tenant_id),
                user_id=str(user_id),
                strategy_id=strategy_id,
                model_id=requested_model_id or None,
            )
            return self._predict_with_resolved(
                requested_model_id=requested_model_id,
                resolved=resolved,
                data=data,
                trace_id=trace_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )

        return self._predict_system_chain(
            model_id=requested_model_id,
            data=data,
            trace_id=trace_id,
            model_source="legacy_request",
            effective_model_id=requested_model_id,
        )

    async def predict_with_fallback_async(
        self,
        model_id: str,
        data: dict[str, Any] | list[dict[str, Any]],
        *,
        tenant_id: str,
        user_id: str,
        strategy_id: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        requested_model_id = str(model_id or "").strip() or self.primary_model_id
        resolved = await self.resolve_effective_model(
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            strategy_id=strategy_id,
            model_id=requested_model_id or None,
        )
        return self._predict_with_resolved(
            requested_model_id=requested_model_id,
            resolved=resolved,
            data=data,
            trace_id=trace_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )

    def _predict_with_resolved(
        self,
        *,
        requested_model_id: str,
        resolved: dict[str, Any],
        data: dict[str, Any] | list[dict[str, Any]],
        trace_id: str | None,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        effective_model_id = str(resolved.get("effective_model_id") or self.primary_model_id)
        model_source = str(resolved.get("model_source") or "")
        storage_path = str(resolved.get("storage_path") or "").strip()
        fallback_reason = str(resolved.get("fallback_reason") or "").strip()
        fallback_used = bool(resolved.get("fallback_used"))

        if storage_path and Path(storage_path).exists() and model_source in {
            "explicit_model_id",
            "strategy_binding",
            "user_default",
        }:
            user_result = self._predict_single_model(
                model_id=effective_model_id,
                data=data,
                model_dir=storage_path,
                cache_namespace=f"{tenant_id}:{user_id}",
            )
            if user_result.get("status") == "success":
                return self._finalize(
                    raw=user_result,
                    fallback_used=fallback_used,
                    fallback_reason=fallback_reason,
                    active_model_id=effective_model_id,
                    active_data_source=self._resolve_data_source(effective_model_id, model_source=model_source),
                    trace_id=trace_id,
                    model_source=model_source,
                    effective_model_id=effective_model_id,
                )

            chain_reason = (
                f"user_model_failed={user_result.get('error', 'unknown')}; "
                f"{fallback_reason or 'fallback to system chain'}"
            ).strip()
            result = self._predict_system_chain(
                model_id=self.primary_model_id,
                data=data,
                trace_id=trace_id,
                model_source=model_source or "system_fallback",
                effective_model_id=effective_model_id,
            )
            result["fallback_reason"] = chain_reason
            return result

        if model_source in {"explicit_model_id", "strategy_binding", "user_default"}:
            chain_model = self.primary_model_id
        else:
            chain_model = effective_model_id or requested_model_id

        return self._predict_system_chain(
            model_id=chain_model,
            data=data,
            trace_id=trace_id,
            model_source=model_source or "system_fallback",
            effective_model_id=effective_model_id,
        )

    def run_daily_inference_script(
        self,
        *,
        date: str,
        tenant_id: str,
        user_id: str,
        strategy_id: str | None = None,
        model_id: str | None = None,
        resolved_model: dict[str, Any] | None = None,
        redis_client=None,
    ) -> ExecutionResult:
        if resolved_model is not None:
            resolved = dict(resolved_model)
        else:
            resolved = model_registry_service.resolve_effective_model_sync(
                tenant_id=str(tenant_id),
                user_id=str(user_id),
                strategy_id=str(strategy_id) if strategy_id is not None else None,
                model_id=model_id,
            )
        effective_model_id = str(resolved.get("effective_model_id") or self.primary_model_id)
        model_source = str(resolved.get("model_source") or "")
        storage_path = str(resolved.get("storage_path") or "").strip()
        fallback_reason = str(resolved.get("fallback_reason") or "")

        primary_dir = storage_path if storage_path and Path(storage_path).exists() else self.primary_model_dir
        primary_id = effective_model_id if primary_dir == storage_path else self.primary_model_id
        fallback_dir = self.primary_model_dir if primary_id != self.primary_model_id else self.fallback_model_dir
        fallback_id = self.primary_model_id if primary_id != self.primary_model_id else self.fallback_model_id
        primary_data_dir = self.fallback_data_source if (
            primary_id == self.fallback_model_id or Path(primary_dir).resolve() == Path(self.fallback_model_dir).resolve()
        ) else self.primary_data_source
        fallback_data_dir = self.fallback_data_source if fallback_id == self.fallback_model_id else primary_data_dir

        runner = InferenceScriptRunner(
            primary_model_dir=primary_dir,
            fallback_model_dir=fallback_dir,
            primary_data_dir=primary_data_dir,
            fallback_data_dir=fallback_data_dir,
            primary_model_id=primary_id,
            fallback_model_id=fallback_id,
        )
        result = runner.execute(date, tenant_id=tenant_id, user_id=user_id, redis_client=redis_client)

        # 若用户模型 -> model_qlib 两级仍失败，再补 alpha158 最后兜底。
        if not result.success and primary_id not in {self.primary_model_id, self.fallback_model_id}:
            final_runner = InferenceScriptRunner(
                primary_model_dir=self.fallback_model_dir,
                fallback_model_dir=self.fallback_model_dir,
                primary_data_dir=self.fallback_data_source,
                fallback_data_dir=self.fallback_data_source,
                primary_model_id=self.fallback_model_id,
                fallback_model_id=self.fallback_model_id,
            )
            final_result = final_runner.execute(date, tenant_id=tenant_id, user_id=user_id, redis_client=redis_client)
            if final_result.success:
                final_result.fallback_used = True
                reason = fallback_reason or result.fallback_reason or result.error or "fallback to alpha158"
                final_result.fallback_reason = reason
                final_result.active_model_id = self.fallback_model_id
                return final_result

        if result.success and model_source in {"explicit_model_id", "strategy_binding", "user_default", "explicit_system_model"}:
            result.active_model_id = effective_model_id
            if fallback_reason and not result.fallback_reason:
                result.fallback_reason = fallback_reason
        return result
