"""影子代价账落库读写侧的不变量（P1.6）。

盯两件事，都是**静默**损坏（不报错、只让数悄悄变样）：

1. **重跑抽取不得抹掉已算好的价**。`extract` 反复跑，产出的行不带定价字段；若写入
   把它们并进同一条 SQL 无脑覆盖，第一次 `price` 之后再来一次 `extract` 就把整批
   价格清成 NULL，报表上只显示"未定价"。这里从三个层面钉：纯函数列集、**编译后的
   SQL 文本**、以及真库往返。
2. **瞬时列必须是 aware UTC**。naive 值进 `TIMESTAMPTZ` 会被 PG 按会话时区补时区，
   同一笔单可能落到相邻两天。
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timezone

import pytest
from sqlalchemy.dialects import postgresql

from backend.scripts.risk_ghost_store import (
    _DISCOVERY_COLS,
    _PRICING_COLS,
    _where,
    count_rows,
    delete_ids,
    delete_test_tenants,
    exclude_clause,
    from_record,
    list_test_tenants,
    load_rows,
    match_clause,
    rename_row_ids,
    pricing_values,
    row_values,
    update_cols,
    upsert_rows,
)
from backend.shared.ghost_ledger_contract import TABLE
from backend.shared.risk.ghost import GhostRow, ghost_id

PRICED_AT = "2026-09-24T08:00:00+08:00"
TEST_TENANT = "__test_ghost_store__"


def _row(**kw) -> GhostRow:
    base = {
        "date": "2026-09-18",
        "rule_id": "l1.position_cap",
        "kind": "veto",
        "tenant": TEST_TENANT,
        "uid": "10000001",
        "symbol": "SH600000",
        "side": "buy",
        "quantity": 900.0,
        "source": "tdx_l2",
        "reason": "集中度超限",
        "evidence": {"projected": 0.31},
        "enforced": False,
        "version": 2,
        "ts": 1790159202.039,
    }
    return GhostRow(**{**base, **kw})


def _priced_row(**kw) -> GhostRow:
    priced = {
        "entry_date": "2026-09-21",
        "entry_px": 10.0,
        "tradable": True,
        "fwd": {"t1": {"state": "ok", "cost": 0.02}},
        "priced_at": PRICED_AT,
    }
    return _row(**{**priced, **kw})


class _Result:
    """最小 Result 替身：`rowcount` 给 DELETE，`mappings()` 给 SELECT。

    真驱动两条都返回 Result（`.rowcount` 恒在），替身也该如此——少一个属性就会
    让"删了几行"这类读取在测试里换成 AttributeError 而不是被断言到。
    """

    def __init__(
        self, mapping: dict | None = None, rows: list | None = None, rowcount: int = 0
    ) -> None:
        self.rowcount = rowcount
        self._mapping = mapping or {}
        self._rows = list(rows or [])

    def mappings(self):  # noqa: ANN201
        return self

    def one(self):  # noqa: ANN201
        return self._mapping

    def all(self):  # noqa: ANN201
        return self._rows


class _FakeSession:
    """只记账不落库的会话：把发出去的语句原样留下来给断言看。"""

    def __init__(
        self, mapping: dict | None = None, rows: list | None = None, rowcount: int = 0
    ) -> None:
        self.stmts: list = []
        self.params: list = []
        self._result = _Result(mapping, rows, rowcount)

    async def execute(self, stmt, params=None):  # noqa: ANN001
        self.stmts.append(stmt)
        self.params.append(params)
        return self._result

    def sql_and_params(self) -> list[tuple[str, dict]]:
        out = []
        for s in self.stmts:
            c = s.compile(dialect=postgresql.dialect())
            out.append((str(c), dict(c.params)))
        return out


# ── 写纪律 1 的第一层：列集 ─────────────────────────────────────────
def test_unpriced_writes_never_carry_a_pricing_column():
    """本模块最关键的一条：未定价那拨的列集里不能有任何定价列。"""
    assert _PRICING_COLS, "反空洞：定价列集为空的话，下面两条断言就恒真了"
    assert not set(update_cols(priced=False)) & set(_PRICING_COLS)


def test_priced_writes_carry_every_pricing_column():
    assert set(_PRICING_COLS) <= set(update_cols(priced=True))


def test_both_paths_always_carry_every_discovery_column():
    for priced in (False, True):
        assert set(_DISCOVERY_COLS) <= set(update_cols(priced=priced))


# ── 写纪律 1 的第二层：发出去的 SQL 本身 ────────────────────────────
def _update_set_clause(sql: str) -> str:
    """取 `ON CONFLICT ... DO UPDATE SET` 之后的那一段（只看它有没有碰定价列）。"""
    assert "ON CONFLICT" in sql, "没有 ON CONFLICT 的语句说明 upsert 没生效"
    return sql.split("ON CONFLICT", 1)[1]


def test_unpriced_statement_does_not_mention_fwd_anywhere():
    """编译出来的 SQL 里连 `fwd` 这三个字母都不该出现（列不在 INSERT 列表里）。"""
    s = _FakeSession()
    asyncio.run(upsert_rows(s, [_row()]))
    assert len(s.stmts) == 1
    (sql, _), = s.sql_and_params()
    assert not re.search(r"\bfwd\b", sql), sql
    assert "entry_px" not in _update_set_clause(sql)


def test_mixed_batch_splits_into_two_statements_with_disjoint_ids():
    """**每行判断、不是每批判断**——否则同批未定价行的 fwd 会被 NULL 冲掉。"""
    a, b = _priced_row(symbol="SH600000"), _row(symbol="SH600036")
    s = _FakeSession()
    assert asyncio.run(upsert_rows(s, [a, b])) == 2
    assert len(s.stmts) == 2, "带价与不带价必须分成两条语句"
    (sql0, p0), (sql1, p1) = s.sql_and_params()
    assert not re.search(r"\bfwd\b", sql0), "先发的（未定价）那条不得碰定价列"
    assert re.search(r"\bfwd\b", _update_set_clause(sql1))
    assert ghost_id(b) in p0.values() and ghost_id(a) not in p0.values()
    assert ghost_id(a) in p1.values() and ghost_id(b) not in p1.values()


def test_an_all_priced_batch_is_one_statement():
    s = _FakeSession()
    assert asyncio.run(upsert_rows(s, [_priced_row(), _priced_row(symbol="SH600036")])) == 2
    assert len(s.stmts) == 1


def test_empty_batch_writes_nothing():
    s = _FakeSession()
    assert asyncio.run(upsert_rows(s, [])) == 0
    assert s.stmts == []


def test_duplicate_keys_collapse_before_hitting_the_database():
    """同一条语句里重复的键会让 PG 报 `cannot affect row a second time`。

    抽取/合并多来源时重复是常态，故必须在客户端先收口——保留最后写入的那条。
    """
    s = _FakeSession()
    rows = [_row(reason="先"), _row(reason="后")]
    assert asyncio.run(upsert_rows(s, rows)) == 1
    (_, params), = s.sql_and_params()
    reasons = [v for k, v in params.items() if k.startswith("reason_m")]
    assert reasons == ["后"]


# ── 行 ↔ 列 的映射 ─────────────────────────────────────────────────
def test_row_values_maps_domain_names_to_column_names():
    v = row_values(_row())
    assert v["trade_date"] == date(2026, 9, 18)
    assert v["tenant_id"] == TEST_TENANT and v["user_id"] == "10000001"
    assert v["id"] == ghost_id(_row())
    assert "fwd" not in v and "entry_px" not in v, "发现期字段不得越界带定价列"


def test_pricing_values_is_none_until_the_row_went_through_the_pricer():
    assert pricing_values(_row()) is None
    pv = pricing_values(_priced_row())
    assert pv is not None and pv["entry_px"] == 10.0
    assert pv["entry_date"] == date(2026, 9, 21)


def test_pricing_values_treats_a_price_only_row_as_unpriced():
    """`is_priced` 的判据是 `fwd`：只有价格没有 fwd 的行仍算未定价。"""
    assert pricing_values(_row(entry_px=10.0)) is None


def test_blocked_at_is_aware_utc():
    v = row_values(_row(ts=1790159202.039))
    assert v["blocked_at"].tzinfo is not None
    assert v["blocked_at"].utcoffset() == timezone.utc.utcoffset(None)
    assert v["blocked_at"].timestamp() == pytest.approx(1790159202.039)


def test_missing_timestamp_becomes_the_explicit_sentinel_not_now():
    """列为 NOT NULL；没 ts 就记哨兵，**不许**用写入时刻冒充（那会造出一个假时间）。"""
    from backend.scripts.risk_ghost_store import EPOCH_SENTINEL

    assert row_values(_row(ts=0))["blocked_at"] == EPOCH_SENTINEL
    assert row_values(_row(ts=None))["blocked_at"] == EPOCH_SENTINEL


def test_from_record_round_trips_a_priced_row():
    back = from_record(row_values(_priced_row()) | pricing_values(_priced_row()))
    assert back.date == "2026-09-18" and back.symbol == "SH600000"
    assert back.tenant == TEST_TENANT and back.enforced is False and back.version == 2
    assert back.entry_date == "2026-09-21" and back.entry_px == 10.0
    assert back.fwd == {"t1": {"state": "ok", "cost": 0.02}}
    assert back.ts == pytest.approx(1790159202.039, abs=0.01)


def test_from_record_maps_the_sentinel_back_to_zero():
    """哨兵读回来必须是"ts 不可信"，不是 1970 年那个时刻。"""
    assert from_record(row_values(_row(ts=0))).ts == 0.0


def test_from_record_tolerates_empty_and_dirty_values():
    back = from_record({"trade_date": None, "evidence": "not-a-dict", "fwd": [1, 2]})
    assert back.date == "" and back.evidence == {} and back.fwd is None
    assert back.kind == "veto" and back.tenant == "default"


def test_naive_timestamps_from_the_database_are_read_as_utc():
    """库里若给出了 naive 值，按 UTC 补——按本机时区猜会让窗口整体平移。"""
    got = from_record({"blocked_at": datetime(2026, 9, 18, 3, 0)})
    assert got.ts == datetime(2026, 9, 18, 3, 0, tzinfo=timezone.utc).timestamp()


# ── 过滤条件 ───────────────────────────────────────────────────────
def test_where_builds_only_the_requested_clauses():
    clause, params = _where(
        start="2026-09-01", end=None, regex="^l1\\.", only_unpriced=True, tenant=None
    )
    assert "trade_date >= :start" in clause and "priced_at IS NULL" in clause
    assert "trade_date <= :end" not in clause and "tenant_id" not in clause
    assert params == {"start": "2026-09-01", "regex": "^l1\\."}


def test_where_is_empty_without_filters():
    assert _where(start=None, end=None, regex=None, only_unpriced=False, tenant=None) == ("", {})


def test_match_clause_is_the_dual_of_exclude_clause():
    """清理用 `= 1`（命中），读侧用 `<> 1`（排除）——同一套前缀、同一套参数名。"""
    pe: dict = {}
    pm: dict = {}
    ex = exclude_clause(["t-hotset"], pe)
    mt = match_clause(["t-hotset"], pm)
    assert mt == [c.replace("<>", "=") for c in ex], (mt, ex)
    assert pe == pm == {"xt0": "t-hotset"}


def test_delete_test_tenants_refuses_an_empty_prefix_list():
    """空清单下这条 DELETE 会命中全表——必须硬拦，不能"没前缀就当没找到"。"""
    s = _FakeSession()
    with pytest.raises(ValueError, match="拒绝无前缀删除"):
        asyncio.run(delete_test_tenants(s, []))
    assert s.stmts == [], "拒绝时不许把语句发出去"


def test_list_test_tenants_with_no_prefixes_asks_nothing():
    assert asyncio.run(list_test_tenants(_FakeSession(), [])) == []


def test_cleanup_statements_join_prefixes_with_or():
    """多个前缀是"命中任一个"——用 AND 连的话永远匹配不到任何租户。"""
    s = _FakeSession()
    asyncio.run(delete_test_tenants(s, ["t-hotset", "_t_p206"]))
    sql = s.stmts[0].text
    assert " OR " in sql and " AND " not in sql.split("WHERE")[1], sql
    assert s.params[0] == {"xt0": "t-hotset", "xt1": "_t_p206"}


def test_exclude_clause_uses_strpos_not_like():
    """前缀里带 `_`（`_t_p206`），而 `_` 在 LIKE 里是通配符——用会误伤别的租户。"""
    params: dict = {}
    got = exclude_clause(["_t_p206", "t-hotset"], params)
    assert len(got) == 2
    assert all("strpos(tenant_id," in c and "<> 1" in c for c in got)
    assert "LIKE" not in " ".join(got)
    assert params == {"xt0": "_t_p206", "xt1": "t-hotset"}


def test_load_rows_excludes_test_tenants_by_default():
    """存量脏行只靠抽取侧拦不住（它们早就在库里了）——读侧必须也排。"""
    from backend.shared.risk.ghost import test_tenant_prefixes

    pref = test_tenant_prefixes()
    assert pref, "反空洞：清单为空的话下面两条断言就恒真了"
    s = _FakeSession()
    asyncio.run(load_rows(s))
    assert s.stmts[0].text.count("strpos(tenant_id,") == len(pref)
    assert set(pref) <= set(s.params[0].values())


def test_load_rows_can_be_told_to_include_test_tenants():
    s = _FakeSession()
    asyncio.run(load_rows(s, include_test_tenants=True))
    assert "strpos(tenant_id," not in s.stmts[0].text
    assert s.params == [{}]


_COUNT_ROW = {"n": 3, "n_priced": 2, "n_enforced": 0, "n_rules": 1, "d0": None, "d1": None}


def test_count_rows_is_table_wide_by_default():
    s = _FakeSession(_COUNT_ROW)
    assert asyncio.run(count_rows(s))["rows"] == 3
    # 只看表名后面有没有 WHERE：SQL 里本来就有 `FILTER (WHERE enforced)`
    assert f"{TABLE} WHERE" not in s.stmts[0].text, s.stmts[0].text
    assert s.params == [{}]


def test_count_rows_can_be_scoped_to_one_tenant():
    """测试必须能只数自己那个租户——否则库里任何别的行都能让断言失真。"""
    s = _FakeSession(_COUNT_ROW)
    assert asyncio.run(count_rows(s, tenant=TEST_TENANT))["rows"] == 3
    assert "WHERE tenant_id = :tenant" in s.stmts[0].text
    assert s.params == [{"tenant": TEST_TENANT}]


def test_delete_ids_uses_an_expanding_bindparam():
    """`IN :ids` 必须 expanding——asyncpg 不吃裸 Python 列表。"""
    s = _FakeSession()
    asyncio.run(delete_ids(s, ["a", "b"]))
    assert "IN " in s.stmts[0].text
    assert s.params == [{"ids": ["a", "b"]}]


def test_delete_ids_with_nothing_to_do_sends_nothing():
    s = _FakeSession()
    assert asyncio.run(delete_ids(s, [])) == 0
    assert s.stmts == []


def test_rename_row_ids_counts_each_row_and_never_batches():
    """批量执行时驱动给的 `rowcount` 是 -1（"不知道"）。

    调用方拿这个数做"实际改动==计划"的守卫——真跑迁移时它确实报过一次 -1，
    把一次做对了的迁移标成可疑。宁可行数少一次往返，也不能给一个假数字。
    """
    s = _FakeSession(rowcount=1)
    got = asyncio.run(rename_row_ids(s, [("a", "A", "SH600036"), ("b", "B", "SH600000")]))
    assert got == 2, "返回的必须是**实际**改动行数"
    assert len(s.stmts) == 2, "逐行发语句；批量拿不到真实改动数"


def test_a_row_that_did_not_match_does_not_count_as_renamed():
    """并发下目标行可能已经不在——那时改动数必须小于计划数（守卫要靠这个）。"""
    s = _FakeSession(rowcount=0)
    assert asyncio.run(rename_row_ids(s, [("a", "A", "SH600036")])) == 0


def test_rename_row_ids_touches_only_the_key_and_the_symbol():
    """改名**不得**碰定价列——这就是"能保住就绝不重建"的落点。"""
    s = _FakeSession(rowcount=1)
    asyncio.run(rename_row_ids(s, [("old", "new", "SH600036")]))
    sql = s.stmts[0].text
    assert "SET id = :new_id, symbol = :symbol" in sql
    assert "WHERE id = :old_id" in sql
    for col in _PRICING_COLS:
        assert col not in sql, f"改名碰到了定价列 {col}"
    assert s.params == [{"old_id": "old", "new_id": "new", "symbol": "SH600036"}]


# ── 真库往返（不可用则 skip，不假过）────────────────────────────────
async def _cleanup(session) -> None:
    from sqlalchemy import text

    await session.execute(
        text("DELETE FROM qm_risk_ghost_ledger WHERE tenant_id = :t"), {"t": TEST_TENANT}
    )


@pytest.mark.asyncio
async def test_real_db_rekey_preserves_prices_and_collapses_duplicates():
    """迁移的**唯一风险**是丢数据——真跑一遍：改名保价、撞键并价、删掉重复。

    这是 2026-09-23 加标的归一之后的一次性迁移（实测真账 495 行非规范、83 行撞键）。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import close_database, get_session
    from backend.shared.ghost_ledger_contract import ensure_ghost_ledger_table_async
    from backend.shared.risk.ghost import normalize_symbol, plan_rekey

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")
    try:
        assert await ensure_ghost_ledger_table_async()
        async with get_session() as session:
            await _cleanup(session)
            await upsert_rows(
                session,
                [
                    _priced_row(symbol="600036.SH"),  # 独自一行 → 只改名
                    _priced_row(symbol="600000.SH", entry_px=7.7),  # 与下一行撞键
                    _row(symbol="SH600000"),  # 规范写法但**没有价**
                ],
            )
            await session.commit()

        async with get_session(read_only=True) as session:
            before = await load_rows(session, tenant=TEST_TENANT)
        plan = plan_rekey(before)
        assert len(plan.renames) == 1 and len(plan.doomed) == 1 and len(plan.carried) == 1

        async with get_session() as session:
            await upsert_rows(session, list(plan.carried))
            assert await delete_ids(session, list(plan.doomed)) == 1
            assert await rename_row_ids(session, list(plan.renames)) == 1
            await session.commit()

        async with get_session(read_only=True) as session:
            after = await load_rows(session, tenant=TEST_TENANT)
        by_symbol = {r.symbol: r for r in after}
        assert sorted(by_symbol) == ["SH600000", "SH600036"], f"落点不对: {sorted(by_symbol)}"
        assert all(normalize_symbol(r.symbol) == r.symbol for r in after)

        renamed = by_symbol["SH600036"]
        assert renamed.entry_px == pytest.approx(10.0), "改名把价弄丢了"
        assert renamed.fwd and renamed.priced_at, "改名把定价列弄丢了"
        assert renamed.to_record()["id"] == ghost_id(_row(symbol="SH600036")), "改名落点不对"

        survivor = by_symbol["SH600000"]
        assert survivor.entry_px == pytest.approx(7.7), "撞键行被删之前，价没补给保留者"
        assert survivor.fwd, "保留者仍无价——等于把这笔单的定价丢了"
    finally:
        async with get_session() as session:
            await _cleanup(session)
            await session.commit()
        await close_database()


