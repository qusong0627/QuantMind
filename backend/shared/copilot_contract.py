"""副驾驶建议卡契约（T-P6-16）：表 DDL + 自愈 + 状态机（纯常量，供 API/前端共用）。

建议卡生命周期（状态机单向，决定后不可回改）::

    pending ──accept(执行)──→ executed | partial | failed
       └────reject────────→ rejected

- ``actions``：建议动作列表 ``[{symbol, side(buy|sell), quantity, order_type?, price?}]``；
- ``context_refs``：依据下钻（总线事件 alert_id / 信号 trade_date+symbol / 行情快照键）；
- ``execution``：逐动作执行结果（RouterOutcome：order_id/trade_id/fill_price/message/duplicate）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

TABLE = "copilot_advice"

STATUS_PENDING = "pending"
STATUS_EXECUTED = "executed"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"

_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id           BIGSERIAL PRIMARY KEY,
    advice_id    UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id    VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id      INTEGER NOT NULL,
    source       VARCHAR(32) NOT NULL DEFAULT 'quantbot',
    title        VARCHAR(256) NOT NULL,
    rationale    TEXT NOT NULL DEFAULT '',
    actions      JSONB NOT NULL DEFAULT '[]',
    context_refs JSONB NOT NULL DEFAULT '{{}}',
    status       VARCHAR(16) NOT NULL DEFAULT 'pending',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at   TIMESTAMPTZ,
    decided_by   INTEGER,
    reject_reason VARCHAR(500),
    executed_at  TIMESTAMPTZ,
    execution    JSONB
);
CREATE INDEX IF NOT EXISTS idx_copilot_advice_user ON {TABLE} (tenant_id, user_id, status, created_at);
"""


def ensure_copilot_advice_table() -> bool:
    """幂等建表（存在即零 DDL 快路径；失败仅告警不抛出）。"""
    from sqlalchemy import text

    from backend.shared.sync_db import sync_session

    try:
        with sync_session() as session:
            exists = session.execute(
                text("SELECT to_regclass(:t)"), {"t": f"public.{TABLE}"}
            ).scalar()
        if exists is not None:
            return True
        with sync_session() as session:
            session.execute(text("SET LOCAL lock_timeout = '3s'"))
            for statement in _CREATE_SQL.strip().split(";\n"):
                if statement.strip():
                    session.execute(text(statement))
            session.commit()
        logger.info("[CopilotContract] %s 表已创建", TABLE)
        return True
    except Exception as exc:  # noqa: BLE001 - 不阻断业务
        logger.warning("[CopilotContract] 自愈建表失败（不阻断）: %s", exc)
        return False
