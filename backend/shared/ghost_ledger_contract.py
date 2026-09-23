"""影子代价账落库契约（P1.6）——表结构 + 启动期自愈，与 `db_init.sql` 同口径。

为什么要有这张表
----------------
风控留痕（`qm:risk:decisions:{date}`）是 **Redis Stream**：会 trim、会过期，且无法
跨规则/跨日聚合查询。"这条闸门该不该留"这种问题要的是**多年**的账，不能寄在流里。
本表是影子账的**唯一持久面**：一行 = 一条被拦决策 × 一条规则（幂等键见
`backend/shared/risk/ghost.py:ghost_id`）。

两层做法（照 `holding_alert_contract` / `sentinel_alert_contract`）：
  * 全新安装 → `backend/shared/db_init.sql`（同一份 DDL，带注释头）；
  * 老库自愈 → 本模块的 `ensure_*`，由 trade 服务启动期调用。
两处**必须同口径**，有测试守着（`backend/tests/test_ghost_ledger_contract.py`）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

TABLE = "qm_risk_ghost_ledger"

#: 幂等键长度 = `ghost_id` 的 sha1 前 16 hex（`risk/ghost.py`）
ID_LEN = 32

_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id             VARCHAR({ID_LEN}) PRIMARY KEY,
    tenant_id      VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id        VARCHAR(64) NOT NULL DEFAULT '',
    trade_date     DATE NOT NULL,
    rule_id        VARCHAR(64) NOT NULL,
    kind           VARCHAR(16) NOT NULL,
    registered     BOOLEAN NOT NULL DEFAULT TRUE,
    symbol         VARCHAR(32) NOT NULL,
    side           VARCHAR(8) NOT NULL,
    quantity       DOUBLE PRECISION,
    source         VARCHAR(32) NOT NULL DEFAULT '',
    reason         VARCHAR(200) NOT NULL DEFAULT '',
    evidence       JSONB NOT NULL DEFAULT '{{}}',
    enforced       BOOLEAN NOT NULL DEFAULT FALSE,
    version        INTEGER NOT NULL DEFAULT 0,
    blocked_at     TIMESTAMPTZ NOT NULL,
    entry_date     DATE,
    entry_px       DOUBLE PRECISION,
    tradable       BOOLEAN,
    fwd            JSONB,
    priced_at      TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ghost_ledger_date ON {TABLE} (trade_date, rule_id);
CREATE INDEX IF NOT EXISTS idx_ghost_ledger_rule ON {TABLE} (rule_id, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_ghost_ledger_user ON {TABLE} (tenant_id, user_id, trade_date DESC);
"""


def ensure_ghost_ledger_table() -> bool:
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
        logger.info("[GhostLedgerContract] %s 表已创建", TABLE)
        return True
    except Exception as exc:  # noqa: BLE001 - 不阻断业务
        logger.warning("[GhostLedgerContract] 自愈建表失败（不阻断）: %s", exc)
        return False


async def ensure_ghost_ledger_table_async() -> bool:
    """trade 服务启动期自愈（与 :func:`ensure_ghost_ledger_table` 等价）。"""
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as session:
            exists = (
                await session.execute(
                    _text("SELECT to_regclass(:t)"), {"t": f"public.{TABLE}"}
                )
            ).scalar()
        if exists is not None:
            return True
        async with get_session() as session:
            await session.execute(_text("SET LOCAL lock_timeout = '3s'"))
            for statement in _CREATE_SQL.strip().split(";\n"):
                if statement.strip():
                    await session.execute(_text(statement))
            await session.commit()
        logger.info("[GhostLedgerContract] %s 表已创建（async）", TABLE)
        return True
    except Exception as exc:  # noqa: BLE001 - 不阻断启动
        logger.warning("[GhostLedgerContract] 自愈建表失败（不阻断）: %s", exc)
        return False


__all__ = ["ID_LEN", "TABLE", "ensure_ghost_ledger_table", "ensure_ghost_ledger_table_async"]
