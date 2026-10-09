"""因子池落库契约（P1）——三张表的 DDL + 启动期自愈，与 `db_init.sql` 同口径。

三张表（一张表一个关注点，**没有**跨表共享写入者）
--------------------------------------------------
* ``rd_agent_factor_pool`` —— 每用户每市场每个因子一行池状态：检索打分
  （``pool_score``）、新颖度（``novelty = 1 - max|ρ|`` 对池）、与池最大相关
  （``max_pool_corr`` / ``max_pool_corr_with``）、多样性边际贡献
  （``diversity_contrib``）、疲劳计数（``times_retrieved`` / ``last_retrieved_at``）、
  价值级面板缓存引用（``panel_ref``）。回测完成时 upsert，refresh 时批量重算。
* ``rd_agent_factor_edges`` —— 谱系边。**v1 不解析 RD-Agent 真派生关系**（trace
  不可靠，不做假溯源）：``task_round``（同任务同轮兄弟，骨架）、``similar_to``
  （公式 token Jaccard / 语义 embedding）、``correlated_with``（价值级 |ρ|）。
  唯一键 ``(src, dst, relation, method)`` 让 refresh 重跑 `ON CONFLICT DO UPDATE`
  收敛——没有它每次刷新边行翻倍，谱系图出现平行重复边。
* ``rd_agent_factor_combos`` —— 组合实验室台账（P2）：因子集 + L1 归一权重 +
  train/valid 两窗指标 + 状态。``weights`` 与 ``train/valid_metrics`` 存 JSONB，
  结果仅供研究展示，不自动进生产链路。

``user_id NOT NULL`` 是隔离硬约束
----------------------------------
池查询、检索注入、谱系图全部按 ``user_id`` 过滤（跨用户泄漏 = 把甲的挖掘成果
注进乙的 prompt）。列上加 NOT NULL 让「忘了传 user_id」在写入时就炸，而不是
在查询时静默返回别人的行。

``data/upgrade_*.sql`` 里**不要**再抄一份
------------------------------------------
* 全新安装 → ``db_init.sql``（同一份 DDL，随代码走、每次启动完整重放）；
* 老库自愈 → 本模块 ``ensure_*``，由 engine 服务启动期调用。
两处**必须同口径**，有测试守着（``backend/tests/test_factor_pool_contract.py``，
含「第三份手抄件」反向守卫）。照 ``agent_ledger_contract`` / ``decision_ledger_contract``
先例（`data/` 是挂载的数据目录不是代码）。
"""

from __future__ import annotations

import logging
from collections.abc import Collection

logger = logging.getLogger(__name__)

POOL_TABLE = "rd_agent_factor_pool"
EDGES_TABLE = "rd_agent_factor_edges"
COMBOS_TABLE = "rd_agent_factor_combos"

#: 建表顺序（也是契约测试里的遍历顺序）
TABLES: tuple[str, ...] = (POOL_TABLE, EDGES_TABLE, COMBOS_TABLE)

