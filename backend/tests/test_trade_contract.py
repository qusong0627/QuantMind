"""``trade_contract``：成交唯一键（P2.7 分账的双记防线）。

分账账本把「成交 → 记账」做成同事务的原子动作，幂等键是券商成交号——而这个幂等
**建立在成交行本身只落一次**之上。已知的窗口是「commit 成功、进程以为失败」：写入方
都是 SELECT-then-INSERT，重投把同一份 fields 原样 xadd 回原流，于是同一笔成交可能落
两行 ⇒ 两次 ``apply_fill`` ⇒ 虚拟现金凭空多一笔。防线是 DB 层的部分唯一索引
``uq_trades_scope_exchange_trade_id``（带租户/用户：券商成交号只在一个账户内唯一）。

本文件三层一起钉：DDL（``db_init.sql``）、启动接线（``trade/main.py``）、行为
（预检拒绝 / 快路径不 DDL / 真库重复插入被拦）。
"""

from __future__ import annotations

import pytest

from backend.shared.trade_contract import (
    DUP_FILLS_SQL,
    SYNTH_TRADE_PREFIX,
    TRADE_UNIQUE_INDEX,
)


class _FakeResult:
    """同时支持 ``fetchone()`` / ``fetchall()`` 的最小结果集。"""

    def __init__(self, rows: list | None = None) -> None:
        self._rows = list(rows or [])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeSession:
    def __init__(
        self, *, index_exists: bool, dupes: list | None = None, fail_ddl=False
    ):
        self.index_exists = index_exists
        self.dupes = list(dupes or [])
        self.fail_ddl = fail_ddl
        self.statements: list[str] = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if "pg_indexes" in sql:
            return _FakeResult([(1,)] if self.index_exists else [])
        if sql.startswith("CREATE UNIQUE INDEX"):  # 先于重复预检判分支：CREATE 自带
            if self.fail_ddl:  # WHERE exchange_trade_id IS NOT NULL
                raise RuntimeError("permission denied for schema public")
            self.index_exists = True
            return _FakeResult([])
        if "HAVING count(*) > 1" in sql or "exchange_trade_id IS NOT NULL" in sql:
            return _FakeResult(self.dupes)
        return _FakeResult([])

    async def commit(self):
        self.committed = True


def _patch_sessions(monkeypatch, *sessions):
    """按调用次序发假 session（``get_session`` 是函数内延迟导入的）。"""
    import backend.shared.database_manager_v2 as dbm

    queue = list(sessions)

    def _get_session(read_only: bool = True):
        return queue.pop(0) if queue else sessions[-1]

    monkeypatch.setattr(dbm, "get_session", _get_session)


@pytest.fixture(autouse=True)
def _reset_index_cache():
    """模块级 ``_trade_index_ready`` 缓存跨用例复位（否则「拒绝」用例被上一轮的快路径吞掉）。"""
    from backend.shared import trade_contract as mod

    mod._trade_index_ready = None
    yield
    mod._trade_index_ready = None


# --- 契约接线（源码守卫） ---------------------------------------------------


def test_ddl_declares_partial_unique_index() -> None:
    """新装（db_init.sql）必须自带这个索引——否则全新部署从第一天起就没有防线。"""
    import pathlib

    ddl = pathlib.Path("backend/shared/db_init.sql").read_text(encoding="utf-8")
    assert TRADE_UNIQUE_INDEX in ddl
    idx = ddl.index(f"CREATE UNIQUE INDEX IF NOT EXISTS {TRADE_UNIQUE_INDEX}")
    block = ddl[idx : idx + 400]
    assert "ON trades (tenant_id, user_id, exchange_trade_id)" in block
    assert "WHERE exchange_trade_id IS NOT NULL" in block


def test_trade_startup_ensures_the_index() -> None:
    """老库升级路径：trade 服务启动期 ensure（与其它契约列同款，失败只告警不阻断）。"""
    import pathlib

    src = pathlib.Path("backend/services/trade/main.py").read_text(encoding="utf-8")
    assert "ensure_trade_unique_index_async" in src
    assert "from backend.shared.trade_contract import" in src


