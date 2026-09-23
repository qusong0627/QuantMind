"""期初结转（P3 数据迁移）：隔壁 ``live_ledger.json`` → 本仓分账账本。

三层一起钉：

1. **纯解析**（``parse_legacy_ledger``）——真语料（2026-09-23 实测 dump 的形状）、
   脏值（非有限/非正/重码/坏版本）、降级项（时间戳缺失、``used`` 已弃用）；
2. **落库**（``import_legacy_seed``）——**空账本才许结转**（三张表逐张查）、
   dry-run 一行不写、流水行带 ``legacy-seed:`` 前缀；
3. **接线**——体检 C14 认这个前缀（不当孤儿）、CLI 与决策轮**同一身份解析**
   （写错身份 = 账本落在决策轮看不见的空账上）。

真库 E2E 用随机租户跑完即删（``t-seed-*``），跑法见 ``test_trade_contract`` 的同款注释。
"""

from __future__ import annotations

from datetime import date

import pytest

from backend.shared.decision.agent_ledger import (
    SEED_FILL_PREFIX,
    parse_legacy_ledger,
    seed_fill_key,
)

#: 隔壁 2026-09-23 实测 dump 的**形状**（数值取自真文件，代码是其中两只）。
REAL_SHAPE = {
    "version": 1,
    "agents": {
        "deepseek-v4-flash": {
            "positions": {
                "603213.SH": {
                    "volume": 700,
                    "cost_price": 13.6,
                    "buy_ts": "2026-09-11T09:37:26.903941+08:00",
                    "last_ts": "2026-09-14T10:23:02.478733+08:00",
                },
                "002074.SZ": {
                    "volume": 200,
                    "cost_price": 25.44,
                    "buy_ts": "2026-09-14T11:07:35.714953+08:00",
                    "last_ts": "2026-09-21T13:02:01.861988+08:00",
                },
            },
            "virtual_cash": 95044.8,
            "used": 356421.2,  # 已弃用字段（与现算不符，见 note）
        },
        "deepseek-v4-pro": {
            "positions": {
                "002709.SZ": {
                    "volume": 300,
                    "cost_price": 32.97,
                    "buy_ts": "2026-09-17T11:26:19.513199+08:00",
                    "last_ts": "2026-09-17T11:26:19.513199+08:00",
                }
            },
            "virtual_cash": 74011.8,
            "used": 167302.2,
        },
    },
    "applied_fills": {
        "25446": {"filled": 200, "ts": "2026-09-23"},
        "42990": {"filled": 200, "ts": "2026-09-23"},
    },
}


# --- 纯解析 -----------------------------------------------------------------


def test_real_shape_parses_clean():
    s = parse_legacy_ledger(REAL_SHAPE)
    assert s.ok and not s.problems
    assert [a.agent for a in s.agents] == ["deepseek-v4-flash", "deepseek-v4-pro"]
    flash = s.agent("deepseek-v4-flash")
    assert flash is not None
    assert flash.virtual_cash == 95044.8
    assert [p.code for p in flash.positions] == ["002074.SZ", "603213.SH"]
    assert flash.used == pytest.approx(700 * 13.6 + 200 * 25.44, abs=0.01)
    assert s.applied_fills == 2  # 只计数，不搬内容


def test_stale_used_field_is_a_note_not_a_blocker():
    """文件里的 ``used`` 是旧版遗留（实测差一个数量级），读侧本来就现算——不阻断。"""
    s = parse_legacy_ledger(REAL_SHAPE)
    assert s.ok
    assert any("used=" in n and "已弃用" in n for n in s.notes)


def test_parse_is_deterministic():
    """同一份文件两次解析逐字相同（报告可复现是迁移操作的基本要求）。"""
    assert parse_legacy_ledger(REAL_SHAPE) == parse_legacy_ledger(REAL_SHAPE)


def test_timestamps_are_utc_and_buy_date_survives():
    s = parse_legacy_ledger(REAL_SHAPE)
    flash = s.agent("deepseek-v4-flash")
    assert flash is not None
    pos = next(p for p in flash.positions if p.code == "603213.SH")
    assert pos.buy_ts is not None
    assert (
        pos.buy_ts.utcoffset() is not None
        and pos.buy_ts.utcoffset().total_seconds() == 0
    )
    # 北京 09:37 = UTC 01:37 → 同一天；流水日按这个日期记
    assert pos.buy_ts.date() == date(2026, 9, 11)


