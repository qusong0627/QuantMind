from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from backend.services.engine.ai_strategy.models import StrategyGenerationRequest
from backend.services.engine.ai_strategy.provider_registry import get_provider
from backend.services.engine.inference import InferenceRouterService, InferenceService
from backend.services.engine.qlib_app.schemas.backtest import (
    QlibBacktestRequest,
    QlibStrategyParams,
)
from backend.services.engine.qlib_app.services.backtest_service import (
    QlibBacktestService,
)
from backend.services.engine.services.event_stream import EngineSignalStreamPublisher
from backend.services.engine.services.fusion_config import (
    get_fusion_rules_snapshot,
    load_fusion_rules_config,
)
from backend.services.engine.services.fusion_runtime import apply_fusion_rules
from backend.services.engine.services.pipeline_persistence import PipelinePersistence
from backend.services.engine.services.prediction_artifact import (
    build_pred_pkl_from_inference,
)


class PipelineRunRequest(BaseModel):
    prompt: str = Field(..., description="策略生成提示词")
    user_id: str | None = Field(None, description="用户ID")
    tenant_id: str | None = Field(None, description="租户ID")

    provider: str | None = Field(None, description="可选 provider")
    strategy_content: str | None = Field(None, description="直接提供策略代码")
    use_generated_strategy_for_backtest: bool = Field(False, description="是否将生成代码直接用于回测")

    inference_enabled: bool = Field(False, description="是否执行推理")
    model_id: str | None = Field(None, description="推理模型ID")
    strategy_id: str | None = Field(None, description="策略ID（可选，用于自动解析生效模型）")
    inference_data: dict[str, Any] | list[dict[str, Any]] | None = Field(None, description="推理输入")
    tft_model_id: str | None = Field(None, description="TFT模型ID（可选）")
    tft_inference_data: dict[str, Any] | list[dict[str, Any]] | None = Field(None, description="TFT推理输入（可选）")
    risk_features: dict[str, dict[str, Any]] | None = Field(
        None,
        description="风控特征（symbol -> {avg_turnover_20d, volatility_20d, industry})",
    )

    start_date: str | None = Field(None, description="回测开始日期 YYYY-MM-DD")
    end_date: str | None = Field(None, description="回测结束日期 YYYY-MM-DD")
    benchmark: str = Field("SH000300", description="回测基准")
    universe: str = Field("csi300", description="回测股票池")
    initial_capital: float = Field(100000000, description="初始资金")
    topk: int = Field(30, ge=5, le=200)
    n_drop: int = Field(3, ge=1, le=20)
    enable_short_selling: bool = Field(False, description="是否启用双向交易")
    margin_stock_pool: str | None = Field(None, description="融资融券股票池标识")
    financing_rate: float = Field(0.08, ge=0.0, le=1.0, description="融资年化利率")
    borrow_rate: float = Field(0.08, ge=0.0, le=1.0, description="融券年化费率")
    max_short_exposure: float = Field(1.0, ge=0.0, le=3.0, description="最大空头敞口")
    max_leverage: float = Field(1.0, ge=0.0, le=5.0, description="最大总杠杆")


class PipelineRunStatus(BaseModel):
    run_id: str
    status: Literal["running", "completed", "failed"]
    stage: Literal["queued", "generation", "inference", "backtest", "completed", "failed"]
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class PipelineRunResult(BaseModel):
    run_id: str
    status: Literal["completed", "failed"]
    stage: Literal["completed", "failed"]
    strategy_code: str | None = None
    inference_result: dict[str, Any] | None = None
    pred_path: str | None = None
    fused_pred_path: str | None = None
    fusion_report: dict[str, Any] | None = None
    backtest_result: dict[str, Any] | None = None
    fusion_rules: dict[str, Any] | None = None
    fallback_used: bool | None = None
    fallback_reason: str | None = None
    execution_mode: str | None = None
    model_switch_used: bool | None = None
    model_switch_reason: str | None = None
    active_model_id: str | None = None
    effective_model_id: str | None = None
    model_source: str | None = None
    active_data_source: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


