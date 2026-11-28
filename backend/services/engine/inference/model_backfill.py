"""模型推理补推的共享核心（P3）。

一个模型「补推至最新」要做四件事，过去只写在 ``model_training`` 路由里，
且随任务结束即丢：解析模型产物 → 算缺口交易日 → 逐日跑推理 → 逐日合并
``pred.parquet``。本模块把这段收拢成可复用的同步/异步原语：

- ``resolve_backfill_model``：用户态模型 → ``BackfillModel``（系统模型只读，不在此列）；
- ``compute_coverage`` / ``compute_gap_dates``：复用 coverage 口径（含
  「QuantDB 因子尚未产出日不算缺口」的上限收敛）；
- ``run_one_day``：构造 runner → 执行 → 合并 pred → 失败落 run 终态；
- ``run_serial``：多模型串行，天数升序、模型按缺口数升序；取消/锁占用/
  连续失败 3 天止损都在这里。

不触碰 HTTP 与任务表：路由与 job 执行器各自只做编排。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from backend.services.engine.inference.pred_merge import merge_signals_into_pred
from backend.services.engine.inference.script_runner import InferenceScriptRunner
from backend.services.engine.services import model_inference_persistence
from backend.shared.inference_coverage import (
    find_inference_gap_dates,
    trading_dates_between,
)
from backend.shared.model_registry import model_registry_service
from backend.shared.utc_datetime import utc_now

logger = logging.getLogger(__name__)

#: 单个模型可补的缺口上限；超过说明选错了模型/数据，应走区间补推。
MAX_GAP_DAYS_PER_MODEL = 300
#: 同一模型连续失败多少天即放弃该模型（避免在坏模型上耗几小时）。
CONSECUTIVE_FAILURE_LIMIT = 3
#: 无历史 duration 时的单日 ETA（秒）。
DEFAULT_DAY_SECONDS = 120.0

_COVERAGE_STATUSES = frozenset({"ready", "active"})


@dataclass(frozen=True)
class BackfillModel:
    """补推所需的模型最小视图（不含 storage_path 之外的敏感字段）。"""

    model_id: str
    display_name: str
    model_type: str
    market: str
    target_horizon_days: int | None
    status: str
    storage_path: str
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class BackfillCoverage:
    """一个模型的推理覆盖与缺口。"""

    min_date: str | None
    max_date: str | None
    gap_dates: tuple[str, ...]
    latest_trade_date: str
    data_cutoff_date: str | None = None
    estimated: bool = False
    source: str = "pred"
    reason: str | None = None
    count: int = 0

    @property
    def gap_count(self) -> int:
        return len(self.gap_dates)


@dataclass(frozen=True)
class DayResult:
    """单日推理结果。``status`` ∈ completed|failed|skipped_lock。"""

    model_id: str
    day: str
    status: str
    signals_count: int = 0
    merged_rows: int = 0
    run_id: str = ""
    error: str = ""
    failure_stage: str = ""
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "completed"


@dataclass
class ModelRunResult:
    """``run_serial`` 内单个模型的汇总（直接可落 job 的 model_results）。"""

    model_id: str
    display_name: str
    status: str = "queued"
    days_total: int = 0
    days_completed: int = 0
    days_failed: int = 0
    days_skipped: int = 0
    days_merged: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    gap_dates: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "display_name": self.display_name,
            "status": self.status,
            "days_total": self.days_total,
            "days_completed": self.days_completed,
            "days_failed": self.days_failed,
            "days_skipped": self.days_skipped,
            "days_merged": self.days_merged,
            "errors": list(self.errors),
            "gap_dates": list(self.gap_dates),
        }


@dataclass(frozen=True)
class SerialResult:
    """串行执行总结果；状态判定留给 job 执行器。"""

    model_results: tuple[ModelRunResult, ...]
    completed_days: int
    failed_days: int
    skipped_days: int
    cancelled: bool

    @property
    def total_days(self) -> int:
        return self.completed_days + self.failed_days + self.skipped_days


# ── 模型与产物解析 ────────────────────────────────────────────────────────


def _metadata_of(model: dict[str, Any]) -> dict[str, Any]:
    raw = model.get("metadata_json")
    return raw if isinstance(raw, dict) else {}


def to_backfill_model(model: dict[str, Any]) -> BackfillModel:
    """把 ``qm_user_models`` 行归一成补推视图。"""
    meta = _metadata_of(model)
    context = meta.get("context") if isinstance(meta.get("context"), dict) else {}
    horizon = meta.get("target_horizon_days", context.get("target_horizon_days"))
    try:
        horizon_value = int(horizon) if horizon is not None else None
    except (TypeError, ValueError):
        horizon_value = None
    return BackfillModel(
        model_id=str(model.get("model_id") or "").strip(),
        display_name=str(
            meta.get("display_name")
            or meta.get("model_name")
            or model.get("display_name")
            or model.get("model_id")
            or ""
        ),
        model_type=str(meta.get("model_type") or context.get("model_type") or "").lower(),
        market=str(
            context.get("market") or meta.get("market") or "CN"
        ).upper(),
        target_horizon_days=horizon_value,
        status=str(model.get("status") or "").lower(),
        storage_path=str(model.get("storage_path") or "").strip(),
        metadata=meta,
    )


def ineligibility_reason(model: BackfillModel) -> str | None:
    """补推资格：只读系统模型/归档/未就绪/无产物一律给出原因。"""
    if model.model_id.startswith("sys-"):
        return "system_readonly"
    if model.status == "archived":
        return "archived"
    if model.status not in _COVERAGE_STATUSES:
        return "not_ready"
    if not model.storage_path:
        return "missing_storage"
    if not Path(model.storage_path).is_dir():
        return "missing_storage"
    return None


async def resolve_backfill_model(
    tenant_id: str, user_id: str, model_id: str
) -> BackfillModel | None:
    """按归属人取模型（不跨用户）；系统模型不经此路径。"""
    row = await model_registry_service.get_model(
        tenant_id=tenant_id, user_id=user_id, model_id=str(model_id).strip()
    )
    if not row:
        return None
    model = to_backfill_model(row)
    return model if model.model_id else None


def resolve_pred_candidates(storage_path: str) -> list[Path]:
    base = Path(storage_path)
    return [base / "pred.parquet", base / "pred" / "pred.parquet"]


def resolve_pred_parquet(storage_path: str) -> Path:
    """已存在的 pred.parquet；不存在时返回默认落点（合并时创建）。"""
    path = Path(storage_path)
    for candidate in resolve_pred_candidates(storage_path):
        if candidate.is_file():
            return candidate
    return path / "pred.parquet"


def read_pred_dates(parquet_file: Path) -> list[str]:
    import duckdb

    escaped = str(parquet_file).replace("'", "''")
    con = duckdb.connect()
    try:
        cols = [
            r[0]
            for r in con.execute(
                f"SELECT * FROM read_parquet('{escaped}') LIMIT 0"
            ).description
        ]
        date_col = (
            "trade_date" if "trade_date" in cols else "date" if "date" in cols else None
        )
        if not date_col:
            return []
        rows = con.execute(
            f"SELECT DISTINCT CAST({date_col} AS DATE) AS d "
            f"FROM read_parquet('{escaped}') WHERE {date_col} IS NOT NULL ORDER BY d"
        ).fetchall()
        return [str(r[0])[:10] for r in rows if r[0] is not None]
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响结果
            pass


def latest_trading_date(today: date | None = None) -> date:
    """最近交易日（逐日回退；日历不可用时只跳过周末）。"""
    try:
        import exchange_calendars as xcals
        import pandas as pd

        cal = xcals.get_calendar("XSHG")
        cursor = today or date.today()
        for offset in range(10):
            candidate = cursor - timedelta(days=offset)
            if cal.is_session(pd.Timestamp(candidate)):
                return candidate
    except Exception:  # noqa: BLE001 - 回退到周末排除
        pass
    cursor = today or date.today()
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def quantdb_latest_factor_date(market: str = "CN") -> date | None:
    """QuantDB 因子最新可用日；失败返回 None（不阻断 coverage）。"""
    try:
        from backend.services.engine.data_platform.quantdb_factor_reader import (
            QuantDBFactorReader,
            market_data_dir,
        )

        qdir = market_data_dir(market)
        if not qdir.is_dir():
            return None
        reader = QuantDBFactorReader(qdir, market=market)
        dates = reader.available_dates("l1_factors")
        if not dates:
            return None
        return date.fromisoformat(str(dates[-1])[:10])
    except Exception:  # noqa: BLE001 - 数据面不可用时按「无上限收敛」处理
        return None


def _quantdb_available_dates(model: BackfillModel) -> tuple[str, list[str]]:
    context = (
        model.metadata.get("context")
        if isinstance(model.metadata.get("context"), dict)
        else {}
    )
    factor_source = str(
        model.metadata.get("factor_source")
        or context.get("factor_source")
        or "l1_factors"
    )
    market = str(context.get("market") or model.metadata.get("market") or model.market)
    try:
        from backend.services.engine.data_platform.quantdb_factor_reader import (
            QuantDBFactorReader,
            market_data_dir,
        )

        qdir = market_data_dir(market)
        if not qdir.is_dir():
            return factor_source, []
        reader = QuantDBFactorReader(qdir, market=market)
        return factor_source, [
            str(value)[:10] for value in reader.available_dates(factor_source)
        ]
    except Exception:  # noqa: BLE001
        return factor_source, []


def compute_coverage(
    model: BackfillModel,
    *,
    today: date | None = None,
    latest: date | None = None,
    cutoff: date | None = None,
) -> BackfillCoverage:
    """计算覆盖与缺口。

    ``latest`` / ``cutoff`` 仅供测试注入；生产按「最近交易日」与
    「QuantDB 因子已产出日」双上限收敛。
    """
    latest_day = latest or latest_trading_date(today)
    cutoff_day = cutoff or quantdb_latest_factor_date(model.market) or latest_day
    gap_end = min(latest_day, cutoff_day)
    if not model.storage_path or not Path(model.storage_path).is_dir():
        return BackfillCoverage(
            min_date=None,
            max_date=None,
            gap_dates=(),
            latest_trade_date=str(latest_day),
            data_cutoff_date=str(gap_end),
            reason="missing_storage",
        )
    parquet_file = next(
        (path for path in resolve_pred_candidates(model.storage_path) if path.is_file()),
        None,
    )
    if parquet_file is None:
        _, factor_dates = _quantdb_available_dates(model)
        if not factor_dates:
            return BackfillCoverage(
                min_date=None,
                max_date=None,
                gap_dates=(),
                latest_trade_date=str(latest_day),
                data_cutoff_date=str(gap_end),
                estimated=True,
                source="none",
                reason="pred.parquet not found and quantdb unavailable",
            )
        max_date = factor_dates[-1]
        start = date.fromisoformat(max_date) + timedelta(days=1)
        gaps = trading_dates_between(start, gap_end) if start <= gap_end else []
        return BackfillCoverage(
            min_date=factor_dates[0],
            max_date=max_date,
            gap_dates=tuple(gaps),
            latest_trade_date=str(latest_day),
            data_cutoff_date=str(gap_end),
            estimated=True,
            source="quantdb_fallback",
            count=len(factor_dates),
        )
    dates = read_pred_dates(parquet_file)
    if not dates:
        return BackfillCoverage(
            min_date=None,
            max_date=None,
            gap_dates=(),
            latest_trade_date=str(latest_day),
            data_cutoff_date=str(gap_end),
            source="pred",
            reason="pred.parquet has no trade_date column",
        )
    gaps = find_inference_gap_dates(dates, gap_end)
    return BackfillCoverage(
        min_date=dates[0],
        max_date=dates[-1],
        gap_dates=tuple(gaps),
        latest_trade_date=str(latest_day),
        data_cutoff_date=str(gap_end),
        source="pred",
        count=len(dates),
    )


def compute_gap_dates(model: BackfillModel, **kwargs: Any) -> list[str]:
    return list(compute_coverage(model, **kwargs).gap_dates)


# ── 单日执行 ──────────────────────────────────────────────────────────────


def create_redis_client():
    """推理锁使用的同步 Redis 客户端（db 由 ``REDIS_DB`` 指定，默认 0）。

    显式关闭或未配置 Redis 时返回 None：runner 不加锁，但单进程内
    ``run_serial`` 本身仍是串行的。
    """
    flag = str(os.getenv("QM_BACKFILL_INFERENCE_LOCK", "1")).strip().lower()
    if flag in {"0", "false", "off", "no"}:
        return None
    host = str(os.getenv("REDIS_HOST") or "").strip()
    if not host:
        return None
    try:
        import redis

        return redis.Redis(
            host=host,
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD") or None,
            db=int(os.getenv("REDIS_DB", "0")),
            decode_responses=False,
            socket_connect_timeout=2.0,
            socket_timeout=5.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("补推推理锁 Redis 不可用，降级为无锁: %s", exc)
        return None


def _infer_sync(
    model: BackfillModel,
    day: str,
    tenant_id: str,
    user_id: str,
    redis_client: Any,
) -> tuple[Any, list]:
    """只推理、不合并：批量补推按模型收集所有日信号后一次合并。"""
    runner = InferenceScriptRunner(
        primary_model_dir=str(Path(model.storage_path)),
        primary_model_id=model.model_id,
    )
    result = runner.execute(
        date=day, tenant_id=tenant_id, user_id=user_id, redis_client=redis_client
    )
    signals: list = []
    if getattr(result, "success", False):
        signals = list(getattr(result, "signals", None) or [])
    return result, signals


def _execute_sync(
    model: BackfillModel,
    day: str,
    tenant_id: str,
    user_id: str,
    redis_client: Any,
) -> tuple[Any, int]:
    result, signals = _infer_sync(model, day, tenant_id, user_id, redis_client)
    merged_rows = 0
    if getattr(result, "success", False) and signals:
        merged_rows = merge_signals_into_pred(
            resolve_pred_parquet(model.storage_path),
            [(day, signals)],
            create_if_missing=True,
        )
    return result, merged_rows


async def _record_failed_run(
    model: BackfillModel,
    day: str,
    tenant_id: str,
    user_id: str,
    result: Any,
    task_id: str | None,
) -> None:
    """runner 失败时落 run 终态；失败落库本身不得影响补推主流程。"""
    run_id = str(getattr(result, "run_id", "") or "")
    if not run_id:
        return
    now = utc_now()
    try:
        await model_inference_persistence.create_run(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            model_id=model.model_id,
            data_trade_date=date.fromisoformat(day),
            prediction_trade_date=date.fromisoformat(day),
            status="failed",
            request_payload={
                "source": "backfill",
                "task_id": task_id or "",
                "date": day,
            },
            created_at=now,
        )
        await model_inference_persistence.update_run(
            run_id=run_id,
            status="failed",
            updated_at=now,
            failure_stage=str(getattr(result, "failure_stage", "") or ""),
            error_message=str(getattr(result, "error", "") or "backfill 推理失败")[:2000],
        )
    except Exception as exc:  # noqa: BLE001 - 终态写失败只告警
        logger.warning("backfill %s %s 落 run 终态失败: %s", model.model_id, day, exc)


async def run_one_day(
    model: BackfillModel,
    day: str,
    tenant_id: str,
    user_id: str,
    *,
    redis_client: Any = None,
    task_id: str | None = None,
) -> DayResult:
    """跑一天：推理 → 合并 pred → 失败落终态。异常收成 DayResult。"""
    started = time.monotonic()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        result, merged_rows = await asyncio.to_thread(
            _execute_sync, model, day, tenant_id, user_id, redis_client
        )
    except Exception as exc:  # noqa: BLE001 - 单日异常不打断整单
        logger.warning("backfill %s %s runner 异常: %s", model.model_id, day, exc)
        return DayResult(
            model_id=model.model_id,
            day=day,
            status="failed",
            error=str(exc),
            duration_ms=_elapsed_ms(),
        )

    fallback_reason = str(getattr(result, "fallback_reason", "") or "")
    if "LOCK_HELD" in fallback_reason:
        return DayResult(
            model_id=model.model_id,
            day=day,
            status="skipped_lock",
            error=fallback_reason,
            duration_ms=_elapsed_ms(),
        )
    if not getattr(result, "success", False):
        await _record_failed_run(model, day, tenant_id, user_id, result, task_id)
        return DayResult(
            model_id=model.model_id,
            day=day,
            status="failed",
            signals_count=int(getattr(result, "signals_count", 0) or 0),
            run_id=str(getattr(result, "run_id", "") or ""),
            error=str(getattr(result, "error", "") or "推理失败"),
            failure_stage=str(getattr(result, "failure_stage", "") or ""),
            duration_ms=_elapsed_ms(),
        )
    return DayResult(
        model_id=model.model_id,
        day=day,
        status="completed",
        signals_count=int(getattr(result, "signals_count", 0) or 0),
        merged_rows=merged_rows,
        run_id=str(getattr(result, "run_id", "") or ""),
        duration_ms=_elapsed_ms(),
    )


async def run_one_model(
    model: BackfillModel,
    gap_dates: Sequence[str],
    tenant_id: str,
    user_id: str,
    *,
    progress_cb: EventCallback | None = None,
    should_cancel: ShouldCancel | None = None,
    redis_client: Any = None,
    task_id: str | None = None,
    failure_limit: int = CONSECUTIVE_FAILURE_LIMIT,
) -> ModelRunResult:
    """单模型批量补推：模型只加载一次、所有缺口日只合并一次 pred.parquet。

    逐日 ``run_one_day`` 每天都要把 5.8M 行的 pred.parquet 读进来再整份写回；
    13 模型 × 21 日 = 273 次全量读写，是补推慢的主因。本函数把同一模型的
    所有缺口日信号先收集起来，最后调一次 ``merge_signals_into_pred``（其本身
    支持 ``[(day, signals), ...]`` 多日一次合并），把 273 次压到 13 次。
    """
    days = sorted(str(day) for day in gap_dates)
    summary = ModelRunResult(
        model_id=model.model_id,
        display_name=model.display_name,
        days_total=len(days),
        gap_dates=days,
    )
    summary.status = "running"
    pending: list[tuple[str, list[dict]]] = []
    consecutive_failures = 0
    cancelled = False

    for index, day in enumerate(days):
        if should_cancel is not None and await should_cancel():
            cancelled = True
            break
        started = time.monotonic()
        try:
            result, signals = await asyncio.to_thread(
                _infer_sync, model, day, tenant_id, user_id, redis_client
            )
        except Exception as exc:  # noqa: BLE001 - 单日异常不打断整模型
            logger.warning("backfill %s %s runner 异常: %s", model.model_id, day, exc)
            result, signals = None, []
        duration_ms = int((time.monotonic() - started) * 1000)

        fallback_reason = str(getattr(result, "fallback_reason", "") or "")
        if result is not None and "LOCK_HELD" in fallback_reason:
            summary.days_skipped += 1
            consecutive_failures = 0
            day_result = DayResult(
                model_id=model.model_id,
                day=day,
                status="skipped_lock",
                error=fallback_reason,
                duration_ms=duration_ms,
            )
        elif result is None or not getattr(result, "success", False):
            if result is not None:
                await _record_failed_run(model, day, tenant_id, user_id, result, task_id)
            summary.days_failed += 1
            consecutive_failures += 1
            error = str(getattr(result, "error", "") or "推理失败")
            if len(summary.errors) < 20:
                summary.errors.append({"date": day, "error": error})
            day_result = DayResult(
                model_id=model.model_id,
                day=day,
                status="failed",
                signals_count=int(getattr(result, "signals_count", 0) or 0),
                run_id=str(getattr(result, "run_id", "") or "") if result is not None else "",
                error=error,
                failure_stage=(
                    str(getattr(result, "failure_stage", "") or "")
                    if result is not None
                    else ""
                ),
                duration_ms=duration_ms,
            )
        else:
            summary.days_completed += 1
            consecutive_failures = 0
            pending.append((day, signals))
            day_result = DayResult(
                model_id=model.model_id,
                day=day,
                status="completed",
                signals_count=len(signals),
                run_id=str(getattr(result, "run_id", "") or ""),
                duration_ms=duration_ms,
            )

        await _emit(
            progress_cb,
            {
                "type": "day",
                "model_id": model.model_id,
                "day": day,
                "index": index,
                "days_total": len(days),
                "result": {
                    "status": day_result.status,
                    "signals_count": day_result.signals_count,
                    # 合并推迟到模型结束：单日事件先报 0，days_merged 在收尾更新
                    "merged_rows": 0,
                    "error": day_result.error,
                    "duration_ms": day_result.duration_ms,
                },
                "model": summary.as_dict(),
            },
        )
        if consecutive_failures >= failure_limit:
            break

    if pending:
        try:
            merged_rows = await asyncio.to_thread(
                merge_signals_into_pred,
                resolve_pred_parquet(model.storage_path),
                pending,
                create_if_missing=True,
            )
            summary.days_merged = len(pending)
            logger.info(
                "backfill %s batch merge: %d days / %d rows",
                model.model_id,
                len(pending),
                merged_rows,
            )
        except Exception as exc:  # noqa: BLE001 - 合并失败不掩盖逐日推理结果
            logger.warning("backfill %s batch merge 失败: %s", model.model_id, exc)
            if len(summary.errors) < 20:
                summary.errors.append({"date": "*", "error": f"batch merge: {exc}"})

    summary.status = _model_status(summary, cancelled)
    return summary


# ── 串行编排 ──────────────────────────────────────────────────────────────

DayRunner = Callable[..., Awaitable[DayResult]]
ShouldCancel = Callable[[], Awaitable[bool]]
ShouldSkipModel = Callable[[str], Awaitable[bool]]
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


def _model_status(summary: ModelRunResult, cancelled: bool) -> str:
    if cancelled:
        return "cancelled"
    if summary.days_failed == 0 and summary.days_skipped == 0:
        return "completed"
    if summary.days_completed > 0:
        return "partial"
    if summary.days_failed > 0:
        return "failed"
    return "skipped"


async def _emit(callback: EventCallback | None, event: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        await callback(event)
    except Exception as exc:  # noqa: BLE001 - 进度回调失败不打断执行
        logger.warning("补推进度回调失败: %s", exc)


async def run_serial(
    models: Sequence[tuple[BackfillModel, Sequence[str]]],
    tenant_id: str,
    user_id: str,
    *,
    run_day: DayRunner | None = None,
    progress_cb: EventCallback | None = None,
    should_cancel: ShouldCancel | None = None,
    should_skip_model: ShouldSkipModel | None = None,
    redis_client: Any = None,
    task_id: str | None = None,
    failure_limit: int = CONSECUTIVE_FAILURE_LIMIT,
) -> SerialResult:
    """多模型串行：全局同一时刻只跑一个模型（模型内逐日推理）。

    ``models`` 为 ``(model, gap_dates)``；排序口径 = 缺口天数升序、model_id
    升序（少缺口的模型先出结果），模型内日期升序。

    默认走 ``run_one_model``：同一模型的全部缺口日推理完后只合并一次
    ``pred.parquet``（把 13 模型 × 21 日的 273 次全量读写压到 13 次）。
    传入 ``run_day`` 时保留旧的逐日合并路径（测试/自定义）。
    """
    day_runner: DayRunner = run_day or run_one_day
    ordered = sorted(models, key=lambda item: (len(item[1]), item[0].model_id))

    summaries: list[ModelRunResult] = []
    completed = failed = skipped = 0
    cancelled = False

    for model, gap_dates in ordered:
        days = sorted(str(day) for day in gap_dates)
        summary = ModelRunResult(
            model_id=model.model_id,
            display_name=model.display_name,
            days_total=len(days),
            gap_dates=days,
        )
        summaries.append(summary)
        if should_cancel is not None and await should_cancel():
            cancelled = True
            summary.status = "cancelled"
            break
        if should_skip_model is not None and await should_skip_model(model.model_id):
            summary.status = "skipped_busy"
            summary.days_skipped = len(days)
            skipped += len(days)
            await _emit(
                progress_cb,
                {
                    "type": "model_finished",
                    "model": summary.as_dict(),
                    "reason": "busy",
                },
            )
            continue
        summary.status = "running"
        await _emit(
            progress_cb,
            {"type": "model_started", "model": summary.as_dict()},
        )
        # 默认走「模型级批量合并」：同一模型只加载/合并一次；
        # 传 run_day 时保留逐日路径（测试/自定义 runner 兼容）。
        if run_day is None:
            summary = await run_one_model(
                model,
                days,
                tenant_id,
                user_id,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
                redis_client=redis_client,
                task_id=task_id,
                failure_limit=failure_limit,
            )
            summaries[-1] = summary
            completed += summary.days_completed
            failed += summary.days_failed
            skipped += summary.days_skipped
            await _emit(
                progress_cb,
                {"type": "model_finished", "model": summary.as_dict()},
            )
            if summary.status == "cancelled":
                cancelled = True
                break
            continue
        consecutive_failures = 0
        for index, day in enumerate(days):
            if should_cancel is not None and await should_cancel():
                cancelled = True
                break
            result = await day_runner(
                model,
                day,
                tenant_id,
                user_id,
                redis_client=redis_client,
                task_id=task_id,
            )
            if result.status == "completed":
                summary.days_completed += 1
                completed += 1
                if result.merged_rows > 0:
                    summary.days_merged += 1
                consecutive_failures = 0
            elif result.status == "skipped_lock":
                summary.days_skipped += 1
                skipped += 1
            else:
                summary.days_failed += 1
                failed += 1
                consecutive_failures += 1
                if len(summary.errors) < 20:
                    summary.errors.append(
                        {"date": day, "error": result.error or "推理失败"}
                    )
            await _emit(
                progress_cb,
                {
                    "type": "day",
                    "model_id": model.model_id,
                    "day": day,
                    "index": index,
                    "days_total": len(days),
                    "result": {
                        "status": result.status,
                        "signals_count": result.signals_count,
                        "merged_rows": result.merged_rows,
                        "error": result.error,
                        "duration_ms": result.duration_ms,
                    },
                    "model": summary.as_dict(),
                },
            )
            if consecutive_failures >= failure_limit:
                # 坏模型止损：剩余日期不再尝试（也不算 skipped，保留在原缺口里）
                break
        summary.status = _model_status(summary, cancelled)
        if cancelled:
            break
        await _emit(
            progress_cb,
            {"type": "model_finished", "model": summary.as_dict()},
        )

    return SerialResult(
        model_results=tuple(summaries),
        completed_days=completed,
        failed_days=failed,
        skipped_days=skipped,
        cancelled=cancelled,
    )


__all__ = [
    "BackfillCoverage",
    "BackfillModel",
    "CONSECUTIVE_FAILURE_LIMIT",
    "DayResult",
    "MAX_GAP_DAYS_PER_MODEL",
    "ModelRunResult",
    "SerialResult",
    "compute_coverage",
    "create_redis_client",
    "compute_gap_dates",
    "ineligibility_reason",
    "latest_trading_date",
    "quantdb_latest_factor_date",
    "read_pred_dates",
    "resolve_backfill_model",
    "resolve_pred_parquet",
    "run_one_day",
    "run_one_model",
    "run_serial",
    "to_backfill_model",
]
