"""推理缺口补全（与前端「一键补全至最新」同源）。

覆盖判定与补全上限：
- 读模型目录 pred.parquet 的 trade_date 集合
- 缺口 = [首个真实推理日, min(最新交易日, QuantDB 因子最新日)] 内未覆盖交易日
- 逐日 InferenceScriptRunner.execute，成功分数合并回 pred.parquet
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.shared.inference_coverage import find_inference_gap_dates

logger = logging.getLogger(__name__)

ProgressCb = Callable[[dict[str, Any]], None]


def _resolve_backfill_model_lineage(
    *, tenant_id: str, user_id: str, model_id: str
) -> tuple[str, str]:
    """补全链路的模型来源解析：复用默认模型链路，保证托管准入放行。

    backfill 只对 `is_default=TRUE` 的用户默认模型执行，正常应得
    `user_default`。此处走 `resolve_effective_model_sync(model_id=mid)`，
    与手动推理 `_resolve_requested_model` 同口径；解析失败或返回 `none`/空时
    回退为 `(model_id, "user_default")`，避免写出 NULL 来源导致托管误判
    `mismatch`（见 manual_execution_service.get_default_model_hosted_status）。
    """
    mid = str(model_id or "").strip()
    try:
        from backend.shared.model_registry import model_registry_service

        resolved = model_registry_service.resolve_effective_model_sync(
            tenant_id=tenant_id,
            user_id=user_id,
            model_id=mid,
        )
        effective = str(resolved.get("effective_model_id") or mid or "").strip()
        source = str(resolved.get("model_source") or "").strip()
        if not effective:
            effective = mid
        if source in {"user_default", "explicit_system_model"}:
            return effective, source
        if source in {"explicit_model_id", "strategy_binding"} and effective == mid:
            # 显式传入的恰好就是默认模型：信号来源仍是该默认模型
            return effective, "user_default"
        # none/空/其它：backfill 上下文已限定为默认模型，直接归属默认链路
        return effective or mid, "user_default"
    except Exception:
        return mid, "user_default"


def resolve_pred_candidates(storage_path: str) -> list[Path]:
    base = Path(storage_path)
    return [
        base / "pred.parquet",
        base / "pred" / "pred.parquet",
    ]


def read_pred_dates(parquet_file: Path) -> list[str]:
    import duckdb

    con = duckdb.connect()
    try:
        cols = [
            r[0]
            for r in con.execute(
                f"SELECT * FROM read_parquet('{str(parquet_file)}') LIMIT 0"
            ).description
        ]
        date_col = (
            "trade_date" if "trade_date" in cols else "date" if "date" in cols else None
        )
        if not date_col:
            return []
        rows = con.execute(
            f"SELECT DISTINCT CAST({date_col} AS DATE) AS d "
            f"FROM read_parquet('{str(parquet_file)}') ORDER BY d"
        ).fetchall()
        return [str(r[0])[:10] for r in rows if r[0] is not None]
    finally:
        try:
            con.close()
        except Exception:
            pass


def quantdb_latest_factor_date(market: str = "CN") -> date | None:
    """QuantDB 因子最新可用日；失败返回 None。"""
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
    except Exception:
        return None


def latest_trading_date() -> date:
    try:
        import exchange_calendars as xcals
        import pandas as pd

        cal = xcals.get_calendar("XSHG")
        today = date.today()
        for i in range(10):
            cur = today - timedelta(days=i)
            try:
                if cal.is_session(pd.Timestamp(cur)):
                    return cur
            except Exception:
                continue
        try:
            prev = cal.previous_session(pd.Timestamp(today))
            return (
                prev.date()
                if hasattr(prev, "date")
                else date.fromisoformat(str(prev)[:10])
            )
        except Exception:
            pass
        cur = today
        while cur.weekday() >= 5:
            cur -= timedelta(days=1)
        return cur
    except Exception:
        cur = date.today()
        while cur.weekday() >= 5:
            cur -= timedelta(days=1)
        return cur


def compute_coverage(
    *,
    model_id: str,
    storage_path: str,
    metadata: dict[str, Any] | None = None,
    market: str = "CN",
) -> dict[str, Any]:
    """与 API GET /models/{id}/inference/coverage 同口径。"""
    storage_path = str(storage_path or "").strip()
    latest = latest_trading_date()
    if not storage_path:
        return {
            "model_id": model_id,
            "min_date": None,
            "max_date": None,
            "count": 0,
            "gap_dates": [],
            "latest_trade_date": str(latest),
            "is_up_to_date": False,
        }

    parquet_file = next(
        (p for p in resolve_pred_candidates(storage_path) if p.is_file()), None
    )
    meta = metadata if isinstance(metadata, dict) else {}
    ctx = meta.get("context") if isinstance(meta.get("context"), dict) else {}
    factor_source = str(
        meta.get("factor_source") or ctx.get("factor_source") or "l1_factors"
    )
    mkt = str(ctx.get("market") or meta.get("market") or market or "CN")

    if not parquet_file:
        quantdb_fallback_dates: list[str] | None = None
        try:
            from backend.services.engine.data_platform.quantdb_factor_reader import (
                QuantDBFactorReader,
                market_data_dir,
            )

            qdir = market_data_dir(mkt)
            if qdir.is_dir():
                reader = QuantDBFactorReader(qdir, market=mkt)
                quantdb_fallback_dates = reader.available_dates(factor_source)
        except Exception:
            quantdb_fallback_dates = None
        if quantdb_fallback_dates:
            min_date, max_date = quantdb_fallback_dates[0], quantdb_fallback_dates[-1]
            gap_end = min(latest, quantdb_latest_factor_date(mkt) or latest)
            try:
                import exchange_calendars as xcals
                import pandas as pd

                cal = xcals.get_calendar("XSHG")
                start = pd.Timestamp(max_date) + pd.Timedelta(days=1)
                end = pd.Timestamp(gap_end)
                gap = (
                    [d.strftime("%Y-%m-%d") for d in cal.sessions_in_range(start, end)]
                    if start <= end
                    else []
                )
            except Exception:
                gap = []
            return {
                "model_id": model_id,
                "min_date": min_date,
                "max_date": max_date,
                "count": len(quantdb_fallback_dates),
                "gap_dates": gap,
                "latest_trade_date": str(latest),
                "data_cutoff_date": str(gap_end),
                "estimated": True,
                # 这里原先硬编码 False：parquet 缺失时永远「未最新」，
                # 缺口为空也照样被当成待办。门控实际看 gap_dates，但管理台
                # 与推理监控读的是这个字段，口径必须与 gap_dates 一致。
                "is_up_to_date": len(gap) == 0,
                "source": "quantdb_fallback",
            }
        return {
            "model_id": model_id,
            "min_date": None,
            "max_date": None,
            "count": 0,
            "gap_dates": [],
            "latest_trade_date": str(latest),
            "is_up_to_date": False,
            "reason": "pred.parquet not found and quantdb unavailable",
        }

    dates = read_pred_dates(parquet_file)
    if not dates:
        return {
            "model_id": model_id,
            "min_date": None,
            "max_date": None,
            "count": 0,
            "gap_dates": [],
            "latest_trade_date": str(latest),
            "is_up_to_date": False,
        }
    min_date, max_date = dates[0], dates[-1]
    gap_end = min(latest, quantdb_latest_factor_date(mkt) or latest)
    gap = find_inference_gap_dates(dates, gap_end)
    return {
        "model_id": model_id,
        "min_date": min_date,
        "max_date": max_date,
        "count": len(dates),
        "gap_dates": gap,
        "latest_trade_date": str(latest),
        "data_cutoff_date": str(gap_end),
        "is_up_to_date": len(gap) == 0,
    }


def _template_copy_day(parquet_file: Path, d: str) -> tuple[bool, str]:
    """Compatibility guard: copied scores must never stand in for inference."""
    return False, f"{d} 推理失败：已禁止模板复制，保留缺口等待真实推理"


async def _mark_failed_run(
    *,
    result: Any,
    tenant_id: str,
    user_id: str,
    model_id: str,
    d: str,
    source: str,
    effective_model_id: str = "",
    model_source: str = "",
) -> None:
    if result is None or getattr(result, "success", False):
        return
    try:
        from backend.services.engine.services.model_inference_persistence import (
            model_inference_persistence,
        )

        rid = str(getattr(result, "run_id", "") or "")
        if not rid:
            return
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        await model_inference_persistence.create_run(
            run_id=rid,
            tenant_id=tenant_id,
            user_id=user_id,
            model_id=model_id,
            data_trade_date=date.fromisoformat(d),
            prediction_trade_date=date.fromisoformat(d),
            status="failed",
            request_payload={"source": source, "date": d},
            created_at=now,
        )
        await model_inference_persistence.update_run(
            run_id=rid,
            status="failed",
            updated_at=now,
            failure_stage=str(getattr(result, "failure_stage", "") or ""),
            error_message=str(getattr(result, "error", "") or "backfill 推理失败")[
                :2000
            ],
            active_model_id=effective_model_id or model_id,
            effective_model_id=effective_model_id or model_id,
            model_source=model_source or "user_default",
        )
    except Exception:
        pass


async def _mark_success_run(
    *,
    result: Any,
    tenant_id: str,
    user_id: str,
    model_id: str,
    d: str,
    source: str,
    effective_model_id: str = "",
    model_source: str = "",
) -> None:
    """补全成功的日期同样落一条 completed 记录。

    以前只有失败才写 qm_model_inference_runs，于是补全出来的日期在「推理历史」
    里查不到；更关键的是下游按该表判断「最新一次推理」的链路（信号就绪、托管交易、
    TDX 推送）会一直停在补全前的那一天，看不到补全出来的信号。
    """
    if result is None or not getattr(result, "success", False):
        return
    try:
        from backend.services.engine.services.model_inference_persistence import (
            model_inference_persistence,
        )

        rid = str(getattr(result, "run_id", "") or "")
        if not rid:
            return

        def _as_date(value: Any) -> date:
            try:
                return date.fromisoformat(str(value)[:10])
            except Exception:
                return date.fromisoformat(d)

        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        await model_inference_persistence.create_run(
            run_id=rid,
            tenant_id=tenant_id,
            user_id=user_id,
            model_id=model_id,
            data_trade_date=_as_date(getattr(result, "data_trade_date", "") or d),
            prediction_trade_date=_as_date(
                getattr(result, "prediction_trade_date", "") or d
            ),
            status="completed",
            request_payload={"source": source, "date": d},
            created_at=now,
        )
        await model_inference_persistence.update_run(
            run_id=rid,
            status="completed",
            updated_at=now,
            signals_count=int(getattr(result, "signals_count", 0) or 0),
            active_model_id=effective_model_id or model_id,
            effective_model_id=effective_model_id or model_id,
            model_source=model_source or "user_default",
        )
    except Exception as exc:
        # 落历史失败不能影响补全本身（pred.parquet 已经写好）
        logger.warning("record success run %s %s failed: %s", model_id, d, exc)


async def backfill_model_gaps(
    *,
    tenant_id: str,
    user_id: str,
    model_id: str,
    storage_path: str,
    gaps: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    source: str = "backfill",
    progress_cb: ProgressCb | None = None,
    allow_template_copy: bool = False,
) -> dict[str, Any]:
    """对单个模型执行一键补全。gaps 为空时自动算缺口。"""
    storage_path = str(storage_path or "").strip()
    if gaps is None:
        cov = compute_coverage(
            model_id=model_id, storage_path=storage_path, metadata=metadata
        )
        gaps = list(cov.get("gap_dates") or [])

    if not gaps:
        result = {
            "status": "completed",
            "message": "已是最新",
            "gap": 0,
            "gap_requested": 0,
            "capped": False,
            "appended": 0,
            "rebuilt": 0,
            "failed": 0,
            "merge_error": "",
            "logs": [],
            "model_id": model_id,
        }
        if progress_cb:
            progress_cb(result)
        return result

    from backend.services.engine.inference.backfill_guard import (
        covered_dates_from_db,
        scores_for_dates,
    )
    from backend.services.engine.inference.pred_merge import merge_signals_into_pred
    from backend.services.engine.inference.script_runner import InferenceScriptRunner

    appended = 0
    rebuilt = 0
    failed = 0
    logs: list[str] = []
    pred_signals: list[tuple[str, list[dict]]] = []

    # ── 防线 2：库里已有分数的日期不再重推 ────────────────────────────────
    # 缺口判定的唯一凭证是 pred.parquet；而落库（engine_signal_scores +
    # 候选快照）在同一事务里先 commit，parquet 合并却在推理之后、且失败只记日志。
    # 于是「分数在库里、parquet 没有」会永久算作缺口，被每 30 分钟一轮的 Beat
    # 反复重推。这里先用库里已有分数把这些日期从缺口里摘出去，稍后回写 parquet。
    db_covered = covered_dates_from_db(
        tenant_id=tenant_id, user_id=user_id, model_id=model_id, dates=gaps
    )

    parquet_file = next(
        (p for p in resolve_pred_candidates(storage_path) if p.is_file()), None
    )
    if not parquet_file:
        parquet_file = Path(storage_path) / "pred.parquet"

    def _emit(**extra: Any) -> None:
        if not progress_cb:
            return
        payload = {
            "status": "running",
            "model_id": model_id,
            "gap": len(gaps),
            "appended": appended,
            "rebuilt": rebuilt,
            "failed": failed,
            "logs": "\n".join(logs[-100:]),
            **extra,
        }
        progress_cb(payload)

    # 补全行必须带默认模型来源，否则托管准入判 mismatch（观察态）。
    _lineage_effective, _lineage_source = _resolve_backfill_model_lineage(
        tenant_id=tenant_id, user_id=user_id, model_id=model_id
    )

    # ── 防线 4：分批落盘，进度不因进程被杀而丢 ──────────────────────────
    # 旧写法把 merge 放在整个 for 之后：worker 被 OOM 杀掉 / 任务超时 SIGKILL，
    # 本轮已经推理成功的日期全部作废（DB 有分数、parquet 没有），下一轮再来一遍。
    # 每 N 日合并一次：merge 是「整文件重写」，N 太小会把 I/O 打满（pred.parquet
    # 可达数百万行），太大又保不住进度，默认 10 日（单轮上限 30 日 → 最多 3 次重写）。
    _merge_every = max(1, int(os.getenv("INFERENCE_BACKFILL_MERGE_EVERY_DAYS", "10")))
    merge_error = ""
    merged_rows = 0

    def _flush_pred() -> None:
        nonlocal merge_error, merged_rows
        if not pred_signals:
            return
        if parquet_file is None:
            # 模型目录不可用：分数只落在 PG，缺口无法闭合，必须如实报失败，
            # 否则状态 completed 会让下一轮继续按同样的缺口重推。
            merge_error = "pred.parquet 路径不可用，缺口未闭合"
            logs.append(merge_error)
            pred_signals.clear()
            return
        batch = list(pred_signals)
        pred_signals.clear()
        try:
            merged = merge_signals_into_pred(
                Path(parquet_file), batch, create_if_missing=True
            )
            if int(merged or 0) <= 0:
                merge_error = "pred.parquet 合并返回 0 行（缺口未闭合）"
                logs.append(merge_error)
                logger.error("backfill %s %s", model_id, merge_error)
                return
            merged_rows += int(merged)
            logs.append(f"pred.parquet 已合并 {merged} 行（{len(batch)} 日）")
        except Exception as exc:
            merge_error = f"pred.parquet 合并失败: {exc}"
            logs.append(merge_error)
            logger.error("backfill %s merge pred.parquet failed: %s", model_id, exc)

    # ── 防线 2（执行）：库里已有分数的日期，回写 parquet 而不是重推理 ──────
    if db_covered:
        recovered = scores_for_dates(
            tenant_id=tenant_id, user_id=user_id, model_id=model_id, dates=gaps
        )
        for _d, _signals in recovered:
            rebuilt += 1
            pred_signals.append((_d, _signals))
            logs.append(
                f"{_d} 已有落库分数 {len(_signals)} 只，跳过重复推理，仅回写 pred.parquet"
            )
        _recovered_dates = {str(_d)[:10] for _d, _ in recovered}
        gaps = [d for d in gaps if d not in _recovered_dates]
        # 立刻落盘：这一步不花 GPU，先把缺口闭掉，即使后面推理被杀也不会重来
        _flush_pred()

    # ── 防线 1：单轮真实推理天数上限 ──────────────────────────────────────
    # compute_coverage 的 quantdb_fallback 分支在 pred.parquet 缺失时，会把
    # QuantDB 首个可用日之后的**全部**交易日都当成缺口（可达数百天）。
    # 一次点击/一轮 Beat 就打数百次全量推理，是最容易失控的入口。
    # 超出部分保留缺口，交给后续轮询继续推进（Beat 每 30 分钟一轮）。
    _max_days = max(1, int(os.getenv("INFERENCE_BACKFILL_MAX_DAYS_PER_RUN", "30")))
    gap_requested = len(gaps) + rebuilt
    capped = len(gaps) > _max_days
    if capped:
        logs.append(
            f"待推理 {len(gaps)} 日，本轮最多补 {_max_days} 日（最旧优先），"
            f"剩余 {len(gaps) - _max_days} 日留给后续轮次"
        )
        gaps = gaps[:_max_days]

    try:
        for idx, d in enumerate(gaps):
            try:
                _emit(progress=int((idx + 1) / len(gaps) * 100))
                executed = False
                result = None
                try:
                    runner = InferenceScriptRunner(
                        primary_model_dir=str(Path(storage_path)),
                        primary_model_id=model_id,
                    )

                    def _sync_run(
                        _d=d,
                        _runner=runner,
                        _tenant=tenant_id,
                        _user=user_id,
                    ):
                        return _runner.execute(
                            date=_d, tenant_id=_tenant, user_id=_user
                        )

                    loop = asyncio.get_running_loop()
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        result = await loop.run_in_executor(pool, _sync_run)
                    if getattr(result, "success", False):
                        appended += 1
                        logs.append(
                            f"{d} 推理完成（runner {result.signals_count} 行）"
                        )
                        executed = True
                        pred_signals.append(
                            (d, list(getattr(result, "signals", None) or []))
                        )
                    else:
                        logs.append(
                            f"{d} runner 失败: {getattr(result, 'error', '')}"
                        )
                except Exception as exc:
                    logs.append(f"{d} runner 异常: {exc}")
                    result = None

                await _mark_failed_run(
                    result=result,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    model_id=model_id,
                    d=d,
                    source=source,
                    effective_model_id=_lineage_effective,
                    model_source=_lineage_source,
                )
                await _mark_success_run(
                    result=result,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    model_id=model_id,
                    d=d,
                    source=source,
                    effective_model_id=_lineage_effective,
                    model_source=_lineage_source,
                )

                if not executed and allow_template_copy:
                    ok, msg = _template_copy_day(parquet_file, d)
                    logs.append(msg)
                    if ok:
                        appended += 1
                        executed = True
                    else:
                        failed += 1
                elif not executed:
                    failed += 1

                await asyncio.sleep(0.05)
            except Exception as exc:
                failed += 1
                logs.append(f"{d} 失败: {exc}")
                logger.warning("backfill %s %s failed: %s", model_id, d, exc)
            # 分批落盘：每完成 N 日就把分数写进 pred.parquet，进程随后被杀也不会
            # 作废已推理的日期（缺口一旦闭合，下一轮就不会再重推）。
            if (idx + 1) % _merge_every == 0:
                _flush_pred()
            _emit(progress=int((idx + 1) / len(gaps) * 100))

        # ── 防线 3：收尾合并，并把合并结果如实反映到状态里 ────────────────
        # 旧行为是「合并失败只写一行日志，状态仍然 completed」，而下一轮的缺口
        # 判定恰恰只看 parquet —— 等于把失败伪装成成功，循环就此形成。
        _flush_pred()

        if failed or merge_error:
            status = "partial" if (appended + rebuilt) > 0 else "failed"
            parts = [
                f"{appended}/{len(gaps)} 日新推理成功",
                f"{rebuilt}/{len(gaps)} 日复用已落库分数",
            ]
            if failed:
                parts.append(
                    f"{failed} 日失败："
                    + "; ".join(ln for ln in logs[-failed:] if "失败" in ln)[:300]
                )
            if merge_error:
                parts.append(merge_error[:300])
            out = {
                "status": status,
                "progress": 100,
                "gap": len(gaps),
                "gap_requested": gap_requested,
                "capped": capped,
                "appended": appended,
                "rebuilt": rebuilt,
                "merged_rows": merged_rows,
                "failed": failed,
                "merge_error": merge_error,
                "logs": logs[-200:],
                "error": "，".join(parts)[:1000],
                "model_id": model_id,
            }
        else:
            out = {
                "status": "completed",
                "progress": 100,
                "gap": len(gaps),
                "gap_requested": gap_requested,
                "capped": capped,
                "appended": appended,
                "rebuilt": rebuilt,
                "merged_rows": merged_rows,
                "failed": 0,
                "merge_error": "",
                "logs": logs[-200:],
                "model_id": model_id,
            }
        if progress_cb:
            progress_cb(out)
        return out
    except Exception as exc:
        out = {
            "status": "failed",
            "error": str(exc),
            "gap": len(gaps),
            "gap_requested": gap_requested,
            "capped": capped,
            "appended": appended,
            "rebuilt": rebuilt,
            "failed": failed,
            "merge_error": str(exc),
            "logs": logs[-200:],
            "model_id": model_id,
        }
        if progress_cb:
            progress_cb(out)
        return out


def list_default_models(
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
    model_id: str | None = None,
) -> list[dict[str, Any]]:
    """列出用户默认模型（is_default=TRUE 且 ready/active）。"""
    import os

    from sqlalchemy import create_engine as sa_create_engine
    from sqlalchemy import text as sa_text
    from sqlalchemy.orm import sessionmaker as sa_sessionmaker

    sync_db_url = str(os.getenv("DATABASE_URL", "")).strip()
    if "+asyncpg" in sync_db_url:
        sync_db_url = sync_db_url.replace("+asyncpg", "+psycopg2")
    if not sync_db_url or "postgresql" not in sync_db_url:
        sync_db_url = (
            "postgresql+psycopg2://postgres:quantmind2026@"
            "quantmind-postgresql:5432/quantmind"
        )

    engine = sa_create_engine(sync_db_url, pool_pre_ping=True)
    Session = sa_sessionmaker(bind=engine)
    db = Session()
    try:
        clauses = [
            "is_default = TRUE",
            "status IN ('ready', 'active')",
            "storage_path IS NOT NULL",
            "TRIM(storage_path) <> ''",
        ]
        params: dict[str, Any] = {}
        if tenant_id:
            clauses.append("tenant_id = :tenant_id")
            params["tenant_id"] = tenant_id
        if user_id:
            clauses.append("user_id = :user_id")
            params["user_id"] = user_id
        if model_id:
            clauses.append("model_id = :model_id")
            params["model_id"] = model_id
        rows = db.execute(
            sa_text(
                f"""
                SELECT tenant_id, user_id, model_id, storage_path, metadata_json
                FROM qm_user_models
                WHERE {" AND ".join(clauses)}
                ORDER BY tenant_id, user_id, model_id
                """
            ),
            params,
        ).mappings().all()
        out: list[dict[str, Any]] = []
        for row in rows:
            meta = row.get("metadata_json")
            if isinstance(meta, str):
                try:
                    import json

                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            elif not isinstance(meta, dict):
                meta = {}
            out.append(
                {
                    "tenant_id": str(row["tenant_id"] or "default"),
                    "user_id": str(row["user_id"] or ""),
                    "model_id": str(row["model_id"] or ""),
                    "storage_path": str(row["storage_path"] or ""),
                    "metadata_json": meta,
                }
            )
        return out
    finally:
        db.close()
        engine.dispose()


async def backfill_all_default_models(
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
    model_id: str | None = None,
    dry_run: bool = False,
    progress_cb: ProgressCb | None = None,
) -> dict[str, Any]:
    """扫描全部默认模型，按「一键补全至最新」补历史+当日缺口。"""
    models = list_default_models(
        tenant_id=tenant_id, user_id=user_id, model_id=model_id
    )
    summary: dict[str, Any] = {
        "status": "completed",
        "total_models": len(models),
        "skipped": 0,
        "completed": 0,
        "partial": 0,
        "failed": 0,
        # rebuilt: 复用已落库分数（未重复推理）的天数合计
        "rebuilt": 0,
        # capped: 因单轮补全天数上限被截断的模型数
        "capped": 0,
        "details": [],
    }
    logger.info(
        "[DefaultInferenceBackfill] 扫描默认模型 %d 个 dry_run=%s",
        len(models),
        dry_run,
    )
    for m in models:
        mid = m["model_id"]
        cov = compute_coverage(
            model_id=mid,
            storage_path=m["storage_path"],
            metadata=m.get("metadata_json"),
        )
        gaps = list(cov.get("gap_dates") or [])
        detail: dict[str, Any] = {
            "tenant_id": m["tenant_id"],
            "user_id": m["user_id"],
            "model_id": mid,
            "gap": len(gaps),
            "gap_dates_head": gaps[:5],
            "data_cutoff_date": cov.get("data_cutoff_date"),
            "max_date": cov.get("max_date"),
        }
        if not gaps:
            detail["status"] = "up_to_date"
            summary["skipped"] += 1
            summary["details"].append(detail)
            logger.info(
                "[DefaultInferenceBackfill] %s/%s %s 已是最新",
                m["tenant_id"],
                m["user_id"],
                mid,
            )
            continue
        if dry_run:
            detail["status"] = "dry_run"
            summary["details"].append(detail)
            logger.info(
                "[DefaultInferenceBackfill] dry-run %s gaps=%d head=%s",
                mid,
                len(gaps),
                gaps[:5],
            )
            continue

        def _model_progress(state: dict[str, Any], _mid=mid) -> None:
            if progress_cb:
                progress_cb({"current_model": _mid, **state})

        result = await backfill_model_gaps(
            tenant_id=m["tenant_id"],
            user_id=m["user_id"],
            model_id=mid,
            storage_path=m["storage_path"],
            gaps=gaps,
            metadata=m.get("metadata_json"),
            source="default_inference_backfill",
            progress_cb=_model_progress,
        )
        detail["status"] = result.get("status")
        detail["appended"] = result.get("appended", 0)
        # 复用了已落库分数的天数（不重复推理，只回写 parquet）
        detail["rebuilt"] = result.get("rebuilt", 0)
        # 单轮补全天数被上限截断（缺口仍在，下一轮继续）
        detail["capped"] = result.get("capped", False)
        detail["merge_error"] = result.get("merge_error") or ""
        detail["failed"] = result.get("failed", 0)
        summary["rebuilt"] = int(summary.get("rebuilt", 0)) + int(
            result.get("rebuilt", 0) or 0
        )
        if result.get("capped"):
            summary["capped"] = int(summary.get("capped", 0)) + 1
        if result.get("error"):
            detail["error"] = result["error"]
        st = str(result.get("status") or "")
        if st == "completed":
            summary["completed"] += 1
        elif st == "partial":
            summary["partial"] += 1
        else:
            summary["failed"] += 1
        summary["details"].append(detail)
        logger.info(
            "[DefaultInferenceBackfill] %s status=%s appended=%s rebuilt=%s "
            "capped=%s failed=%s gap=%s",
            mid,
            detail["status"],
            detail.get("appended"),
            detail.get("rebuilt"),
            detail.get("capped"),
            detail.get("failed"),
            detail["gap"],
        )

    if summary["failed"] and not summary["completed"] and not summary["partial"]:
        summary["status"] = "failed"
    elif summary["failed"] or summary["partial"]:
        summary["status"] = "partial"
    return summary
