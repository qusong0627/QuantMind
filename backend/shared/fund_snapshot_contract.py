"""资金快照契约（T-P1-07）：市场维度列 + 唯一键升级。

背景（P0-05 遗留的完整形态）：
- 快照表原为**用户级**粒度（tenant/user/date 唯一），跨市场账户合并写一行——
  非 CN 面板（港股/美股/期货/加密）拿不到自己的资金曲线（前端 `NON_CN_CHARTS_PANELS`
  因此关闭面板，注释点名"方案 B2"）；
- 且新开市场账户**首日**的种子注入被算成 today_pnl（基线是用户级合并口径，
  新市场的 100 万种子落在合并差值里）——曾测试出的 +100 万假收益的第二形态。

本批：
- 加 `market` 列：`'ALL'`=跨市场合并行（历史行的等价口径），其余为单市场行；
- 唯一键升级为 (tenant_id, user_id, snapshot_date, market)；历史行回填 'ALL'。

迁移安全化同 ledger/signal 契约（2026-09-16 自阻塞事故教训）：
information_schema / pg_indexes 预检 → 零 DDL 快路径 → 仅缺列/缺索引才 DDL，
**索引创建与旧唯一约束移除在同一事务内**（原子生效或原状保留，避免"旧约束还在、
新行型已启用"的中间态）→ lock_timeout=3s → 异常只告警不抛出（业务走旧口径兜底）。

读取方在契约未就绪时也必须能工作：用 ``fund_snapshot_has_market_column_async()``
（进程内缓存一次）决定是否加 `market='ALL'` 过滤，未就绪则退回旧查询。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

FUND_SNAPSHOT_TABLE = "simulation_fund_snapshots"
MARKET_COLUMN = "market"
MARKET_DDL = "VARCHAR(16)"
ALL_MARKET = "ALL"
UNIQUE_INDEX_NAME = "uq_sim_fund_snapshot_scope_date_market"
OLD_UNIQUE_COLUMNS = ("tenant_id", "user_id", "snapshot_date")
NEW_UNIQUE_COLUMNS = ("tenant_id", "user_id", "snapshot_date", "market")

_ready = False
_has_market_column: bool | None = None


def normalize_snapshot_market(market: object) -> str:
    """快照市场归一：空视为 ALL（合并行）；大写。单市场维度复用 signal_contract 口径。"""
    text = str(market or "").strip().upper()
    if not text:
        return ALL_MARKET
    if text == ALL_MARKET:
        return ALL_MARKET
    from backend.shared.signal_contract import normalize_market

    return normalize_market(text)


async def fund_snapshot_has_market_column_async() -> bool:
    """market 列是否存在（进程内缓存）：读取方据此决定是否加市场过滤。"""
    global _has_market_column
    if _has_market_column is not None:
        return _has_market_column
    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    sa_text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = :t AND column_name = :c LIMIT 1"
                    ),
                    {"t": FUND_SNAPSHOT_TABLE, "c": MARKET_COLUMN},
                )
            ).fetchone()
        _has_market_column = row is not None
    except Exception as exc:  # noqa: BLE001 - 探测失败按未就绪处理（旧查询兜底）
        logger.warning("[FundSnapshotContract] market 列探测失败: %s", exc)
        return False
    return bool(_has_market_column)


async def _contract_ready_async(session) -> bool:
    """预检：列存在 + 新唯一索引存在 + 旧三列唯一约束已移除。"""
    from sqlalchemy import text as sa_text

    col = await fund_snapshot_has_market_column_async()
    if not col:
        return False
    idx = (
        await session.execute(
            sa_text("SELECT 1 FROM pg_indexes WHERE indexname = :n LIMIT 1"),
            {"n": UNIQUE_INDEX_NAME},
        )
    ).fetchone()
    if idx is None:
        return False
    old = (
        await session.execute(
            sa_text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = CAST(:t AS regclass) AND contype = 'u' "
                "AND pg_get_constraintdef(oid) LIKE '%(tenant_id, user_id, snapshot_date)%' "
                "AND pg_get_constraintdef(oid) NOT LIKE '%market%' LIMIT 1"
            ),
            {"t": FUND_SNAPSHOT_TABLE},
        )
    ).fetchone()
    return old is None


async def ensure_fund_snapshot_contract_async() -> bool:
    """幂等供给侧契约（异步）：就绪返回 True；未就绪/失败返回 False（调用方走旧口径）。"""
    global _ready, _has_market_column
    if _ready:
        return True
    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as pre_session:
            if await _contract_ready_async(pre_session):
                _ready = True
                _has_market_column = True
                return True
        # 缺什么补什么；索引创建与旧约束移除同事务（原子）
        async with get_session(read_only=False) as session:
            await session.execute(sa_text("SET LOCAL lock_timeout = '3s'"))
            await session.execute(
                sa_text(
                    f"ALTER TABLE {FUND_SNAPSHOT_TABLE} "
                    f"ADD COLUMN IF NOT EXISTS {MARKET_COLUMN} {MARKET_DDL} "
                    f"NOT NULL DEFAULT '{ALL_MARKET}'"
                )
            )
            # 防御性回填（列曾以可空形态存在时）
            await session.execute(
                sa_text(
                    f"UPDATE {FUND_SNAPSHOT_TABLE} SET {MARKET_COLUMN} = '{ALL_MARKET}' "
                    f"WHERE {MARKET_COLUMN} IS NULL"
                )
            )
            cols = ", ".join(NEW_UNIQUE_COLUMNS)
            await session.execute(
                sa_text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {UNIQUE_INDEX_NAME} "
                    f"ON {FUND_SNAPSHOT_TABLE} ({cols})"
                )
            )
            old = (
                await session.execute(
                    sa_text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = CAST(:t AS regclass) AND contype = 'u' "
                        "AND pg_get_constraintdef(oid) LIKE "
                        "'%(tenant_id, user_id, snapshot_date)%' "
                        "AND pg_get_constraintdef(oid) NOT LIKE '%market%' LIMIT 1"
                    ),
                    {"t": FUND_SNAPSHOT_TABLE},
                )
            ).fetchone()
            if old is not None:
                name = str(old[0]).replace('"', "")
                if name.replace(".", "").replace("_", "").isalnum():
                    await session.execute(
                        sa_text(
                            f"ALTER TABLE {FUND_SNAPSHOT_TABLE} "
                            f"DROP CONSTRAINT IF EXISTS {name}"
                        )
                    )
            await session.commit()
        _ready = True
        _has_market_column = True
        logger.info("[FundSnapshotContract] 市场维度契约已就绪（列+唯一索引+旧约束移除）")
        return True
    except Exception as exc:  # noqa: BLE001 - 失败不阻断（调用方走旧口径）
        logger.warning(
            "[FundSnapshotContract] 契约自愈失败（不阻断，业务走旧口径）: %s", exc
        )
        return False
