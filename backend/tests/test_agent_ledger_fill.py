"""``agent_ledger_fill.post_fill_for_order`` —— 成交 → 分账账本的唯一入口。

两组用例：
* **参数契约**（假 ``apply_fill``，无库也跑）：归属从订单读、无归属静默跳过、空成交号
  当场拒、成交日取 UTC 日、市场由共享判据推。
* **真库端到端**（有库才跑）：一笔成交真的落进 ``qm_agent_ledger_*``，重投是
  duplicate 且不新增行；两家模型共用一个账户时各记各的段。

这是 P2.7「成交回报只能从订单读归属」那条链的**末段验收**：上游（决策轮 → 订单）
把 ``agent`` 写进订单，这里证明它能一路走到账本。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.shared import agent_ledger_fill as mod
from backend.shared.agent_ledger_store import ApplyOutcome

UTC = timezone.utc
DAY = date(2026, 9, 24)
TS = datetime(2026, 9, 24, 1, 37, 26, tzinfo=UTC)


def _order(**over: Any) -> Any:
    base: dict[str, Any] = {
        "order_id": "ord-1",
        "tenant_id": "default",
        "user_id": "10000001",
        "symbol": "SH600036",
        "side": "buy",
        "agent": "deepseek-v4-pro",
    }
    base.update(over)
    return SimpleNamespace(**base)


def _posted(stub: AsyncMock) -> dict[str, Any]:
    assert stub.await_count == 1, "记录账本应恰好写一次"
    return dict(stub.await_args.kwargs)


@pytest.fixture()
def ledger_stub(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    stub = AsyncMock(return_value=ApplyOutcome(applied=100.0, virtual_cash=39_000.0))
    monkeypatch.setattr(mod, "apply_fill", stub)
    return stub


class TestNoAgentSkips:
    """非 LLM 腿（人点/风控/托管/隔壁桥单）不写分账——它们不属于任何模型。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
    async def test_blank_agent_is_a_silent_noop(
        self, ledger_stub: AsyncMock, value: Any
    ) -> None:
        out = await mod.post_fill_for_order(
            object(),  # 无归属时连 session 都不该被碰
            order=_order(agent=value),
            fill_key="T1",
            quantity=100.0,
            price=30.0,
        )
        assert out is None
        assert ledger_stub.await_count == 0

    @pytest.mark.asyncio
    async def test_missing_agent_attribute_is_a_noop(
        self, ledger_stub: AsyncMock
    ) -> None:
        """老订单对象/测试替身没有 agent 属性：按「无归属」处理，不是 AttributeError。"""
        order = SimpleNamespace(
            order_id="ord-1",
            tenant_id="default",
            user_id="10000001",
            symbol="SH600036",
            side="buy",
        )
        out = await mod.post_fill_for_order(
            object(), order=order, fill_key="T1", quantity=100.0, price=30.0
        )
        assert out is None
        assert ledger_stub.await_count == 0


class TestArgumentContract:
    @pytest.mark.asyncio
    async def test_every_field_the_ledger_needs_comes_from_the_order(
        self, ledger_stub: AsyncMock
    ) -> None:
        session = object()
        out = await mod.post_fill_for_order(
            session,
            order=_order(),
            fill_key="00161170",
            quantity=100.0,
            price=30.0,
            filled_at=TS,
        )
        assert out is not None and out.applied == 100.0
        kw = _posted(ledger_stub)
        assert kw["tenant_id"] == "default"
        assert kw["user_id"] == "10000001"
        assert kw["agent"] == "deepseek-v4-pro"
        assert kw["code"] == "SH600036"  # 前缀式入、后缀式由 store 归一
        assert kw["side"] == "buy"
        assert kw["volume"] == 100.0
        assert kw["price"] == 30.0
        assert kw["fill_key"] == "00161170"
        assert kw["order_id"] == "ord-1"
        assert kw["filled_at"] == TS
        assert ledger_stub.await_args.args[0] is session, (
            "必须用调用方的 session（同事务）"
        )

    @pytest.mark.asyncio
    async def test_agent_is_normalized_at_the_boundary(
        self, ledger_stub: AsyncMock
    ) -> None:
        await mod.post_fill_for_order(
            object(),
            order=_order(agent="  deepseek-v4-pro\n"),
            fill_key="T1",
            quantity=1,
            price=1,
        )
        assert _posted(ledger_stub)["agent"] == "deepseek-v4-pro"

    @pytest.mark.asyncio
    async def test_over_long_agent_is_truncated_to_the_column_width(
        self, ledger_stub: AsyncMock
    ) -> None:
        from backend.shared.order_contract import AGENT_LEN

        await mod.post_fill_for_order(
            object(),
            order=_order(agent="m" * (AGENT_LEN + 10)),
            fill_key="T1",
            quantity=1,
            price=1,
        )
        assert _posted(ledger_stub)["agent"] == "m" * AGENT_LEN

    @pytest.mark.asyncio
    async def test_numeric_user_id_is_stringified(self, ledger_stub: AsyncMock) -> None:
        await mod.post_fill_for_order(
            object(), order=_order(user_id=10000001), fill_key="T1", quantity=1, price=1
        )
        assert _posted(ledger_stub)["user_id"] == "10000001"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("symbol", "market"),
        [
            ("SH600036", "CN"),
            ("600036.SH", "CN"),
            ("300750.SZ", "CN"),
            ("00700.HK", "HK"),
            ("AAPL", "US"),
            ("", "CN"),  # 判不出 → 账本契约列的缺省
            ("1234", "CN"),
        ],
    )
    async def test_market_comes_from_the_shared_predicate(
        self, ledger_stub: AsyncMock, symbol: str, market: str
    ) -> None:
        await mod.post_fill_for_order(
            object(), order=_order(symbol=symbol), fill_key="T1", quantity=1, price=1
        )
        assert _posted(ledger_stub)["market"] == market

    @pytest.mark.asyncio
    async def test_blank_fill_key_refuses_instead_of_guessing(
        self, ledger_stub: AsyncMock
    ) -> None:
        """没有成交号就没有幂等键——当场炸，不拿别的东西凑一个键。"""
        for blank in (None, "", "  "):
            with pytest.raises(ValueError, match="fill_key"):
                await mod.post_fill_for_order(
                    object(), order=_order(), fill_key=blank, quantity=1, price=1
                )
        assert ledger_stub.await_count == 0


