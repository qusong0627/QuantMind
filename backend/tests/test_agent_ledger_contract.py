"""分账账本落库契约（P2.7）：两份 DDL 同口径 + 唯一键 + 启动期接线。

为什么值得单独测（照抄 `test_decision_ledger_contract.py` 的四类风险，逐条都成立）
------------------------------------------------------------------------------
1. **两份手抄件**：`agent_ledger_contract._CREATE_SQL`（老库自愈）与 `db_init.sql`
   （全新安装）是同一批表的两次书写。抄漏一张表不会报错——新装的库少一张，直到
   第一次写入报 `UndefinedTable`，而那时新库已经跑了几周。
2. **加列陷阱**：`CREATE TABLE IF NOT EXISTS` 对既有表是空操作，表一旦存在，
   往建表语句里加一列老库**永远长不出来**（故每加一列要登记 `_COLUMN_TOPUPS`）。
3. **静默丢数据**：写入方的每个键都要有对应列（`pg_insert` 只认表里有的列）。
4. **唯一键的形状**：本表的幂等靠 `(tenant_id, user_id, trade_date, fill_key)`，
   **不是**全库唯一——A 股 `exchange_trade_id` 是**每日重排**的成交编号（实测
   `trades.exchange_trade_id` 形如 `00161170`），全库唯一索引会把次日的同号成交
   静默吞掉（`ON CONFLICT DO NOTHING` 不报错）。吞掉一笔真成交 = 账本少一只票 =
   `mine_of` 看不见它 = 该卖的时候卖不掉，正是 2026-09-08 那类事故的翻版。
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path

import pytest

from backend.shared.agent_ledger_contract import (
    ACCOUNT_TABLE,
    FILL_TABLE,
    POSITION_TABLE,
    ROUNDTRIP_TABLE,
    TABLES,
    _CREATE_SQL,
)

DB_INIT = Path(__file__).resolve().parents[1] / "shared" / "db_init.sql"
CONTRACT_MODULE = (
    Path(__file__).resolve().parents[1] / "shared" / "agent_ledger_contract.py"
)

#: 服务端自己填的列（写入方不该写）
_SERVER_COLS = {"created_at", "updated_at"}

#: 自增主键（服务端填）
_SERIAL_COLS = {"id"}

#: 唯一键的列组合——**当日**口径，见模块 docstring 第 4 条
FILL_UNIQUE_COLS = ("tenant_id", "user_id", "trade_date", "fill_key")


def _norm(sql: str) -> str:
    """折叠空白 + 统一小写：DDL 的可执行语义与缩进无关。"""
    return re.sub(r"\s+", " ", sql).strip().rstrip(";").lower()


def _statements(sql: str) -> list[str]:
    return [s for s in sql.strip().split(";\n") if s.strip()]


def _db_init_text() -> str:
    return _norm(DB_INIT.read_text(encoding="utf-8"))


def _insert_columns(table: str, sql: str | None = None) -> list[str]:
    """取某张表的 **CREATE TABLE** 列名（跳过表级约束行）。"""
    body_sql = sql if sql is not None else _CREATE_SQL
    matched = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\);",
        body_sql,
        re.S,
    )
    assert matched, f"契约里没有 {table} 的建表语句"
    cols: list[str] = []
    for line in matched.group(1).splitlines():
        line = line.strip().rstrip(",")
        if not line:
            continue
        head = line.split()[0].lower()
        if head in {"primary", "unique", "constraint", "foreign", "check"}:
            continue
        cols.append(head)
    return cols


# ── 四张表都在，且名字是文档里那个 ──────────────────────────────────
def test_table_names_are_the_documented_ones():
    assert TABLES == (
        "qm_agent_ledger_account",
        "qm_agent_ledger_position",
        "qm_agent_ledger_fill",
        "qm_agent_ledger_roundtrip",
    ), "改名要同时改 db_init.sql 与读侧报表"
    assert (ACCOUNT_TABLE, POSITION_TABLE, FILL_TABLE, ROUNDTRIP_TABLE) == TABLES, (
        "四个具名常量与 TABLES 必须同源"
    )


def test_every_contract_statement_appears_verbatim_in_db_init():
    """老库自愈用的每条语句都必须能在 db_init.sql 里原样找到（否则新库会缺东西）。"""
    hay = _db_init_text()
    missing = [s for s in _statements(_CREATE_SQL) if _norm(s) not in hay]
    assert not missing, f"db_init.sql 缺少以下语句（两份 DDL 已漂移）:\n{missing}"


def test_ddl_is_not_vacuous():
    """反证：语句集本身非空——否则上面那条断言恒真。"""
    stmts = _statements(_CREATE_SQL)
    assert len(stmts) >= 8, f"只解析到 {len(stmts)} 条语句，契约解析已失效"
    n_tables = sum(1 for s in stmts if s.strip().upper().startswith("CREATE TABLE"))
    assert n_tables == len(TABLES), f"解析到 {n_tables} 张建表语句，应为 {len(TABLES)}"
    n_idx = sum(1 for s in stmts if s.strip().upper().startswith("CREATE INDEX"))
    assert n_idx >= 4, f"索引语句只解析到 {n_idx} 条"


def test_db_init_creates_the_same_tables_and_indexes():
    """反向：db_init.sql 里这四张表的语句集与契约一致（多出来的语句也要审）。"""
    hay = _db_init_text()
    contract = {_norm(s) for s in _statements(_CREATE_SQL)}
    in_init = {
        s
        for s in (_norm(x) for x in re.findall(r"create[^;]*(?:;|$)", hay))
        if any(t in s for t in TABLES)
    }
    assert in_init, "反向扫描没扫到任何语句 —— 断言会是空的（假通过）"
    assert in_init == contract, (
        f"db_init.sql 与契约的语句集不一致\n"
        f"仅在 db_init: {in_init - contract}\n仅在契约: {contract - in_init}"
    )


def _strip_sql_comments(text: str) -> str:
    return re.sub(r"--[^\n]*", " ", text)


def _scan_upgrade_sql() -> dict[str, str]:
    """扫描**生产会扫的那些目录**里的升级 SQL（目录列表照抄 main_oss 的候选）。"""
    import os

    roots = [
        os.getenv("QM_UPGRADE_SQL_DIR", ""),
        os.getenv("QM_DATA_DIR", ""),
        "/data",
        str(Path(__file__).resolve().parents[2] / "data"),
    ]
    found: dict[str, str] = {}
    for root in roots:
        if not root or not Path(root).is_dir():
            continue
        for path in sorted(Path(root).glob("upgrade_*.sql")):
            if path.name in found:
                continue
            found[path.name] = _strip_sql_comments(
                path.read_text(encoding="utf-8", errors="replace")
            )
    return found


def test_the_tables_have_exactly_two_copies_in_the_repo():
    """「两份手抄件」是本设计的前提：`data/upgrade_*.sql` 里再抄一份就变成三处改。

    新表走 `db_init.sql`（随代码走、每次启动完整重放），见 contract 模块 docstring。
    """
    scripts = _scan_upgrade_sql()
    if not scripts:
        pytest.skip("没扫到任何 upgrade_*.sql（数据目录未挂载）——守卫无从谈起")
    hits = sorted(
        n
        for n, body in scripts.items()
        if any(re.search(rf"\b{t}\b", body, re.I) for t in TABLES)
    )
    assert not hits, f"data/ 下的升级脚本里出现了本表（第三份手抄件）: {hits}"


def test_no_destructive_ddl_in_the_contract():
    """自愈脚本绝不能带破坏性语句——`main_oss.py` 会跳过含 DROP/DELETE/TRUNCATE 的
    升级脚本，把这类语句写进 ensure 会让整份自愈被静默忽略。"""
    up = _CREATE_SQL.upper()
    for bad in ("DROP ", "TRUNCATE", "DELETE "):
        assert bad not in up, f"契约 DDL 含破坏性语句 {bad!r}"


# ── 幂等键的形状（本批最关键的一条）────────────────────────────────
def test_fill_uniqueness_is_day_scoped_and_not_global():
    """`fill_key` 的唯一性必须是 **(租户, 账户, 交易日, fill_key)**。

    A 股成交编号**每日重排**（实测 `trades.exchange_trade_id` = `00161170` 这类
    8 位号），全库唯一索引的后果不是「报错」而是**静默吞掉次日的同号成交**
    （`ON CONFLICT DO NOTHING`）——账本少一只票、`mine_of` 看不见它，agent 于是
    卖不掉自己的持仓。方向恰好与 2026-09-08 的事故同族。
    """
    cols = _insert_columns(FILL_TABLE)
    assert "fill_key" in cols and "trade_date" in cols, "唯一键的两半必须都在表里"
    normalized = _norm(_CREATE_SQL)
    assert "unique (tenant_id, user_id, trade_date, fill_key)" in normalized, (
        "fill 的唯一键形状变了（是否被改成全库唯一？见本测试 docstring）"
    )
    # 反向：不许同时存在只按 fill_key 的唯一约束（那会让上面那条成为摆设）
    assert "unique (fill_key)" not in normalized
    assert "fill_key varchar(128) unique" not in normalized


def test_fill_key_column_fits_its_width():
    """`broker_order_id:exec_id` 拼接键要比列宽短（写死 64 而实际 80 是经典事故）。"""
    from backend.shared.agent_ledger_contract import FILL_KEY_LEN

    worst = f"{'9' * 40}:{'9' * 40}"
    assert len(worst) <= FILL_KEY_LEN, "拼接键可能超列宽 → 插入报错"


# ── 列覆盖：加了字段必须加列 ───────────────────────────────────────
def test_every_new_column_is_registered_as_a_topup():
    """除首版之外的每一列都要登记补列（老库才会长出来）；反向也要对。"""
    from backend.shared.agent_ledger_contract import _COLUMN_TOPUPS, _V1_COLUMNS

    for table in TABLES:
        cols = set(_insert_columns(table))
        topups = {name for t, name, _ in _COLUMN_TOPUPS if t == table}
        v1 = set(_V1_COLUMNS[table])
        assert cols - v1 == topups, (
            f"{table}: 新列没登记补列（老库会永远缺这些列）: {sorted(cols - v1 - topups)}\n"
            f"{table}: 登记了但建表里没有（会补出野列）: {sorted(topups - (cols - v1))}"
        )


def test_topups_are_idempotent_and_carry_a_default():
    """补列必须 `ADD COLUMN IF NOT EXISTS`，且 `NOT NULL` 时**带默认值**
    （没有默认值的 NOT NULL 列加不进已有行的表，自愈直接空转）。"""
    from backend.shared.agent_ledger_contract import (
        _COLUMN_TOPUPS,
        _ddl_statements,
        ensure_agent_ledger_tables,
        ensure_agent_ledger_tables_async,
    )

    src = CONTRACT_MODULE.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS {name} {ddl}" in src, (
        "补列语句模板变了——幂等性（IF NOT EXISTS）必须留在语句里"
    )
    # 两条 ensure 路径共用一份语句表（`_ddl_statements`，经 `_apply_ddl`）：只有一条
    # 补列 = 另一种部署形态下老库缺列。共用是**结构上**的保证，比两条各自复制一遍更硬。
    for fn in (ensure_agent_ledger_tables, ensure_agent_ledger_tables_async):
        body = inspect.getsource(fn)
        assert ("_apply_ddl" in body) or ("_ddl_statements" in body), (
            f"{fn.__name__} 没走共用的语句表"
        )
        assert "CREATE TABLE" not in body, (
            f"{fn.__name__} 自己内联了建表语句 —— 两份 DDL 会从此各走各的"
        )
    if _COLUMN_TOPUPS:
        assert any("ADD COLUMN IF NOT EXISTS" in s for s in _ddl_statements()), (
            "登记了补列但语句表里没有补列语句"
        )
    for _table, name, ddl in _COLUMN_TOPUPS:
        if "NOT NULL" in ddl.upper():
            assert re.search(r"not null\s+default", ddl, re.I), (
                f"{name}: NOT NULL 的补列必须带 DEFAULT，否则老库上有行时加不进去"
            )


#: 写入方会写的列（store 的 record_values / 各 upsert 的列）——用断言锁住不漂移
_WRITTEN = {
    ACCOUNT_TABLE: {"tenant_id", "user_id", "agent", "virtual_cash"},
    POSITION_TABLE: {
        "tenant_id",
        "user_id",
        "agent",
        "code",
        "volume",
        "cost_price",
        "buy_ts",
        "last_ts",
    },
    FILL_TABLE: {
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
    },
    ROUNDTRIP_TABLE: {
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
    },
}


@pytest.mark.parametrize("table", list(_WRITTEN))
def test_written_columns_all_exist(table: str):
    """写入方写了的列必须都在表里，否则 `pg_insert` 静默丢掉那个字段。"""
    cols = set(_insert_columns(table)) | _SERVER_COLS | _SERIAL_COLS
    missing = sorted(_WRITTEN[table] - cols)
    assert not missing, f"{table}: 写入方写了但表里没有的列（会静默丢数据）: {missing}"


@pytest.mark.parametrize("table", list(_WRITTEN))
def test_no_dead_columns(table: str):
    """反向：DDL 里除服务端填的列外，每一列都要有写入方（死列 = 抄多了）。"""
    cols = set(_insert_columns(table))
    orphan = sorted(cols - _WRITTEN[table] - _SERVER_COLS - _SERIAL_COLS)
    assert not orphan, f"{table}: 这些列没有任何写入方（死列）: {orphan}"


def test_required_columns_are_not_nullable_by_accident():
    """骨架列必须 NOT NULL（读侧/幂等键都靠它们）。"""
    sql = _norm(_CREATE_SQL)
    # 类型可能是多词（`double precision` / `timestamp with time zone`），故不限单词
    for col in ("tenant_id", "user_id", "agent", "volume", "cost_price"):
        assert re.search(rf"{col} [a-z0-9() ]+? not null", sql), f"{col} 应 NOT NULL"
    for col in FILL_UNIQUE_COLS:
        assert re.search(rf"{col} [a-z0-9() ]+? not null", sql), (
            f"唯一键列 {col} 可空 = 唯一约束对 NULL 不生效（幂等直接失效）"
        )


def test_json_columns_are_valid_json_literals():
    """本批没有 JSONB 列——留一条反证，防止后来者加 JSONB 时把 f-string 转义写反
    （`DEFAULT '{}'` 在 f-string 里必须写 `'{{}}'`，写反只在建表那一刻报错）。"""
    for name, sql in (
        ("contract", _CREATE_SQL),
        ("db_init", DB_INIT.read_text("utf-8")),
    ):
        for m in re.finditer(r"\bjsonb\s+not null\s+default\s+'([^']*)'", sql, re.I):
            try:
                json.loads(m.group(1))
            except json.JSONDecodeError as exc:  # 点名是哪一份手抄件写坏了
                raise AssertionError(
                    f"{name} 里的 JSONB 默认值不是合法 JSON：{exc}"
                ) from exc


# ── 启动接线：契约模块必须真的被 trade 服务调用 ─────────────────────
TRADE_MAIN = (
    Path(__file__).resolve().parents[1] / "services" / "trade" / "main.py"
).read_text(encoding="utf-8")


def _ensure_tuple_names(tree: ast.Module) -> set[str]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Tuple):
            continue
        names = {e.id for e in node.elts if isinstance(e, ast.Name)}
        if "ensure_ghost_ledger_table_async" in names:
            return names
    return set()


def test_ensure_is_wired_into_trade_lifespan():
    """**单测绿 ≠ 生产会建表**：只在有人调它时才起作用。漏接线的表现是本地测试全绿、
    生产老库第一次写入报 `UndefinedTable`——而且是在盘中。

    用 AST 而不是正则：`ruff format` 会把 import 拆成括号多行、把元组换行。
    """
    tree = ast.parse(TRADE_MAIN)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").endswith("agent_ledger_contract")
        for alias in node.names
    }
    assert "ensure_agent_ledger_tables_async" in imported, (
        "trade/main.py 没有从 agent_ledger_contract 导入 ensure（接线断了）"
    )
    assert "ensure_agent_ledger_tables_async" in _ensure_tuple_names(tree), (
        "导入但没进 lifespan 的 ensure 元组 —— 模块在、表不建"
    )


def test_wiring_probe_would_notice_a_missing_entry():
    """反证：解析器对**不存在的**名字必须返回假——否则上面那条断言恒真。"""
    names = _ensure_tuple_names(ast.parse(TRADE_MAIN))
    assert names, "没解析到 ensure 元组 —— 接线测试会假通过"
    assert "ensure_no_such_table_async" not in names
