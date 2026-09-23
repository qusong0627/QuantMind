"""决策审计表落库契约（P2.1d）——表结构 + 启动期自愈，与 `db_init.sql` 同口径。

为什么要有这张表
----------------
决策留痕在隔壁有两处，都不够当**账**：

* 隔壁 `logs/decision_pool.jsonl` 只留 **每天第一条**（`make_id(agent|日|code|动作)`
  去重）——同一天 10:00 与 14:00 对同一只票的两次决策，只有第一条留得下来，
  可「模型盘中改了主意」恰恰是最该被审计的一件事；
* 三只 agent 的原始 `log.jsonl` 是**另一套系统**的私有格式，随它一起下线。

本表是 QuantMind 决策的**唯一持久面**：一行 = 一轮里的一条决策（含被拒的），
审计字段写入即不可改（见 `decision_ledger_store.update_cols` 的说明）。

两个身份键（**都有用途，不要合并**）
------------------------------------
* ``id``（主键）＝ sha1(``round_id|序号|code|action``)[:16] —— **审计身份**：一轮一条，
  重跑同一轮且内容相同则幂等；重跑产出不同决策则**新增一行**（两次都留得住）；
* ``pool_key`` ＝ 隔壁 `make_id(agent|日|code|action)`[:16] —— **记分卡身份**：
  隔壁 `decision_track.py` 的池去重口径原样搬过来，读侧 `DISTINCT ON (pool_key)`
  即可复现「每天第一条」的池。与存量池对账走 `(agent, 日, 标的, 动作)` **元组**，
  **不比哈希**——隔壁的哈希输入是模型原样写法，本表是归一写法（见
  :func:`decision_ledger_store.pool_key`）。

把去重留给**读侧**（记分卡）而不是写侧（审计），是因为两者的正确性方向相反：
审计漏一行 = 无法回答「当时它说了什么」；记分卡多一行 = 同一只票被重复计一次
样本（样本量虚高，Kelly 折扣就失效了）。

DDL 的两份手抄件（**没有** `data/upgrade_v1.*.sql`）
------------------------------------------------------
* 全新安装 → `backend/shared/db_init.sql`（同一份 DDL，带注释头）；
* 老库自愈 → 本模块的 `ensure_*`，由 trade 服务启动期调用。
两处**必须同口径**，有测试守着（`backend/tests/test_decision_ledger_contract.py`）。

为什么**不**写进 `data/upgrade_v1.*.sql`——**不是因为那张目录里没有建表语句**
（早期判断如此，实测是错的：`grep -i` 一扫，`upgrade_v1.0.2/1.0.4/1.1.0.sql` 里
`system_events`、`qm_stock_pool{,_version,_member}`、`stock_daily_latest` 都是
`CREATE TABLE IF NOT EXISTS` 建的。当时那条 `grep -lE "create table"` **漏了大小写**，
把全部大写 DDL 筛掉了——用错正则得出的「零命中」会静默变成一条假结论，故此处留痕）。
真实原因是**送达路径的可靠性**，三条都是实测：

1. `db_init.sql` 随**代码**走，且每次启动被完整重放——`main_oss.py:449` 的 psql
   `-f`（`ON_ERROR_STOP=0`，单条失败不阻断）与 `:657` 的 psycopg2 兜底两条路径
   都会跑。把新表写进去，老库不需要任何新工件就能长出来；
   `test_every_contract_statement_appears_verbatim_in_db_init` 守着这一点。
2. `data/upgrade_*.sql` 靠**目录探测**发现（`_upgrade_sql_files`：五个候选目录取
   **第一个命中**），而它自己的 docstring 就记着一次「目录找错、迁移从未执行」的
   线上事故；且 `data/` 是**挂载的数据目录**不是代码——文件到不到服务器取决于数据
   同步，不取决于代码发布。
3. 本工作区的 `data/` 是指向仓库外的**符号链接**，git 已把 11 个受跟踪的 upgrade
   脚本报成「已删除」（`git add -A` 会真的删掉它们）。把新工件放进这里是全仓风险
   最高的一处。

即：`CREATE TABLE IF NOT EXISTS` 写进升级脚本**并非不可行**，是**送达不可靠**。
同批的 P1.6 影子账与近期各 `*_contract` 表走的都是本路线；且 ensure 由 trade 服务
启动期直接调用，唯一消费方自己就能痊愈，不依赖 `main_oss.py` 的 psql 路径。
"""

from __future__ import annotations

import logging
from collections.abc import Collection

logger = logging.getLogger(__name__)

TABLE = "qm_decision_ledger"

#: 幂等键列宽（实际 id 为 sha1 前 16 hex，列留一倍余量以便将来扩键不迁表）
ID_COL_LEN = 32

#: id / pool_key 实际取的 sha1 前缀长度（与隔壁 `decision_track.make_id` 同口径）
ID_HEX_LEN = 16

