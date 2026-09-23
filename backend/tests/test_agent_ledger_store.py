"""分账账本落库读写侧的不变量（P2.7）。

盯三件事，都是**静默**损坏（不报错、只让数悄悄变样）：

1. **一笔成交只记一次**。靠的是 `qm_agent_ledger_fill` 上
   `(tenant_id, user_id, trade_date, fill_key)` 的唯一约束，**不是** SELECT-then-INSERT：
   消费者重投事件（``_retry_or_dlq`` 把同一份 fields 重新 xadd 回原流）时没有新事件号，
   「提交成功但进程以为失败」那一下就会双记——虚拟现金凭空多一笔。
2. **唯一键是当日的，不是全库的**。A 股成交编号每日重排（实测 `00161170` 这类），
   全库唯一会把**次日的同号成交**静默吞掉（`ON CONFLICT DO NOTHING` 不报错）——
   账本少一只票、`mine_of` 看不见它、agent 卖不掉自己的持仓。
3. **代码一律后缀式**。读侧 `HoldingRow.code` 是后缀式，存前缀式则 `mine_of`
   永远匹配不上，互卖防线与成本列**同时**静默失效。

真库用例自带清理（租户前缀 `t-`，与真账隔离），DB 不可用时 skip。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest

from backend.shared.agent_ledger_store import (
    ApplyOutcome,
    apply_fill,
    load_agent_positions,
    load_ledger,
    normalize_code,
    position_deltas,
)

TS1 = datetime(2026, 9, 11, 1, 37, 26, tzinfo=timezone.utc)
TS2 = datetime(2026, 9, 14, 2, 23, 2, tzinfo=timezone.utc)
DAY1 = date(2026, 9, 11)
DAY2 = date(2026, 9, 14)


# ── 纯函数：代码归一 + 行 ↔ 账本 ────────────────────────────────────


def test_normalize_code_is_suffix_and_idempotent():
    """写入边界统一后缀式：`SH600036` → `600036.SH`；已是后缀式则原样。"""
    assert normalize_code("SH600036") == "600036.SH"
    assert normalize_code("600036.SH") == "600036.SH"
    assert normalize_code(" sh600036 ") == "600036.SH"
    assert normalize_code("") == ""


def test_position_deltas_report_upserts_and_removals():
    """diff 出**要写的行**与**要删的码**——整段覆盖会抹掉并发写者刚落的行。"""
    before = {"600036.SH": {"volume": 100, "cost_price": 30.0}}
    after = {
        "000001.SZ": {"volume": 200, "cost_price": 12.0, "buy_ts": "x", "last_ts": "y"},
    }
    upserts, removed = position_deltas(before, after)
    assert removed == ["600036.SH"]
    assert [u["code"] for u in upserts] == ["000001.SZ"]

    upserts, removed = position_deltas(before, before)
    assert upserts == [] and removed == []


def test_position_deltas_detect_a_changed_volume():
    before = {"600036.SH": {"volume": 100, "cost_price": 30.0}}
    after = {"600036.SH": {"volume": 400, "cost_price": 33.0}}
    upserts, removed = position_deltas(before, after)
    assert removed == [] and len(upserts) == 1 and upserts[0]["volume"] == 400


def test_position_deltas_are_blind_to_readback_shapes():
    """**判等只看数量与成本**：库里的行（时间戳是 datetime、量是 float）与内存里的行
    （时间戳是 Z 串、量是 int）在没成交时必须判等。

    否则每个被拒的成交都会把持仓行重写一遍——「无变化就不写」这条性质会静默消失，
    而它在表上表现为无谓的 `updated_at` 抖动，没人会怀疑到判等键头上。
    """
    z = "2026-09-11T01:37:26Z"
    before = {
        "600036.SH": {"volume": 400.0, "cost_price": 30.0, "buy_ts": z, "last_ts": z}
    }
    after = {
        "600036.SH": {
            "volume": 400,
            "cost_price": 30.0,
            "buy_ts": datetime(2026, 9, 11, 1, 37, 26, tzinfo=timezone.utc),
            "last_ts": datetime(2026, 9, 11, 1, 37, 26, tzinfo=timezone.utc),
        }
    }
    assert position_deltas(before, after) == ([], [])


def test_apply_outcome_is_frozen():
    from dataclasses import FrozenInstanceError

    out = ApplyOutcome(applied=100)
    with pytest.raises(FrozenInstanceError):
        out.applied = 1  # type: ignore[misc]


# ── 真库 ─────────────────────────────────────────────────────────────


def _scope() -> tuple[str, str]:
    """测试租户/账户（前缀 `t-`，与真账隔离；用完必删）。"""
    tag = uuid.uuid4().hex[:10]
    return f"t-p27-{tag}", f"99{int(tag, 16) % 1_000_000:06d}"


async def _cleanup(session, tenant: str, user: str) -> None:
    from sqlalchemy import text

    from backend.shared.agent_ledger_contract import TABLES

    for table in TABLES:
        await session.execute(
            text(f"DELETE FROM {table} WHERE tenant_id = :t AND user_id = :u"),
            {"t": tenant, "u": user},
        )


async def _ready() -> None:
    """建表 + DB 探活；不可用则 skip（真库用例只在有库时有意义）。"""
    from sqlalchemy import text

    from backend.shared.agent_ledger_contract import ensure_agent_ledger_tables_async
    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")
    assert await ensure_agent_ledger_tables_async() is True


async def _count(tenant: str, user: str, table: str) -> int:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        return int(
            (
                await session.execute(
                    text(
                        f"SELECT count(*) FROM {table} "
                        "WHERE tenant_id = :t AND user_id = :u"
                    ),
                    {"t": tenant, "u": user},
                )
            ).scalar_one()
        )


@pytest.mark.asyncio
async def test_real_db_buy_creates_account_position_and_journal():
    """买入一笔：账户行（初始 = quota）、持仓行、流水行三样都落，现金按成本扣。"""
    from backend.shared.database_manager_v2 import close_database, get_session

    await _ready()
    tenant, user = _scope()
    try:
        async with get_session() as session:
            out = await apply_fill(
                session,
                tenant_id=tenant,
                user_id=user,
                agent="m-a",
                code="SH600036",
                side="buy",
                volume=100,
                price=30.0,
                fill_key="00161170",
                trade_date=DAY1,
                order_id="ord-1",
                filled_at=TS1,
                quota=50_000.0,
            )
            await session.commit()
        assert out.applied == 100 and not out.note and not out.duplicate
        assert out.virtual_cash == pytest.approx(47_000.0)

        async with get_session(read_only=True) as session:
            led = await load_ledger(session, tenant_id=tenant, user_id=user)
        assert set(led["agents"]) == {"m-a"}
        pos = led["agents"]["m-a"]["positions"]["600036.SH"]  # 后缀式（写入边界归一）
        assert pos["volume"] == 100 and pos["cost_price"] == 30.0
        assert pos["buy_ts"] == "2026-09-11T01:37:26Z", "瞬时列必须是 Z 结尾的 UTC"
        assert led["agents"]["m-a"]["virtual_cash"] == pytest.approx(47_000.0)

        from backend.shared.agent_ledger_contract import FILL_TABLE

        assert await _count(tenant, user, FILL_TABLE) == 1
    finally:
        async with get_session() as session:
            await _cleanup(session, tenant, user)
            await session.commit()
        await close_database()


@pytest.mark.asyncio
async def test_real_db_same_fill_key_same_day_is_applied_once():
    """**exactly-once**：同一天同一个 fill_key 第二次 → duplicate，账本一格不动。

    这就是「提交成功但进程以为失败 → 重投」那条路径的护栏。
    """
    from backend.shared.database_manager_v2 import close_database, get_session

    await _ready()
    tenant, user = _scope()
    try:
        async with get_session() as session:
            first = await apply_fill(
                session,
                tenant_id=tenant,
                user_id=user,
                agent="m-a",
                code="600036.SH",
                side="buy",
                volume=100,
                price=30.0,
                fill_key="00161170",
                trade_date=DAY1,
                filled_at=TS1,
            )
            await session.commit()
        assert first.applied == 100

        async with get_session() as session:
            second = await apply_fill(
                session,
                tenant_id=tenant,
                user_id=user,
                agent="m-a",
                code="600036.SH",
                side="buy",
                volume=100,
                price=30.0,
                fill_key="00161170",
                trade_date=DAY1,
                filled_at=TS1,
            )
            await session.commit()
        assert second.duplicate is True
        assert second.applied == 0

        async with get_session(read_only=True) as session:
            led = await load_ledger(session, tenant_id=tenant, user_id=user)
        agent = led["agents"]["m-a"]
        assert agent["positions"]["600036.SH"]["volume"] == 100, "重投把持仓记了两遍"
        assert agent["virtual_cash"] == pytest.approx(97_000.0)
    finally:
        async with get_session() as session:
            await _cleanup(session, tenant, user)
            await session.commit()
        await close_database()


@pytest.mark.asyncio
async def test_real_db_same_fill_key_next_day_is_a_different_fill():
    """**唯一键是当日的**：成交编号每日重排（实测 `00161170` 这类 8 位号）。

    全库唯一键会在**次日的新成交**上撞键（`ON CONFLICT DO NOTHING` 静默吞掉）——
    账本少一只票、`mine_of` 看不见它、agent 卖不掉自己的持仓。这条用例是那个
    方向的回归护栏。
    """
    from backend.shared.database_manager_v2 import close_database, get_session

    await _ready()
    tenant, user = _scope()
    try:
        async with get_session() as session:
            await apply_fill(
                session,
                tenant_id=tenant,
                user_id=user,
                agent="m-a",
                code="600036.SH",
                side="buy",
                volume=100,
                price=30.0,
                fill_key="00161170",
                trade_date=DAY1,
                filled_at=TS1,
            )
            await session.commit()
        async with get_session() as session:
            second = await apply_fill(
                session,
                tenant_id=tenant,
                user_id=user,
                agent="m-a",
                code="000001.SZ",
                side="buy",
                volume=200,
                price=12.0,
                fill_key="00161170",  # 同一个号，**另一天**
                trade_date=DAY2,
                filled_at=TS2,
            )
            await session.commit()
        assert second.duplicate is False and second.applied == 200, (
            "次日同号成交被吞掉了"
        )

        async with get_session(read_only=True) as session:
            pos = await load_agent_positions(
                session, tenant_id=tenant, user_id=user, agent="m-a"
            )
        assert set(pos) == {"600036.SH", "000001.SZ"}

        from backend.shared.agent_ledger_contract import FILL_TABLE

        assert await _count(tenant, user, FILL_TABLE) == 2
    finally:
        async with get_session() as session:
            await _cleanup(session, tenant, user)
            await session.commit()
        await close_database()


@pytest.mark.asyncio
async def test_real_db_sell_of_unheld_stock_is_journaled_but_not_applied():
    """卖非持仓：账本一格不动，但**流水行要落**（`applied_volume=0` + 理由）。

    不留痕的后果是「这次卖出没记上」在账本里没有任何痕迹——台账与柜台漂移是最难查
    的一类事故。留痕还有第二个作用：这笔 fill_key 被消费掉，重投不会在账本后来的
    状态上**重新**执行一次（买入流水迟到时会把卖出记账算两遍）。
    """
    from backend.shared.database_manager_v2 import close_database, get_session

    await _ready()
    tenant, user = _scope()
    try:
        async with get_session() as session:
            out = await apply_fill(
                session,
                tenant_id=tenant,
                user_id=user,
                agent="m-a",
                code="600036.SH",
                side="sell",
                volume=100,
                price=31.0,
                fill_key="00161199",
                trade_date=DAY1,
                filled_at=TS1,
            )
            await session.commit()
        assert out.applied == 0 and not out.duplicate
        assert "无持仓" in out.note

        from backend.shared.agent_ledger_contract import FILL_TABLE

        assert await _count(tenant, user, FILL_TABLE) == 1
        async with get_session(read_only=True) as session:
            led = await load_ledger(session, tenant_id=tenant, user_id=user)
        assert led["agents"]["m-a"]["positions"] == {}

        # 重投同一笔 → 幂等命中（不会再"发现"后来到账的持仓而补记一次卖出）
        async with get_session() as session:
            again = await apply_fill(
                session,
                tenant_id=tenant,
                user_id=user,
                agent="m-a",
                code="600036.SH",
                side="sell",
                volume=100,
                price=31.0,
                fill_key="00161199",
                trade_date=DAY1,
                filled_at=TS1,
            )
            await session.commit()
        assert again.duplicate is True
    finally:
        async with get_session() as session:
            await _cleanup(session, tenant, user)
            await session.commit()
        await close_database()


@pytest.mark.asyncio
async def test_real_db_unknown_side_is_journaled_not_applied():
    """方向不认识（适配层传了 ``"short_sell"`` 之类）：落流水、不动账、留理由。

    静默跳过比拒单更糟：账本会按「这笔成交不存在」继续走，而柜台那边是真成交了。
    """
    from backend.shared.database_manager_v2 import close_database, get_session

    await _ready()
    tenant, user = _scope()
    try:
        async with get_session() as session:
            out = await apply_fill(
                session,
                tenant_id=tenant,
                user_id=user,
                agent="m-a",
                code="600036.SH",
                side="short_sell",
                volume=100,
                price=31.0,
                fill_key="00161200",
                trade_date=DAY1,
                filled_at=TS1,
            )
            await session.commit()
        assert out.applied == 0 and not out.duplicate
        assert "方向" in out.note and "short_sell" in out.note

        from backend.shared.agent_ledger_contract import FILL_TABLE

        assert await _count(tenant, user, FILL_TABLE) == 1

        # 大小写与枚举形态都要认（`orders.side` 是 str 子类枚举，取值 buy/sell）
        async with get_session() as session:
            up = await apply_fill(
                session,
                tenant_id=tenant,
                user_id=user,
                agent="m-a",
                code="SH600036",
                side="BUY",
                volume=100,
                price=30.0,
                fill_key="00161201",
                trade_date=DAY1,
                filled_at=TS2,
            )
            await session.commit()
        assert up.applied == 100 and not up.note
        async with get_session(read_only=True) as session:
            pos = await load_agent_positions(
                session, tenant_id=tenant, user_id=user, agent="m-a"
            )
        assert set(pos) == {"600036.SH"}
    finally:
        async with get_session() as session:
            await _cleanup(session, tenant, user)
            await session.commit()
        await close_database()


@pytest.mark.asyncio
async def test_real_db_cross_agent_sell_does_not_touch_the_other_ledger():
    """2026-09-08 事故的可执行复现（真库版）：B 卖 A 的票 —— A 的账一格不动。"""
    from backend.shared.database_manager_v2 import close_database, get_session

    await _ready()
    tenant, user = _scope()
    try:
        async with get_session() as session:
            await apply_fill(
                session,
                tenant_id=tenant,
                user_id=user,
                agent="m-a",
                code="600036.SH",
                side="buy",
                volume=400,
                price=30.0,
                fill_key="00161170",
                trade_date=DAY1,
                filled_at=TS1,
            )
            await session.commit()
        async with get_session() as session:
            out = await apply_fill(
                session,
                tenant_id=tenant,
                user_id=user,
                agent="m-b",
                code="600036.SH",
                side="sell",
                volume=100,
                price=31.0,
                fill_key="00161171",
                trade_date=DAY1,
                filled_at=TS2,
            )
            await session.commit()
        assert out.applied == 0 and "无持仓" in out.note

        async with get_session(read_only=True) as session:
            led = await load_ledger(session, tenant_id=tenant, user_id=user)
        assert led["agents"]["m-a"]["positions"]["600036.SH"]["volume"] == 400
        assert led["agents"]["m-b"]["positions"] == {}
    finally:
        async with get_session() as session:
            await _cleanup(session, tenant, user)
            await session.commit()
        await close_database()


async def _wait_blocked_by(pid: int, *, timeout: float = 5.0) -> bool:
    """等到**真有别的后端被 ``pid`` 挡住**为止（``pg_blocking_pids``，不是猜时间窗）。

    这条判据比「等 0.5 秒看对方有没有做完」硬：它问的是数据库「谁在等谁」，而不是
    「调度器有没有把对方跑起来」。测试机忙的时候后者会假红，前者不会。
    """
    import asyncio

    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        async with get_session(read_only=True) as probe:
            n = (
                await probe.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE pid <> pg_backend_pid() "
                        "AND :p = ANY(pg_blocking_pids(pid))"
                    ),
                    {"p": pid},
                )
            ).scalar_one()
        if int(n) > 0:
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.asyncio
async def test_real_db_concurrent_fills_serialize_on_the_account_row(monkeypatch):
    """两笔 fill 并发到同一 agent：**后一笔必须等前一笔提交**（``SELECT … FOR UPDATE``）。

    不等的话两笔各自读到同一份现金，各自写回自己的算法结果——典型的 lost update：
    持仓记了两笔、现金只扣了一笔，而账本**每一行单看都对**（这正是它难查的原因）。
    这类写偏斜只在真并发下出现，顺序调用跑一百遍也照不出来。

    难点在**把窗口摆出来**：危险窗口 = 「读到现金」与「写回现金」之间。若只是让两笔
    各自跑一遍，第一笔往往已经跑到写回那一步——那时它自己就占着行锁，第二笔撞上的
    是它而不是 ``FOR UPDATE``，用例会因为错的理由变绿（实测：把 ``FOR UPDATE`` 删掉，
    那种写法照样通过）。所以这里用 ``_insert_fill`` 的闸门把第一笔**按在窗口正中**
    （读完现金、还没写任何行），再放第二笔进来。

    两者缺一不可的判据：
    ① ``pg_blocking_pids`` 显示第二笔正被第一笔挡着（问「谁在等谁」，不看时钟）；
    ② 收尾时现金恰好扣了两笔的钱——少扣一笔就是有人读了旧值。
    """
    import asyncio

    from sqlalchemy import text

    from backend.shared import agent_ledger_store as als
    from backend.shared.database_manager_v2 import close_database, get_session

    await _ready()
    tenant, user = _scope()
    kw = {
        "tenant_id": tenant,
        "user_id": user,
        "agent": "m-a",
        "code": "600036.SH",
        "side": "buy",
        "volume": 100,
        "price": 30.0,
        "trade_date": DAY1,
        "filled_at": TS1,
    }
    try:
        # 前置：账户行**已提交**地存在。开户那次 INSERT 未提交时会替行锁挡人，
        # 账户行一旦存在（生产上除第一笔之外的所有情形）就只剩 FOR UPDATE 了。
        async with get_session() as seed:
            await apply_fill(seed, fill_key="00161209", **kw)
            await seed.commit()

        paused, release = asyncio.Event(), asyncio.Event()
        real_insert = als._insert_fill
        seen = {"n": 0}

        async def gated(session, **row):
            seen["n"] += 1
            if seen["n"] == 1:  # 只按第一笔；第二笔直通
                paused.set()
                await release.wait()
            return await real_insert(session, **row)

        monkeypatch.setattr(als, "_insert_fill", gated)

        async with get_session() as a:
            pid_a = int((await a.execute(text("SELECT pg_backend_pid()"))).scalar_one())

            async def _first() -> ApplyOutcome:
                return await apply_fill(a, fill_key="00161210", **kw)

            async def _second() -> ApplyOutcome:
                async with get_session() as b:  # 另一条连接
                    out = await apply_fill(b, fill_key="00161211", **kw)
                    await b.commit()
                    return out

            first_task = asyncio.create_task(_first())
            await asyncio.wait_for(paused.wait(), timeout=30)  # 第一笔停在窗口正中
            second_task = asyncio.create_task(_second())

            blocked = await _wait_blocked_by(pid_a)
            assert blocked, (
                "第二笔没有在账户行上等第一笔 —— 现金的「读—改—写」没被锁住，"
                "两笔会各读各的旧值（lost update），而账本每一行单看都对"
            )
            release.set()
            first = await asyncio.wait_for(first_task, timeout=30)
            await a.commit()
            second = await asyncio.wait_for(second_task, timeout=30)

        assert first.applied == 100 and second.applied == 100
        async with get_session(read_only=True) as session:
            led = await load_ledger(session, tenant_id=tenant, user_id=user)
        agent = led["agents"]["m-a"]
        assert agent["positions"]["600036.SH"]["volume"] == 300, "三笔都要记进持仓"
        assert agent["virtual_cash"] == pytest.approx(100_000.0 - 3 * 3_000.0), (
            "现金少扣了一笔 —— 有谁读到了别人提交前的旧值"
        )
    finally:
        async with get_session() as session:
            await _cleanup(session, tenant, user)
            await session.commit()
        await close_database()


@pytest.mark.asyncio
async def test_real_db_sell_clamps_and_writes_a_roundtrip_row():
    """超卖夹取 + 回合台账落库（影子账户与行为归因的底座）。"""
    from backend.shared.database_manager_v2 import close_database, get_session

    await _ready()
    tenant, user = _scope()
    try:
        async with get_session() as session:
            await apply_fill(
                session,
                tenant_id=tenant,
                user_id=user,
                agent="m-a",
                code="600036.SH",
                side="buy",
                volume=100,
                price=30.0,
                fill_key="00161170",
                trade_date=DAY1,
                filled_at=TS1,
            )
            await session.commit()
        async with get_session() as session:
            out = await apply_fill(
                session,
                tenant_id=tenant,
                user_id=user,
                agent="m-a",
                code="600036.SH",
                side="sell",
                volume=500,  # 只有 100
                price=31.0,
                fill_key="00161172",
                trade_date=DAY1,
                filled_at=TS2,
            )
            await session.commit()
        assert out.applied == 100, "只记实际持有的"
        assert "500" in out.note and "100" in out.note

        from backend.shared.agent_ledger_contract import ROUNDTRIP_TABLE

        assert await _count(tenant, user, ROUNDTRIP_TABLE) == 1
        async with get_session(read_only=True) as session:
            led = await load_ledger(session, tenant_id=tenant, user_id=user)
        assert led["agents"]["m-a"]["positions"] == {}
        assert led["agents"]["m-a"]["virtual_cash"] == pytest.approx(
            100_000.0 - 3_000.0 + 100 * 31.0
        )
    finally:
        async with get_session() as session:
            await _cleanup(session, tenant, user)
            await session.commit()
        await close_database()


@pytest.mark.asyncio
async def test_real_db_bad_tick_is_recorded_at_the_reference_price():
    """坏 tick 闸（逐字移植隔壁 2026-09-08 的实测口径）：成交价越界 → 按参考价记账并标 approx。

    001312 那次：桥报成交价 4.789 而实时价 17.5，坏成本入账虚增虚拟净值约 1.4 万。
    """
    from backend.shared.database_manager_v2 import close_database, get_session

    await _ready()
    tenant, user = _scope()
    try:
        async with get_session() as session:
            out = await apply_fill(
                session,
                tenant_id=tenant,
                user_id=user,
                agent="m-a",
                code="001312.SZ",
                side="buy",
                volume=100,
                price=4.789,
                fill_key="00161173",
                trade_date=DAY1,
                filled_at=TS1,
                ref_price=17.5,
            )
            await session.commit()
        assert out.approx_price is True
        assert out.note and "坏" in out.note

        async with get_session(read_only=True) as session:
            pos = await load_agent_positions(
                session, tenant_id=tenant, user_id=user, agent="m-a"
            )
        assert pos["001312.SZ"]["cost_price"] == pytest.approx(17.5), (
            "坏价没被参考价替换 —— 虚拟净值被虚增"
        )
    finally:
        async with get_session() as session:
            await _cleanup(session, tenant, user)
            await session.commit()
        await close_database()


@pytest.mark.asyncio
async def test_real_db_quota_is_not_stored_only_used_for_the_initial_cash():
    """`quota` 是绑定层参数，**不入库**（否则改配额时库里那个数会静默压过配置）。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import close_database, get_session

    await _ready()
    tenant, user = _scope()
    try:
        async with get_session() as session:
            await apply_fill(
                session,
                tenant_id=tenant,
                user_id=user,
                agent="m-a",
                code="600036.SH",
                side="buy",
                volume=100,
                price=30.0,
                fill_key="00161170",
                trade_date=DAY1,
                filled_at=TS1,
                quota=50_000.0,
            )
            await session.commit()
        from backend.shared.agent_ledger_contract import ACCOUNT_TABLE

        async with get_session(read_only=True) as session:
            cols = {
                str(r[0])
                for r in (
                    await session.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema='public' AND table_name=:t"
                        ),
                        {"t": ACCOUNT_TABLE},
                    )
                ).all()
            }
        assert "quota" not in cols and "quota_total" not in cols, (
            "quota 入库了 —— 改配置会被库里的旧值静默压过"
        )
        # 换一个 quota 再读：已存在账户的现金不受影响
        async with get_session() as session:
            out = await apply_fill(
                session,
                tenant_id=tenant,
                user_id=user,
                agent="m-a",
                code="000001.SZ",
                side="buy",
                volume=100,
                price=12.0,
                fill_key="00161174",
                trade_date=DAY1,
                filled_at=TS2,
                quota=999_999.0,
            )
            await session.commit()
        assert out.virtual_cash == pytest.approx(50_000.0 - 3_000.0 - 1_200.0)
    finally:
        async with get_session() as session:
            await _cleanup(session, tenant, user)
            await session.commit()
        await close_database()