def test_unknown_version_is_blocked():
    s = parse_legacy_ledger({**REAL_SHAPE, "version": 2})
    assert not s.ok
    assert any("版本" in p for p in s.problems)


def test_missing_version_is_blocked():
    raw = {k: v for k, v in REAL_SHAPE.items() if k != "version"}
    s = parse_legacy_ledger(raw)
    assert not s.ok


def test_non_finite_and_non_positive_values_are_blocked():
    raw = {
        "version": 1,
        "agents": {
            "a": {
                "positions": {
                    "600036.SH": {"volume": 100, "cost_price": float("inf")},
                    "600000.SH": {"volume": -5, "cost_price": 10.0},
                },
                "virtual_cash": 1000.0,
            }
        },
    }
    s = parse_legacy_ledger(raw)
    assert not s.ok
    assert len(s.problems) == 2  # 一条一只票，不许「报第一条就收工」


def test_duplicate_code_after_normalization_is_blocked():
    """同一 agent 里 ``SH600036`` 与 ``600036.SH`` 归一后撞主键——必须阻断（不能静默并账）。"""
    raw = {
        "version": 1,
        "agents": {
            "a": {
                "positions": {
                    "600036.SH": {"volume": 100, "cost_price": 10.0},
                    "SH600036": {"volume": 200, "cost_price": 11.0},
                },
                "virtual_cash": 1000.0,
            }
        },
    }
    s = parse_legacy_ledger(raw)
    assert not s.ok
    assert any("重码" in p for p in s.problems)


def test_prefix_codes_are_normalized_with_a_note():
    raw = {
        "version": 1,
        "agents": {
            "a": {
                "positions": {"SH600036": {"volume": 100, "cost_price": 10.0}},
                "virtual_cash": 1000.0,
            }
        },
    }
    s = parse_legacy_ledger(raw)
    assert s.ok
    a = s.agent("a")
    assert a is not None and a.positions[0].code == "600036.SH"
    assert any("归一为" in n for n in s.notes)


def test_missing_buy_ts_degrades_with_a_note():
    raw = {
        "version": 1,
        "agents": {
            "a": {
                "positions": {"600036.SH": {"volume": 100, "cost_price": 10.0}},
                "virtual_cash": 1000.0,
            }
        },
    }
    s = parse_legacy_ledger(raw)
    assert s.ok
    assert any("无 buy_ts" in n for n in s.notes)


def test_negative_cash_is_a_note_not_a_blocker():
    """透支如实搬（``load_agent_ledger`` 明文说负值原样带回），只提示。"""
    raw = {
        "version": 1,
        "agents": {"a": {"positions": {}, "virtual_cash": -1200.5}},
    }
    s = parse_legacy_ledger(raw)
    assert s.ok
    assert s.agent("a") is not None and s.agent("a").virtual_cash == -1200.5
    assert any("透支" in n for n in s.notes)


def test_missing_agents_section_is_a_blocker():
    s = parse_legacy_ledger({"version": 1})
    assert not s.ok


def test_top_level_garbage_is_a_blocker():
    assert not parse_legacy_ledger([]).ok
    assert not parse_legacy_ledger("nope").ok


def test_unknown_fields_are_noted_not_blocked():
    raw = {
        "version": 1,
        "agents": {"a": {"positions": {}, "virtual_cash": 1.0, "score": 3}},
        "extra_top": 1,
    }
    s = parse_legacy_ledger(raw)
    assert s.ok
    assert any("score" in n for n in s.notes)
    assert any("extra_top" in n for n in s.notes)


def test_seed_fill_key_is_suffix_coded():
    assert seed_fill_key("SH600036") == f"{SEED_FILL_PREFIX}600036.SH"


# --- 落库（假 session） ------------------------------------------------------


class _FakeResult:
    def __init__(self, rows=None):
        self._rows = list(rows or [])

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self._rows)


class _SeedSession:
    """按 SQL 形态分发：三张表的脏检查 → 空；写语句一律记账不留痕。"""

    def __init__(self, *, dirty=None):
        self.dirty = dict(dirty or {})
        self.writes: list[str] = []
        self.params: list[dict] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "COUNT(*)" in sql and "GROUP BY agent" in sql:
            for table, rows in self.dirty.items():
                if table in sql:
                    return _FakeResult(rows)
            return _FakeResult([])
        self.writes.append(sql)
        try:
            self.params.append(dict(statement.compile().params))
        except Exception:  # noqa: BLE001 - 纯文本语句取不到编译参数
            self.params.append(dict(params or {}))
        if "RETURNING" in sql:  # _insert_fill：有返回行 = 真插进去了
            return _FakeResult([(1,)])
        return _FakeResult([])

    def seed_keys(self) -> set[str]:
        """写语句里出现过的结转幂等键（C14 就是按这个前缀认它们）。"""
        return {
            str(v)
            for p in self.params
            for v in p.values()
            if isinstance(v, str) and v.startswith(SEED_FILL_PREFIX)
        }