_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id             VARCHAR({ID_COL_LEN}) PRIMARY KEY,
    pool_key       VARCHAR({ID_COL_LEN}) NOT NULL,
    round_id       VARCHAR(64) NOT NULL,
    tenant_id      VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id        VARCHAR(64) NOT NULL DEFAULT '',
    agent          VARCHAR(64) NOT NULL DEFAULT '',
    market         VARCHAR(16) NOT NULL DEFAULT 'CN',
    trade_date     DATE NOT NULL,
    decided_at     TIMESTAMPTZ NOT NULL,
    code           VARCHAR(32) NOT NULL DEFAULT '',
    code_raw       VARCHAR(32) NOT NULL DEFAULT '',
    action         VARCHAR(16) NOT NULL,
    kind           VARCHAR(16) NOT NULL DEFAULT 'none',
    pct            DOUBLE PRECISION,
    pct_state      VARCHAR(16) NOT NULL DEFAULT 'missing',
    pct_raw        VARCHAR(32) NOT NULL DEFAULT '',
    confidence     DOUBLE PRECISION,
    stop_loss      DOUBLE PRECISION,
    take_profit    DOUBLE PRECISION,
    move_stop      DOUBLE PRECISION,
    invalidation   TEXT NOT NULL DEFAULT '',
    risk_amount    DOUBLE PRECISION,
    reason         TEXT NOT NULL DEFAULT '',
    armed          BOOLEAN NOT NULL DEFAULT FALSE,
    reject_reason  TEXT NOT NULL DEFAULT '',
    notes          JSONB NOT NULL DEFAULT '[]',
    order_id       VARCHAR(64) NOT NULL DEFAULT '',
    pool_ctx       JSONB,
    context_meta   JSONB NOT NULL DEFAULT '{{}}',
    entry_date     DATE,
    entry_px       DOUBLE PRECISION,
    tradable       BOOLEAN,
    fwd            JSONB,
    tags           JSONB NOT NULL DEFAULT '[]',
    priced_at      TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_decision_ledger_round ON {TABLE} (round_id);
CREATE INDEX IF NOT EXISTS idx_decision_ledger_pool ON {TABLE} (pool_key);
CREATE INDEX IF NOT EXISTS idx_decision_ledger_day ON {TABLE} (tenant_id, user_id, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_decision_ledger_agent ON {TABLE} (agent, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_decision_ledger_unpriced ON {TABLE} (trade_date) WHERE priced_at IS NULL;
"""

#: 建表**之后**追加的列（老库补列用）。
#:
#: `CREATE TABLE IF NOT EXISTS` 对既有表是空操作——表一旦存在，新加的列永远长不出来，
#: 而这条路径只在「全新库/首次自愈」走到，本地测试库早就建好了，**永远发现不了**，
#: 报错要等到线上第一次写入（`UndefinedColumn`）。故每加一列就在此登记一行，
#: 并同步 ``db_init.sql``；契约测试逐列比对两份 DDL。
#:
#: 本表当前无写入方（P2.2 才接），补列是一次性的冷表 DDL；表一旦热起来，加列要先
#: 按 ``signal_contract`` 那套（锁窗口 + 调用方事务纪律）评估，不能无条件 ADD。
_COLUMN_TOPUPS: tuple[tuple[str, str], ...] = (("tags", "JSONB NOT NULL DEFAULT '[]'"),)


_COLUMNS_SQL = (
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = :t"
)


def _missing_topups(current: Collection[str]) -> tuple[str, ...]:
    """登记了但库里没有的列（决定要不要走 DDL 路径）。

    表**存在**不等于**列全**——这正是 ``CREATE TABLE IF NOT EXISTS`` 的盲区，
    也是本函数存在的理由：只查 `to_regclass` 的快路径会把「老库缺列」判成「一切就绪」。
    """
    have = set(current)
    return tuple(name for name, _ in _COLUMN_TOPUPS if name not in have)


def ensure_decision_ledger_table() -> bool:
    """幂等建表（表在且列全即零 DDL 快路径；失败仅告警不抛出）。"""
    from sqlalchemy import text

    from backend.shared.sync_db import sync_session

    try:
        with sync_session() as session:
            # 一次查询同时回答两问：表在不在（列集非空 ⟺ 存在）、列全不全。
            current = {
                str(r[0])
                for r in session.execute(text(_COLUMNS_SQL), {"t": TABLE}).all()
            }
        if current and not _missing_topups(current):
            return True
        with sync_session() as session:
            session.execute(text("SET LOCAL lock_timeout = '3s'"))
            for statement in _CREATE_SQL.strip().split(";\n"):
                if statement.strip():
                    session.execute(text(statement))
            for name, ddl in _COLUMN_TOPUPS:
                session.execute(
                    text(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {name} {ddl}")
                )
            session.commit()
        logger.info("[DecisionLedgerContract] %s 表已就绪（建表/补列）", TABLE)
        return True
    except Exception as exc:  # noqa: BLE001 - 不阻断业务
        logger.warning("[DecisionLedgerContract] 自愈建表失败（不阻断）: %s", exc)
        return False


async def ensure_decision_ledger_table_async() -> bool:
    """trade 服务启动期自愈（与 :func:`ensure_decision_ledger_table` 等价）。"""
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as session:
            current = {
                str(r[0])
                for r in (
                    await session.execute(_text(_COLUMNS_SQL), {"t": TABLE})
                ).all()
            }
        if current and not _missing_topups(current):
            return True
        async with get_session() as session:
            await session.execute(_text("SET LOCAL lock_timeout = '3s'"))
            for statement in _CREATE_SQL.strip().split(";\n"):
                if statement.strip():
                    await session.execute(_text(statement))
            for name, ddl in _COLUMN_TOPUPS:
                await session.execute(
                    _text(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {name} {ddl}")
                )
            await session.commit()
        logger.info("[DecisionLedgerContract] %s 表已就绪（建表/补列，async）", TABLE)
        return True
    except Exception as exc:  # noqa: BLE001 - 不阻断启动
        logger.warning("[DecisionLedgerContract] 自愈建表失败（不阻断）: %s", exc)
        return False


__all__ = [
    "ID_COL_LEN",
    "ID_HEX_LEN",
    "TABLE",
    "ensure_decision_ledger_table",
    "ensure_decision_ledger_table_async",
]
