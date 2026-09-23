"""分账账本落库契约（P2.7）——四张表的 DDL + 启动期自愈，与 `db_init.sql` 同口径。

为什么要有这一层
----------------
纯核心（``backend/shared/decision/agent_ledger.py``）只收 dict、只返回新 dict，
不管落盘。可多模型竞争的语义有**一半是持久化语义**：账本要跨进程、跨重启、跨
「进程以为失败但其实提交成功」这类重投，还得能被对账 SQL 直接查。隔壁用单文件
``logs/live_ledger.json`` 扛这些（原子写 + ``applied_fills`` 幂等标记），本仓换成
**唯一索引**——文件系统的原子写在容器/多进程下没有可依赖的语义，而唯一索引有。

四张表（一张表一个写入者，**没有**跨表共享写入）
------------------------------------------------
* ``qm_agent_ledger_account`` —— 每个 ``(租户, 账户, agent)`` 一行，只有
  ``virtual_cash`` 一个业务列。**quota 不存**：它是绑定层的参数，存进库就有了
  第二个事实源，改配额时库里那个 ¥10 万会静默压过配置（见纯核心 ``ensure_agent``）。
* ``qm_agent_ledger_position`` —— 该 agent **名下**的持仓（``mine`` 的唯一来源）。
  `mine_of` 读的就是这张表：空表 = 名下无仓，**不是**「没配分账就全给我」。
* ``qm_agent_ledger_fill`` —— 成交记账流水（幂等键在这张表上）。
* ``qm_agent_ledger_roundtrip`` —— 回合台账（影子账户与行为归因的底座）。

`code` 一律存**后缀式**（``600036.SH``），不是本仓 PG 的惯例前缀式
-----------------------------------------------------------------
读侧 ``decision/context.ledger_cost_rows`` 按 ``HoldingRow.code`` 取键，而
``positions_to_holding_rows`` 给的是后缀式（``decision_context_source.py:320``）。
存前缀式的话 ``mine_of`` 永远匹配不上——互卖防线与成本列会**同时静默失效**，
账本看着一切正常。故 store 层在写入边界上统一 ``StockCodeUtil.to_suffix``。

幂等键是 **(租户, 账户, 交易日, fill_key)**，不是全库唯一
---------------------------------------------------------
A 股 ``exchange_trade_id`` 是**每日重排**的成交编号（实测 dev 库
``trades.exchange_trade_id`` 形如 ``00161170``）。全库唯一索引的后果不是报错，
而是 ``ON CONFLICT DO NOTHING`` **静默吞掉次日的同号成交**——账本少一只票、
``mine_of`` 看不见它、该卖的时候卖不掉。方向上与 2026-09-08 那次跨 agent 卖仓
同族（都是「账本看不见真实持仓」），故唯一键必须带 ``trade_date``。

``trade_date`` 由**调用方**给（不在这里取 ``now()``）：重投事件要算出同一个日期，
取处理时刻的话跨零点的重投会算出第二天、幂等键随之失效。见 store 层
``apply_fill`` 的 ``trade_date`` 说明。

DDL 的两份手抄件（**没有** `data/upgrade_v1.*.sql`）
----------------------------------------------------
* 全新安装 → ``backend/shared/db_init.sql``（同一份 DDL，带注释头）；
* 老库自愈 → 本模块的 ``ensure_*``，由 trade 服务启动期调用。

两处**必须同口径**，有测试守着（``backend/tests/test_agent_ledger_contract.py``）。
不写进 ``data/upgrade_*.sql`` 的原因是**送达路径的可靠性**（三条都是实测，逐字
沿用 ``decision_ledger_contract`` 的结论）：``db_init.sql`` 随代码走且每次启动
被完整重放；``data/upgrade_*.sql`` 靠目录探测发现，而 ``data/`` 是挂载的数据目录
不是代码，且本工作区的 ``data/`` 是指向仓库外的符号链接（git 已把 11 个受跟踪的
upgrade 脚本报成「已删除」）。同批的 P1.6 影子账与本仓各 ``*_contract`` 表走的
都是本路线。
"""

from __future__ import annotations

import logging
from collections.abc import Collection

logger = logging.getLogger(__name__)

ACCOUNT_TABLE = "qm_agent_ledger_account"
POSITION_TABLE = "qm_agent_ledger_position"
FILL_TABLE = "qm_agent_ledger_fill"
ROUNDTRIP_TABLE = "qm_agent_ledger_roundtrip"

#: 建表顺序（也是契约测试里的遍历顺序）
TABLES: tuple[str, ...] = (
    ACCOUNT_TABLE,
    POSITION_TABLE,
    FILL_TABLE,
    ROUNDTRIP_TABLE,
)