@pytest.mark.asyncio
async def test_import_refuses_on_a_non_empty_book():
    """三个 agent 只要有一行既有数据 → 整批拒绝、一行不写（否则同一批仓记两遍）。"""
    from backend.shared.agent_ledger_store import (
        POSITION_TABLE,
        import_legacy_seed,
    )

    raw = REAL_SHAPE
    session = _SeedSession(dirty={POSITION_TABLE: [("deepseek-v4-pro", 1)]})
    rep = await import_legacy_seed(
        session,
        tenant_id="default",
        user_id="1001",
        seed=parse_legacy_ledger(raw),
        as_of=date(2026, 9, 25),
    )
    assert rep.applied is False
    assert rep.accounts_written == 0
    assert session.writes == [], "拒绝时不许有任何写语句"
    assert any("deepseek-v4-pro" in r and "持仓行" in r for r in rep.refused)


@pytest.mark.asyncio
async def test_import_writes_accounts_positions_and_seed_fills():
    from backend.shared.agent_ledger_store import import_legacy_seed

    session = _SeedSession()
    rep = await import_legacy_seed(
        session,
        tenant_id="default",
        user_id="1001",
        seed=parse_legacy_ledger(REAL_SHAPE),
        as_of=date(2026, 9, 25),
    )
    assert rep.applied is True and rep.refused == ()
    assert rep.accounts_written == 2
    assert rep.positions_written == 3
    assert rep.fills_written == 3
    assert rep.fills_skipped == 0
    assert rep.cost_total == pytest.approx(14608.0 + 9891.0, abs=0.02)
    assert any("qm_agent_ledger_fill" in w for w in session.writes)
    # 流水键必须是**带前缀的后缀码**：C14 靠这个前缀把它们单独计数（不带 = 当孤儿报）
    assert session.seed_keys() == {
        f"{SEED_FILL_PREFIX}603213.SH",
        f"{SEED_FILL_PREFIX}002074.SZ",
        f"{SEED_FILL_PREFIX}002709.SZ",
    }


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_but_reports_the_plan():
    from backend.shared.agent_ledger_store import import_legacy_seed

    session = _SeedSession()
    rep = await import_legacy_seed(
        session,
        tenant_id="default",
        user_id="1001",
        seed=parse_legacy_ledger(REAL_SHAPE),
        as_of=date(2026, 9, 25),
        dry_run=True,
    )
    assert rep.applied is True and rep.dry_run is True
    assert session.writes == [], "dry-run 一行都不许写"
    assert rep.positions_written == 0
    assert [(a.agent, a.positions) for a in rep.agents] == [
        ("deepseek-v4-flash", 2),
        ("deepseek-v4-pro", 1),
    ]


@pytest.mark.asyncio
async def test_blocked_plan_never_touches_the_session():
    from backend.shared.agent_ledger_store import import_legacy_seed

    session = _SeedSession()
    bad = parse_legacy_ledger(
        {"version": 1, "agents": {"a": {"positions": {}, "virtual_cash": None}}}
    )
    rep = await import_legacy_seed(
        session, tenant_id="default", user_id="1001", seed=bad, as_of=date(2026, 9, 25)
    )
    assert rep.applied is False
    assert rep.refused == bad.problems
    assert session.writes == []


@pytest.mark.asyncio
async def test_empty_plan_is_not_an_error():
    from backend.shared.agent_ledger_store import import_legacy_seed

    session = _SeedSession()
    rep = await import_legacy_seed(
        session,
        tenant_id="default",
        user_id="1001",
        seed=parse_legacy_ledger({"version": 1, "agents": {}}),
        as_of=date(2026, 9, 25),
    )
    assert rep.applied is True and rep.accounts_written == 0
    assert session.writes == []


# --- 接线（源码守卫） -------------------------------------------------------


