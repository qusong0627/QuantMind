"""因子池落库契约（P1）：两份 DDL 同口径 + 边表幂等键 + 启动期接线。

为什么值得单独测（照抄 `test_agent_ledger_contract.py` 的几类风险，逐条都成立）
--------------------------------------------------------------------------
1. **两份手抄件**：`factor_pool_contract._CREATE_SQL`（老库自愈）与 `db_init.sql`
   （全新安装）是同一批表的两次书写。抄漏一张表不会报错——新装的库少一张，直到
   第一次写池报 `UndefinedTable`。
2. **边表幂等键**：`refresh_pool` 重跑时靠 `(src, dst, relation, method)` 唯一约束
   `ON CONFLICT DO UPDATE` 收敛。没有它，每次刷新边行翻倍，谱系图出现平行重复边，
   「相似度 top-k」的分母随之漂移。
3. **第三份手抄件守卫**：`data/upgrade_*.sql` 里再抄一份就变成三处改（`data/` 是
   挂载数据目录不是代码，且本工作区是指向仓库外的符号链接）。新表一律走
   `db_init.sql`（随代码走、每次启动完整重放）+ 本模块 `ensure_*`。
4. **无破坏性语句**：`main_oss.py` 启动期会跳过含 DROP/DELETE/TRUNCATE 的 SQL，
   把这类语句写进 ensure 会让整份自愈被静默忽略。
5. **疲劳计数的形状**：`times_retrieved INTEGER NOT NULL DEFAULT 0`——注入检索的
   疲劳衰减从这列读。允许为 NULL 的话 `1/(1+fatigue)` 会在旧行上炸成 NaN。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.shared.factor_pool_contract import (
    COMBOS_TABLE,
    EDGES_TABLE,
    POOL_TABLE,
    TABLES,
    _CREATE_SQL,
)

DB_INIT = Path(__file__).resolve().parents[1] / "shared" / "db_init.sql"
CONTRACT_MODULE = (
    Path(__file__).resolve().parents[1] / "shared" / "factor_pool_contract.py"
)


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


# ── 三张表都在，且名字是文档里那些 ────────────────────────────────
def test_table_names_are_the_documented_ones():
    assert TABLES == (
        "rd_agent_factor_pool",
        "rd_agent_factor_edges",
        "rd_agent_factor_combos",
    ), "改名要同时改 db_init.sql 与读侧端点"
    assert (POOL_TABLE, EDGES_TABLE, COMBOS_TABLE) == TABLES, (
        "具名常量与 TABLES 必须同源"
    )


def test_every_contract_statement_appears_verbatim_in_db_init():
    """老库自愈用的每条语句都必须能在 db_init.sql 里原样找到（否则新库会缺东西）。"""
    hay = _db_init_text()
    missing = [s for s in _statements(_CREATE_SQL) if _norm(s) not in hay]
    assert not missing, f"db_init.sql 缺少以下语句（两份 DDL 已漂移）:\n{missing}"


def test_ddl_is_not_vacuous():
    """反证：语句集本身非空——否则上面那条断言恒真。"""
    stmts = _statements(_CREATE_SQL)
    assert len(stmts) >= 6, f"只解析到 {len(stmts)} 条语句，契约解析已失效"
    n_tables = sum(1 for s in stmts if s.strip().upper().startswith("CREATE TABLE"))
    assert n_tables == len(TABLES), f"解析到 {n_tables} 张建表语句，应为 {len(TABLES)}"
    n_idx = sum(1 for s in stmts if s.strip().upper().startswith("CREATE INDEX"))
    assert n_idx >= 3, f"索引语句只解析到 {n_idx} 条"


def test_db_init_creates_the_same_tables_and_indexes():
    """反向：db_init.sql 里这三张表的语句集与契约一致（多出来的语句也要审）。"""
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

    新表走 `db_init.sql`（随代码走、每次启动完整重放）+ 本模块 ensure，
    见 contract 模块 docstring。照 `test_agent_ledger_contract.py` 先例。
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


# ── 幂等键与疲劳计数的形状 ────────────────────────────────────────
def test_edges_uniqueness_is_src_dst_relation_method():
    """边表的唯一键是 **(src, dst, relation, method)**——refresh 收敛的替代物。

    没有它，每次 refresh 边会翻倍（平行重复边），谱系图与「相似度 top-k」
    的分母同时漂移。**排除**只按 edge_id 或 (src,dst) 的写法：
    同一对因子允许同时存在 method=formula 与 method=value 两条不同证据边。
    """
    normalized = _norm(_CREATE_SQL)
    assert "unique (src_factor_id, dst_factor_id, relation, method)" in normalized, (
        "边表唯一键形状变了（refresh 会对重跑边去重失败）"
    )
    assert "unique (src_factor_id, dst_factor_id)" not in normalized, (
        "只按 (src,dst) 的唯一键会让 formula 与 value 两条证据边互相覆盖"
    )


def test_pool_fatigue_columns_cannot_be_null():
    """疲劳计数列必须 NOT NULL DEFAULT 0：NULL 上的 `1/(1+fatigue)` 会炸成 NaN，
    且「从未被检索」与「计数丢失」将无法区分。"""
    normalized = _norm(_CREATE_SQL)
    assert "times_retrieved integer not null default 0" in normalized, (
        "疲劳计数的列形状变了"
    )
    cols = _insert_columns(POOL_TABLE)
    assert "last_retrieved_at" in cols, "疲劳时间戳列缺失"
    assert "factor_id" in cols, "池表主键缺失"


# ── 列覆盖：加了字段必须加列 ───────────────────────────────────────
def test_every_new_column_is_registered_as_a_topup():
    """除首版之外的每一列都要登记补列（老库才会长出来）；反向也要对。"""
    from backend.shared.factor_pool_contract import _COLUMN_TOPUPS, _V1_COLUMNS

    for table in TABLES:
        cols = set(_insert_columns(table))
        topups = {name for t, name, _ in _COLUMN_TOPUPS if t == table}
        v1 = set(_V1_COLUMNS[table])
        assert cols - v1 == topups, (
            f"{table}: 新列没登记补列（老库会永远缺这些列）: {sorted(cols - v1 - topups)}\n"
            f"{table}: 登记了但建表里没有（会补出野列）: {sorted(topups - (cols - v1))}"
        )


def test_ensure_paths_share_one_statement_table():
    """同步/异步两条 ensure 必须共用 `_ddl_statements`——只有一条补列 = 另一种
    部署形态下老库缺列。共用是**结构上**的保证，比两条各自复制一遍更硬。"""
    import inspect

    from backend.shared.factor_pool_contract import (
        ensure_factor_pool_tables,
        ensure_factor_pool_tables_async,
    )

    src = CONTRACT_MODULE.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS {name} {ddl}" in src, (
        "补列语句模板变了——幂等性（IF NOT EXISTS）必须留在语句里"
    )
    for fn in (ensure_factor_pool_tables, ensure_factor_pool_tables_async):
        body = inspect.getsource(fn)
        assert ("_apply_ddl" in body) or ("_ddl_statements" in body), (
            f"{fn.__name__} 没走共用的语句表"
        )
        assert "CREATE TABLE" not in body, (
            f"{fn.__name__} 自己内联了建表语句 —— 两份 DDL 会从此各走各的"
        )


def test_topups_are_idempotent_and_carry_a_default():
    """补列若为 NOT NULL 必须带 DEFAULT（没有默认值的 NOT NULL 列加不进已有行的表）。"""
    from backend.shared.factor_pool_contract import _COLUMN_TOPUPS

    for _table, name, ddl in _COLUMN_TOPUPS:
        if "NOT NULL" in ddl.upper():
            assert re.search(r"not null\s+default", ddl, re.I), (
                f"{name}: NOT NULL 的补列必须带 DEFAULT，否则老库上有行时加不进去"
            )


# ── 真库：ensure 幂等（表在且列全 → 快路径）────────────────────────
def test_ensure_is_idempotent_against_real_db():
    """连打两次 ensure 都为 True（第二次走零 DDL 快路径）。真库不可用则 skip。"""
    import asyncio

    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session
    from backend.shared.factor_pool_contract import ensure_factor_pool_tables_async

    async def _run() -> tuple[bool, bool, set[str]]:
        try:
            async with get_session(read_only=True) as probe:
                await probe.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"DB 不可用: {exc}")
        first = await ensure_factor_pool_tables_async()
        second = await ensure_factor_pool_tables_async()
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :t"
                    ),
                    {"t": POOL_TABLE},
                )
            ).all()
        return first, second, {str(r[0]) for r in rows}

    first, second, cols = asyncio.run(_run())
    assert first is True and second is True
    assert {"factor_id", "times_retrieved", "panel_ref"} <= cols