def test_synth_prefix_has_a_single_source() -> None:
    """合成成交前缀只有一处字面量：生产者按常量赋值，不许各写一份。"""
    import inspect

    from backend.services.live_trading.services import qmt_exec_poller as poller

    assert poller._SYNTH_TRADE_PREFIX is SYNTH_TRADE_PREFIX
    src = inspect.getsource(poller)
    assert "'qmt-synth-'" not in src and '"qmt-synth-"' not in src


def test_health_check_reuses_the_shared_sql_and_index_name() -> None:
    """体检 C14 与唯一键预检共用同一份 SQL/索引名（口径不许两处各写一份）。"""
    import inspect

    from backend.scripts.diagnose import health

    src = inspect.getsource(health.check_c14_agent_ledger_parity)
    assert "DUP_FILLS_SQL" in src and "TRADE_UNIQUE_INDEX" in src
    assert "GROUP BY tenant_id, user_id, exchange_trade_id" not in src


def test_dup_sql_groups_by_the_same_scope_as_the_index() -> None:
    """预检口径必须与索引口径逐字一致，否则「预检说干净、建索引却失败」。"""
    for needle in (
        "tenant_id, user_id, exchange_trade_id",
        "exchange_trade_id IS NOT NULL",
        "HAVING count(*) > 1",
    ):
        assert needle in DUP_FILLS_SQL


# --- ensure 的三条纪律（假 session） ---------------------------------------


@pytest.mark.asyncio
async def test_ensure_refuses_to_build_when_duplicates_exist(monkeypatch) -> None:
    """存量重复存在 → **不建索引**并返回 False（自动删金融行比重复本身更危险）。"""
    from backend.shared.trade_contract import ensure_trade_unique_index_async

    # 预检与探测走同一个只读 session（都在 read_only=True 那段里）
    probe = _FakeSession(index_exists=False, dupes=[("default", "1001", "T9", 2)])
    ddl = _FakeSession(index_exists=False)
    _patch_sessions(monkeypatch, probe, ddl)

    assert await ensure_trade_unique_index_async() is False
    all_statements = probe.statements + ddl.statements
    assert not any(s.startswith("CREATE UNIQUE INDEX") for s in all_statements)
    assert ddl.committed is False


@pytest.mark.asyncio
async def test_ensure_takes_the_zero_ddl_fast_path(monkeypatch) -> None:
    """索引已在 → 只探测、不发 DDL（启动期每次调用都不该动 schema）。"""
    from backend.shared.trade_contract import ensure_trade_unique_index_async

    probe = _FakeSession(index_exists=True)
    _patch_sessions(monkeypatch, probe)

    assert await ensure_trade_unique_index_async() is True
    assert len(probe.statements) == 1
    assert not any(s.startswith("CREATE UNIQUE INDEX") for s in probe.statements)


@pytest.mark.asyncio
async def test_ensure_ddl_failure_only_warns(monkeypatch) -> None:
    """DDL 失败（权限/锁超时）返回 False 但不抛：旧语义继续，业务不中断。"""
    from backend.shared.trade_contract import ensure_trade_unique_index_async

    probe = _FakeSession(index_exists=False)
    ddl = _FakeSession(index_exists=False, fail_ddl=True)
    _patch_sessions(monkeypatch, probe, ddl)

    assert await ensure_trade_unique_index_async() is False


@pytest.mark.asyncio
async def test_ensure_is_idempotent_across_calls(monkeypatch) -> None:
    """第二次调用走进程内缓存（真服务里每个请求都可能调它）。"""
    from backend.shared.trade_contract import ensure_trade_unique_index_async

    probe = _FakeSession(index_exists=False)
    ddl = _FakeSession(index_exists=False)
    _patch_sessions(monkeypatch, probe, ddl)

    assert await ensure_trade_unique_index_async() is True
    assert await ensure_trade_unique_index_async() is True
    assert sum(1 for s in ddl.statements if s.startswith("CREATE UNIQUE INDEX")) == 1


# --- 真库 E2E --------------------------------------------------------------


async def _ensure_db_pool() -> None:
    from sqlalchemy import text as _t

    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(_t("SELECT 1"))
        return
    except Exception:  # noqa: BLE001 - 事件循环换了（pytest-asyncio 每例一个新 loop）
        await close_database()
    async with get_session(read_only=True) as probe:
        await probe.execute(_t("SELECT 1"))