@dataclass
class _RunRecord:
    request: PipelineRunRequest
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    status: str = "running"
    stage: str = "queued"
    error: str | None = None
    strategy_code: str | None = None
    inference_result: dict[str, Any] | None = None
    pred_path: str | None = None
    fused_pred_path: str | None = None
    fusion_report: dict[str, Any] | None = None
    backtest_result: dict[str, Any] | None = None
    fusion_rules: dict[str, Any] | None = None
    fallback_used: bool | None = None
    fallback_reason: str | None = None
    execution_mode: str | None = None
    model_switch_used: bool | None = None
    model_switch_reason: str | None = None
    active_model_id: str | None = None
    effective_model_id: str | None = None
    model_source: str | None = None
    active_data_source: str | None = None


class PipelineService:
    def __init__(
        self,
        *,
        inference_service: InferenceService | None = None,
        backtest_service: QlibBacktestService | None = None,
        artifacts_root: Path | None = None,
        persistence: PipelinePersistence | None = None,
    ):
        self.inference_service = inference_service or InferenceService()
        self.inference_router_service = InferenceRouterService(inference_service=self.inference_service)
        self.backtest_service = backtest_service or QlibBacktestService(
            provider_uri=os.getenv("QLIB_PROVIDER_URI", "db/qlib_data"),
            region=os.getenv("QLIB_REGION", "cn"),
        )
        self.artifacts_root = artifacts_root or Path("data/pipeline_artifacts")
        self.persistence = persistence or PipelinePersistence()
        self.fusion_rules = get_fusion_rules_snapshot()
        self.fusion_config = load_fusion_rules_config()
        self.signal_stream_publisher = EngineSignalStreamPublisher()
        self._runs: dict[str, _RunRecord] = {}
        self._tables_ready = False

    async def _ensure_tables(self) -> None:
        if self._tables_ready:
            return
        await self.persistence.ensure_tables()
        self._tables_ready = True

    @staticmethod
    def _extract_inference_meta(inference_result: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(inference_result, dict):
            return {
                "fallback_used": None,
                "fallback_reason": None,
                "execution_mode": None,
                "model_switch_used": None,
                "model_switch_reason": None,
                "active_model_id": None,
                "effective_model_id": None,
                "model_source": None,
                "active_data_source": None,
            }
        return {
            "fallback_used": inference_result.get("fallback_used"),
            "fallback_reason": inference_result.get("fallback_reason"),
            "execution_mode": inference_result.get("execution_mode"),
            "model_switch_used": inference_result.get("model_switch_used"),
            "model_switch_reason": inference_result.get("model_switch_reason"),
            "active_model_id": inference_result.get("active_model_id"),
            "effective_model_id": inference_result.get("effective_model_id"),
            "model_source": inference_result.get("model_source"),
            "active_data_source": inference_result.get("active_data_source"),
        }

    async def cleanup_old_runs(self, *, user_id: str, tenant_id: str, keep_days: int = 30) -> int:
        await self._ensure_tables()
        return await self.persistence.cleanup_old_runs(user_id=user_id, tenant_id=tenant_id, keep_days=keep_days)

    async def create_run(self, request: PipelineRunRequest) -> str:
        await self._ensure_tables()
        run_id = uuid.uuid4().hex
        rec = _RunRecord(request=request)
        self._runs[run_id] = rec
        await self.persistence.create_run(
            run_id=run_id,
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            status=rec.status,
            stage=rec.stage,
            created_at=rec.created_at,
            request_payload=request.model_dump(),
        )
        return run_id

    async def get_status(self, run_id: str, *, user_id: str, tenant_id: str) -> PipelineRunStatus | None:
        rec = self._runs.get(run_id)
        if rec is not None and rec.request.user_id == user_id and rec.request.tenant_id == tenant_id:
            return PipelineRunStatus(
                run_id=run_id,
                status=rec.status,
                stage=rec.stage,  # type: ignore[arg-type]
                error=rec.error,
                created_at=rec.created_at,
                updated_at=rec.updated_at,
            )

        row = await self.persistence.get_run(run_id, user_id=user_id, tenant_id=tenant_id)
        if row is None:
            return None
        return PipelineRunStatus(
            run_id=run_id,
            status=row["status"],
            stage=row["stage"],
            error=row.get("error_message"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get_result(self, run_id: str, *, user_id: str, tenant_id: str) -> PipelineRunResult | None:
        rec = self._runs.get(run_id)
        if rec is not None and rec.request.user_id == user_id and rec.request.tenant_id == tenant_id:
            if rec.status == "running":
                return None
            return PipelineRunResult(
                run_id=run_id,
                status=rec.status,  # type: ignore[arg-type]
                # type: ignore[arg-type]
                stage=("completed" if rec.status == "completed" else "failed"),
                strategy_code=rec.strategy_code,
                inference_result=rec.inference_result,
                pred_path=rec.pred_path,
                fused_pred_path=rec.fused_pred_path,
                fusion_report=rec.fusion_report,
                backtest_result=rec.backtest_result,
                fusion_rules=rec.fusion_rules,
                fallback_used=rec.fallback_used,
                fallback_reason=rec.fallback_reason,
                active_model_id=rec.active_model_id,
                effective_model_id=rec.effective_model_id,
                model_source=rec.model_source,
                active_data_source=rec.active_data_source,
                error=rec.error,
                created_at=rec.created_at,
                updated_at=rec.updated_at,
            )

        row = await self.persistence.get_run(run_id, user_id=user_id, tenant_id=tenant_id)
        if row is None or row["status"] == "running":
            return None
        result_json = row.get("result_json") or {}
        infer_meta = self._extract_inference_meta(result_json.get("inference_result"))
        status = row["status"]
        return PipelineRunResult(
            run_id=run_id,
            status=status,
            stage=("completed" if status == "completed" else "failed"),
            strategy_code=result_json.get("strategy_code"),
            inference_result=result_json.get("inference_result"),
            pred_path=result_json.get("pred_path"),
            fused_pred_path=result_json.get("fused_pred_path"),
            fusion_report=result_json.get("fusion_report"),
            backtest_result=result_json.get("backtest_result"),
            fusion_rules=result_json.get("fusion_rules"),
            fallback_used=result_json.get("fallback_used", infer_meta.get("fallback_used")),
            fallback_reason=result_json.get("fallback_reason", infer_meta.get("fallback_reason")),
            execution_mode=result_json.get("execution_mode", infer_meta.get("execution_mode")),
            model_switch_used=result_json.get("model_switch_used", infer_meta.get("model_switch_used")),
            model_switch_reason=result_json.get("model_switch_reason", infer_meta.get("model_switch_reason")),
            active_model_id=result_json.get("active_model_id", infer_meta.get("active_model_id")),
            effective_model_id=result_json.get("effective_model_id", infer_meta.get("effective_model_id")),
            model_source=result_json.get("model_source", infer_meta.get("model_source")),
            active_data_source=result_json.get("active_data_source", infer_meta.get("active_data_source")),
            error=row.get("error_message") or result_json.get("error"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def _update_run(
        self,
        *,
        run_id: str,
        status: str,
        stage: str,
        error: str | None = None,
        result_payload: dict[str, Any] | None = None,
    ) -> None:
        rec = self._runs.get(run_id)
        updated_at = datetime.now()
        if rec is not None:
            rec.status = status
            rec.stage = stage
            rec.error = error
            rec.updated_at = updated_at
        await self.persistence.update_run(
            run_id=run_id,
            status=status,
            stage=stage,
            updated_at=updated_at,
            error_message=error,
            result_payload=result_payload,
        )

    async def execute_run(self, run_id: str) -> PipelineRunResult:
        rec = await self._get_or_restore_run_record(run_id)

        try:
            rec.fusion_rules = self.fusion_rules
            await self._update_run(run_id=run_id, status="running", stage="generation")
            strategy_code = await self._run_generation(rec.request)
            rec.strategy_code = strategy_code

            pred_path: Path | None = None
            signal_path: Path | None = None
            if rec.request.inference_enabled:
                await self._update_run(run_id=run_id, status="running", stage="inference")
                inference_result = await self._run_inference(rec.request, run_id=run_id)
                rec.inference_result = inference_result
                inference_meta = self._extract_inference_meta(inference_result)
                rec.fallback_used = inference_meta.get("fallback_used")
                rec.fallback_reason = inference_meta.get("fallback_reason")
                rec.execution_mode = inference_meta.get("execution_mode")
                rec.model_switch_used = inference_meta.get("model_switch_used")
                rec.model_switch_reason = inference_meta.get("model_switch_reason")
                rec.active_model_id = inference_meta.get("active_model_id")
                rec.effective_model_id = inference_meta.get("effective_model_id")
                rec.model_source = inference_meta.get("model_source")
                rec.active_data_source = inference_meta.get("active_data_source")
                pred_path = build_pred_pkl_from_inference(
                    run_id=run_id,
                    user_id=rec.request.user_id,
                    tenant_id=rec.request.tenant_id,
                    inference_result=inference_result,
                    base_dir=self.artifacts_root,
                )
                rec.pred_path = str(pred_path.resolve())
                signal_path = pred_path

                tft_result: dict[str, Any] | None = None
                if rec.request.tft_model_id and rec.request.tft_inference_data is not None:
                    tft_result = self._run_tft_inference(rec.request, run_id=run_id)

                try:
                    fusion_exec = apply_fusion_rules(
                        run_id=run_id,
                        user_id=rec.request.user_id,
                        tenant_id=rec.request.tenant_id,
                        base_pred_path=pred_path,
                        base_dir=self.artifacts_root,
                        fusion_rules=self.fusion_config,
                        tft_result=tft_result,
                        risk_features=rec.request.risk_features,
                    )
                    signal_path = fusion_exec.pred_path
                    rec.fused_pred_path = str(signal_path.resolve())
                    rec.fusion_report = fusion_exec.report
                except Exception as fusion_exc:
                    signal_path = pred_path
                    rec.fused_pred_path = None
                    rec.fusion_report = {
                        "enabled": bool(self.fusion_config.enabled),
                        "fallback_to_lgbm": True,
                        "reason": str(fusion_exc),
                    }

                signal_events, signal_source = self._build_signal_events(
                    request=rec.request,
                    run_id=run_id,
                    fusion_report=rec.fusion_report,
                    inference_result=rec.inference_result,
                )
                self.signal_stream_publisher.mark_latest_run(
                    tenant_id=str(rec.request.tenant_id or "default"),
                    user_id=str(rec.request.user_id or ""),
                    run_id=run_id,
                )
                published_events = self.signal_stream_publisher.publish_signals(
                    tenant_id=str(rec.request.tenant_id or "default"),
                    user_id=str(rec.request.user_id or ""),
                    run_id=run_id,
                    trace_id=run_id,
                    signal_source=signal_source,
                    signals=signal_events,
                )
                if rec.fusion_report is None:
                    rec.fusion_report = {}
                rec.fusion_report["published_signal_events"] = int(published_events)

            await self._update_run(run_id=run_id, status="running", stage="backtest")
            backtest_result = await self._run_backtest(
                request=rec.request,
                strategy_code=strategy_code,
                pred_path=signal_path or pred_path,
            )
            rec.backtest_result = backtest_result

            final_payload = {
                "strategy_code": rec.strategy_code,
                "inference_result": rec.inference_result,
                "pred_path": rec.pred_path,
                "fused_pred_path": rec.fused_pred_path,
                "fusion_report": rec.fusion_report,
                "backtest_result": rec.backtest_result,
                "fusion_rules": rec.fusion_rules,
                "fallback_used": rec.fallback_used,
                "fallback_reason": rec.fallback_reason,
                "execution_mode": rec.execution_mode,
                "model_switch_used": rec.model_switch_used,
                "model_switch_reason": rec.model_switch_reason,
                "active_model_id": rec.active_model_id,
                "effective_model_id": rec.effective_model_id,
                "model_source": rec.model_source,
                "active_data_source": rec.active_data_source,
                "error": None,
            }
            await self._update_run(
                run_id=run_id,
                status="completed",
                stage="completed",
                result_payload=final_payload,
            )
        except Exception as exc:
            await self._update_run(
                run_id=run_id,
                status="failed",
                stage="failed",
                error=str(exc),
                result_payload={
                    "strategy_code": rec.strategy_code,
                    "inference_result": rec.inference_result,
                    "pred_path": rec.pred_path,
                    "fused_pred_path": rec.fused_pred_path,
                    "fusion_report": rec.fusion_report,
                    "backtest_result": rec.backtest_result,
                    "fusion_rules": rec.fusion_rules,
                    "fallback_used": rec.fallback_used,
                    "fallback_reason": rec.fallback_reason,
                    "execution_mode": rec.execution_mode,
                    "model_switch_used": rec.model_switch_used,
                    "model_switch_reason": rec.model_switch_reason,
                    "active_model_id": rec.active_model_id,
                    "effective_model_id": rec.effective_model_id,
                    "model_source": rec.model_source,
                    "active_data_source": rec.active_data_source,
                    "error": str(exc),
                },
            )

        result = await self.get_result(run_id, user_id=rec.request.user_id, tenant_id=rec.request.tenant_id)
        if result is None:
            raise RuntimeError("pipeline finished but result is unavailable")
        return result

    async def _get_or_restore_run_record(self, run_id: str) -> _RunRecord:
        rec = self._runs.get(run_id)
        if rec is not None:
            return rec

        await self._ensure_tables()
        row = await self.persistence.get_run_by_id(run_id)
        if row is None:
            raise RuntimeError(f"pipeline run not found: {run_id}")

        request_payload = row.get("request_json") or {}
        request = PipelineRunRequest(**request_payload)
        rec = _RunRecord(
            request=request,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            status=row["status"],
            stage=row["stage"],
            error=row.get("error_message"),
        )
        result_json = row.get("result_json") or {}
        rec.strategy_code = result_json.get("strategy_code")
        rec.inference_result = result_json.get("inference_result")
        rec.pred_path = result_json.get("pred_path")
        rec.fused_pred_path = result_json.get("fused_pred_path")
        rec.fusion_report = result_json.get("fusion_report")
        rec.backtest_result = result_json.get("backtest_result")
        rec.fusion_rules = result_json.get("fusion_rules")
        infer_meta = self._extract_inference_meta(rec.inference_result)
        rec.fallback_used = result_json.get("fallback_used", infer_meta.get("fallback_used"))
        rec.fallback_reason = result_json.get("fallback_reason", infer_meta.get("fallback_reason"))
        rec.execution_mode = result_json.get("execution_mode", infer_meta.get("execution_mode"))
        rec.model_switch_used = result_json.get("model_switch_used", infer_meta.get("model_switch_used"))
        rec.model_switch_reason = result_json.get("model_switch_reason", infer_meta.get("model_switch_reason"))
        rec.active_model_id = result_json.get("active_model_id", infer_meta.get("active_model_id"))
        rec.effective_model_id = result_json.get("effective_model_id", infer_meta.get("effective_model_id"))
        rec.model_source = result_json.get("model_source", infer_meta.get("model_source"))
        rec.active_data_source = result_json.get("active_data_source", infer_meta.get("active_data_source"))
        self._runs[run_id] = rec
        return rec

    async def _run_generation(self, request: PipelineRunRequest) -> str:
        if request.strategy_content and request.strategy_content.strip():
            return request.strategy_content

        provider = get_provider(request.provider)
        gen_request = StrategyGenerationRequest(
            description=request.prompt,
            user_id=request.user_id,
            provider=request.provider,
        )
        generated = await provider.generate(gen_request)
        if not generated.artifacts:
            raise RuntimeError("strategy generation returned no artifacts")
        return generated.artifacts[0].code

    async def _run_inference(self, request: PipelineRunRequest, *, run_id: str) -> dict[str, Any]:
        if request.inference_data is None:
            raise ValueError("inference_data is required when inference_enabled=true")
        if not request.user_id or not request.tenant_id:
            raise ValueError("user_id and tenant_id are required when inference_enabled=true")

        result = await self.inference_router_service.predict_with_fallback_async(
            request.model_id or "",
            request.inference_data,
            tenant_id=str(request.tenant_id),
            user_id=str(request.user_id),
            strategy_id=request.strategy_id,
            trace_id=run_id,
        )
        if result.get("status") != "success":
            raise RuntimeError(result.get("error") or "inference failed")
        return result

    def _run_tft_inference(self, request: PipelineRunRequest, *, run_id: str) -> dict[str, Any]:
        if not request.tft_model_id:
            raise ValueError("tft_model_id is required for TFT inference")
        if request.tft_inference_data is None:
            raise ValueError("tft_inference_data is required for TFT inference")

        result = self.inference_router_service.predict_with_fallback(
            request.tft_model_id,
            request.tft_inference_data,
            trace_id=f"{run_id}:tft",
        )
        if result.get("status") != "success":
            raise RuntimeError(result.get("error") or "tft inference failed")
        return result

    @staticmethod
    def _extract_close_price_map(inference_data: dict[str, Any] | list[dict[str, Any]] | None) -> dict[str, float]:
        rows: list[dict[str, Any]]
        if inference_data is None:
            return {}
        if isinstance(inference_data, list):
            rows = [item for item in inference_data if isinstance(item, dict)]
        elif isinstance(inference_data, dict):
            rows = [inference_data]
        else:
            return {}

        result: dict[str, float] = {}
        for row in rows:
            symbol = str(row.get("instrument") or row.get("symbol") or "").upper().strip()
            if not symbol:
                continue
            try:
                result[symbol] = float(row.get("close") or 0.0)
            except Exception:
                continue
        return result

    def _build_signal_events(
        self,
        *,
        request: PipelineRunRequest,
        run_id: str,
        fusion_report: dict[str, Any] | None,
        inference_result: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], str]:
        close_map = self._extract_close_price_map(request.inference_data)
        signals: list[dict[str, Any]] = []
        enable_short = bool(request.enable_short_selling)

        def _build_signal_event(
            *, signal_id: str, client_order_id: str, symbol: str, score_val: float
        ) -> dict[str, Any]:
            signal: dict[str, Any] = {
                "signal_id": signal_id,
                "client_order_id": client_order_id,
                "symbol": symbol,
                "score": score_val,
                "quantity": 100,
                "price": float(close_map.get(symbol, 0.0)),
            }
            if enable_short:
                is_short = score_val < 0
                signal["side"] = "SELL" if is_short else "BUY"
                signal["position_side"] = "short" if is_short else "long"
                signal["trade_action"] = "sell_to_open" if is_short else "buy_to_open"
                signal["is_margin_trade"] = is_short
            return signal

        if isinstance(fusion_report, dict):
            instruments = fusion_report.get("selected_instruments") or []
            scores = fusion_report.get("selected_scores") or []
            if isinstance(instruments, list) and isinstance(scores, list) and instruments:
                for idx, (inst, score) in enumerate(zip(instruments, scores)):
                    symbol = str(inst).upper()
                    score_val = float(score)
                    signals.append(
                        _build_signal_event(
                            signal_id=f"{run_id}-{idx:04d}",
                            client_order_id=f"coid-{run_id}-{idx:04d}",
                            symbol=symbol,
                            score_val=score_val,
                        )
                    )
                return signals, "fusion_report"

        if isinstance(inference_result, dict):
            symbols = inference_result.get("symbols") or []
            predictions = inference_result.get("predictions") or []
            if isinstance(symbols, list) and isinstance(predictions, list) and symbols:
                for idx, (inst, score) in enumerate(zip(symbols, predictions)):
                    symbol = str(inst).upper()
                    score_val = float(score)
                    signals.append(
                        _build_signal_event(
                            signal_id=f"{run_id}-fb-{idx:04d}",
                            client_order_id=f"coid-{run_id}-fb-{idx:04d}",
                            symbol=symbol,
                            score_val=score_val,
                        )
                    )
                return signals, "inference_fallback"

        return [], "inference_fallback"

    async def _run_backtest(
        self,
        *,
        request: PipelineRunRequest,
        strategy_code: str,
        pred_path: Path | None,
    ) -> dict[str, Any]:
        end = request.end_date or datetime.now().strftime("%Y-%m-%d")
        start = request.start_date or (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

        signal_value = str(pred_path.resolve()) if pred_path else "<PRED>"

        backtest_request = QlibBacktestRequest(
            strategy_type="TopkDropout",
            strategy_params=QlibStrategyParams(
                topk=request.topk,
                n_drop=request.n_drop,
                signal=signal_value,
                enable_short_selling=request.enable_short_selling,
                margin_stock_pool=request.margin_stock_pool,
                financing_rate=request.financing_rate,
                borrow_rate=request.borrow_rate,
                max_short_exposure=request.max_short_exposure,
                max_leverage=request.max_leverage,
            ),
            strategy_content=(strategy_code if request.use_generated_strategy_for_backtest else None),
            start_date=start,
            end_date=end,
            initial_capital=request.initial_capital,
            benchmark=request.benchmark,
            universe=request.universe,
            user_id=request.user_id,
            tenant_id=request.tenant_id,
        )

        result = await self.backtest_service.run_backtest(backtest_request)
        return result.model_dump()
