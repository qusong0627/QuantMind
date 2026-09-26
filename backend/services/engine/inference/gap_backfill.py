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
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.shared.inference_coverage import find_inference_gap_dates

logger = logging.getLogger(__name__)

ProgressCb = Callable[[dict[str, Any]], None]


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
                "is_up_to_date": False,
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
    """runner 失败时回退：复制 pred 最后一日截面改日期。"""
    import duckdb
    import pandas as pd

    con = duckdb.connect()
    try:
        last_date = read_pred_dates(parquet_file)[-1] if parquet_file.is_file() else None
        if not last_date:
            return False, f"{d} 失败：无模板日且 runner 未成功"
        df = con.execute(
            f"SELECT * FROM read_parquet('{str(parquet_file)}') "
            f"WHERE CAST(trade_date AS VARCHAR) = '{last_date}'"
        ).df()
        if df.empty:
            return False, f"{d} 跳过：模板日无数据"
        df["trade_date"] = pd.Timestamp(d)
        existing = con.execute(
            f"SELECT * FROM read_parquet('{str(parquet_file)}')"
        ).df()
        combined = pd.concat([existing, df], ignore_index=True)
        combined = combined.drop_duplicates(
            subset=["symbol", "trade_date"], keep="last"
        )
        combined.to_parquet(str(parquet_file), index=False)
        return True, f"{d} 推理完成（模板复制 {len(df)} 行）"
    finally:
        try:
            con.close()
        except Exception:
            pass


async def _mark_failed_run(
    *,
    result: Any,
    tenant_id: str,
    user_id: str,
    model_id: str,
    d: str,
    source: str,
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
    allow_template_copy: bool = True,
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
            "appended": 0,
            "failed": 0,
            "logs": [],
            "model_id": model_id,
        }
        if progress_cb:
            progress_cb(result)
        return result

    from backend.services.engine.inference.pred_merge import merge_signals_into_pred
    from backend.services.engine.inference.script_runner import InferenceScriptRunner

    appended = 0
    failed = 0
    logs: list[str] = []
    pred_signals: list[tuple[str, list[dict]]] = []
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
            "failed": failed,
            "logs": "\n".join(logs[-100:]),
            **extra,
        }
        progress_cb(payload)

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
                )
                await _mark_success_run(
                    result=result,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    model_id=model_id,
                    d=d,
                    source=source,
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
            _emit(progress=int((idx + 1) / len(gaps) * 100))

        if pred_signals and parquet_file is not None:
            try:
                merged = merge_signals_into_pred(
                    Path(parquet_file), pred_signals, create_if_missing=True
                )
                logs.append(
                    f"pred.parquet 已合并 {merged} 行（{len(pred_signals)} 日）"
                )
            except Exception as exc:
                logs.append(f"pred.parquet 合并失败: {exc}")
                logger.warning(
                    "backfill %s merge pred.parquet failed: %s", model_id, exc
                )

        if failed:
            status = "partial" if appended > 0 else "failed"
            error = (
                f"{appended}/{len(gaps)} 日补全成功，{failed} 日失败："
                + "; ".join(ln for ln in logs[-failed:] if "失败" in ln)[:500]
            )
            out = {
                "status": status,
                "progress": 100,
                "gap": len(gaps),
                "appended": appended,
                "failed": failed,
                "logs": logs[-200:],
                "error": error,
                "model_id": model_id,
            }
        else:
            out = {
                "status": "completed",
                "progress": 100,
                "gap": len(gaps),
                "appended": appended,
                "failed": 0,
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
            "appended": appended,
            "failed": failed,
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
        detail["failed"] = result.get("failed", 0)
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
            "[DefaultInferenceBackfill] %s status=%s appended=%s failed=%s gap=%s",
            mid,
            detail["status"],
            detail.get("appended"),
            detail.get("failed"),
            detail["gap"],
        )

    if summary["failed"] and not summary["completed"] and not summary["partial"]:
        summary["status"] = "failed"
    elif summary["failed"] or summary["partial"]:
        summary["status"] = "partial"
    return summary