@pytest.mark.asyncio
async def test_unique_index_on_live_db_blocks_duplicate_fill() -> None:
    """真库：索引建成 → 同账户同成交号第二行被拦；不同成交号与 NULL 不受影响。

    插入走**非 LLM 腿**（``agent`` 为 NULL）：跑到一半被体检 C14 撞见也不会被误读成
    「成交有、账本没有」的漏记。
    """
    import uuid as _uuid

    for attempt in range(2):
        try:
            await _ensure_db_pool()
            break
        except Exception:  # noqa: BLE001
            if attempt == 1:
                pytest.skip("数据库不可用")

    from sqlalchemy import text as sa_text
    from sqlalchemy.exc import IntegrityError

    from backend.shared.database_manager_v2 import close_database, get_session
    from backend.shared.trade_contract import (
        ensure_trade_unique_index_async,
        trade_unique_index_ready_async,
    )

    assert await ensure_trade_unique_index_async() is True
    assert await trade_unique_index_ready_async() is True

    async with get_session(read_only=True) as session:
        row = (
            await session.execute(
                sa_text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
                {"n": TRADE_UNIQUE_INDEX},
            )
        ).fetchone()
    assert row is not None
    indexdef = str(row[0])
    assert "tenant_id, user_id, exchange_trade_id" in indexdef
    assert "WHERE (exchange_trade_id IS NOT NULL)" in indexdef

    order_id = _uuid.uuid4()
    user = f"99{_uuid.uuid4().int % 1_000_000:06d}"
    trade_a, trade_b, trade_c = _uuid.uuid4(), _uuid.uuid4(), _uuid.uuid4()

    def _trade(tid, etid):
        return {
            "tid": str(tid),
            "oid": str(order_id),
            "uid": user,
            "etid": etid,
        }

    insert_trade = sa_text(
        "INSERT INTO trades (trade_id, order_id, tenant_id, user_id, portfolio_id, "
        "symbol, side, position_side, trading_mode, quantity, price, trade_value, "
        "executed_at, exchange_trade_id) VALUES (CAST(:tid AS uuid), "
        "CAST(:oid AS uuid), 'default', :uid, 0, '600036.SH', 'buy', 'LONG', 'REAL', "
        "100, 40.0, 4000.0, NOW(), :etid)"
    )
    try:
        async with get_session(read_only=False) as session:
            await session.execute(
                sa_text(
                    "INSERT INTO orders (order_id, tenant_id, user_id, portfolio_id, "
                    "symbol, side, position_side, order_type, trading_mode, status, "
                    "quantity) VALUES (CAST(:oid AS uuid), 'default', :uid, 0, "
                    "'600036.SH', 'buy', 'LONG', 'limit', 'REAL', 'filled', 100)"
                ),
                {"oid": str(order_id), "uid": user},
            )
            await session.execute(insert_trade, _trade(trade_a, "E2E-T1"))
            await session.execute(
                insert_trade, _trade(trade_b, "E2E-T2")
            )  # 另一笔，放行
            await session.execute(
                insert_trade, _trade(trade_c, None)
            )  # 无成交号，不落唯一范围
            await session.commit()

        # 同一 (租户, 用户, 成交号) 再插一行 → DB 层拦住（这就是双记防线）
        async with get_session(read_only=False) as session:
            with pytest.raises(IntegrityError):
                await session.execute(insert_trade, _trade(_uuid.uuid4(), "E2E-T1"))
            await session.rollback()
    finally:
        async with get_session(read_only=False) as session:
            await session.execute(
                sa_text("DELETE FROM trades WHERE order_id = CAST(:oid AS uuid)"),
                {"oid": str(order_id)},
            )
            await session.execute(
                sa_text("DELETE FROM orders WHERE order_id = CAST(:oid AS uuid)"),
                {"oid": str(order_id)},
            )
            await session.commit()
        await close_database()


