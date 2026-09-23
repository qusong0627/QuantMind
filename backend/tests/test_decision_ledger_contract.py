"""决策审计表落库契约（P2.1d）：两份 DDL 同口径 + 列覆盖不留暗坑。

为什么值得单独测
----------------
`decision_ledger_contract._CREATE_SQL`（老库自愈）与 `db_init.sql`（全新安装）是同一张
表的**两份手抄件**（P1.6 影子账同款结构，照抄那份的守护）。抄漏一列不会报错——老库
跑得好好的、新装的库少一列，直到某天报表读不到字段才发现，而那时新库已经跑了几周。

第二类风险是**静默丢数据**：`DecisionRecord` 加了字段、DDL 没加，写入时该字段悄悄
消失（`pg_insert` 只认表里有的列）。故再加覆盖断言（两个方向都要：写了没列 = 丢数据；
有列没人写 = 死列）。

第三类是 **JSON 默认值的 f-string 转义**：`DEFAULT '{}'` 在 f-string 里必须写成
`'{{}}'`。写反了（或漏了）会让 DDL 在**建表那一刻**报语法错——而这条路径只在全新
安装/老库首次自愈时走到，本地测试库早就建好了，永远发现不了。故直接解析默认值。
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
from pathlib import Path

import pytest

from backend.shared.decision_ledger_contract import (
    ID_COL_LEN,
    TABLE,
    _CREATE_SQL,
)
from backend.shared.decision_ledger_store import (
    DecisionRecord,
    build_records,
    decision_id,
    pool_key,
    record_values,
)

DB_INIT = Path(__file__).resolve().parents[1] / "shared" / "db_init.sql"
CONTRACT_MODULE = (
    Path(__file__).resolve().parents[1] / "shared" / "decision_ledger_contract.py"
)

#: 服务端自己填的列（写入方不该写）
_SERVER_COLS = {"created_at", "updated_at"}


def _norm(sql: str) -> str:
    """折叠空白 + 统一小写：DDL 的可执行语义与缩进无关。"""
    return re.sub(r"\s+", " ", sql).strip().rstrip(";").lower()


def _statements(sql: str) -> list[str]:
    return [s for s in sql.strip().split(";\n") if s.strip()]


def _db_init_text() -> str:
    return _norm(DB_INIT.read_text(encoding="utf-8"))


def _insert_columns(sql: str) -> list[str]:
    """取 **CREATE TABLE** 里的列名（跳过表级约束行）。

    先把建表语句切出来：契约里表后面还跟着 5 条 CREATE INDEX，若直接
    `sql.rindex(")")` 取表体，末尾几条索引会被当成列名读进来（"create"/"index"
    这类假列），做反向断言时就会告出一堆不存在的「死列」。
    """
    create_table = sql.split(");")[0]
    assert "CREATE TABLE" in create_table.upper(), "契约里没有建表语句？"
    body = create_table[create_table.index("(") + 1 :]
    cols = []
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line:
            continue
        head = line.split()[0].lower()
        if head in {"primary", "unique", "constraint", "foreign", "check"}:
            continue
        cols.append(head)
    return cols


# ── 两份 DDL 同口径 ────────────────────────────────────────────────
def test_table_name_is_the_documented_one():
    assert TABLE == "qm_decision_ledger", "改名要同时改 db_init.sql 与报表/CLI"


def test_every_contract_statement_appears_verbatim_in_db_init():
    """老库自愈用的每条语句都必须能在 db_init.sql 里原样找到（否则新库会缺东西）。"""
    hay = _db_init_text()
    missing = [s for s in _statements(_CREATE_SQL) if _norm(s) not in hay]
    assert not missing, f"db_init.sql 缺少以下语句（两份 DDL 已漂移）:\n{missing}"


def test_db_init_creates_the_same_table_and_indexes():
    """反向：db_init.sql 里本表的语句集与契约一致（多出来的语句也要审）。"""
    hay = _db_init_text()
    contract = {_norm(s) for s in _statements(_CREATE_SQL)}
    in_init = {
        s
        for s in (_norm(x) for x in re.findall(r"create[^;]*(?:;|$)", hay))
        if TABLE in s
    }
    assert in_init, "反向扫描没扫到任何语句 —— 断言会是空的（假通过）"
    assert in_init == contract, (
        f"db_init.sql 与契约的语句集不一致\n"
        f"仅在 db_init: {in_init - contract}\n仅在契约: {contract - in_init}"
    )


def _strip_sql_comments(text: str) -> str:
    return re.sub(r"--[^\n]*", " ", text)


def _scan_upgrade_sql() -> dict[str, str]:
    """扫描**生产会扫的那些目录**里的升级 SQL，返回 {文件名: 去注释正文}。

    目录列表照抄 `main_oss.py:_upgrade_sql_files` 的候选（env 覆盖 → `/data` →
    `<repo>/data`）：本地开发机命中 `<repo>/data`（符号链接指向真数据目录），容器里
    `/app/data` 是空壳、实际挂载在 `/data`——只认单一路径的守卫在容器内会静默变空。
    """
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


def test_the_table_has_exactly_two_copies_in_the_repo():
    """docstring 里的「两份手抄件」是本设计的前提，这条把它变成**可执行**的：
    多出第三份（尤其在 `data/upgrade_*.sql` 里再抄一遍）会让「改一处必须改两处」
    当场变成假话。

    这条的前身是一次**失败的验证**：早期用 `grep -lE "create table" data/*.sql`
    得出结论「该目录没有任何建表语句」，据此写进了 docstring——实际是漏了大小写，
    4 个文件里的全大写 `CREATE TABLE` 全被筛掉了。所以这里一律 `re.I`，并且
    `test_data_dir_scan_is_not_vacuous` 专门盯着「扫描面是不是空的」。
    """
    scripts = _scan_upgrade_sql()
    if not scripts:
        pytest.skip("没扫到任何 upgrade_*.sql（数据目录未挂载）——守卫无从谈起")
    hits = sorted(
        n for n, body in scripts.items() if re.search(rf"\b{TABLE}\b", body, re.I)
    )
    assert not hits, (
        f"data/ 下的升级脚本里出现了本表（第三份手抄件）: {hits}\n"
        "新表走 db_init.sql（随代码走、每次启动完整重放）；见 contract 模块 docstring"
    )


def test_data_dir_scan_is_not_vacuous():
    """反证：那个扫描**必须真的读到了内容**，否则「零命中」什么也没证明。

    这正是前身那条 grep 的教训——「没有命中」和「没扫到」长得一模一样。
    """
    scripts = _scan_upgrade_sql()
    if not scripts:
        pytest.skip("没扫到任何 upgrade_*.sql（数据目录未挂载）")
    assert len(scripts) >= 5, f"只扫到 {len(scripts)} 个脚本 —— 扫描面太小，守卫是空的"
    # 这批文件里确实**有**建表语句（大小写不敏感地扫得到），证明扫描本身有效
    with_ddl = sorted(
        n for n, body in scripts.items() if re.search(r"create\s+table", body, re.I)
    )
    assert with_ddl, "一个建表语句都没扫到 —— 说明扫描逻辑或目录假设已经失效"


#: 首版 DDL 的列（冻结）——凡是**不在这份清单里**的列都必须登记进 `_COLUMN_TOPUPS`
_V1_COLUMNS = frozenset(
    """
    id pool_key round_id tenant_id user_id agent market trade_date decided_at code
    code_raw action kind pct pct_state pct_raw confidence stop_loss take_profit
    move_stop invalidation risk_amount reason armed reject_reason notes order_id
    pool_ctx context_meta entry_date entry_px tradable fwd priced_at
    created_at updated_at
    """.split()
)


def test_every_new_column_is_registered_as_a_topup():
    """**加列陷阱**：`CREATE TABLE IF NOT EXISTS` 对既有表是空操作——表已存在时，
    往建表语句里加一列，老库**永远长不出这一列**，而本地库早就建好了，测试全绿，
    直到线上第一次写入报 `UndefinedColumn`。

    这条把「新列必须登记补列」变成编译期式的检查：`_CREATE_SQL` 里除首版外的每一列
    都要出现在 `_COLUMN_TOPUPS`，反之亦然（登记了却没建表 = 补出一个不该有的列）。
    """
    from backend.shared.decision_ledger_contract import _COLUMN_TOPUPS

    cols = set(_insert_columns(_CREATE_SQL))
    new_cols = cols - _V1_COLUMNS
    topups = {name for name, _ in _COLUMN_TOPUPS}
    assert new_cols == topups, (
        f"新列没登记补列（老库会永远缺这些列）: {sorted(new_cols - topups)}\n"
        f"登记了但建表里没有（会补出野列）: {sorted(topups - new_cols)}"
    )


def test_topups_are_idempotent_and_carry_a_default():
    """补列必须 `ADD COLUMN IF NOT EXISTS`（幂等）且 `NOT NULL` 时**带默认值**：
    没有默认值的 NOT NULL 列加不进已有行的表——ALTER 直接失败，自愈变成空转。"""
    from backend.shared.decision_ledger_contract import (
        _COLUMN_TOPUPS,
        ensure_decision_ledger_table,
        ensure_decision_ledger_table_async,
    )

    src = CONTRACT_MODULE.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS {name} {ddl}" in src, (
        "补列语句模板变了——幂等性（IF NOT EXISTS）必须留在语句里"
    )
    # 两条 ensure 路径都要补列（只有一条补 = 另一条部署形态下老库缺列）
    for fn in (ensure_decision_ledger_table, ensure_decision_ledger_table_async):
        body = inspect.getsource(fn)
        assert "ADD COLUMN IF NOT EXISTS" in body, f"{fn.__name__} 没有补列步骤"
    for name, ddl in _COLUMN_TOPUPS:
        if "NOT NULL" in ddl.upper():
            assert re.search(r"not null\s+default", ddl, re.I), (
                f"{name}: NOT NULL 的补列必须带 DEFAULT，否则老库上有行时加不进去"
            )


def test_no_destructive_ddl_in_the_contract():
    """自愈脚本绝不能带破坏性语句——`main_oss.py` 会跳过含 DROP/DELETE/TRUNCATE 的
    升级脚本，把这类语句写进 ensure 会让整份自愈被静默忽略。"""
    up = _CREATE_SQL.upper()
    for bad in ("DROP ", "TRUNCATE", "DELETE "):
        assert bad not in up, f"契约 DDL 含破坏性语句 {bad!r}"


def test_json_defaults_are_valid_json_in_both_copies():
    """`DEFAULT '[]'` / `'{}'` 必须是**合法 JSON 字面量**（f-string 转义写反会在这里红）。"""
    for name, sql in (
        ("contract", _CREATE_SQL),
        ("db_init", DB_INIT.read_text("utf-8")),
    ):
        for col in ("notes", "context_meta"):
            m = re.search(
                rf"\b{col}\s+jsonb\s+not null\s+default\s+'([^']*)'", sql, re.I
            )
            assert m, f"{name}: {col} 的默认值没找到（DDL 形状变了？）"
            json.loads(m.group(1))  # 非法 JSON 直接抛


# ── 列覆盖：加了字段必须加列 ───────────────────────────────────────
def _sample_record() -> DecisionRecord:
    return DecisionRecord(
        id=decision_id("r1", 0, "SH600519", "watch"),
        pool_key=pool_key("m", "2026-09-23", "SH600519", "watch"),
        round_id="r1",
        tenant_id="default",
        user_id="10000001",
        agent="deepseek-v4-pro",
        market="CN",
        trade_date="2026-09-23",  # type: ignore[arg-type]
        decided_at="2026-09-23T01:30:00Z",  # type: ignore[arg-type]
        code="SH600519",
        action="watch",
    )


def test_record_fields_are_all_backed_by_columns():
    """`record_values()` 的每个键都要有对应列，否则写入时被静默丢弃。"""
    cols = set(_insert_columns(_CREATE_SQL))
    keys = set(record_values(_sample_record()))
    missing = sorted(keys - cols)
    assert not missing, f"record_values() 的字段没有对应列（会静默丢数据）: {missing}"


def test_every_column_is_written_by_someone():
    """反向：DDL 里除了服务端填的两列，每一列都要有写入方（死列 = 抄多了）。"""
    cols = set(_insert_columns(_CREATE_SQL))
    keys = set(record_values(_sample_record()))
    orphan = sorted(cols - keys - _SERVER_COLS)
    assert not orphan, f"这些列没有任何写入方（死列）: {orphan}"


def test_required_columns_are_not_nullable_by_accident():
    """这几列是查询/幂等的骨架，必须 NOT NULL。"""
    sql = _norm(_CREATE_SQL)
    for col in (
        "pool_key",
        "round_id",
        "trade_date",
        "decided_at",
        "action",
        "kind",
        "pct_state",
        "armed",
    ):
        assert re.search(rf"{col} [a-z0-9()]+ not null", sql), f"{col} 应 NOT NULL"


def test_id_columns_fit_their_width():
    """幂等键比列宽还长 → 插入报错（写死 32 而生成 64 位 hex 是经典事故）。"""
    rid = decision_id("round-abcdefgh", 12345, "SH600519", "watch")
    pk = pool_key("deepseek-v4-pro", "2026-09-23", "SH600519", "watch")
    assert len(rid) <= ID_COL_LEN and len(pk) <= ID_COL_LEN


def test_unpriced_partial_index_matches_the_query():
    """待定价查询（`priced_at IS NULL AND kind <> 'none'`）必须有索引支撑：
    那是每天都要跑的滚动回填，全表扫会随审计表增长线性变慢。"""
    sql = _norm(_CREATE_SQL)
    assert "where priced_at is null" in sql, "待定价的部分索引丢了"


def test_build_records_writes_every_row_including_holds():
    """**不丢行**：`hold` 与无码行也入库（审计问的是「当时它说了什么」）。

    隔壁的池把 `hold` 挡在门外（没有收益语义）——那是记分卡口径，由读侧
    `kind <> 'none'` 表达；写侧少一行就没法回答「它当时为什么不动」。
    """
    from backend.shared.decision.contract import SCHEMA_INTRADAY, parse_decisions

    batch = parse_decisions(
        json.dumps(
            {
                "decisions": [
                    {"action": "hold", "code": "600519.SH"},
                    {"action": "watch", "code": "SH600519", "stop_loss": 1500.0},
                ]
            }
        ),
        schema=SCHEMA_INTRADAY,
    )
    assert batch.ok
    rows = build_records(
        batch.decisions,
        round_id="r9",
        agent="m",
        trade_date="2026-09-23",
        decided_at="2026-09-23T01:30:00Z",  # type: ignore[arg-type]
    )
    assert len(rows) == len(batch.decisions) == 2
    assert [r.kind for r in rows] == ["none", "bullish"]
    assert rows[1].code == "SH600519" and rows[1].code_raw == "SH600519"


# ── 启动接线：契约模块必须真的被 trade 服务调用 ─────────────────────
TRADE_MAIN = (
    Path(__file__).resolve().parents[1] / "services" / "trade" / "main.py"
).read_text(encoding="utf-8")


def _ensure_tuple_names(tree: ast.Module) -> set[str]:
    """lifespan 里 `for _ensure in (...)` 那个元组里的函数名集合。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Tuple):
            continue
        names = {e.id for e in node.elts if isinstance(e, ast.Name)}
        if "ensure_ghost_ledger_table_async" in names:
            return names
    return set()