def test_health_c14_uses_the_shared_seed_prefix():
    """C14 的 seed 前缀必须来自纯核心常量（两处各写一份就会一边认一边不认）。"""
    import inspect

    from backend.scripts.diagnose import health

    assert "SEED_FILL_PREFIX" in inspect.getsource(health.check_c14_agent_ledger_parity)


def test_cli_shares_the_decision_round_identity():
    """CLI 的账户身份与决策轮同源（``resolve_db_account_user``），不许手打。"""
    import inspect

    from backend.scripts import import_agent_ledger_seed as cli

    src = inspect.getsource(cli)
    assert "resolve_db_account_user" in src
    assert "ENV_ACCOUNT_USER" in src


def test_store_has_exactly_two_write_entries():
    """写入口清单：``apply_fill``（成交）与 ``import_legacy_seed``（结转），第三个不许有。"""
    from backend.shared import agent_ledger_store as store

    public_writers = [
        name
        for name in store.__all__
        if name
        in (
            "apply_fill",
            "import_legacy_seed",
            "seed_state",
            "import_seed",
            "write_seed",
        )
    ]
    assert sorted(public_writers) == ["apply_fill", "import_legacy_seed"]


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
async def test_seed_import_on_live_db_round_trips_through_load_ledger() -> None:
    """真库：结转 → ``load_ledger`` 读回的形状与隔壁文件**逐字同形**，且二次结转被拒。

    用随机租户（``t-seed-*``）跑完即删；真身是 dev 库，每次跑完
    ``qm_agent_ledger_*`` 里不该留这家的行。
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

    from backend.shared.agent_ledger_store import (
        ACCOUNT_TABLE,
        FILL_TABLE,
        POSITION_TABLE,
        import_legacy_seed,
        load_ledger,
    )
    from backend.shared.database_manager_v2 import close_database, get_session

    tenant = f"t-seed-{_uuid.uuid4().hex[:8]}"
    user = f"99{_uuid.uuid4().int % 1_000_000:06d}"
    seed = parse_legacy_ledger(REAL_SHAPE)
    try:
        async with get_session(read_only=False) as session:
            first = await import_legacy_seed(
                session, tenant_id=tenant, user_id=user, seed=seed, as_of="2026-09-25"
            )
            await session.commit()
        assert first.applied and first.accounts_written == 2

        async with get_session(read_only=True) as session:
            doc = await load_ledger(session, tenant_id=tenant, user_id=user)
        flash = doc["agents"]["deepseek-v4-flash"]
        assert flash["virtual_cash"] == pytest.approx(95044.8)
        assert set(flash["positions"]) == {"603213.SH", "002074.SZ"}
        assert flash["positions"]["603213.SH"]["volume"] == 700
        assert (
            flash["positions"]["603213.SH"]["buy_ts"] == "2026-09-11T01:37:26.903941Z"
        )

        # 落库的流水行：键带前缀、无订单归属、applied=volume、note 讲清来路
        async with get_session(read_only=True) as session:
            fills = (
                (
                    await session.execute(
                        sa_text(
                            "SELECT fill_key, order_id, trade_date, volume, applied_volume, "
                            "note FROM qm_agent_ledger_fill WHERE tenant_id = :t"
                        ),
                        {"t": tenant},
                    )
                )
                .mappings()
                .all()
            )
        assert {r["fill_key"] for r in fills} == {
            f"{SEED_FILL_PREFIX}603213.SH",
            f"{SEED_FILL_PREFIX}002074.SZ",
            f"{SEED_FILL_PREFIX}002709.SZ",
        }
        by_key = {r["fill_key"]: r for r in fills}
        assert by_key[f"{SEED_FILL_PREFIX}603213.SH"]["trade_date"] == date(2026, 9, 11)
        assert all(
            r["order_id"] == "" and r["applied_volume"] == r["volume"] for r in fills
        )
        assert all("期初结转" in r["note"] for r in fills)

        # 二次结转：空账本纪律生效（正是「同一批仓记两遍」的入口）
        async with get_session(read_only=False) as session:
            again = await import_legacy_seed(
                session, tenant_id=tenant, user_id=user, seed=seed, as_of="2026-09-25"
            )
        assert again.applied is False
        assert again.refused, "非空账本必须拒绝"
    finally:
        async with get_session(read_only=False) as session:
            from sqlalchemy import text as sa_text

            for table in (FILL_TABLE, POSITION_TABLE, ACCOUNT_TABLE):
                await session.execute(
                    sa_text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant}
                )
            await session.commit()
        await close_database()
