"""严格评估后置任务（WP2）：训练完成后自动产出 strict_report，写库供前端读取。

放在应用容器侧执行（读 /data 的 pred.parquet 与行情），不进入训练镜像：
- 训练容器只负责训练，评估口径单源在这里维护；
- 存量模型可随时回填（`python backend/scripts/strict_eval_run.py --store`）。

并发与可靠性：计算在线程里跑，落库在事件循环上；任务由模块级 set 持有，
防 asyncio task 被 GC。失败只记日志，不影响训练主流程。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy import text

from backend.services.engine.training.strict_eval import (
    DEFAULT_COST_BPS,
    DEFAULT_HORIZON,
    DEFAULT_TOP_NS,
    DATA_ROOT,
    dump_report,
    evaluate_run,
)
from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)
_TASKS: set[asyncio.Task] = set()

_DDL = """
CREATE TABLE IF NOT EXISTS qm_training_strict_reports (
    run_id TEXT PRIMARY KEY,
    model_id TEXT,
    market TEXT NOT NULL DEFAULT 'CN',
    horizon_days INTEGER NOT NULL DEFAULT 10,
    entry_price TEXT NOT NULL DEFAULT 'open',
    status TEXT NOT NULL DEFAULT 'ready',
    report JSONB NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


async def ensure_table(session) -> None:
    await session.execute(text(_DDL))


def segments_from_config(run_id: str) -> dict[str, tuple[str, str]] | None:
    """从训练 run 的 config.yaml 取 valid/test 窗口；缺配置时返回 None。"""
    cfg_path = DATA_ROOT / "training_jobs" / run_id / "config.yaml"
    if not cfg_path.exists():
        return None
    try:
        import yaml

        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        split = cfg.get("split") or {}
        out: dict[str, tuple[str, str]] = {}
        for key in ("valid", "test"):
            pair = split.get(key)
            if isinstance(pair, list) and len(pair) == 2 and all(pair):
                out[key] = (str(pair[0]).replace("-", ""), str(pair[1]).replace("-", ""))
        return out or None
    except Exception:  # noqa: BLE001 — 配置损坏不阻断，回落到全区间
        return None


def _compute_sync(
    run_id: str,
    *,
    market: str = "CN",
    horizon: int = DEFAULT_HORIZON,
    entry_price: str = "open",
    segments: dict[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    return evaluate_run(
        run_id=run_id,
        market=market,
        horizon=horizon,
        segments=segments or segments_from_config(run_id),
        top_ns=DEFAULT_TOP_NS,
        cost_bps=DEFAULT_COST_BPS,
        entry_price=entry_price,
    )


async def upsert_report(
    *,
    run_id: str,
    model_id: str | None,
    report: dict[str, Any] | None,
    status: str = "ready",
    error: str = "",
) -> None:
    async with get_session() as session:
        await ensure_table(session)
        payload = json.dumps(report or {}, ensure_ascii=False)
        await session.execute(
            text("""
                INSERT INTO qm_training_strict_reports
                    (run_id, model_id, market, horizon_days, entry_price,
                     status, report, error, generated_at, updated_at)
                VALUES (:run_id, :model_id, :market, :horizon, :entry,
                        :status, CAST(:report AS JSONB), :error, NOW(), NOW())
                ON CONFLICT (run_id) DO UPDATE SET
                    model_id = EXCLUDED.model_id,
                    market = EXCLUDED.market,
                    horizon_days = EXCLUDED.horizon_days,
                    entry_price = EXCLUDED.entry_price,
                    status = EXCLUDED.status,
                    report = EXCLUDED.report,
                    error = EXCLUDED.error,
                    updated_at = NOW()
            """),
            {
                "run_id": run_id,
                "model_id": model_id,
                "market": (report or {}).get("market", "CN"),
                "horizon": int((report or {}).get("horizon_days", DEFAULT_HORIZON)),
                "entry": (report or {}).get("entry_price", "open"),
                "status": status,
                "report": payload,
                "error": error,
            },
        )
        await session.commit()


async def _mark_running(run_id: str, model_id: str | None) -> None:
    async with get_session() as session:
        await ensure_table(session)
        await session.execute(
            text("""
                INSERT INTO qm_training_strict_reports (run_id, model_id, status, report)
                VALUES (:run_id, :model_id, 'running', '{}'::jsonb)
                ON CONFLICT (run_id) DO UPDATE SET
                    model_id = COALESCE(EXCLUDED.model_id, qm_training_strict_reports.model_id),
                    status = 'running',
                    error = '',
                    updated_at = NOW()
            """),
            {"run_id": run_id, "model_id": model_id},
        )
        await session.commit()


async def compute_and_store(
    run_id: str,
    model_id: str | None = None,
    *,
    entry_price: str = "open",
    write_standard: bool = True,
) -> dict[str, Any] | None:
    try:
        await _mark_running(run_id, model_id)
        report = await asyncio.to_thread(_compute_sync, run_id, entry_price=entry_price)
    except Exception as exc:  # noqa: BLE001 — 评估失败不得影响训练结果
        logger.warning("[strict-eval] %s failed: %s", run_id, exc)
        await upsert_report(run_id=run_id, model_id=model_id, report=None, status="failed", error=str(exc))
        return None
    await upsert_report(run_id=run_id, model_id=model_id, report=report)
    if write_standard:
        try:
            dump_report(report, DATA_ROOT / "training_jobs" / run_id / "strict_report.json")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[strict-eval] %s dump failed: %s", run_id, exc)
    logger.info(
        "[strict-eval] %s ready: %s",
        run_id,
        {seg: list(payload) for seg, payload in (report.get("results") or {}).items()},
    )
    return report


def schedule_strict_eval(run_id: str, model_id: str | None = None) -> None:
    """训练完成回调里调用：后台计算并落库（不阻塞回调响应）。"""
    task = asyncio.create_task(compute_and_store(run_id, model_id))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def fetch_report(run_id: str) -> dict[str, Any] | None:
    async with get_session() as session:
        await ensure_table(session)
        row = (
            await session.execute(
                text("""
                    SELECT run_id, model_id, market, horizon_days, entry_price,
                           status, report, error, generated_at, updated_at
                    FROM qm_training_strict_reports WHERE run_id = :run_id
                """),
                {"run_id": run_id},
            )
        ).mappings().first()
    if not row:
        return None
    out = dict(row)
    if isinstance(out.get("report"), str):
        out["report"] = json.loads(out["report"])
    return out
