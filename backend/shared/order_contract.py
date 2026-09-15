"""Order/Fill 契约（T-P1-03）：订单台账的契约列 + 客户端幂等键合成。

侦察结论（2026-09-16，按真实缺口收窄）：
- ``sim_orders`` 已有 ``price_source/execution_model``（apply_filled 已在写取价来源），
  ``reason`` 已由 ``remarks`` 承载——**不重复造列**；
- 真实缺口：① ``client_order_id`` 只写 ``simulation_orders`` 投影（注释自述），投影表
  为空时幂等实际断链 → 落到 ``sim_orders``；② ``orders``(REAL) 无 ``price_source``；
  ③ 两表均无 ``source``（rebalance/manual/mirror/sltp 来源分类，供对账与交易台下钻）。

迁移沿用自愈式先例（独立事务，不污染调用方；失败不置标记可重试）。
**不加唯一索引**：投影幂等查全路径未验证前，硬约束会把"重复单"变成 500——待
T-P1-04 台账链路打通后一并启用。
"""

from __future__ import annotations

SIM_ORDER_COLUMNS = (
    ("client_order_id", "VARCHAR(100)"),
    ("source", "VARCHAR(32)"),
)

ORDER_COLUMNS = (
    ("price_source", "VARCHAR(64)"),
    ("source", "VARCHAR(32)"),
)

# source 取值域（Order 契约：来源分类，供过滤/对账/下钻）
SOURCE_REBALANCE = "rebalance"
SOURCE_MANUAL = "manual"
SOURCE_HOSTED = "hosted"  # 托管调度自动单（dispatcher auto- 前缀）
SOURCE_FORCED_LIQUIDATION = "forced_liquidation"  # 融券维持担保比例强平
SOURCE_INTERNAL = "internal"
SOURCE_MIRROR = "mirror"
SOURCE_SLTP = "sltp"
SOURCE_SANDBOX = "sandbox"  # 沙箱策略信号（T-P2-01 收敛入 Router）
SOURCE_TDX_ROLLING = "tdx_rolling"  # 通达信滚动 paper 单（T-P2-01 收敛入 Router）

# Fill 取价来源（REAL 侧：成交回报来自券商）
PRICE_SOURCE_BROKER_FILL = "broker_fill"

# 幂等键长度上限（与 VARCHAR(100) 对齐）
MAX_CLIENT_ORDER_ID_LEN = 100

_ensured = False


def build_sim_client_order_id(run_id: str, symbol: str, side: str) -> str | None:
    """合成引擎直发路径的确定性幂等键（同 run 同标的同方向 → 同键）。

    供托管调仓重跑时观测/未来去重使用；run_id 缺失返回 None（不强造）。
    """
    rid = str(run_id or "").strip()
    if not rid:
        return None
    sym = str(symbol or "").strip()
    sd = str(side or "").strip().lower()
    if not sym or not sd:
        return None
    return f"sim-{rid}-{sym}-{sd}"[:MAX_CLIENT_ORDER_ID_LEN]


_TABLE_COLUMNS = {
    "sim_orders": SIM_ORDER_COLUMNS,
    "orders": ORDER_COLUMNS,
}

_PRECHECK_SQL = (
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_name = :table AND column_name = ANY(:cols)"
)


def _missing_for(table: str, present: set[str]) -> list[tuple[str, str]]:
    return [
        (name, col_type)
        for name, col_type in _TABLE_COLUMNS[table]
        if name not in present
    ]


async def _missing_columns_async(session) -> dict[str, set[str]]:
    from sqlalchemy import text as sa_text

    out: dict[str, set[str]] = {}
    for table, cols in _TABLE_COLUMNS.items():
        names = [n for n, _ in cols]
        rows = (
            await session.execute(
                sa_text(_PRECHECK_SQL), {"table": table, "cols": names}
            )
        ).fetchall()
        present = {str(r[0]) for r in rows}
        out[table] = {n for n in names if n not in present}
    return out


def ensure_order_contract_columns(conn) -> None:
    """幂等补齐契约列（同步；安全化：先查 existence，只对缺列 DDL + lock_timeout）。

    同日事故教训（见 signal_contract 注释）：热表无条件 ADD COLUMN IF NOT EXISTS
    仍申请 AccessExclusive，会与调用方未提交事务自阻塞并堵死全表；故先走
    information_schema 预检，列齐全零 DDL；缺列才 ALTER 且 3s 超时快速失败；
    异常只告警不抛出。
    """
    global _ensured
    if _ensured:
        return
    import logging

    from sqlalchemy import text as sa_text

    logger = logging.getLogger(__name__)
    try:
        missing: dict[str, list[tuple[str, str]]] = {}
        for table, cols in _TABLE_COLUMNS.items():
            names = [n for n, _ in cols]
            rows = conn.execute(
                sa_text(_PRECHECK_SQL), {"table": table, "cols": names}
            ).fetchall()
            present = {str(r[0]) for r in rows}
            gaps = _missing_for(table, present)
            if gaps:
                missing[table] = gaps
        if not missing:
            _ensured = True
            return
        engine = conn.get_bind()
        with engine.begin() as migration_conn:
            migration_conn.execute(sa_text("SET LOCAL lock_timeout = '3s'"))
            for table, gaps in missing.items():
                for name, col_type in gaps:
                    migration_conn.execute(
                        sa_text(
                            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {col_type}"
                        )
                    )
        _ensured = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[OrderContract] 契约列自愈失败（不阻断业务；缺口列将在写入时报错）: %s", exc
        )


async def ensure_order_contract_columns_async() -> None:
    """幂等补齐契约列（异步；与同步变体同款安全化）。"""
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
        gaps = {t: cols for t, cols in missing.items() if cols}
        if not gaps:
            _ensured = True
            return
        async with get_session(read_only=False) as migration_session:
            await migration_session.execute(sa_text("SET LOCAL lock_timeout = '3s'"))
            for table, names in gaps.items():
                for name, col_type in _missing_for(table, set()):
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
            "[OrderContract] 契约列自愈失败（不阻断业务；缺口列将在写入时报错）: %s", exc
        )
