"""影子代价账落库契约（P1.6）：两份 DDL 同口径 + 列覆盖不留暗坑。

为什么值得单独测
----------------
`ghost_ledger_contract._CREATE_SQL`（老库自愈）与 `db_init.sql`（全新安装）是同一张表
的两份手抄件。抄漏一列不会报错——老库跑得好好的、新装的库少一列，直到某天报表
读不到字段才发现，而那时新库已经跑了几周。这里用**逐语句归一化比对**把两份钉在一起。

第二类风险是**静默丢数据**：`GhostRow.to_record()` 加了字段、DDL 没加，写入时
该字段悄悄消失（`pg_insert` 只认表里有的列）。故再加一条覆盖断言。
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.shared.ghost_ledger_contract import (
    ID_LEN,
    TABLE,
    _CREATE_SQL,
)
from backend.shared.risk.ghost import GhostRow, ghost_id

DB_INIT = Path(__file__).resolve().parents[1] / "shared" / "db_init.sql"


def _norm(sql: str) -> str:
    """折叠空白 + 统一小写：DDL 的可执行语义与缩进无关。"""
    return re.sub(r"\s+", " ", sql).strip().rstrip(";").lower()


def _statements(sql: str) -> list[str]:
    return [s for s in sql.strip().split(";\n") if s.strip()]


def _db_init_text() -> str:
    return _norm(DB_INIT.read_text(encoding="utf-8"))


def _insert_columns(sql: str) -> list[str]:
    """取 CREATE TABLE 里的列名（跳过表级约束行）。"""
    body = sql[sql.index("(") + 1 : sql.rindex(")")]
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
    assert TABLE == "qm_risk_ghost_ledger", "改名要同时改 db_init.sql 与报表/CLI"


def test_every_contract_statement_appears_verbatim_in_db_init():
    """老库自愈用的每条语句都必须能在 db_init.sql 里原样找到（否则新库会缺东西）。"""
    hay = _db_init_text()
    missing = [s for s in _statements(_CREATE_SQL) if _norm(s) not in hay]
    assert not missing, f"db_init.sql 缺少以下语句（两份 DDL 已漂移）:\n{missing}"


def test_db_init_creates_the_same_table_and_indexes():
    """反向：db_init.sql 里本表的语句集与契约一致（多出来的语句也要审）。

    注意 `(?:;|$)`：文件末尾那条语句**没有**终止分号（`_norm` 会剥掉它），
    只写 `;` 会让这条断言静默漏掉最后一句——本表恰好就是文件的最后一张表。
    """
    hay = _db_init_text()
    contract = {_norm(s) for s in _statements(_CREATE_SQL)}
    in_init = {
        s
        for s in (_norm(x) for x in re.findall(r"create[^;]*(?:;|$)", hay))
        if TABLE in s
    }
    assert in_init, "反向扫描没扫到任何语句 —— 断言会是空的（假通过）"
    assert in_init == contract, (
        f"db_init.sql 与契约的语句集不一致\n仅在 db_init: {in_init - contract}\n仅在契约: {contract - in_init}"
    )


def test_no_destructive_ddl_in_the_contract():
    """自愈脚本绝不能带破坏性语句——`main_oss.py` 会跳过含 DROP/DELETE/TRUNCATE 的
    升级脚本，把这类语句写进 ensure 会让整份自愈被静默忽略。"""
    up = _CREATE_SQL.upper()
    for bad in ("DROP ", "TRUNCATE", "DELETE "):
        assert bad not in up, f"契约 DDL 含破坏性语句 {bad!r}"


# ── 列覆盖：加了字段必须加列 ───────────────────────────────────────
def test_record_fields_are_all_backed_by_columns():
    """`to_record()` 的每个字段都要有对应列，否则写入时被静默丢弃。

    `symbol`/`side` 等列名与 dataclass 字段同名；`date`/`ts` 走列名映射（见下方别名表），
    与 CLI 的写入映射保持同一份真相。
    """
    cols = set(_insert_columns(_CREATE_SQL))
    aliases = {"date": "trade_date", "ts": "blocked_at", "uid": "user_id", "tenant": "tenant_id"}
    rec = GhostRow(
        date="2026-09-18",
        rule_id="l1.position_cap",
        kind="veto",
        tenant="default",
        uid="10000001",
        symbol="SH600000",
        side="buy",
        quantity=100.0,
        source="rebalance",
        reason="r",
    ).to_record()
    missing = sorted(aliases.get(k, k) for k in rec if aliases.get(k, k) not in cols)
    assert not missing, f"to_record() 的字段没有对应列（会静默丢数据）: {missing}"


def test_required_columns_are_not_nullable_by_accident():
    """这几列是查询/幂等的骨架，必须 NOT NULL。"""
    sql = _norm(_CREATE_SQL)
    for col in ("trade_date", "rule_id", "symbol", "side"):
        assert re.search(rf"{col} [a-z0-9()]+ not null", sql), f"{col} 应 NOT NULL"


def test_id_column_fits_every_ghost_id():
    row = GhostRow(
        date="2026-09-18",
        rule_id="l1.position_cap",
        kind="veto",
        tenant="default",
        uid="10000001",
        symbol="SH600000",
        side="buy",
        quantity=1.0,
        source="s",
        reason="r",
    )
    assert len(ghost_id(row)) <= ID_LEN, "幂等键比列宽还长 → 插入报错"
