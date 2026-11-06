"""Authenticated API for owner-scoped A-share factor research jobs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.services.engine.auth_context import get_authenticated_identity
from backend.shared.secret_redaction import redact_data, redact_text
from backend.shared.utc_datetime import to_utc_iso
from backend.services.engine.alpha_agent.factor_jobs import (
    AlphaFactorJobPersistence,
    FactorJobIdempotencyConflict,
    FactorJobNotTerminal,
    FactorJobProtected,
    FactorJobQueueFull,
)
from backend.services.engine.alpha_agent.doc_store import DocStore
from backend.services.engine.alpha_agent.factor_visibility import (
    FactorVisibilityConflict,
)
from backend.services.engine.qlib_app.services.rd_agent_persistence import (
    RDAgentFactorPersistence,
)

router = APIRouter()


async def _factor_input_datasets(
    factor: dict, *, tenant_id: str, user_id: str
) -> list[str]:
    """Recover the original owner's selection; never widen a legacy task."""
    from backend.services.engine.rd_agent.mining_datasets import resolve_datasets
    from backend.services.engine.qlib_app.services.rd_agent_persistence import (
        AlphaMiningTaskPersistence,
    )

    metadata = factor.get("metadata") or factor.get("metadata_json") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=409, detail="原因子元数据无法核验")
    task_id = metadata.get("task_id")
    if task_id:
        task = await AlphaMiningTaskPersistence().get_for_owner_with_spec(
            str(task_id), tenant_id=tenant_id, user_id=user_id, consistent=True
        )
        if task is not None:
            params = task.get("params_snapshot") or {}
            if isinstance(params, str):
                params = json.loads(params)
            if not isinstance(params, dict):
                raise HTTPException(status_code=409, detail="原挖掘参数无法核验")
            return list(
                resolve_datasets(
                    params.get("datasets") or ["features_daily", "l1_factors"]
                )
            )
        if "quantdb_data" in str(factor.get("factor_code", "")):
            raise HTTPException(
                status_code=409, detail="原挖掘任务的数据集选择无法核验"
            )
        return ["features_daily", "l1_factors"]
    return list(resolve_datasets(metadata.get("datasets")))


def _factor_job_kind(factor_code: str, alpha_router) -> str:
    """Validate a durable-job factor entrypoint without executing user code."""
    if factor_code.startswith("fm:"):
        from backend.services.engine.alpha_agent.factorminer.dsl import parse

        parse(factor_code[3:])
        return "factor_miner"
    return alpha_router._detect_factor_kind(factor_code)


logger = logging.getLogger(__name__)
_jobs = AlphaFactorJobPersistence()
_docs = DocStore()
_factors = RDAgentFactorPersistence()


class FactorBacktestSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    factor_id: str = Field(min_length=1, max_length=256)
    kind: Literal["backtest", "validate"] = "backtest"
    universe: str | None = Field(default=None, min_length=1, max_length=128)
    start_date: str | None = None
    end_date: str | None = None
    data_source: Literal["qlib_bin"] = "qlib_bin"


class FactorBatchSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    factor_ids: list[str] = Field(min_length=2, max_length=20)
    kind: Literal["backtest", "validate"] = "backtest"
    universe: str = Field(default="csi300", min_length=1, max_length=128)
    start_date: str | None = None
    end_date: str | None = None
    data_source: Literal["qlib_bin"] = "qlib_bin"


class FactorMaterializeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    factor_id: str = Field(min_length=1, max_length=256)
    universe: str = Field(default="csi300", min_length=1, max_length=128)
    start_date: str | None = None
    end_date: str | None = None


class FactorComboSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    factor_ids: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        min_length=2, max_length=12
    )
    universe: str = Field(min_length=1, max_length=128)
    name: str = Field(default="", max_length=128)
    seed: int = Field(default=42, ge=0, le=4_294_967_295)


class DocumentOrganizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["free", "paper"]
    extra: str | None = Field(default=None, max_length=2000)


class DocumentParseReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


def _owner(request: Request) -> tuple[str, str]:
    user_id, tenant_id = get_authenticated_identity(request)
    if not tenant_id or not user_id:
        raise HTTPException(status_code=401, detail="Authenticated owner is required")
    return str(tenant_id), str(user_id)


def _cost_snapshot() -> dict[str, float]:
    from backend.services.engine.inference.trading_cost import CostModel

    return CostModel.resolve().as_dict()


def _factor_revision_no(factor: dict) -> int:
    """Require the immutable revision captured by the factor table read."""
    revision_no = factor.get("factor_revision_no")
    if type(revision_no) is not int or revision_no < 1:
        raise HTTPException(
            status_code=503,
            detail="Factor source revision is unavailable; retry after schema recovery",
        )
    return revision_no


def _report_cell(value: object) -> str:
    return (
        str(value or "")
        .replace("|", "\\|")
        .replace("`", "\\`")
        .replace("\r", " ")
        .replace("\n", " ")[:256]
    )