def test_ensure_is_wired_into_trade_lifespan():
    """**单测绿 ≠ 生产会建表**：`ensure_decision_ledger_table_async` 只在有人调它时
    才起作用。DB 往返测试显式调了它，所以「表建得起来」有证据；但「新库/老库启动时
    会有谁去调」只有接线能回答。漏接线的表现是：本地测试全绿，生产老库第一次写入
    报 `UndefinedTable`——而且是在盘中。

    用 AST 而不是正则：`ruff format` 会把 import 拆成括号多行、把元组换行，
    正则匹配字符串迟早被重排成假红。
    """
    tree = ast.parse(TRADE_MAIN)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").endswith("decision_ledger_contract")
        for alias in node.names
    }
    assert "ensure_decision_ledger_table_async" in imported, (
        "trade/main.py 没有从 decision_ledger_contract 导入 ensure（接线断了）"
    )
    assert "ensure_decision_ledger_table_async" in _ensure_tuple_names(tree), (
        "导入但没进 lifespan 的 ensure 元组 —— 模块在、表不建"
    )


def test_wiring_probe_would_notice_a_missing_entry():
    """反证：解析器对**不存在的**名字必须返回假——否则上面那条断言恒真。"""
    tree = ast.parse(TRADE_MAIN)
    names = _ensure_tuple_names(tree)
    assert names, "没解析到 ensure 元组 —— 接线测试会假通过"
    assert "ensure_no_such_table_async" not in names
