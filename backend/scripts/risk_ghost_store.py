"""影子代价账落库读写侧（P1.6）——`GhostRow` ↔ `qm_risk_ghost_ledger`。

本模块是影子账**唯一的持久化出入口**（CLI、报表、巡检都经它）。表结构见
`backend/shared/ghost_ledger_contract.py`（与 `db_init.sql` 同口径，有测试逐语句守着）。

写纪律 1：重跑抽取不得抹掉已算好的价
-----------------------------------
`extract` 会反复跑（每天、每次补留痕），它产出的行**没有**定价字段。若 upsert 用一句
SQL 无脑覆盖，第一次 `price` 之后再跑一次 `extract` 就把整批价格清成 NULL，而报表上
只会显示"未定价"——看起来像"还没跑定价"，实际是被人抹了，**且不会报任何错**。

故写入按行**分成两拨**（`:func:`upsert_rows`）：带定价的行连同定价列一起写，不带的
只碰发现期字段。`update_cols()` 是这条纪律的唯一表达处，测试直接断言"未定价那拨的
列集里没有任何定价列"。**注意必须是每行判断，不是每批**：按批判断的话，一批里只要有
一行带价，同批其它行的 `fwd` 就会被 `excluded.fwd = NULL` 冲掉。

写纪律 2：`blocked_at` 走 aware UTC
----------------------------------
留痕里是 epoch 秒（绝对时刻），但写进 `TIMESTAMPTZ` 必须是 aware：naive 值会被 PG 按
会话时区补一个时区，跨时区部署会把同一笔单记到相邻两天。列为 NOT NULL，留痕没带 ts
时记 1970-01-01 哨兵（显式写出"这个时间不可信"，而不是偷偷用写入时刻冒充）。

写纪律 3：读侧默认排掉测试租户
--------------------------------
集成测试会真往决策流里写（实测 845 行里 230 行是这么来的，见 `ghost.py` 模块头②），
且它们在库里**没有标记**——与真账同形同表。抽取侧拒收只挡得住将来，挡不住存量，
故 `load_rows` 默认按前缀排除。排除必须在**读侧唯一入口**做：报表、定价、巡检
都走它，漏一处就会有一张报表悄悄多出几百个假样本。
`_where` 用 `strpos(...) <> 1` 而非 `LIKE`——前缀里带 `_`，LIKE 会误伤。

读侧一律返回 `GhostRow`（不是 dict），让定价器与报表只认一种形状。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.shared.ghost_ledger_contract import TABLE
from backend.shared.risk.ghost import GhostRow, ghost_id, test_tenant_prefixes

logger = logging.getLogger(__name__)

#: 发现期字段（每次抽取都可刷新）
_DISCOVERY_COLS: tuple[str, ...] = (
    "tenant_id",
    "user_id",
    "trade_date",
    "rule_id",
    "kind",
    "registered",
    "symbol",
    "side",
    "quantity",
    "source",
    "reason",
    "evidence",
    "enforced",
    "version",
    "blocked_at",
)

#: 定价期字段（**只在行确已定过价时**写入，见模块头写纪律 1）
_PRICING_COLS: tuple[str, ...] = ("entry_date", "entry_px", "tradable", "fwd", "priced_at")

_ALL_COLS: tuple[str, ...] = ("id", *_DISCOVERY_COLS, *_PRICING_COLS)

#: 留痕没带 ts 时的哨兵（列为 NOT NULL；见模块头写纪律 2）
EPOCH_SENTINEL = datetime(1970, 1, 1, tzinfo=timezone.utc)


def update_cols(*, priced: bool) -> tuple[str, ...]:
    """upsert 命中已有行时要覆盖的列——**写纪律 1 的唯一表达处**。

    未定价的行**绝不能带上 `_PRICING_COLS`**：那会把库里已算好的价冲成 NULL。
    """
    return _DISCOVERY_COLS + (_PRICING_COLS if priced else ())


def _blocked_at(ts: Any) -> datetime:
    """epoch 秒 → aware UTC（≤0 记哨兵，见模块头写纪律 2）。"""
    try:
        f = float(ts)
    except (TypeError, ValueError):
        return EPOCH_SENTINEL
    return datetime.fromtimestamp(f, tz=timezone.utc) if f > 0 else EPOCH_SENTINEL


def _parse_dt(v: Any) -> datetime | None:
    """库里的时间列 → aware datetime（naive 一律按 UTC 补，不按本机时区猜）。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_date(v: Any) -> date | None:
    """`YYYY-MM-DD` → `datetime.date`。

    必须显式转：驱动（asyncpg）对 DATE 参数只认 `datetime.date`，传字符串会
    直接抛类型错；而库里读回来也可能是 `date` 或字符串，两种都要能吃。
    """
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def row_values(row: GhostRow) -> dict[str, Any]:
    """`GhostRow` → 表列（含 id）。**只含发现期字段**，定价字段由 `pricing_values` 出。"""
    return {
        "id": ghost_id(row),
        "tenant_id": row.tenant or "default",
        "user_id": row.uid or "",
        "trade_date": _to_date(row.date),
        "rule_id": row.rule_id,
        "kind": row.kind,
        "registered": bool(row.registered),
        "symbol": row.symbol,
        "side": row.side,
        "quantity": row.quantity,
        "source": row.source or "",
        "reason": (row.reason or "")[:200],
        "evidence": row.evidence or {},
        "enforced": bool(row.enforced),
        "version": int(row.version or 0),
        "blocked_at": _blocked_at(row.ts),
    }