#: ``fill_key`` 的列宽。键是 ``exchange_trade_id`` 或 ``broker_order_id:exec_id``
#: 的拼接；券商侧订单号与成交号都在 40 字符以内，128 留了一倍余量（键是**拼接**
#: 出来的，写死 64 而后人换了更长的段就是一次插入报错）。
FILL_KEY_LEN = 128

#: ``agent`` 列宽与 ``qm_decision_ledger.agent`` 同口径（两表要能直接 JOIN 比）。
AGENT_COL_LEN = 64

_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {ACCOUNT_TABLE} (
    tenant_id      VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id        VARCHAR(64) NOT NULL,
    agent          VARCHAR({AGENT_COL_LEN}) NOT NULL,
    virtual_cash   DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id, agent)
);

CREATE TABLE IF NOT EXISTS {POSITION_TABLE} (
    tenant_id      VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id        VARCHAR(64) NOT NULL,
    agent          VARCHAR({AGENT_COL_LEN}) NOT NULL,
    code           VARCHAR(32) NOT NULL,
    volume         DOUBLE PRECISION NOT NULL,
    cost_price     DOUBLE PRECISION NOT NULL,
    buy_ts         TIMESTAMPTZ,
    last_ts        TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id, agent, code)
);

CREATE TABLE IF NOT EXISTS {FILL_TABLE} (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id        VARCHAR(64) NOT NULL,
    agent          VARCHAR({AGENT_COL_LEN}) NOT NULL,
    fill_key       VARCHAR({FILL_KEY_LEN}) NOT NULL,
    order_id       VARCHAR(64) NOT NULL DEFAULT '',
    trade_date     DATE NOT NULL,
    code           VARCHAR(32) NOT NULL,
    side           VARCHAR(16) NOT NULL,
    volume         DOUBLE PRECISION NOT NULL,
    price          DOUBLE PRECISION NOT NULL,
    applied_volume DOUBLE PRECISION NOT NULL,
    approx_price   BOOLEAN NOT NULL DEFAULT FALSE,
    note           TEXT NOT NULL DEFAULT '',
    filled_at      TIMESTAMPTZ NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_agent_ledger_fill_key
        UNIQUE (tenant_id, user_id, trade_date, fill_key)
);

