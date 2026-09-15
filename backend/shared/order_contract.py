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
SOURCE_INTERNAL = "internal"
SOURCE_MIRROR = "mirror"
SOURCE_SLTP = "sltp"

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


def _statements() -> list[str]:
    stmts: list[str] = []
    for name, col_type in SIM_ORDER_COLUMNS:
        stmts.append(
            f"ALTER TABLE sim_orders ADD COLUMN IF NOT EXISTS {name} {col_type}"
        )
    for name, col_type in ORDER_COLUMNS:
        stmts.append(
            f"ALTER TABLE orders ADD COLUMN IF NOT EXISTS {name} {col_type}"
        )
    return stmts


def ensure_order_contract_columns(conn) -> None:
    """幂等补齐契约列（同步；独立事务，调用方回滚不影响"已迁移"标记语义）。"""
    global _ensured
    if _ensured:
        return
    from sqlalchemy import text as sa_text

    engine = conn.get_bind()
    with engine.begin() as migration_conn:
        for stmt in _statements():
            migration_conn.execute(sa_text(stmt))
    _ensured = True


async def ensure_order_contract_columns_async() -> None:
    """幂等补齐契约列（异步；独立会话，不混入调用方业务事务）。"""
    global _ensured
    if _ensured:
        return
    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=False) as migration_session:
        for stmt in _statements():
            await migration_session.execute(sa_text(stmt))
        await migration_session.commit()
    _ensured = True