def pricing_values(row: GhostRow) -> dict[str, Any] | None:
    """定价列；**没走过定价器的行返回 None**（调用方据此不把它算进"带价那拨"）。"""
    if not row.fwd:
        return None
    return {
        "entry_date": _to_date(row.entry_date),
        "entry_px": row.entry_px,
        "tradable": row.tradable,
        "fwd": row.fwd,
        "priced_at": _parse_dt(row.priced_at),
    }


def from_record(rec: Any) -> GhostRow:
    """表行（RowMapping / 普通映射）→ `GhostRow`（列名别名在此收口）。"""
    m = dict(rec)
    ts = _parse_dt(m.get("blocked_at"))
    evidence, fwd = m.get("evidence"), m.get("fwd")
    priced_at = _parse_dt(m.get("priced_at"))
    entry_date = m.get("entry_date")
    return GhostRow(
        date=str(m.get("trade_date") or "")[:10],
        rule_id=str(m.get("rule_id") or ""),
        kind=str(m.get("kind") or "veto"),
        tenant=str(m.get("tenant_id") or "default"),
        uid=str(m.get("user_id") or ""),
        symbol=str(m.get("symbol") or ""),
        side=str(m.get("side") or ""),
        quantity=m.get("quantity"),
        source=str(m.get("source") or ""),
        reason=str(m.get("reason") or ""),
        evidence=dict(evidence) if isinstance(evidence, dict) else {},
        enforced=bool(m.get("enforced")),
        version=int(m.get("version") or 0),
        # 哨兵读回来即"ts 不可信" → 0.0（与 ghost.py 的哨兵语义对齐）
        ts=0.0 if ts is None or ts == EPOCH_SENTINEL else ts.timestamp(),
        registered=bool(m.get("registered", True)),
        entry_date=str(entry_date)[:10] if entry_date is not None else None,
        entry_px=m.get("entry_px"),
        tradable=m.get("tradable"),
        fwd=dict(fwd) if isinstance(fwd, dict) else None,
        priced_at=priced_at.isoformat() if priced_at else None,
    )


