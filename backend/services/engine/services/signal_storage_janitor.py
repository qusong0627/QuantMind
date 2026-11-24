"""推理落库存储治理：按保留窗口清理信号明细与配套留痕。

为什么需要独立 janitor（而不只是 script_runner 里的 Step 0.1）：
Step 0.1 的清理只在「该用户还有模型在跑」时顺带执行。客户侧典型场景恰恰是
跑过一批模型后再也不跑（试训、换默认模型），旧 `feature_version` 的整块
`engine_signal_scores` / `qm_research_candidate_snapshot` 就此永久留存；
`qm_model_inference_runs` 与 `qm_model_inference_dispatch_logs` 同样只增不减
（Beat 每 30 分钟一轮，每天给每个模型写一行 UP_TO_DATE 留痕）。

口径与 Step 0.1 保持一致：按 tenant_id + user_id + 信号生效日（自然日窗口），
不与 model_version / feature_version 相关。全部 DELETE 幂等，可反复执行。
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# 信号明细/候选快照保留窗口（自然日），与 script_runner 同一 env，避免两套口径
DEFAULT_RETENTION_DAYS = int(os.getenv("INFERENCE_PREDICTION_RETENTION_DAYS", "365"))
# 调度留痕保留窗口（管理台只需回溯最近几次轮询）
DEFAULT_DISPATCH_LOG_DAYS = int(
    os.getenv("INFERENCE_DISPATCH_LOG_RETENTION_DAYS", "30")
)
# 推理历史（qm_model_inference_runs）保留窗口：明细已按此窗口删除，
# 超窗且明细不存在的 run 记录只会指向空批次，一并清理。
DEFAULT_RUN_HISTORY_DAYS = int(os.getenv("INFERENCE_RUN_HISTORY_RETENTION_DAYS", "365"))


def _sync_session() -> tuple[Any, Any]:
    from sqlalchemy import create_engine as sa_create_engine
    from sqlalchemy.orm import sessionmaker as sa_sessionmaker

    sync_db_url = str(os.getenv("DATABASE_URL", "")).strip()
    if "+asyncpg" in sync_db_url:
        sync_db_url = sync_db_url.replace("+asyncpg", "+psycopg2")
    if not sync_db_url or "postgresql" not in sync_db_url:
        return None, None
    engine = sa_create_engine(sync_db_url, pool_pre_ping=True)
    return sa_sessionmaker(bind=engine)(), engine


def _existing_tables(db: Any, names: list[str]) -> list[str]:
    """老部署可能还没有某些表（dispatch 留痕是按需建表的），先探测再拼 SQL。"""
    from sqlalchemy import text as sa_text

    rows = db.execute(
        sa_text(
            """
            SELECT t
            FROM unnest(CAST(:names AS text[])) AS u(t)
            WHERE to_regclass(t) IS NOT NULL
            """
        ),
        {"names": names},
    ).fetchall()
    return [str(r[0]) for r in rows]


def _user_pairs(db: Any) -> list[tuple[str, str]]:
    """所有需要治理的 (tenant_id, user_id)：相关表并集，不依赖模型表仍在线。"""
    from sqlalchemy import text as sa_text

    candidates = [
        "engine_signal_scores",
        "qm_research_candidate_snapshot",
        "qm_model_inference_runs",
        "qm_model_inference_dispatch_logs",
    ]
    present = _existing_tables(db, candidates)
    if not present:
        return []
    sql = " UNION ".join(f"SELECT tenant_id, user_id FROM {t}" for t in present)
    rows = db.execute(sa_text(sql)).fetchall()
    return [(str(r[0] or "default"), str(r[1] or "")) for r in rows]


def purge_signal_storage(
    *,
    retention_days: int | None = None,
    dispatch_log_days: int | None = None,
    run_history_days: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """按保留窗口清理推理落库；默认 dry_run，只统计不动数据。

    返回每一项的预计/实际影响行数，供脚本输出与 Celery 留痕。
    """
    from datetime import date, timedelta

    from sqlalchemy import text as sa_text

    days = int(retention_days if retention_days is not None else DEFAULT_RETENTION_DAYS)
    log_days = int(
        dispatch_log_days
        if dispatch_log_days is not None
        else DEFAULT_DISPATCH_LOG_DAYS
    )
    run_days = int(
        run_history_days if run_history_days is not None else DEFAULT_RUN_HISTORY_DAYS
    )
    if days < 1 or log_days < 1 or run_days < 1:
        raise ValueError(
            "retention_days / dispatch_log_days / run_history_days 必须 >= 1"
        )

    signal_floor = (date.today() - timedelta(days=days)).isoformat()
    run_floor = (date.today() - timedelta(days=run_days)).isoformat()
    log_floor = (date.today() - timedelta(days=log_days)).isoformat()

    db, engine = _sync_session()
    if db is None:
        return {
            "status": "skipped",
            "reason": "DATABASE_URL 不可用（非 PostgreSQL 或未配置）",
            "dry_run": dry_run,
        }
    stats: dict[str, int] = {}
    try:
        pairs = _user_pairs(db)
        if tenant_id or user_id:
            pairs = [
                (t, u)
                for (t, u) in pairs
                if (not tenant_id or t == tenant_id) and (not user_id or u == user_id)
            ]

        checks = [
            (
                "engine_signal_scores_expired",
                """
                SELECT count(*) FROM engine_signal_scores
                WHERE tenant_id = :tenant_id AND user_id = :user_id
                  AND trade_date < :floor
                """,
                {"floor": signal_floor},
            ),
            (
                # 明细已不在窗口内、且同日已无任何 run 存活的孤儿快照
                "candidate_snapshot_expired",
                """
                SELECT count(*) FROM qm_research_candidate_snapshot
                WHERE tenant_id = :tenant_id AND user_id = :user_id
                  AND prediction_trade_date < :floor
                """,
                {"floor": signal_floor},
            ),
            (
                # run 记录早于窗口，且其信号明细已不存在 → 指向空批次的幽灵行
                "inference_runs_without_scores",
                """
                SELECT count(*) FROM qm_model_inference_runs r
                WHERE r.tenant_id = :tenant_id AND r.user_id = :user_id
                  AND r.prediction_trade_date < :floor
                  AND NOT EXISTS (
                      SELECT 1 FROM engine_signal_scores e WHERE e.run_id = r.run_id
                  )
                """,
                {"floor": run_floor},
            ),
            (
                "dispatch_logs_expired",
                """
                SELECT count(*) FROM qm_model_inference_dispatch_logs
                WHERE tenant_id = :tenant_id AND user_id = :user_id
                  AND created_at < CAST(:floor AS DATE)
                """,
                {"floor": log_floor},
            ),
        ]

        deletes = [
            (
                "engine_signal_scores_expired",
                """
                DELETE FROM engine_signal_scores
                WHERE tenant_id = :tenant_id AND user_id = :user_id
                  AND trade_date < :floor
                """,
                {"floor": signal_floor},
            ),
            (
                "candidate_snapshot_expired",
                """
                DELETE FROM qm_research_candidate_snapshot
                WHERE tenant_id = :tenant_id AND user_id = :user_id
                  AND prediction_trade_date < :floor
                """,
                {"floor": signal_floor},
            ),
            (
                "inference_runs_without_scores",
                """
                DELETE FROM qm_model_inference_runs r
                WHERE r.tenant_id = :tenant_id AND r.user_id = :user_id
                  AND r.prediction_trade_date < :floor
                  AND NOT EXISTS (
                      SELECT 1 FROM engine_signal_scores e WHERE e.run_id = r.run_id
                  )
                """,
                {"floor": run_floor},
            ),
            (
                "dispatch_logs_expired",
                """
                DELETE FROM qm_model_inference_dispatch_logs
                WHERE tenant_id = :tenant_id AND user_id = :user_id
                  AND created_at < CAST(:floor AS DATE)
                """,
                {"floor": log_floor},
            ),
        ]

        for key, sql, extra in checks:
            total = 0
            for t, u in pairs:
                params = {"tenant_id": t, "user_id": u, **extra}
                total += int(db.execute(sa_text(sql), params).scalar() or 0)
            stats[key] = total

        if not dry_run:
            for key, sql, extra in deletes:
                affected = 0
                for t, u in pairs:
                    params = {"tenant_id": t, "user_id": u, **extra}
                    res = db.execute(sa_text(sql), params)
                    affected += int(res.rowcount or 0)
                stats[key + "_deleted"] = affected
            db.commit()
        else:
            db.rollback()

        logger.info(
            "[SignalStorageJanitor] dry_run=%s signal_floor=%s stats=%s",
            dry_run,
            signal_floor,
            stats,
        )
        return {
            "status": "dry_run" if dry_run else "purged",
            "dry_run": dry_run,
            "users": len(pairs),
            "signal_retention_days": days,
            "run_history_retention_days": run_days,
            "dispatch_log_retention_days": log_days,
            "signal_floor": signal_floor,
            "stats": stats,
            # DELETE 不回收磁盘，需要空间还给 OS 时另行 pg_repack / VACUUM FULL
            "note": "如需把空间归还操作系统，请在低峰执行 pg_repack 或 VACUUM FULL",
        }
    finally:
        db.close()
        engine.dispose()
