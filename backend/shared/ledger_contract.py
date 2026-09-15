"""Ledger 契约（T-P1-04）：模拟台账的市场维度列。

背景（2026-09-15 诊断 R4）：Redis 账户键分市场（`simulation:account:{t}:{u}[:MARKET]`），
但 PG 台账（lots/cash_ledger）无市场维度，`load_projection` 只按 account_id 过滤——
Redis 缓存丢失后重建 HK 账户会把 CN 持仓一并灌入。本批为两表补 `market` 列，
写入落市场、读取按市场过滤。

口径：历史行 market 为 NULL，读取一律 `COALESCE(market,'CN')`（等价于导入期回填）；
账户层（simulation_accounts / Redis cash）**暂保持合并视图**——彻底的市场化账户
（account_id 带市场段）属后续步骤，见实施计划 T-P1-04 记录。

迁移安全化同 signal/order 契约：information_schema 预检 → 零 DDL 快路径 →
仅缺列 ALTER（lock_timeout=3s）→ 异常只告警不抛出。
"""

from __future__ import annotations

LEDGER_TABLE_COLUMNS = {
    "simulation_position_lots": (("market", "VARCHAR(16)"),),
    "simulation_cash_ledger": (("market", "VARCHAR(16)"),),
}

_PRECHECK_SQL = (
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_name = :table AND column_name = ANY(:cols)"
)

_ensured = False


def normalize_ledger_market(market: object) -> str:
    """台账市场归一：空视为 CN；大写；与 signal_contract.normalize_market 同口径。"""
    from backend.shared.signal_contract import normalize_market

    return normalize_market(market)


async def _missing_columns_async(session) -> dict[str, list[str]]:
    from sqlalchemy import text as sa_text

    out: dict[str, list[str]] = {}
    for table, cols in LEDGER_TABLE_COLUMNS.items():
        names = [n for n, _ in cols]
        rows = (
            await session.execute(
                sa_text(_PRECHECK_SQL), {"table": table, "cols": names}
            )
        ).fetchall()
        present = {str(r[0]) for r in rows}
        gaps = [n for n in names if n not in present]
        if gaps:
            out[table] = gaps
    return out


def ensure_ledger_contract_columns(conn) -> None:
    """幂等补齐台账契约列（同步；安全化：预检 + lock_timeout + 不抛出）。"""
    global _ensured
    if _ensured:
        return
    import logging

    from sqlalchemy import text as sa_text

    logger = logging.getLogger(__name__)
    try:
        missing: dict[str, list[str]] = {}
        for table, cols in LEDGER_TABLE_COLUMNS.items():
            names = [n for n, _ in cols]
            rows = conn.execute(
                sa_text(_PRECHECK_SQL), {"table": table, "cols": names}
            ).fetchall()
            present = {str(r[0]) for r in rows}
            gaps = [n for n in names if n not in present]
            if gaps:
                missing[table] = gaps
        if not missing:
            _ensured = True
            return
        engine = conn.get_bind()
        with engine.begin() as migration_conn:
            migration_conn.execute(sa_text("SET LOCAL lock_timeout = '3s'"))
            for table, names in missing.items():
                for name, col_type in LEDGER_TABLE_COLUMNS[table]:
                    if name in names:
                        migration_conn.execute(
                            sa_text(
                                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {col_type}"
                            )
                        )
        _ensured = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[LedgerContract] 契约列自愈失败（不阻断业务；缺口列将在写入时报错）: %s", exc
        )


async def ensure_ledger_contract_columns_async() -> None:
    """幂等补齐台账契约列（异步；与同步变体同款安全化）。"""
    global _ensured
    if _ensured:
        return
    import logging

    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import get_session

    logger = logging.getLogger(__name__)
    try:
        async with get_session(read_only=False) as pre_session:
            missing = await _missing_columns_async(pre_session)
        if not missing:
            _ensured = True
            return
        async with get_session(read_only=False) as migration_session:
            await migration_session.execute(sa_text("SET LOCAL lock_timeout = '3s'"))
            for table, names in missing.items():
                for name, col_type in LEDGER_TABLE_COLUMNS[table]:
                    if name in names:
                        await migration_session.execute(
                            sa_text(
                                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {col_type}"
                            )
                        )
            await migration_session.commit()
        _ensured = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[LedgerContract] 契约列自愈失败（不阻断业务；缺口列将在写入时报错）: %s", exc
        )