def _table() -> Any:
    """`qm_risk_ghost_ledger` 的轻量 Table（纯台账表，不建 ORM 模型），只建一次。"""
    global _TABLE
    if _TABLE is not None:
        return _TABLE
    from sqlalchemy import Column, MetaData, Table
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.types import Boolean, Date, DateTime, Float, Integer, String

    cols = [
        Column("id", String(32), primary_key=True),
        Column("tenant_id", String(64)),
        Column("user_id", String(64)),
        Column("trade_date", Date),
        Column("rule_id", String(64)),
        Column("kind", String(16)),
        Column("registered", Boolean),
        Column("symbol", String(32)),
        Column("side", String(8)),
        Column("quantity", Float),
        Column("source", String(32)),
        Column("reason", String(200)),
        Column("evidence", JSONB),
        Column("enforced", Boolean),
        Column("version", Integer),
        Column("blocked_at", DateTime(timezone=True)),
        Column("entry_date", Date),
        Column("entry_px", Float),
        Column("tradable", Boolean),
        Column("fwd", JSONB),
        Column("priced_at", DateTime(timezone=True)),
        Column("created_at", DateTime(timezone=True)),
        Column("updated_at", DateTime(timezone=True)),
    ]
    _TABLE = Table(TABLE, MetaData(), *cols)
    return _TABLE


_TABLE: Any = None


def _dedup(rows: Sequence[GhostRow]) -> list[GhostRow]:
    """按幂等键去重（后写覆盖先写）。

    `ON CONFLICT` 对**同一条语句里**重复的键会报 `cannot affect row a second time`，
    而抽取/合并多来源时重复是常态而非异常，故在客户端先收口。
    """
    by_id: dict[str, GhostRow] = {}
    for r in rows:
        by_id[ghost_id(r)] = r
    return list(by_id.values())


async def _upsert(session: Any, rows: Sequence[GhostRow], *, priced: bool) -> int:
    """一拨写入（`priced` 决定要不要带上定价列，见模块头写纪律 1）。"""
    if not rows:
        return 0
    cols = update_cols(priced=priced)
    prepared: list[dict[str, Any]] = []
    for r in rows:
        vals = row_values(r)
        if priced:
            pv = pricing_values(r)
            if pv is None:  # 防御：带价那拨不该混进未定价的行
                continue
            vals.update(pv)
        prepared.append(vals)
    if not prepared:
        return 0
    stmt = pg_insert(_table()).values(prepared)
    set_: dict[str, Any] = {c: getattr(stmt.excluded, c) for c in cols}
    set_["updated_at"] = text("NOW()")
    await session.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=set_))
    return len(prepared)


async def upsert_rows(session: Any, rows: Sequence[GhostRow]) -> int:
    """写入/刷新影子账行（幂等，按 `ghost_id`）。返回写入的行数。

    分两拨（带价 / 不带价）各发一条语句——**不能按批合并成一条**：那
    一批里只要有一行带价，同批其它行的 `fwd` 就会被 `excluded.fwd = NULL` 冲掉。
    """
    if not rows:
        return 0
    uniq = _dedup(rows)
    priced = [r for r in uniq if r.fwd]
    unpriced = [r for r in uniq if not r.fwd]
    n = await _upsert(session, unpriced, priced=False)
    n += await _upsert(session, priced, priced=True)
    return n


def _prefix_clause(
    prefixes: Sequence[str], params: dict[str, Any], *, op: str, offset: int = 0
) -> list[str]:
    """`strpos(tenant_id, :xN) {op} 1` 的若干子句（`op` 取 `<>` 排除 / `=` 命中）。

    **不用 `LIKE 'x%'`**：测试租户前缀里带 `_`（如 `_t_p206`），而 `_` 在 LIKE 里是
    单字符通配，`_t_p206` 会连 `atbp206…` 一起排掉。`strpos` 是精确子串定位，
    且前缀一律走参数，不拼进 SQL 文本。
    """
    out: list[str] = []
    for i, p in enumerate(prefixes):
        key = f"xt{offset + i}"
        out.append(f"strpos(tenant_id, :{key}) {op} 1")
        params[key] = p
    return out


