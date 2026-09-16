"""eval_scores 契约（T-P4-05b）：评分结果唯一落表——自愈迁移安全三纪律。

表结构（设计《评估与打分体系》§四）：对象类型/对象ID/日期/维度明细 JSONB/总分/评级/
置信/输入版本；UNIQUE(object_type, object_id, snapshot_date, tenant_id, user_id) 保幂等。

迁移纪律（同 signal/order/ledger 契约）：information_schema/to_regclass 预检 → 零 DDL
快路径；仅缺表才 CREATE（lock_timeout=3s）；异常不阻断业务。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_EVAL_SCORES_COLUMNS: dict[str, str] = {
    "object_type": "VARCHAR(32)",
    "object_id": "VARCHAR(128)",
    "snapshot_date": "DATE",
    "tenant_id": "VARCHAR(64)",
    "user_id": "VARCHAR(64)",
    "score": "DOUBLE PRECISION",
    "grade": "VARCHAR(8)",
    "low_confidence": "BOOLEAN",
    "red_line_failed": "JSONB",
    "dimensions": "JSONB",
    "inputs_version": "JSONB",
    "created_at": "TIMESTAMPTZ",
}

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS eval_scores (
    id SERIAL PRIMARY KEY,
    object_type VARCHAR(32) NOT NULL,
    object_id VARCHAR(128) NOT NULL,
    snapshot_date DATE NOT NULL,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id VARCHAR(64) NOT NULL DEFAULT '',
    score DOUBLE PRECISION,
    grade VARCHAR(8),
    low_confidence BOOLEAN NOT NULL DEFAULT FALSE,
    red_line_failed JSONB NOT NULL DEFAULT '[]'::jsonb,
    dimensions JSONB NOT NULL DEFAULT '{}'::jsonb,
    inputs_version JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (object_type, object_id, snapshot_date, tenant_id, user_id)
)
"""


async def ensure_eval_scores_table_async() -> bool:
    """自愈创建 eval_scores（存在即零 DDL 快路径；失败仅告警不抛出）。"""
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as session:
            exists = (
                await session.execute(_text("SELECT to_regclass('public.eval_scores')"))
            ).scalar()
        if exists is not None:
            return True
        async with get_session() as session:
            await session.execute(_text("SET LOCAL lock_timeout = '3s'"))
            await session.execute(_text(_CREATE_SQL))
            await session.commit()
        logger.info("[EvalContract] eval_scores 表已创建")
        return True
    except Exception as exc:  # noqa: BLE001 - 不阻断业务
        logger.warning("[EvalContract] eval_scores 自愈失败（不阻断）: %s", exc)
        return False


async def save_eval_score(
    *,
    object_type: str,
    object_id: str,
    snapshot_date: str,
    score: float | None,
    grade: str | None,
    low_confidence: bool,
    red_line_failed: list[str],
    dimensions: dict,
    inputs_version: dict,
    tenant_id: str = "default",
    user_id: str = "",
) -> bool:
    """幂等落表（UNIQUE 冲突 → 更新），失败仅告警。"""
    import json

    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    if not await ensure_eval_scores_table_async():
        return False
    from datetime import date as _date_cls

    try:
        bound_date = (
            snapshot_date
            if isinstance(snapshot_date, _date_cls)
            else _date_cls.fromisoformat(str(snapshot_date))
        )
    except ValueError:
        logger.warning("[EvalContract] snapshot_date 非法: %r", snapshot_date)
        return False
    try:
        async with get_session() as session:
            await session.execute(
                _text(
                    "INSERT INTO eval_scores (object_type, object_id, snapshot_date, "
                    "tenant_id, user_id, score, grade, low_confidence, red_line_failed, "
                    "dimensions, inputs_version) VALUES "
                    "(:ot, :oid, :d, :tid, :uid, :score, :grade, :lc, "
                    "CAST(:rlf AS jsonb), CAST(:dims AS jsonb), CAST(:ver AS jsonb)) "
                    "ON CONFLICT (object_type, object_id, snapshot_date, tenant_id, user_id) "
                    "DO UPDATE SET score=EXCLUDED.score, grade=EXCLUDED.grade, "
                    "low_confidence=EXCLUDED.low_confidence, red_line_failed=EXCLUDED.red_line_failed, "
                    "dimensions=EXCLUDED.dimensions, inputs_version=EXCLUDED.inputs_version, "
                    "created_at=now()"
                ),
                {
                    "ot": object_type,
                    "oid": object_id,
                    "d": bound_date,
                    "tid": tenant_id,
                    "uid": user_id,
                    "score": score,
                    "grade": grade,
                    "lc": bool(low_confidence),
                    "rlf": json.dumps(red_line_failed, ensure_ascii=False),
                    "dims": json.dumps(dimensions, ensure_ascii=False, default=str),
                    "ver": json.dumps(inputs_version, ensure_ascii=False, default=str),
                },
            )
            await session.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - 不阻断
        logger.warning("[EvalContract] eval_scores 写入失败（不阻断）: %s", exc)
        return False