class TestTradeDate:
    """成交日 = 幂等键的一半：必须来自成交数据（可重放），不取处理时刻。"""

    @pytest.mark.asyncio
    async def test_defaults_to_the_utc_day_of_the_fill_instant(
        self, ledger_stub: AsyncMock
    ) -> None:
        await mod.post_fill_for_order(
            object(),
            order=_order(),
            fill_key="T1",
            quantity=1,
            price=1,
            filled_at=datetime(2026, 9, 24, 23, 30, tzinfo=UTC),
        )
        # UTC 23:30 = 上海次日 07:30：账本按 UTC 日记，重投才算出同一个键
        assert _posted(ledger_stub)["trade_date"] == date(2026, 9, 24)

    @pytest.mark.asyncio
    async def test_naive_instant_is_read_as_utc(self, ledger_stub: AsyncMock) -> None:
        # 口径单源：无时区输入一律当成 UTC（禁止当成 Asia/Shanghai 再减 8 小时）
        await mod.post_fill_for_order(
            object(),
            order=_order(),
            fill_key="T1",
            quantity=1,
            price=1,
            filled_at=datetime(2026, 9, 24, 23, 30),
        )
        kw = _posted(ledger_stub)
        assert kw["trade_date"] == date(2026, 9, 24)
        assert kw["filled_at"] == datetime(2026, 9, 24, 23, 30, tzinfo=UTC)
        assert kw["filled_at"].tzinfo is not None

    @pytest.mark.asyncio
    async def test_explicit_trade_date_wins(self, ledger_stub: AsyncMock) -> None:
        await mod.post_fill_for_order(
            object(),
            order=_order(),
            fill_key="T1",
            quantity=1,
            price=1,
            filled_at=TS,
            trade_date="2026-09-23",
        )
        assert _posted(ledger_stub)["trade_date"] == "2026-09-23"

    @pytest.mark.asyncio
    async def test_missing_instant_falls_back_to_now(
        self, ledger_stub: AsyncMock
    ) -> None:
        from backend.shared.utc_datetime import as_utc, utc_now

        before = as_utc(utc_now())
        await mod.post_fill_for_order(
            object(), order=_order(), fill_key="T1", quantity=1, price=1
        )
        kw = _posted(ledger_stub)
        assert kw["filled_at"].tzinfo is not None
        assert (kw["filled_at"] - before).total_seconds() >= 0
        assert kw["trade_date"] == kw["filled_at"].date()