def exclude_clause(
    prefixes: Sequence[str], params: dict[str, Any], *, offset: int = 0
) -> list[str]:
    """按前缀**排除**租户的子句（读侧默认用它）。"""
    return _prefix_clause(prefixes, params, op="<>", offset=offset)


def match_clause(
    prefixes: Sequence[str], params: dict[str, Any], *, offset: int = 0
) -> list[str]:
    """按前缀**命中**租户的子句（清理用；调用方须自行用 `OR` 连接）。"""
    return _prefix_clause(prefixes, params, op="=", offset=offset)


def _where(
    *,
    start: str | None,
    end: str | None,
    regex: str | None,
    only_unpriced: bool,
    tenant: str | None,
    exclude_tenants: Sequence[str] = (),
) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if start:
        clauses.append("trade_date >= :start")
        params["start"] = start
    if end:
        clauses.append("trade_date <= :end")
        params["end"] = end
    if regex:
        clauses.append("rule_id ~ :regex")
        params["regex"] = regex
    if only_unpriced:
        clauses.append("priced_at IS NULL")
    if tenant:
        clauses.append("tenant_id = :tenant")
        params["tenant"] = tenant
    clauses.extend(exclude_clause(exclude_tenants, params))
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


async def load_rows(
    session: Any,
    *,
    start: str | None = None,
    end: str | None = None,
    regex: str | None = None,
    only_unpriced: bool = False,
    tenant: str | None = None,
    include_test_tenants: bool = False,
    limit: int | None = None,
) -> list[GhostRow]:
    """读影子账行（按日、规则、标的、方向升序——顺序稳定才可比、可 diff）。

    默认**不含测试租户**——那些行是集成测试跑出来的，从来没对应过一笔真单
    （见 `ghost.py` 模块头②）。抽取侧已拒收，但**库里存量已经脏了**，只靠抽取侧
    挡不住，故读侧也排一次：报表/定价的入口只有这里。
    """
    where, params = _where(
        start=start,
        end=end,
        regex=regex,
        only_unpriced=only_unpriced,
        tenant=tenant,
        exclude_tenants=() if include_test_tenants else test_tenant_prefixes(),
    )
    sql = f"SELECT {', '.join(_ALL_COLS)} FROM {TABLE}{where}"  # noqa: S608 - 表名/列名皆为模块常量
    sql += " ORDER BY trade_date, rule_id, symbol, side"
    if limit:
        sql += f" LIMIT {int(limit)}"
    res = await session.execute(text(sql), params)
    return [from_record(r) for r in res.mappings().all()]