@pytest.mark.asyncio
async def test_real_db_purge_removes_only_the_test_tenants():
    """清理命令的**唯一风险**是把真账一起删掉——用一个假前缀真跑一遍来钉它。

    两个租户名必须**互不为前缀**（`__test_ghost_store__` 与 `__test_ghost_purge__`
    在第三段就分岔）：若"幸存者"以被删前缀开头，这测试自己就变成了反例。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import close_database, get_session
    from backend.shared.ghost_ledger_contract import ensure_ghost_ledger_table_async

    purge_prefix = "__test_ghost_purge__"
    doomed, keep = f"{purge_prefix}a", TEST_TENANT  # keep 是"真账"的替身
    assert not keep.startswith(purge_prefix), "幸存者不能命中被删前缀"

    async def _wipe(session) -> None:
        await session.execute(
            text("DELETE FROM qm_risk_ghost_ledger WHERE tenant_id IN (:a, :b)"),
            {"a": doomed, "b": keep},
        )

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")
    try:
        assert await ensure_ghost_ledger_table_async()
        async with get_session() as session:
            await _wipe(session)
            await upsert_rows(
                session,
                [
                    _row(tenant=doomed, symbol="SH600000"),
                    _row(tenant=keep, symbol="SH600036"),
                ],
            )
            await session.commit()

        async with get_session(read_only=True) as session:
            found = await list_test_tenants(session, [purge_prefix])
        assert [t for t, _ in found] == [doomed], found

        async with get_session() as session:
            n = await delete_test_tenants(session, [purge_prefix])
            await session.commit()
        assert n == 1

        async with get_session(read_only=True) as session:
            assert await list_test_tenants(session, [purge_prefix]) == []
            survivor = await load_rows(session, tenant=keep)
        assert len(survivor) == 1 and survivor[0].symbol == "SH600036", "清理误伤了真账"
    finally:
        async with get_session() as session:
            await _wipe(session)
            await session.commit()
        await close_database()


@pytest.mark.asyncio
async def test_real_db_round_trip_keeps_prices_and_refreshes_discovery():
    """**本模块存在的理由**：定价后重跑抽取，价必须还在；同一次跑里正向也要验。

    两个方向都验，是因为"拒写"太容易做过头——真正的规则是"未定价的写入不碰定价列"，
    不是"定价列一律不变"。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import close_database, get_session
    from backend.shared.ghost_ledger_contract import ensure_ghost_ledger_table_async

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")
    try:
        assert await ensure_ghost_ledger_table_async(), "影子账表未能建起"

        async with get_session() as session:
            await _cleanup(session)
            await upsert_rows(session, [_priced_row()])
            await session.commit()

        async with get_session() as session:
            # 同一笔单，重跑抽取：这一行**不带**定价字段
            await upsert_rows(session, [_row(reason="重跑抽取")])
            await session.commit()

        async with get_session() as session:
            rows = await load_rows(session, tenant=TEST_TENANT)
            got = await count_rows(session, tenant=TEST_TENANT)
        assert got["rows"] == 1, f"本测试应恰好留 1 行: {got}"

        r = rows[0]
        assert r.reason == "重跑抽取", "发现期字段应被刷新"
        assert r.fwd == {"t1": {"state": "ok", "cost": 0.02}}, "重跑抽取把已算好的价抹掉了"
        assert r.entry_px == 10.0 and r.entry_date == "2026-09-21"

        # 正向：真跑了定价，库里就该是新值
        async with get_session() as session:
            await upsert_rows(session, [_priced_row(entry_px=12.0)])
            await session.commit()
        async with get_session() as session:
            again = await load_rows(session, tenant=TEST_TENANT)
        assert len(again) == 1 and again[0].entry_px == pytest.approx(12.0)
    finally:
        async with get_session() as session:
            await _cleanup(session)
            await session.commit()
        await close_database()
