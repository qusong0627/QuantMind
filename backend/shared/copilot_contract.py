"""副驾驶建议卡契约（T-P6-16）：表 DDL + 自愈 + 状态机（纯常量，供 API/前端共用）。

建议卡生命周期（状态机单向，决定后不可回改）::

    pending ──accept(执行)──→ executed | partial | failed
       └────reject────────→ rejected

- ``actions``：建议动作列表 ``[{symbol, side(buy|sell), quantity, order_type?, price?}]``；
- ``context_refs``：依据下钻（总线事件 alert_id / 信号 trade_date+symbol / 行情快照键）；
- ``execution``：逐动作执行结果（RouterOutcome：order_id/trade_id/fill_price/message/duplicate）；
- ``outcome``：兑现结果（T+1/T+3/T+5 决策日收盘→第 h 交易日收盘，超额=个股-沪深300；
  买入取实际收益、卖出取规避收益；见 ``services/trade/services/advice_backfill.py``），
  ``outcome_status``：pending → partial → done（终态还有 not_scorable/no_data）。
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

# 兑现回填状态（outcome_status）
OUTCOME_PENDING = "pending"
OUTCOME_PARTIAL = "partial"
OUTCOME_DONE = "done"
OUTCOME_NOT_SCORABLE = "not_scorable"
OUTCOME_NO_DATA = "no_data"

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
    execution    JSONB,
    outcome      JSONB,
    outcome_status VARCHAR(16) NOT NULL DEFAULT 'pending',
    outcome_checked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_copilot_advice_user ON {TABLE} (tenant_id, user_id, status, created_at);
"""

# 存量表补列（老库存在即快路径；进程内一次性，失败下轮重试——「契约自愈挂读写入口」纪律）
_COLUMN_DDL = (
    f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS outcome JSONB",
    f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS outcome_status VARCHAR(16) NOT NULL DEFAULT 'pending'",
    f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS outcome_checked_at TIMESTAMPTZ",
)
_columns_ensured = False


def _ensure_outcome_columns() -> None:
    global _columns_ensured
    if _columns_ensured:
        return
    from sqlalchemy import text

    from backend.shared.sync_db import sync_session

    with sync_session() as session:
        session.execute(text("SET LOCAL lock_timeout = '3s'"))
        for statement in _COLUMN_DDL:
            session.execute(text(statement))
        session.commit()
    _columns_ensured = True


def ensure_copilot_advice_table() -> bool:
    """幂等建表（存在即零 DDL 快路径 + 补列自愈；失败仅告警不抛出）。"""
    from sqlalchemy import text

    from backend.shared.sync_db import sync_session

    try:
        with sync_session() as session:
            exists = session.execute(
                text("SELECT to_regclass(:t)"), {"t": f"public.{TABLE}"}
            ).scalar()
        if exists is not None:
            try:
                _ensure_outcome_columns()
            except Exception as exc:  # noqa: BLE001 - 补列失败不阻断（读老列仍可用）
                logger.warning("[CopilotContract] outcome 补列失败（下轮重试）: %s", exc)
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