class TestOutcomeSurfacing:
    """store 的写纪律 3：note 非空必须进日志（它是「这笔为什么没记全」的唯一答案）。"""

    @pytest.mark.asyncio
    async def test_note_is_logged_as_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        stub = AsyncMock(
            return_value=ApplyOutcome(applied=0.0, note="方向不认识：side='hold'")
        )
        monkeypatch.setattr(mod, "apply_fill", stub)
        with caplog.at_level("WARNING", logger=mod.__name__):
            out = await mod.post_fill_for_order(
                object(), order=_order(), fill_key="T1", quantity=1, price=1
            )
        assert out is not None and out.applied == 0.0
        assert any("方向不认识" in rec.getMessage() for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_duplicate_is_logged_but_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        stub = AsyncMock(return_value=ApplyOutcome(duplicate=True, note=""))
        monkeypatch.setattr(mod, "apply_fill", stub)
        with caplog.at_level("INFO", logger=mod.__name__):
            out = await mod.post_fill_for_order(
                object(), order=_order(), fill_key="T1", quantity=1, price=1
            )
        assert out is not None and out.duplicate is True
        assert not [r for r in caplog.records if r.levelno >= 30], "重投不是错误"


# ── 真库端到端 ───────────────────────────────────────────────────────


def _scope() -> tuple[str, str]:
    tag = uuid.uuid4().hex[:10]
    return f"t-p27f-{tag}", f"98{int(tag, 16) % 1_000_000:06d}"


async def _ready() -> None:
    from sqlalchemy import text

    from backend.shared.agent_ledger_contract import ensure_agent_ledger_tables_async
    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")
    assert await ensure_agent_ledger_tables_async() is True


@pytest.mark.asyncio
async def test_real_db_post_lands_and_replay_is_a_duplicate() -> None:
    """成交真的落进账本；同键重投是 duplicate，账本一个字节不动。"""
    from backend.shared.agent_ledger_contract import FILL_TABLE, TABLES
    from backend.shared.agent_ledger_store import load_ledger
    from backend.shared.database_manager_v2 import close_database, get_session

    await _ready()
    tenant, user = _scope()
    order = _order(tenant_id=tenant, user_id=user, agent="m-a")
    try:
        async with get_session() as session:
            first = await mod.post_fill_for_order(
                session,
                order=order,
                fill_key="T1001",
                quantity=100.0,
                price=30.0,
                filled_at=TS,
            )
            await session.commit()
        assert first is not None
        assert first.applied == 100.0 and not first.note and not first.duplicate

        async with get_session() as session:
            replay = await mod.post_fill_for_order(
                session,
                order=order,
                fill_key="T1001",
                quantity=100.0,
                price=30.0,
                filled_at=TS,
            )
            await session.commit()
        assert replay is not None and replay.duplicate is True and replay.applied == 0.0

        from sqlalchemy import text

        async with get_session(read_only=True) as session:
            led = await load_ledger(session, tenant_id=tenant, user_id=user)
            fill_rows = await session.execute(
                text(
                    f"SELECT count(*) FROM {FILL_TABLE} "
                    "WHERE tenant_id = :t AND user_id = :u"
                ),
                {"t": tenant, "u": user},
            )
        assert int(fill_rows.scalar_one()) == 1, "重投不得新增流水"
        seg = led["agents"]["m-a"]
        assert seg["positions"]["600036.SH"]["volume"] == 100
        assert seg["virtual_cash"] == pytest.approx(97_000.0)
    finally:
        from sqlalchemy import text

        async with get_session() as session:
            for table in TABLES:
                await session.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :t AND user_id = :u"),
                    {"t": tenant, "u": user},
                )
            await session.commit()
        # 关连接池：pytest-asyncio 每个用例一个新事件循环，池里的连接绑在**上一个**
        # 循环上——不关的话下一个真库用例会 "attached to a different loop" 而 skip。
        await close_database()


@pytest.mark.asyncio
async def test_real_db_two_agents_share_one_account_but_not_one_book() -> None:
    """多模型共用一个账户：成交按 ``orders.agent`` 分段，各记各的仓与现金。"""
    from backend.shared.agent_ledger_contract import TABLES
    from backend.shared.agent_ledger_store import load_ledger
    from backend.shared.database_manager_v2 import close_database, get_session

    await _ready()
    tenant, user = _scope()
    try:
        async with get_session() as session:
            for agent, symbol, qty in (
                ("m-a", "SH600036", 100.0),
                ("m-b", "SH600036", 200.0),
            ):
                await mod.post_fill_for_order(
                    session,
                    order=_order(
                        tenant_id=tenant, user_id=user, agent=agent, symbol=symbol
                    ),
                    fill_key=f"T-{agent}",
                    quantity=qty,
                    price=30.0,
                    filled_at=TS,
                )
            await session.commit()

        async with get_session(read_only=True) as session:
            led = await load_ledger(session, tenant_id=tenant, user_id=user)
        assert set(led["agents"]) == {"m-a", "m-b"}
        assert led["agents"]["m-a"]["positions"]["600036.SH"]["volume"] == 100
        assert led["agents"]["m-b"]["positions"]["600036.SH"]["volume"] == 200
        assert led["agents"]["m-a"]["virtual_cash"] == pytest.approx(97_000.0)
        assert led["agents"]["m-b"]["virtual_cash"] == pytest.approx(94_000.0)
    finally:
        from sqlalchemy import text

        async with get_session() as session:
            for table in TABLES:
                await session.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :t AND user_id = :u"),
                    {"t": tenant, "u": user},
                )
            await session.commit()
        # 关连接池：pytest-asyncio 每个用例一个新事件循环，池里的连接绑在**上一个**
        # 循环上——不关的话下一个真库用例会 "attached to a different loop" 而 skip。
        await close_database()