def _report_metric(metrics: dict, *keys: str, percent: bool = False) -> str:
    value = next((metrics.get(key) for key in keys if key in metrics), None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    if not math.isfinite(float(value)):
        return "—"
    number = float(value) * (100 if percent else 1)
    return f"{number:.2f}%" if percent else f"{number:.4f}"


def _factor_job_report_markdown(job: dict) -> str:
    """Render a report only from the job's persisted result snapshot."""
    if job.get("kind") == "pool_research":
        raise ValueError(
            "池正交证据请从因子库的池研究历史查看，不能导出为单因子收益报告"
        )
    request_snapshot = job.get("request_snapshot")
    request = (
        request_snapshot.get("request") if isinstance(request_snapshot, dict) else None
    )
    request = request if isinstance(request, dict) else {}
    result = job.get("result_snapshot")
    if not isinstance(result, dict):
        raise ValueError("Factor job has no saved result")
    items = result.get("results")
    if not isinstance(items, list):
        items = [
            {
                "factor_id": request.get("factor_id") or result.get("factor_id"),
                "metrics": result.get("metrics"),
                "series": result.get("series"),
            }
        ]
    rows = [item for item in items if isinstance(item, dict)]
    if not rows or any(not isinstance(item.get("metrics"), dict) for item in rows):
        raise ValueError("Factor job has no reportable metrics")

    created = job.get("completed_at") or job.get("updated_at")
    if isinstance(created, datetime):
        report_date = created.astimezone(timezone.utc).date().isoformat()
    elif isinstance(created, str):
        report_date = created[:10]
    else:
        report_date = datetime.now(timezone.utc).date().isoformat()
    start = request.get("start_date") or "数据源可用起始日"
    end = request.get("end_date") or "数据源可用末端"
    universe = _report_cell(request.get("universe") or "A 股默认股票池")
    cost_model = request.get("cost_model")
    cost_rate = (
        cost_model.get("round_trip_cost") if isinstance(cost_model, dict) else None
    )
    cost_text = (
        f"{float(cost_rate) * 100:.3f}%"
        if isinstance(cost_rate, (int, float))
        and not isinstance(cost_rate, bool)
        and math.isfinite(float(cost_rate))
        else "历史任务未记录"
    )

    lines = [
        "# 因子回测与验证报告",
        f"> 报告日期：{report_date}",
        f"> 数据截至：{_report_cell(end)}",
        "",
        "## 运行范围",
        f"- 类型：{_report_cell(job.get('kind') or 'backtest')}",
        f"- 股票池：{universe}",
        f"- 窗口：{_report_cell(start)} 至 {_report_cell(end)}",
        f"- 数据源：{_report_cell(request.get('data_source') or '历史任务')}",
        f"- 冻结双边成本率：{cost_text}",
        "",
        "## 结果摘要",
        "年化收益采用扣费净收益；分位组合为毛收益。结果仅使用任务保存的指标和序列。",
        "",
        "| 因子 | 样本数 | IC 均值 | Rank IC | 换手后 Rank IC | 年化净收益 | 年化毛收益 | Sharpe | 最大回撤 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for index, item in enumerate(rows, start=1):
        metrics = item["metrics"]
        factor_id = item.get("factor_id") or request.get("factor_id") or f"因子 {index}"
        lines.append(
            "| "
            + " | ".join(
                [
                    _report_cell(factor_id),
                    _report_metric(metrics, "observations", "n_obs"),
                    _report_metric(metrics, "ic_mean", "ic_value", "ic"),
                    _report_metric(metrics, "rank_ic_median", "rank_ic"),
                    _report_metric(metrics, "net_rank_ic_mean"),
                    _report_metric(
                        metrics, "annual_return", "ann_return_net", percent=True
                    ),
                    _report_metric(
                        metrics, "gross_annual_return", "ann_return", percent=True
                    ),
                    _report_metric(metrics, "sharpe_ratio", "sharpe_net", "sharpe"),
                    _report_metric(metrics, "max_drawdown", percent=True),
                ]
            )
            + " |"
        )
        validation = item.get("validation")
        if not isinstance(validation, dict):
            validation = result.get("validation")
        if isinstance(validation, dict):
            training = validation.get("training_window") or {}
            testing = validation.get("validation_window") or {}
            lines.append(
                f"\n- 训练窗口：{_report_cell(training.get('start_date'))} 至 {_report_cell(training.get('end_date'))}；"
                f"验证窗口：{_report_cell(testing.get('start_date'))} 至 {_report_cell(testing.get('end_date'))}；"
                f"不重叠：{'是' if validation.get('non_overlapping') is True else '否/未知'}"
            )
        series = item.get("series")
        dates = series.get("dates") if isinstance(series, dict) else None
        if isinstance(dates, list) and dates:
            lines.extend(
                [
                    "",
                    f"### 因子 {_report_cell(factor_id)}：最近 10 个序列点",
                    "| 日期 | 多头净值 | 等权基准 | 超额收益 | IC | Rank IC | 新进持仓比例 |",
                    "|---|---:|---:|---:|---:|---:|---:|",
                ]
            )
            series_fields = (
                "nav_long",
                "nav_bench",
                "nav_excess_pct",
                "ic",
                "rank_ic",
                "turnover",
            )
            values = {
                key: series.get(key) if isinstance(series.get(key), list) else []
                for key in series_fields
            }
            for point in range(max(0, len(dates) - 10), len(dates)):
                cells = [_report_cell(dates[point])]
                for key in series_fields:
                    raw = values[key][point] if point < len(values[key]) else None
                    if (
                        isinstance(raw, bool)
                        or not isinstance(raw, (int, float))
                        or not math.isfinite(float(raw))
                    ):
                        cells.append("—")
                    elif key == "nav_excess_pct":
                        cells.append(f"{float(raw):.2f}%")
                    else:
                        cells.append(f"{float(raw):.4f}")
                lines.append("| " + " | ".join(cells) + " |")
        else:
            lines.extend(["", f"### 因子 {_report_cell(factor_id)}：未保存日度序列"])

    lines.extend(
        [
            "",
            "## 口径与限制",
            "- 多头组合等权持有因子值前 30% 股票，收益使用次日收益；交易成本按新进多头持仓比例和任务冻结的 A 股双边费率估算。",
            "- 报告中的等权基准、IC/RankIC、分位组合和换手序列来自本次已保存结果；未重新读取行情或重跑因子。",
            "- 此估算未模拟最低佣金、个股涨跌停、停牌成交约束和逐笔撮合；不构成可交易收益承诺或投资建议。",
        ]
    )
    return "\n".join(lines)


def _render_factor_job_pdf(markdown: str) -> bytes:
    from backend.scripts.md_to_pdf_report import render_markdown_pdf

    return render_markdown_pdf(markdown)


def _public(job: dict) -> dict:
    request = job.get("request_snapshot")
    if not isinstance(request, dict):
        request = {}
    request = request.get("request") if isinstance(request.get("request"), dict) else {}
    safe_request = {
        key: request[key]
        for key in (
            "factor_id",
            "task_id",
            "action",
            "task_ids",
            "doc_id",
            "source_sha256",
            "organize_kind",
            "factor_ids",
            "name",
            "market",
            "seed",
            "batch_id",
            "batch_size",
            "universe",
            "start_date",
            "end_date",
            "data_source",
            "cost_model",
            "validation_context",
        )
        if key in request
    }
    output = {
        key: job[key]
        for key in (
            "job_id",
            "parent_task_id",
            "kind",
            "status",
            "execution_generation",
            "progress_snapshot",
            "result_snapshot",
            "error_code",
            "error_message",
            "created_at",
            "updated_at",
            "completed_at",
        )
        if key in job
    }
    output["request"] = safe_request
    for key in ("created_at", "updated_at", "completed_at"):
        value = output.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, datetime):
            output[key] = to_utc_iso(value.astimezone(timezone.utc))
    if output.get("error_message"):
        output["error_message"] = redact_text(str(output["error_message"]))[:2000]
    return redact_data(output)


def _validation_context(factor: dict, start: str | None, end: str | None) -> dict:
    """Require a user-selected validation period strictly after the mining range."""
    from backend.services.engine.alpha_agent.factor_job_executor import _request_date

    if not start or not end:
        raise ValueError("Validation requires explicit start_date and end_date")
    date_range = factor.get("date_range")
    if not isinstance(date_range, str) or date_range.count("/") != 1:
        raise ValueError("Factor mining window is unknown; validation is unavailable")
    training_start, training_end = date_range.split("/", 1)
    training_start = _request_date(training_start, "factor training start")
    training_end = _request_date(training_end, "factor training end")
    validation_start = _request_date(start, "start_date")
    validation_end = _request_date(end, "end_date")
    if (
        not training_start
        or not training_end
        or not validation_start
        or not validation_end
    ):
        raise ValueError("Training and validation windows must be complete")
    if training_start > training_end:
        raise ValueError("Factor mining window is invalid")
    if validation_start > validation_end:
        raise ValueError("start_date must not be after end_date")
    if validation_start <= training_end:
        raise ValueError("Validation window must begin after the factor mining window")
    return {
        "kind": "out_of_sample_validation",
        "training_window": {"start_date": training_start, "end_date": training_end},
        "validation_window": {
            "start_date": validation_start,
            "end_date": validation_end,
        },
        "non_overlapping": True,
    }


@router.post("/factor-jobs", status_code=202)
async def submit_factor_backtest(
    payload: FactorBacktestSubmitRequest, request: Request
) -> dict:
    tenant_id, user_id = _owner(request)
    from backend.services.engine.routers import alpha_agent as alpha_router

    factor = await _factors.get_factor(
        payload.factor_id, tenant_id=tenant_id, user_id=user_id
    )
    if factor is None:
        raise HTTPException(status_code=404, detail="Factor not found")
    factor_code = factor.get("factor_code")
    if not isinstance(factor_code, str) or not factor_code.strip():
        raise HTTPException(status_code=400, detail="Factor code is empty")
    factor_revision_no = _factor_revision_no(factor)
    if len(factor_code.encode("utf-8")) > 256 * 1024:
        raise HTTPException(status_code=413, detail="Factor code exceeds 256 KiB")
    try:
        kind = _factor_job_kind(factor_code, alpha_router)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if kind == "unknown":
        raise HTTPException(
            status_code=400, detail="Factor code has no supported entrypoint"
        )
    if kind == "factor_miner" and payload.data_source != "qlib_bin":
        raise HTTPException(
            status_code=400,
            detail="FactorMiner expressions require the Qlib data source",
        )
    universe = (
        payload.universe
        or (factor.get("universe") if payload.kind == "validate" else None)
        or "csi300"
    )
    try:
        from backend.services.engine.alpha_agent.factor_job_executor import (
            _request_date,
        )

        start = _request_date(payload.start_date, "start_date")
        end = _request_date(payload.end_date, "end_date")
        if start and end and start > end:
            raise ValueError("start_date must not be after end_date")
        validation_context = (
            _validation_context(factor, start, end)
            if payload.kind == "validate"
            else None
        )
        if validation_context is not None:
            validation_context["factor_revision_no"] = factor_revision_no
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not alpha_router._universe_is_valid(
        universe, tenant_id=tenant_id, user_id=user_id
    ):
        raise HTTPException(status_code=400, detail="A-share universe is unavailable")

    universe_symbols = None
    try:
        if universe not in alpha_router._VALID_CN_UNIVERSES:
            suffix_symbols = alpha_router._resolve_custom_pool_instruments(
                universe, tenant_id=tenant_id, user_id=user_id
            )
            if suffix_symbols is None:
                raise ValueError(
                    "Unbounded custom pools cannot be frozen for a backtest"
                )
            if not suffix_symbols or len(suffix_symbols) > 20_000:
                raise ValueError(
                    "Custom factor universe is empty or exceeds 20,000 symbols"
                )
            from backend.shared.stock_utils import StockCodeUtil

            universe_symbols = sorted(
                {StockCodeUtil.to_qlib(symbol) for symbol in suffix_symbols}
            )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    request_snapshot = {
        "factor_id": payload.factor_id,
        "datasets": await _factor_input_datasets(
            factor, tenant_id=tenant_id, user_id=user_id
        ),
        "factor_code": factor_code,
        "factor_code_sha256": hashlib.sha256(factor_code.encode("utf-8")).hexdigest(),
        "factor_revision_no": factor_revision_no,
        "factor_kind": kind,
        "universe": universe,
        "universe_symbols": universe_symbols,
        "market": "a_share",
        "start_date": payload.start_date,
        "end_date": payload.end_date,
        "data_source": payload.data_source,
        "cost_model": _cost_snapshot(),
    }
    if validation_context is not None:
        request_snapshot["validation_context"] = validation_context
    try:
        job_id = await _jobs.create(
            tenant_id=tenant_id,
            user_id=user_id,
            kind=payload.kind,
            request=request_snapshot,
            request_key=request.headers.get("Idempotency-Key"),
        )
    except (FactorJobIdempotencyConflict, FactorVisibilityConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FactorJobQueueFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Factor job admission is unavailable"
        ) from exc

    job = await _jobs.get(job_id, tenant_id=tenant_id, user_id=user_id)
    if job is None:
        raise HTTPException(
            status_code=503, detail="Factor job status could not be verified"
        )
    return {"code": 202, "data": _public(job), "dispatch": "durable_outbox"}


@router.post("/factor-jobs/materialize", status_code=202)
async def submit_factor_materialization(
    payload: FactorMaterializeRequest, request: Request
) -> dict:
    """Compute one saved Factor class into a private immutable dataset generation."""
    tenant_id, user_id = _owner(request)
    request_key = request.headers.get("Idempotency-Key", "").strip()
    if not request_key:
        raise HTTPException(
            status_code=400, detail="Materialization requires Idempotency-Key"
        )
    factor = await _factors.get_factor(
        payload.factor_id, tenant_id=tenant_id, user_id=user_id
    )
    if factor is None:
        raise HTTPException(status_code=404, detail="Factor not found")
    factor_code = factor.get("factor_code")
    if not isinstance(factor_code, str) or not factor_code.strip():
        raise HTTPException(status_code=400, detail="Factor code is empty")
    factor_revision_no = _factor_revision_no(factor)
    from backend.services.engine.routers import alpha_agent as alpha_router
    from backend.services.engine.alpha_agent.factor_job_executor import _request_date

    statistical_snapshot: dict[str, str] = {}
    try:
        metadata = factor.get("metadata") or {}
        from backend.services.engine.alpha_agent.statistical.materialization import (
            trusted_statistical_candidate,
        )

        candidate = trusted_statistical_candidate(metadata, factor_code)
        if candidate is not None:
            factor_kind = "qlib_expression"
            candidate_key = candidate.key
            source_task_id = metadata.get("task_id")
            if not isinstance(source_task_id, str) or not source_task_id.strip():
                raise ValueError("Statistical factor is missing its source task")
            from backend.services.engine.qlib_app.services.rd_agent_persistence import (
                AlphaMiningTaskPersistence,
            )

            source_task = await AlphaMiningTaskPersistence().get_for_owner_with_spec(
                source_task_id,
                tenant_id=tenant_id,
                user_id=user_id,
                consistent=True,
            )
            if (
                source_task is None
                or source_task.get("algorithm") != "statistical"
                or source_task.get("status") != "completed"
            ):
                raise ValueError("Statistical factor source task is unavailable")
            source_data = source_task.get("data_snapshot") or {}
            source_params = source_task.get("params_snapshot") or {}
            if isinstance(source_data, str):
                source_data = json.loads(source_data)
            if isinstance(source_params, str):
                source_params = json.loads(source_params)
            if (
                not isinstance(source_data, dict)
                or source_data.get("status") != "frozen"
                or not isinstance(source_params, dict)
            ):
                raise ValueError("Statistical factor source snapshot is not frozen")
            panel_hash = source_data.get("panel_sha256")
            source_start = source_data.get("mining_start")
            source_end = source_data.get("valid_end") or source_data.get("mining_end")
            source_universe = source_params.get("universe")
            if (
                not isinstance(panel_hash, str)
                or len(panel_hash) != 64
                or any(char not in "0123456789abcdef" for char in panel_hash.lower())
                or not isinstance(source_start, str)
                or not isinstance(source_end, str)
                or source_universe != payload.universe
            ):
                raise ValueError(
                    "Materialization must use its frozen statistical source window and universe"
                )
            # The research window is read back from the hashed frozen panel.
            # Dates after that freeze are appended later from backward-adjusted prices.
            statistical_snapshot = {
                "statistical_task_id": source_task_id,
                "statistical_panel_sha256": panel_hash.lower(),
                "research_task_id": source_task_id,
                "research_panel_sha256": panel_hash.lower(),
                "statistical_freeze_start": source_start,
                "statistical_freeze_end": source_end,
                "price_source": "frozen_statistical_panel",
            }
        else:
            try:
                factor_kind = alpha_router._detect_factor_kind(factor_code)
            except RuntimeError as exc:
                raise ValueError(
                    f"Only Factor class, calculate_* or trusted statistical factors can be materialized ({exc})"
                ) from exc
            candidate_key = None
        if factor_kind not in {"factor_class", "functional", "qlib_expression"}:
            raise ValueError(
                "Only Factor class, calculate_* or trusted statistical factors can be materialized"
            )
        start = _request_date(payload.start_date, "start_date")
        end = _request_date(payload.end_date, "end_date")
        # Omitted dates materialize the model-training history. The frozen
        # research window stays on the snapshot and is not reused as this span.
        if start is None or end is None:
            from backend.services.engine.alpha_agent.materialization_window import (
                TRAINING_MATERIALIZATION_START,
            )

            _, default_end = alpha_router._default_backtest_window("a_share")
            start = start or TRAINING_MATERIALIZATION_START
            end = end or default_end
        if start > end:
            raise ValueError("start_date must not be after end_date")
        if not alpha_router._universe_is_valid(
            payload.universe, tenant_id=tenant_id, user_id=user_id
        ):
            raise ValueError("A-share universe is unavailable")
        symbols = None
        if payload.universe not in alpha_router._VALID_CN_UNIVERSES:
            suffix_symbols = alpha_router._resolve_custom_pool_instruments(
                payload.universe, tenant_id=tenant_id, user_id=user_id
            )
            if (
                suffix_symbols is None
                or not suffix_symbols
                or len(suffix_symbols) > 20_000
            ):
                raise ValueError("Unbounded or invalid factor universe")
            from backend.shared.stock_utils import StockCodeUtil

            symbols = sorted({StockCodeUtil.to_qlib(value) for value in suffix_symbols})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    generation_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"{tenant_id}\0{user_id}\0{request_key}"
    ).hex
    snapshot = {
        "factor_id": payload.factor_id,
        "datasets": await _factor_input_datasets(
            factor, tenant_id=tenant_id, user_id=user_id
        ),
        "factor_code": factor_code,
        "factor_code_sha256": hashlib.sha256(factor_code.encode("utf-8")).hexdigest(),
        "factor_revision_no": factor_revision_no,
        "factor_kind": factor_kind,
        "price_source": "quantdb_daily_forward",
        "statistical_candidate_key": candidate_key,
        "statistical_evidence": metadata.get("evidence")
        if candidate is not None
        else None,
        "universe": payload.universe,
        "universe_symbols": symbols,
        "start_date": start,
        "end_date": end,
        "generation_id": generation_id,
        **statistical_snapshot,
    }
    # OSS-ADAPT: OSS 单容器不支持远端挖掘节点，跳过共享覆盖校验分支。
    try:
        from backend.services.engine.alpha_agent.materialization_window import (
            assert_materialization_fits,
            infer_functional_warmup,
            universe_symbol_cap,
            weekday_count,
        )

        warmup_bars = int(candidate.min_history) if candidate is not None else 0
        if candidate is None and factor_kind == "functional":
            warmup_bars = infer_functional_warmup(factor_code)
        elif candidate is None and factor_kind == "factor_class":
            from backend.services.engine.alpha_agent.factor_codegen import (
                panel_contract,
            )

            contract = panel_contract(factor_code)
            if contract is not None:
                warmup_bars = int(contract["warmup_bars"])
        counted_symbols = snapshot.get("universe_symbols")
        n_symbols = (
            len(counted_symbols)
            if isinstance(counted_symbols, list) and counted_symbols
            else universe_symbol_cap(payload.universe, symbols)
        )
        assert_materialization_fits(n_symbols, weekday_count(start, end), warmup_bars)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        from sqlalchemy import text

        def create_generation(session, job_id: str) -> None:
            session.execute(
                text(
                    "INSERT INTO alpha_user_factor_generations "
                    "(tenant_id,user_id,generation_id,dataset_key,status,job_id) "
                    "VALUES (:tenant,:user,:generation,'rd_mined','building',:job) "
                    "ON CONFLICT (tenant_id,user_id,generation_id) DO NOTHING"
                ),
                {
                    "tenant": tenant_id,
                    "user": user_id,
                    "generation": generation_id,
                    "job": job_id,
                },
            )

        job_id = await _jobs.create(
            tenant_id=tenant_id,
            user_id=user_id,
            kind="materialize",
            request=snapshot,
            request_key=request_key,
            parent_task_id=getattr(
                getattr(request, "state", None), "factor_parent_task_id", None
            ),
            on_created=create_generation,
        )
    except (FactorJobIdempotencyConflict, FactorVisibilityConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FactorJobQueueFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Factor materialization admission failed"
        ) from exc
    job = await _jobs.get(job_id, tenant_id=tenant_id, user_id=user_id)
    if job is None:
        raise HTTPException(
            status_code=503, detail="Factor job status could not be verified"
        )
    return {"code": 202, "data": _public(job), "dispatch": "durable_outbox"}


class FactorExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    factor_ids: list[str] = Field(min_length=1, max_length=12)
    start_date: str | None = None
    end_date: str | None = None
    name: str = Field(default="", max_length=128)


@router.post("/factors/export")
async def export_factors(request: Request, payload: FactorExportRequest) -> dict:
    """把多个已物化因子的分区 parquet 合并成一张宽表单文件（下载用）。

    同步执行（上限 12 个因子）；训练仍直接读 rd_mined 源列，导出文件
    只做下载与审计留痕，不进入训练特征发现口径。
    """
    from backend.services.engine.alpha_agent.factor_export import (
        ExportPreconditionError,
        export_factors as run_export,
    )
    from backend.services.engine.alpha_agent.factor_job_executor import _request_date

    tenant_id, user_id = _owner(request)
    factor_ids = [str(factor_id).strip() for factor_id in payload.factor_ids or []]
    factor_ids = [factor_id for factor_id in factor_ids if factor_id]
    if not factor_ids:
        raise HTTPException(status_code=422, detail="factor_ids 不能为空")
    if len(set(factor_ids)) != len(factor_ids):
        raise HTTPException(status_code=422, detail="因子 ID 必须互不相同")
    try:
        start = _request_date(payload.start_date, "start_date")
        end = _request_date(payload.end_date, "end_date")
        if start and end and start > end:
            raise ValueError("start_date must not be after end_date")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        owned = await _factors.get_factors_by_ids(
            factor_ids, tenant_id=tenant_id, user_id=user_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if len(owned) != len(factor_ids):
        raise HTTPException(status_code=404, detail="一个或多个因子不存在")
    names = {
        str(item.get("factor_id")): str(
            item.get("factor_name") or item.get("factor_id")
        )
        for item in owned
    }
    try:
        result = await run_export(
            tenant_id=tenant_id,
            user_id=user_id,
            factor_ids=factor_ids,
            factor_names=names,
            start=start,
            end=end,
            export_name=payload.name or "",
        )
    except ExportPreconditionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"code": 200, "data": result}