async def count_rows(
    session: Any,
    *,
    tenant: str | None = None,
    exclude_tenants: Sequence[str] = (),
) -> dict[str, int]:
    """体检计数（CLI `status` 用）：总数 / 已定价 / 翻闸后 / 规则数。

    `tenant` 传了就只数该租户——不传是**全表**。测试必须传：否则断言会被库里
    任何别的行（比如真账）满足或打破，看着过了其实什么都没验。
    `exclude_tenants` 不传**不排除任何租户**（与 `load_rows` 的默认相反）：
    本函数就是拿来看"库里到底有什么"的，那两拨数之间的**差**正是脏行数。
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if tenant:
        clauses.append("tenant_id = :tenant")
        params["tenant"] = tenant
    clauses.extend(exclude_clause(exclude_tenants, params))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    res = await session.execute(
        text(
            f"SELECT COUNT(*) AS n, "  # noqa: S608 - 表名是模块常量
            f"COUNT(priced_at) AS n_priced, "
            f"COUNT(*) FILTER (WHERE enforced) AS n_enforced, "
            f"COUNT(DISTINCT rule_id) AS n_rules, "
            f"MIN(trade_date) AS d0, MAX(trade_date) AS d1 FROM {TABLE}{where}"
        ),
        params,
    )
    m = res.mappings().one()
    return {
        "rows": int(m["n"] or 0),
        "priced": int(m["n_priced"] or 0),
        "enforced": int(m["n_enforced"] or 0),
        "rules": int(m["n_rules"] or 0),
        "first_date": str(m["d0"] or "") or "",
        "last_date": str(m["d1"] or "") or "",
    }


async def delete_ids(session: Any, ids: Sequence[str]) -> int:
    """按主键批量删。`IN :ids` 必须用 expanding bindparam——asyncpg 不吃裸列表。"""
    if not ids:
        return 0
    stmt = text(f"DELETE FROM {TABLE} WHERE id IN :ids").bindparams(  # noqa: S608 - 表名是模块常量
        bindparam("ids", expanding=True)
    )
    res = await session.execute(stmt, {"ids": list(ids)})
    return int(res.rowcount or 0)


async def rename_row_ids(
    session: Any, pairs: Sequence[tuple[str, str, str]]
) -> int:
    """就地改主键与标的：``(旧 id, 新 id, 规范标的)``。返回**实际**改动的行数。

    **不改任何其它列**——定价字段原样留着（这正是"能保住就绝不重建"的落点）。
    调用方必须先把撞键的旧行删干净，否则改名会撞上还在占着新键的那一行。

    **逐行发语句，不用 executemany**：批量执行下驱动返回的 `rowcount` 是 ``-1``
    （"不知道"，实测 asyncpg 如此），而调用方正是拿这个数做"实际改动==计划"的守卫
    ——用 -1 去比会把一次**做对了的**迁移报成可疑，反过来也等于没有守卫。
    这个函数只服务于一次性迁移（数百行），逐行的代价可以忽略。
    """
    if not pairs:
        return 0
    stmt = text(
        f"UPDATE {TABLE} SET id = :new_id, symbol = :symbol, updated_at = NOW() "  # noqa: S608
        "WHERE id = :old_id"
    )
    n = 0
    for old_id, new_id, symbol in pairs:
        res = await session.execute(
            stmt, {"old_id": old_id, "new_id": new_id, "symbol": symbol}
        )
        n += int(getattr(res, "rowcount", 0) or 0)
    return n


async def list_test_tenants(
    session: Any, prefixes: Sequence[str]
) -> list[tuple[str, int]]:
    """库里**命中**这些前缀的租户及行数（清理前的清单；空清单 → 空结果）。"""
    if not prefixes:
        return []
    params: dict[str, Any] = {}
    clauses = match_clause(prefixes, params)
    res = await session.execute(
        text(
            f"SELECT tenant_id, COUNT(*) AS n FROM {TABLE} "  # noqa: S608 - 表名是模块常量
            f"WHERE {' OR '.join(clauses)} GROUP BY tenant_id ORDER BY tenant_id"
        ),
        params,
    )
    return [(str(m["tenant_id"]), int(m["n"])) for m in res.mappings().all()]


async def delete_test_tenants(session: Any, prefixes: Sequence[str]) -> int:
    """删掉命中前缀的租户的全部行，返回删除条数。

    **空清单直接抛**：一条没有 `WHERE` 的 DELETE 会清掉全表，而调用方（CLI）恰恰
    是拿"你没配前缀"这种状态跑起来的——这里必须硬拦，不能"没前缀就当没找到"。
    """
    if not prefixes:
        raise ValueError("拒绝无前缀删除：prefixes 为空时这条 DELETE 会命中全表")
    params: dict[str, Any] = {}
    clauses = match_clause(prefixes, params)
    res = await session.execute(
        text(f"DELETE FROM {TABLE} WHERE {' OR '.join(clauses)}"),  # noqa: S608 - 表名是模块常量
        params,
    )
    return int(res.rowcount or 0)


__all__ = [
    "EPOCH_SENTINEL",
    "delete_ids",
    "delete_test_tenants",
    "exclude_clause",
    "list_test_tenants",
    "match_clause",
    "count_rows",
    "from_record",
    "load_rows",
    "pricing_values",
    "rename_row_ids",
    "row_values",
    "update_cols",
    "upsert_rows",
]