CREATE TABLE IF NOT EXISTS {ROUNDTRIP_TABLE} (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id        VARCHAR(64) NOT NULL,
    agent          VARCHAR({AGENT_COL_LEN}) NOT NULL,
    market         VARCHAR(16) NOT NULL DEFAULT 'CN',
    code           VARCHAR(32) NOT NULL,
    volume         DOUBLE PRECISION NOT NULL,
    cost_price     DOUBLE PRECISION NOT NULL,
    sell_price     DOUBLE PRECISION NOT NULL,
    realized_pnl   DOUBLE PRECISION NOT NULL,
    pnl_pct        DOUBLE PRECISION,
    buy_ts         TIMESTAMPTZ,
    sell_ts        TIMESTAMPTZ NOT NULL,
    holding_days   DOUBLE PRECISION,
    closed         BOOLEAN NOT NULL DEFAULT FALSE,
    exit_reason    VARCHAR(32) NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_ledger_pos_agent ON {POSITION_TABLE} (tenant_id, user_id, agent);
CREATE INDEX IF NOT EXISTS idx_agent_ledger_fill_day ON {FILL_TABLE} (tenant_id, user_id, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_agent_ledger_fill_order ON {FILL_TABLE} (order_id);
CREATE INDEX IF NOT EXISTS idx_agent_ledger_rt_agent ON {ROUNDTRIP_TABLE} (tenant_id, user_id, agent, sell_ts DESC);
"""

#: 每张表的**首版**列（冻结）。凡是**不在这份清单里**的列都必须登记进 `_COLUMN_TOPUPS`
#: ——`CREATE TABLE IF NOT EXISTS` 对既有表是空操作，表一旦存在，往建表语句里加一列
#: 老库**永远长不出来**，而本地测试库早就建好了，测试全绿，直到线上第一次写入报
#: ``UndefinedColumn``。
_V1_COLUMNS: dict[str, frozenset[str]] = {
    ACCOUNT_TABLE: frozenset(
        {"tenant_id", "user_id", "agent", "virtual_cash", "created_at", "updated_at"}
    ),
    POSITION_TABLE: frozenset(
        {
            "tenant_id",
            "user_id",
            "agent",
            "code",
            "volume",
            "cost_price",
            "buy_ts",
            "last_ts",
            "updated_at",
        }
    ),
    FILL_TABLE: frozenset(
        {
            "id",
            "tenant_id",
            "user_id",
            "agent",
            "fill_key",
            "order_id",
            "trade_date",
            "code",
            "side",
            "volume",
            "price",
            "applied_volume",
            "approx_price",
            "note",
            "filled_at",
            "created_at",
        }
    ),
    ROUNDTRIP_TABLE: frozenset(
        {
            "id",
            "tenant_id",
            "user_id",
            "agent",
            "market",
            "code",
            "volume",
            "cost_price",
            "sell_price",
            "realized_pnl",
            "pnl_pct",
            "buy_ts",
            "sell_ts",
            "holding_days",
            "closed",
            "exit_reason",
            "created_at",
        }
    ),
}

#: 建表**之后**追加的列（老库补列用）：``(表, 列名, 列 DDL)``。
#:
#: 表一旦热起来（有成交在写），加列要先按 ``signal_contract`` 那套（锁窗口 + 调用方
#: 事务纪律）评估，不能无条件 ADD。当前为空——四张表都是本批首建。
_COLUMN_TOPUPS: tuple[tuple[str, str, str], ...] = ()

_COLUMNS_SQL = (
    "SELECT table_name, column_name FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name IN :t"
)


def _ddl_statements() -> list[str]:
    """自愈要发的全部语句（建表 + 补列），**同步/异步两条路径共用一份**。

    每条 ``CREATE`` 单独发：整批一个事务时，一条失败的 ``CREATE INDEX`` 会让
    刚建好的表跟着回滚——分开发才能「能建的先建」。
    """
    out = ["SET LOCAL lock_timeout = '3s'"]
    out += [s for s in _CREATE_SQL.strip().split(";\n") if s.strip()]
    out += [
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl}"
        for table, name, ddl in _COLUMN_TOPUPS
    ]
    return out


def _missing_topups(current: dict[str, set[str]]) -> tuple[str, ...]:
    """登记了但库里没有的列（决定要不要走 DDL 路径）。

    表**存在**不等于**列全**——这正是 ``CREATE TABLE IF NOT EXISTS`` 的盲区，
    也是本函数存在的理由：只查 ``to_regclass`` 的快路径会把「老库缺列」判成
    「一切就绪」。
    """
    out: list[str] = []
    for table, name, _ddl in _COLUMN_TOPUPS:
        if name not in current.get(table, set()):
            out.append(f"{table}.{name}")
    return tuple(out)


def _missing_tables(current: dict[str, set[str]]) -> tuple[str, ...]:
    return tuple(t for t in TABLES if not current.get(t))


def _apply_ddl(execute) -> None:
    """执行建表 + 补列。``execute`` 是同步/异步两条路径共用的语句执行器。"""
    for statement in _ddl_statements():
        execute(statement)


def ensure_agent_ledger_tables() -> bool:
    """幂等建表（表在且列全即零 DDL 快路径；失败仅告警不抛出）。"""
    from sqlalchemy import bindparam, text

    from backend.shared.sync_db import sync_session

    try:
        with sync_session() as session:
            current = _read_columns_sync(session, text, bindparam)
        if not _missing_tables(current) and not _missing_topups(current):
            return True
        with sync_session() as session:
            _apply_ddl(lambda s: session.execute(text(s)))
            session.commit()
        logger.info("[AgentLedgerContract] %d 张表已就绪（建表/补列）", len(TABLES))
        return True
    except Exception as exc:  # noqa: BLE001 - 不阻断业务
        logger.warning("[AgentLedgerContract] 自愈建表失败（不阻断）: %s", exc)
        return False


async def ensure_agent_ledger_tables_async() -> bool:
    """trade 服务启动期自愈（与同步版等价）。"""
    from sqlalchemy import bindparam, text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as session:
            current = await _read_columns_async(session, text, bindparam)
        if not _missing_tables(current) and not _missing_topups(current):
            return True
        async with get_session() as session:
            for statement in _ddl_statements():
                await session.execute(text(statement))
            await session.commit()
        logger.info("[AgentLedgerContract] %d 张表已就绪（建表/补列，async）", len(TABLES))
        return True
    except Exception as exc:  # noqa: BLE001 - 不阻断启动
        logger.warning("[AgentLedgerContract] 自愈建表失败（不阻断）: %s", exc)
        return False


def _read_columns_sync(session, text, bindparam) -> dict[str, set[str]]:
    rows = session.execute(
        text(_COLUMNS_SQL).bindparams(bindparam("t", expanding=True)),
        {"t": list(TABLES)},
    ).all()
    return _group_columns(rows)


async def _read_columns_async(session, text, bindparam) -> dict[str, set[str]]:
    rows = (
        await session.execute(
            text(_COLUMNS_SQL).bindparams(bindparam("t", expanding=True)),
            {"t": list(TABLES)},
        )
    ).all()
    return _group_columns(rows)


def _group_columns(rows: Collection) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for row in rows:
        out.setdefault(str(row[0]), set()).add(str(row[1]))
    return out


__all__ = [
    "ACCOUNT_TABLE",
    "AGENT_COL_LEN",
    "FILL_KEY_LEN",
    "FILL_TABLE",
    "POSITION_TABLE",
    "ROUNDTRIP_TABLE",
    "TABLES",
    "ensure_agent_ledger_tables",
    "ensure_agent_ledger_tables_async",
]