@router.get("/factors/exports/{export_id}/download")
async def download_factor_export(export_id: str, request: Request):
    """下载本人某次合并导出的 parquet 单文件（属主路径服务端拼装防穿越）。"""
    from backend.services.engine.alpha_agent.factor_export import (
        ExportPreconditionError,
        export_file_path,
    )

    tenant_id, user_id = _owner(request)
    try:
        path = export_file_path(tenant_id, user_id, export_id)
    except ExportPreconditionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="导出文件不存在或已清理")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=path.name,
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/docs/{doc_id}/parse", status_code=202)
async def submit_document_parse(doc_id: str, request: Request) -> dict:
    """Queue parsing of an authenticated user's immutable PDF source."""
    tenant_id, user_id = _owner(request)
    request_key = request.headers.get("Idempotency-Key", "").strip()
    if not request_key:
        raise HTTPException(
            status_code=400, detail="Document parse submissions require Idempotency-Key"
        )
    if len(request_key) > 256:
        raise HTTPException(status_code=400, detail="Idempotency-Key is too long")
    doc = await _docs.get_doc(doc_id, tenant_id=tenant_id, user_id=user_id)
    if doc is None or doc["status"] in {"deleted", "expired"}:
        raise HTTPException(status_code=404, detail="文档不存在")
    if doc["status"] == "parsing":
        active_job = (
            await _jobs.get(
                str(doc.get("parse_job_id") or ""),
                tenant_id=tenant_id,
                user_id=user_id,
            )
            if doc.get("parse_job_id")
            else None
        )
        if active_job and active_job["status"] in {"queued", "running", "stopping"}:
            return {
                "code": 202,
                "data": _public(active_job),
                "dispatch": "existing_job",
            }
        raise HTTPException(status_code=409, detail="文档解析状态需要先核验")
    if doc["status"] in {"parsed", "organized"}:
        raise HTTPException(status_code=409, detail="文档已解析")
    if doc["status"] not in {"uploaded", "parse_failed"}:
        raise HTTPException(status_code=409, detail="当前文档状态不支持解析")

    from backend.services.engine.alpha_agent.mineru_client import resolve_mineru_config

    if resolve_mineru_config() is None:
        raise HTTPException(
            status_code=503,
            detail="文档解析服务未配置；请在部署环境配置 MinerU 通道后重试",
        )
    try:
        job_id = await _jobs.create(
            tenant_id=tenant_id,
            user_id=user_id,
            kind="doc_parse",
            request={"doc_id": doc_id, "source_sha256": doc["sha256"]},
            request_key=request_key,
        )
    except (FactorJobIdempotencyConflict, FactorVisibilityConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FactorJobQueueFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="文档解析任务队列暂不可用") from exc

    try:
        linked = await _docs.attach_parse_job(
            doc_id,
            tenant_id=tenant_id,
            user_id=user_id,
            job_id=job_id,
        )
        latest = await _docs.get_doc(doc_id, tenant_id=tenant_id, user_id=user_id)
        if not linked and not (latest and latest.get("parse_job_id") == job_id):
            raise HTTPException(status_code=409, detail="文档状态已变化，请刷新后重试")
        job = await _jobs.get(job_id, tenant_id=tenant_id, user_id=user_id)
        if job is None:
            raise HTTPException(status_code=503, detail="解析任务状态暂不可核验")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="解析任务登记暂不可用") from exc
    return {"code": 202, "data": _public(job), "dispatch": "durable_outbox"}