@pytest.mark.asyncio
async def test_c14_sql_pairs_a_real_fill_with_its_ledger_row() -> None:
    """真库：体检 C14 的**取数 SQL** 跑在真表上（列名/类型漂移只有这里能证）。

    造一对「LLM 腿成交 + 同订单账本流水」（随机租户 ``t-``，跑完即删），用 C14 自己的
    两条 SQL 取回来喂给判定函数——配对成功即证明取数与判定在真 schema 上是一条链。
    判定函数本身的各分支由 ``test_health_checks.py`` 的假上下文用例覆盖。
    """
    import uuid as _uuid
    from datetime import date, timedelta

    from sqlalchemy import text as sa_text

    from backend.scripts.diagnose.health import (
        C14_LEDGER_SQL,
        C14_TRADES_SQL,
        classify_agent_ledger_parity,
        ledger_parity,
    )
    from backend.shared.decision.agent_ledger import SEED_FILL_PREFIX
    from backend.shared.database_manager_v2 import close_database, get_session

    for attempt in range(2):
        try:
            await _ensure_db_pool()
            break
        except Exception:  # noqa: BLE001
            if attempt == 1:
                pytest.skip("数据库不可用")

    tenant = f"t-e2e-{_uuid.uuid4().hex[:8]}"
    user = f"99{_uuid.uuid4().int % 1_000_000:06d}"
    order_id = _uuid.uuid4()
    fill_key = f"E2E-C14-{_uuid.uuid4().hex[:8]}"

    async def _query(sql, **params):
        async with get_session(read_only=True) as session:
            result = await session.execute(sa_text(sql), params)
            return [dict(r._mapping) for r in result]

    try:
        async with get_session(read_only=False) as session:
            await session.execute(
                sa_text(
                    "INSERT INTO orders (order_id, tenant_id, user_id, portfolio_id, "
                    "symbol, side, position_side, order_type, trading_mode, status, "
                    "quantity, agent) VALUES (CAST(:oid AS uuid), :t, :u, 0, "
                    "'600036.SH', 'buy', 'LONG', 'limit', 'REAL', 'filled', 100, "
                    "'e2e-agent')"
                ),
                {"oid": str(order_id), "t": tenant, "u": user},
            )
            await session.execute(
                sa_text(
                    "INSERT INTO trades (trade_id, order_id, tenant_id, user_id, "
                    "portfolio_id, symbol, side, position_side, trading_mode, quantity, "
                    "price, trade_value, executed_at, exchange_trade_id) VALUES "
                    "(CAST(:tid AS uuid), CAST(:oid AS uuid), :t, :u, 0, '600036.SH', "
                    "'buy', 'LONG', 'REAL', 100, 40.0, 4000.0, NOW(), :etid)"
                ),
                {
                    "tid": str(_uuid.uuid4()),
                    "oid": str(order_id),
                    "t": tenant,
                    "u": user,
                    "etid": fill_key,
                },
            )
            await session.execute(
                sa_text(
                    "INSERT INTO qm_agent_ledger_fill (tenant_id, user_id, agent, "
                    "fill_key, order_id, trade_date, code, side, volume, price, "
                    "applied_volume, filled_at) VALUES (:t, :u, 'e2e-agent', :etid, "
                    ":oid, CURRENT_DATE, '600036.SH', 'buy', 100, 40.0, 100, NOW())"
                ),
                {"t": tenant, "u": user, "etid": fill_key, "oid": str(order_id)},
            )
            await session.commit()

        since = date.today() - timedelta(days=1)
        trades = [
            r
            for r in await _query(C14_TRADES_SQL, since=since)
            if str(r["order_id"]) == str(order_id)
        ]
        ledger = [
            r
            for r in await _query(C14_LEDGER_SQL, since=since)
            if str(r["order_id"]) == str(order_id)
        ]
        assert len(trades) == 1, f"C14 成交取数没取到这一笔: {trades}"
        assert len(ledger) == 1, f"C14 账本取数没取到这一行: {ledger}"
        r = classify_agent_ledger_parity(
            ledger_parity(
                trades,
                ledger,
                index_enabled=True,
                synth_prefix=SYNTH_TRADE_PREFIX,
                seed_prefix=SEED_FILL_PREFIX,
            )
        )
        assert r.level == "ok", r.detail
        assert r.metrics["posted"] == 1
    finally:
        async with get_session(read_only=False) as session:
            await session.execute(
                sa_text("DELETE FROM qm_agent_ledger_fill WHERE tenant_id = :t"),
                {"t": tenant},
            )
            await session.execute(
                sa_text("DELETE FROM trades WHERE order_id = CAST(:oid AS uuid)"),
                {"oid": str(order_id)},
            )
            await session.execute(
                sa_text("DELETE FROM orders WHERE order_id = CAST(:oid AS uuid)"),
                {"oid": str(order_id)},
            )
            await session.commit()
        await close_database()