_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {POOL_TABLE} (
    factor_id           TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    market              TEXT NOT NULL DEFAULT 'a_share',
    universe            TEXT NOT NULL DEFAULT '',
    pool_score          DOUBLE PRECISION,
    novelty             DOUBLE PRECISION,
    max_pool_corr       DOUBLE PRECISION,
    max_pool_corr_with  TEXT,
    diversity_contrib   DOUBLE PRECISION,
    times_retrieved     INTEGER NOT NULL DEFAULT 0,
    last_retrieved_at   TIMESTAMPTZ,
    panel_ref           TEXT,
    extra               JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at         TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS {EDGES_TABLE} (
    edge_id        BIGSERIAL PRIMARY KEY,
    user_id        TEXT NOT NULL,
    src_factor_id  TEXT NOT NULL,
    dst_factor_id  TEXT NOT NULL,
    relation       TEXT NOT NULL,
    method         TEXT NOT NULL,
    weight         DOUBLE PRECISION,
    extra          JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_rd_agent_factor_edges
        UNIQUE (src_factor_id, dst_factor_id, relation, method)
);

CREATE TABLE IF NOT EXISTS {COMBOS_TABLE} (
    combo_id       TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    market         TEXT NOT NULL DEFAULT 'a_share',
    universe       TEXT NOT NULL DEFAULT '',
    name           TEXT NOT NULL DEFAULT '',
    factor_ids     JSONB NOT NULL,
    weights        JSONB NOT NULL,
    train_window   TEXT,
    train_metrics  JSONB,
    valid_metrics  JSONB,
    status         TEXT NOT NULL DEFAULT 'pending',
    error          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rd_factor_pool_scope ON {POOL_TABLE} (user_id, market, universe);
CREATE INDEX IF NOT EXISTS idx_rd_factor_edges_src ON {EDGES_TABLE} (src_factor_id);
CREATE INDEX IF NOT EXISTS idx_rd_factor_edges_dst ON {EDGES_TABLE} (dst_factor_id);
CREATE INDEX IF NOT EXISTS idx_rd_factor_combos_scope ON {COMBOS_TABLE} (user_id, market);
"""

#: 每张表的**首版**列（冻结）。凡是不在这份清单里的列都必须登记进 `_COLUMN_TOPUPS`
#: ——`CREATE TABLE IF NOT EXISTS` 对既有表是空操作，表一旦存在，往建表语句里加一
#: 列老库**永远长不出来**，而本地测试库早就建好了、测试全绿，直到线上第一次写入
#: 报 ``UndefinedColumn``。
_V1_COLUMNS: dict[str, frozenset[str]] = {
    POOL_TABLE: frozenset(
        {
            "factor_id",
            "user_id",
            "market",
            "universe",
            "pool_score",
            "novelty",
            "max_pool_corr",
            "max_pool_corr_with",
            "diversity_contrib",
            "times_retrieved",
            "last_retrieved_at",
            "panel_ref",
            "extra",
            "created_at",
            "updated_at",
        }
    ),
    EDGES_TABLE: frozenset(
        {
            "edge_id",
            "user_id",
            "src_factor_id",
            "dst_factor_id",
            "relation",
            "method",
            "weight",
            "extra",
            "created_at",
        }
    ),
    COMBOS_TABLE: frozenset(
        {
            "combo_id",
            "user_id",
            "market",
            "universe",
            "name",
            "factor_ids",
            "weights",
            "train_window",
            "train_metrics",
            "valid_metrics",
            "status",
            "error",
            "created_at",
            "updated_at",
        }
    ),
}

#: 建表**之后**追加的列（老库补列用）：``(表, 列名, 列 DDL)``。
#: ``archived_at``（P3 清理面）：非 NULL = 用户主动归档——归档不是删除，
#: 因子行/边/面板全保留，只是默认不再进注入摘要、池列表、谱系图与总览
#: 聚合；随时可恢复。判据见 ``mining_plugins/pool_cleanup.py``。
_COLUMN_TOPUPS: tuple[tuple[str, str, str], ...] = (
    (POOL_TABLE, "archived_at", "TIMESTAMPTZ"),
)

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
    """登记了但库里没有的列（决定要不要走 DDL 路径）。"""
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


def ensure_factor_pool_tables() -> bool:
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
        logger.info("[FactorPoolContract] %d 张表已就绪（建表/补列）", len(TABLES))
        return True
    except Exception as exc:  # noqa: BLE001 - 不阻断业务
        logger.warning("[FactorPoolContract] 自愈建表失败（不阻断）: %s", exc)
        return False


async def ensure_factor_pool_tables_async() -> bool:
    """engine 服务启动期自愈（与同步版等价）。"""
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
        logger.info(
            "[FactorPoolContract] %d 张表已就绪（建表/补列，async）", len(TABLES)
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 不阻断启动
        logger.warning("[FactorPoolContract] 自愈建表失败（不阻断）: %s", exc)
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
    "COMBOS_TABLE",
    "EDGES_TABLE",
    "POOL_TABLE",
    "TABLES",
    "ensure_factor_pool_tables",
    "ensure_factor_pool_tables_async",
]