@router.post("/docs/{doc_id}/parse/reconcile", status_code=202)
async def reconcile_document_parse(
    doc_id: str, payload: DocumentParseReconcileRequest, request: Request
) -> dict:
    """Verify and resume one uncertain MinerU submission for its document owner."""
    tenant_id, user_id = _owner(request)
    doc = await _docs.get_doc(doc_id, tenant_id=tenant_id, user_id=user_id)
    if doc is None or doc.get("status") in {"deleted", "expired"}:
        raise HTTPException(status_code=404, detail="文档不存在")
    if (
        doc.get("status") == "parsing"
        and doc.get("parser_batch_id") == payload.batch_id
        and doc.get("parse_state") in {"pending", "running"}
        and doc.get("parse_job_id")
    ):
        existing = await _jobs.get(
            str(doc["parse_job_id"]), tenant_id=tenant_id, user_id=user_id
        )
        if existing and existing.get("status") in {"queued", "running", "stopping"}:
            return {
                "code": 202,
                "data": _public(existing),
                "dispatch": "existing_reconciled_batch",
            }
    if (
        doc.get("status") != "parsing"
        or doc.get("parse_state") != "uncertain"
        or doc.get("parser_batch_id")
        or not doc.get("parse_job_id")
    ):
        raise HTTPException(status_code=409, detail="文档当前不需要批次核验")

    from backend.services.engine.alpha_agent.doc_parser import (
        _make_client,
        _part_data_id,
    )
    from backend.services.engine.alpha_agent.doc_store import decode_original_parts
    from backend.services.engine.alpha_agent.mineru_client import resolve_mineru_config

    config = resolve_mineru_config()
    if config is None:
        raise HTTPException(status_code=503, detail="文档解析服务未配置")
    parts = decode_original_parts(doc)
    if not parts:
        raise HTTPException(status_code=409, detail="文档原件清单不可核验")
    expected_ids = {
        _part_data_id(doc_id, index, len(parts)) for index in range(1, len(parts) + 1)
    }
    client = _make_client(config)
    try:
        items = await client.get_batch_results(payload.batch_id)
    except Exception as exc:  # noqa: BLE001 - provider detail must not leak
        logger.warning(
            "MinerU reconciliation lookup failed for owner document: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503, detail="MinerU 批次暂时无法核验，请稍后重试"
        ) from exc
    finally:
        await client.aclose()
    actual_ids = [str(item.data_id or "") for item in items]
    if (
        len(actual_ids) != len(expected_ids)
        or set(actual_ids) != expected_ids
        or any(not item.data_id for item in items)
    ):
        raise HTTPException(
            status_code=422,
            detail="该 MinerU 批次未精确匹配此文档的全部原件，未恢复任务",
        )

    try:
        resumed = await _jobs.reconcile_uncertain_document_parse(
            str(doc["parse_job_id"]),
            doc_id=doc_id,
            tenant_id=tenant_id,
            user_id=user_id,
            batch_id=payload.batch_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("MinerU parse reconciliation persistence failed")
        raise HTTPException(status_code=503, detail="解析恢复状态暂不可用") from exc
    if not resumed:
        raise HTTPException(
            status_code=409,
            detail="解析请求仍在执行或任务状态已变化，请刷新后重试",
        )
    return {
        "code": 202,
        "data": {
            "job_id": doc["parse_job_id"],
            "status": "running",
        },
        "dispatch": "reconciled_existing_batch",
    }


@router.post("/docs/{doc_id}/organize", status_code=202)
async def submit_document_organization(
    doc_id: str, payload: DocumentOrganizeRequest, request: Request
) -> dict:
    """Queue a private, version-pinned organization draft for one parsed document."""
    tenant_id, user_id = _owner(request)
    request_key = request.headers.get("Idempotency-Key", "").strip()
    if not request_key:
        raise HTTPException(
            status_code=400,
            detail="Document organization submissions require Idempotency-Key",
        )
    if len(request_key) > 128:
        raise HTTPException(status_code=400, detail="Idempotency-Key is too long")
    doc = await _docs.get_doc(doc_id, tenant_id=tenant_id, user_id=user_id)
    if doc is None or doc["status"] in {"deleted", "expired"}:
        raise HTTPException(status_code=404, detail="文档不存在")
    if doc.get("organize_state") in {"queued", "running", "stopping"}:
        active_job = (
            await _jobs.get(
                str(doc.get("organize_job_id") or ""),
                tenant_id=tenant_id,
                user_id=user_id,
            )
            if doc.get("organize_job_id")
            else None
        )
        if active_job and active_job["status"] in {"queued", "running", "stopping"}:
            return {
                "code": 202,
                "data": _public(active_job),
                "dispatch": "existing_job",
            }
        raise HTTPException(status_code=409, detail="文档整理状态需要先核验")
    if doc["status"] not in {"parsed", "organized"} or not doc.get("markdown_key"):
        raise HTTPException(status_code=409, detail="文档尚未完成解析")

    from backend.services.engine.alpha_agent.doc_organize import PROMPT_VERSION
    from backend.services.engine.alpha_agent.llm_client import resolve_llm_config

    if resolve_llm_config() is None:
        raise HTTPException(
            status_code=503,
            detail="文档整理服务未配置；请在部署环境配置 LLM 后重试",
        )
    job_request = {
        "doc_id": doc_id,
        "source_sha256": doc["sha256"],
        "markdown_key": doc["markdown_key"],
        "organize_kind": payload.kind,
        "extra": (payload.extra or "").strip(),
        "prompt_version": PROMPT_VERSION,
    }

    def attach_job(session, job_id: str) -> None:
        if not _docs.attach_organize_job(
            session,
            doc_id,
            tenant_id=tenant_id,
            user_id=user_id,
            job_id=job_id,
            source_sha256=doc["sha256"],
            markdown_key=doc["markdown_key"],
        ):
            raise ValueError("文档状态或版本已变化，请刷新后重试")

    try:
        job_id = await _jobs.create(
            tenant_id=tenant_id,
            user_id=user_id,
            kind="doc_organize",
            request=job_request,
            request_key=request_key,
            on_created=attach_job,
        )
    except (FactorJobIdempotencyConflict, FactorVisibilityConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FactorJobQueueFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="文档整理任务队列暂不可用") from exc
    job = await _jobs.get(job_id, tenant_id=tenant_id, user_id=user_id)
    if job is None:
        raise HTTPException(status_code=503, detail="文档整理任务状态暂不可核验")
    return {"code": 202, "data": _public(job), "dispatch": "durable_outbox"}


@router.get("/factor-jobs")
async def list_factor_jobs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    tenant_id, user_id = _owner(request)
    rows = await _jobs.list(
        tenant_id=tenant_id, user_id=user_id, limit=limit, offset=offset
    )
    return {"code": 200, "data": [_public(row) for row in rows]}


@router.post("/factor-jobs/batch", status_code=202)
async def submit_factor_batch(
    payload: FactorBatchSubmitRequest, request: Request
) -> dict:
    tenant_id, user_id = _owner(request)
    request_key = request.headers.get("Idempotency-Key", "").strip()
    if not request_key:
        raise HTTPException(
            status_code=400, detail="Batch submissions require Idempotency-Key"
        )
    try:
        factors = await _factors.get_factors_by_ids(
            payload.factor_ids,
            tenant_id=tenant_id,
            user_id=user_id,
            include_archived=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if len(factors) != len(payload.factor_ids):
        raise HTTPException(
            status_code=404, detail="One or more factors were not found"
        )
    factor_by_id = {factor["factor_id"]: factor for factor in factors}

    from backend.services.engine.routers import alpha_agent as alpha_router
    from backend.services.engine.alpha_agent.factor_job_executor import _request_date

    try:
        start = _request_date(payload.start_date, "start_date")
        end = _request_date(payload.end_date, "end_date")
        if start and end and start > end:
            raise ValueError("start_date must not be after end_date")
        if payload.kind == "validate" and (not start or not end):
            raise ValueError(
                "Batch validation requires explicit start_date and end_date"
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not alpha_router._universe_is_valid(
        payload.universe, tenant_id=tenant_id, user_id=user_id
    ):
        raise HTTPException(status_code=400, detail="A-share universe is unavailable")

    universe_symbols = None
    try:
        if payload.universe not in alpha_router._VALID_CN_UNIVERSES:
            suffix_symbols = alpha_router._resolve_custom_pool_instruments(
                payload.universe, tenant_id=tenant_id, user_id=user_id
            )
            if (
                suffix_symbols is None
                or not suffix_symbols
                or len(suffix_symbols) > 20_000
            ):
                raise ValueError("Unbounded or invalid custom factor universe")
            from backend.shared.stock_utils import StockCodeUtil

            universe_symbols = sorted(
                {StockCodeUtil.to_qlib(symbol) for symbol in suffix_symbols}
            )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    factor_items = []
    cost_model = _cost_snapshot()
    try:
        for factor_id in payload.factor_ids:
            factor = factor_by_id[factor_id]
            factor_code = factor.get("factor_code")
            if not isinstance(factor_code, str) or not factor_code.strip():
                raise ValueError("A selected factor has empty code")
            factor_revision_no = _factor_revision_no(factor)
            if len(factor_code.encode("utf-8")) > 256 * 1024:
                raise ValueError("A selected factor exceeds 256 KiB")
            try:
                factor_kind = _factor_job_kind(factor_code, alpha_router)
            except Exception as exc:
                raise ValueError("A selected factor code is invalid") from exc
            if factor_kind == "unknown":
                raise ValueError("A selected factor has no supported entrypoint")
            if factor_kind == "factor_miner" and payload.data_source != "qlib_bin":
                raise ValueError("FactorMiner expressions require the Qlib data source")
            item = {
                "factor_id": factor_id,
                "datasets": await _factor_input_datasets(
                    factor, tenant_id=tenant_id, user_id=user_id
                ),
                "factor_code": factor_code,
                "factor_code_sha256": hashlib.sha256(
                    factor_code.encode("utf-8")
                ).hexdigest(),
                "factor_revision_no": factor_revision_no,
                "factor_kind": factor_kind,
                "universe": payload.universe,
                "universe_symbols": universe_symbols,
                "market": "a_share",
                "start_date": payload.start_date,
                "end_date": payload.end_date,
                "data_source": payload.data_source,
                "cost_model": cost_model,
            }
            if payload.kind == "validate":
                item["validation_context"] = _validation_context(factor, start, end)
            factor_items.append(item)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    batch_id = hashlib.sha256(
        f"{tenant_id}\0{user_id}\0{request_key}".encode()
    ).hexdigest()[:32]
    request_snapshot = {
        "batch_id": batch_id,
        "batch_size": len(factor_items),
        "factor_ids": list(payload.factor_ids),
        "factor_items": factor_items,
        "market": "a_share",
        "universe": payload.universe,
        "start_date": payload.start_date,
        "end_date": payload.end_date,
        "data_source": payload.data_source,
        "cost_model": cost_model,
    }
    try:
        job_id = await _jobs.create(
            tenant_id=tenant_id,
            user_id=user_id,
            kind=payload.kind,
            request=request_snapshot,
            request_key=request_key,
        )
    except (FactorJobIdempotencyConflict, FactorVisibilityConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FactorJobQueueFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Factor batch admission is unavailable"
        ) from exc
    job = await _jobs.get(job_id, tenant_id=tenant_id, user_id=user_id)
    if job is None:
        raise HTTPException(
            status_code=503, detail="Factor batch status could not be verified"
        )
    return {"code": 202, "data": _public(job), "dispatch": "durable_outbox"}


@router.post("/factor-jobs/compose", status_code=202)
async def submit_factor_combo(
    payload: FactorComboSubmitRequest, request: Request
) -> dict:
    """Freeze the caller's factor revisions and private panel versions atomically."""
    tenant_id, user_id = _owner(request)
    if len(set(payload.factor_ids)) != len(payload.factor_ids):
        raise HTTPException(status_code=400, detail="Factor IDs must be unique")
    request_key = request.headers.get("Idempotency-Key", "").strip()
    if not request_key:
        raise HTTPException(
            status_code=400, detail="Combination jobs require Idempotency-Key"
        )
    from backend.services.engine.routers import alpha_agent as alpha_router

    if not alpha_router._universe_is_valid(
        payload.universe, tenant_id=tenant_id, user_id=user_id
    ):
        raise HTTPException(status_code=400, detail="A-share universe is unavailable")

    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session
    from backend.services.engine.alpha_agent.combo_optimizer import ComboConfig

    async with get_session(read_only=True) as session:
        selected = await session.execute(
            text(
                "SELECT pool.factor_id,pool.panel_version,factor.factor_name,"
                "MAX(revision.revision_no) AS factor_revision_no "
                "FROM rd_agent_factor_pool AS pool "
                "JOIN rd_agent_factors AS factor ON factor.tenant_id=pool.tenant_id "
                "AND factor.user_id=pool.user_id AND factor.factor_id=pool.factor_id "
                "JOIN rd_agent_factor_revisions AS revision "
                "ON revision.tenant_id=factor.tenant_id AND revision.user_id=factor.user_id "
                "AND revision.factor_id=factor.factor_id "
                "WHERE pool.tenant_id=:tenant_id AND pool.user_id=:user_id "
                "AND pool.market='a_share' AND pool.universe=:universe "
                "AND pool.archived_at IS NULL AND pool.panel_version IS NOT NULL "
                "AND pool.factor_id=ANY(CAST(:factor_ids AS TEXT[])) "
                "GROUP BY pool.factor_id,pool.panel_version,factor.factor_name"
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "universe": payload.universe,
                "factor_ids": payload.factor_ids,
            },
        )
        rows = [dict(row) for row in selected.mappings().all()]
    by_id = {str(row["factor_id"]): row for row in rows}
    if len(by_id) != len(payload.factor_ids):
        raise HTTPException(
            status_code=400,
            detail="Every factor must be an active member of your selected universe",
        )

    factor_items = []
    for factor_id in payload.factor_ids:
        row = by_id[factor_id]
        version = row.get("panel_version")
        revision_no = row.get("factor_revision_no")
        if (
            not isinstance(version, str)
            or len(version) != 64
            or any(char not in "0123456789abcdef" for char in version)
            or type(revision_no) is not int
            or revision_no < 1
        ):
            raise HTTPException(
                status_code=503,
                detail="Factor revision or private panel metadata is unavailable",
            )
        factor_items.append(
            {
                "factor_id": factor_id,
                "factor_revision_no": revision_no,
                "panel_version": version,
            }
        )

    config = ComboConfig(seed=payload.seed)
    snapshot = {
        "market": "a_share",
        "universe": payload.universe,
        "name": payload.name.strip(),
        "factor_ids": payload.factor_ids,
        "factor_items": factor_items,
        "config": {
            "max_days": config.max_days,
            "train_ratio": config.train_ratio,
            "popsize": config.popsize,
            "maxiter": config.maxiter,
            "tolerance": config.tolerance,
            "seed": config.seed,
            "time_budget_seconds": config.time_budget_seconds,
            "cost_rate": config.cost_rate,
        },
    }

    def persist_combo(session, job_id: str) -> None:
        session.execute(
            text(
                "INSERT INTO alpha_factor_combos "
                "(tenant_id,user_id,combo_id,job_id,market,universe,name) "
                "VALUES (:tenant,:user,:combo,:job,'a_share',:universe,:name)"
            ),
            {
                "tenant": tenant_id,
                "user": user_id,
                "combo": job_id,
                "job": job_id,
                "universe": payload.universe,
                "name": payload.name.strip(),
            },
        )
        for ordinal, item in enumerate(factor_items):
            session.execute(
                text(
                    "INSERT INTO alpha_factor_combo_members "
                    "(tenant_id,user_id,combo_id,factor_id,factor_revision_no,"
                    "panel_version,ordinal) VALUES "
                    "(:tenant,:user,:combo,:factor,:revision,:panel,:ordinal)"
                ),
                {
                    "tenant": tenant_id,
                    "user": user_id,
                    "combo": job_id,
                    "factor": item["factor_id"],
                    "revision": item["factor_revision_no"],
                    "panel": item["panel_version"],
                    "ordinal": ordinal,
                },
            )

    try:
        job_id = await _jobs.create(
            tenant_id=tenant_id,
            user_id=user_id,
            kind="compose",
            request=snapshot,
            request_key=request_key,
            on_created=persist_combo,
        )
    except (FactorJobIdempotencyConflict, FactorVisibilityConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FactorJobQueueFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Factor combination admission failed")
        raise HTTPException(
            status_code=503, detail="Combination admission failed"
        ) from exc

    job = await _jobs.get(job_id, tenant_id=tenant_id, user_id=user_id)
    if job is None:
        raise HTTPException(status_code=503, detail="Combination status is unavailable")
    return {"code": 202, "data": _public(job), "dispatch": "durable_outbox"}


@router.get("/factor-jobs/history")
async def factor_job_history(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: Literal[
        "pending", "queued", "running", "stopping", "completed", "failed", "cancelled"
    ]
    | None = None,
) -> dict:
    tenant_id, user_id = _owner(request)
    try:
        page = await _jobs.history_page(
            tenant_id=tenant_id,
            user_id=user_id,
            limit=limit,
            offset=offset,
            status=status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "code": 200,
        "data": {**page, "tasks": [_public(row) for row in page["tasks"]]},
    }


@router.get("/factors/{factor_id}/validation-history")
async def factor_validation_history(
    factor_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    tenant_id, user_id = _owner(request)
    factor = await _factors.get_factor(factor_id, tenant_id=tenant_id, user_id=user_id)
    if factor is None:
        raise HTTPException(status_code=404, detail="Factor not found")
    try:
        latest_validation_id = await _jobs.latest_validation_id(
            factor_id, tenant_id=tenant_id, user_id=user_id
        )
        attempts = await _jobs.validation_history(
            factor_id,
            tenant_id=tenant_id,
            user_id=user_id,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    safe_attempts = []
    for attempt in attempts:
        safe = {
            key: attempt[key]
            for key in (
                "validation_id",
                "factor_id",
                "status",
                "validation_context",
                "result_snapshot",
                "error_code",
                "created_at",
                "is_latest",
            )
            if key in attempt
        }
        created_at = safe.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if isinstance(created_at, datetime):
            safe["created_at"] = to_utc_iso(created_at.astimezone(timezone.utc))
        safe["is_latest"] = safe.get("validation_id") == latest_validation_id
        safe_attempts.append(redact_data(safe))
    return {
        "code": 200,
        "data": {
            "latest_validation_id": latest_validation_id,
            "attempts": safe_attempts,
        },
    }


@router.get("/factor-jobs/{job_id}")
async def get_factor_job(job_id: str, request: Request) -> dict:
    tenant_id, user_id = _owner(request)
    row = await _jobs.get(job_id, tenant_id=tenant_id, user_id=user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Factor job not found")
    return {"code": 200, "data": _public(row)}


@router.get("/factor-jobs/{job_id}/report.pdf")
async def export_factor_job_report(job_id: str, request: Request) -> Response:
    tenant_id, user_id = _owner(request)
    row = await _jobs.get(job_id, tenant_id=tenant_id, user_id=user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Factor job not found")
    if row.get("status") != "completed":
        raise HTTPException(
            status_code=409, detail="A completed factor job is required for PDF export"
        )
    try:
        markdown = _factor_job_report_markdown(row)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        pdf = await asyncio.to_thread(_render_factor_job_pdf, markdown)
    except ModuleNotFoundError as exc:
        if exc.name == "reportlab":
            raise HTTPException(
                status_code=503, detail="PDF report rendering is unavailable"
            ) from exc
        raise
    filename = f"factor-report-{hashlib.sha256(job_id.encode()).hexdigest()[:12]}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/factor-jobs/{job_id}/cancel")
async def cancel_factor_job(job_id: str, request: Request) -> dict:
    tenant_id, user_id = _owner(request)
    row = await _jobs.get(job_id, tenant_id=tenant_id, user_id=user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Factor job not found")
    if row["status"] not in {"queued", "running", "stopping"}:
        return {"code": 200, "data": _public(row)}
    outcome = await _jobs.request_cancel(
        job_id,
        tenant_id=tenant_id,
        user_id=user_id,
        generation=row["execution_generation"],
    )
    latest = await _jobs.get(job_id, tenant_id=tenant_id, user_id=user_id)
    if latest is None:
        raise HTTPException(status_code=404, detail="Factor job not found")
    return {"code": 200, "data": _public(latest), "cancel_request": outcome}


@router.delete("/factor-jobs/{job_id}")
async def delete_factor_job(job_id: str, request: Request) -> dict:
    """清除一条已结束的回测/验证记录（前后端一起消失，不可恢复）。"""
    tenant_id, user_id = _owner(request)
    try:
        deleted_status = await _jobs.delete_terminal(
            job_id, tenant_id=tenant_id, user_id=user_id
        )
    except FactorJobNotTerminal as exc:
        raise HTTPException(
            status_code=409, detail="该任务仍在运行，请先取消再清除"
        ) from exc
    except FactorJobProtected as exc:
        raise HTTPException(
            status_code=409, detail="只支持清除回测与验证记录"
        ) from exc
    if deleted_status is None:
        raise HTTPException(status_code=404, detail="Factor job not found")
    logger.info(
        "[factor-jobs] cleared job owner=%s/%s job=%s status=%s",
        tenant_id,
        user_id,
        job_id,
        deleted_status,
    )
    return {"code": 200, "data": {"job_id": job_id, "deleted": True}}
