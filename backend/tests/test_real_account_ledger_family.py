"""实盘台账「账户家族」读取测试（2026-09-23）。

背景：账户键在 2026-09-18 用户 id 规范化时整键改名
（``tdx-default-00000001`` → ``tdx-default-10000001``），历史行留在旧键、新行写新键，
且行会分落 ``00000001`` / ``10000001`` 两个 user_id 别名。实盘账户页按单一
``account_id`` 读台账 → 权益曲线在改名日断头（实测库里 tdx 家族 08-13..09-16 在旧键、
09-18.. 在新键）。

家族口径与风控档位生产者（``risk_tier_producer``）**同一实现**
（``real_account_ledger_service.account_family``），禁两套定义。
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from backend.services.trade.services.real_account_ledger_service import (
    account_family,
    list_real_account_daily_ledgers_by_family,
    merge_family_ledger_rows,
)


def _row(account_id: str, day: str, moment: str, total: float = 920_000.0):
    return SimpleNamespace(
        account_id=account_id,
        snapshot_date=date.fromisoformat(day),
        last_snapshot_at=datetime.fromisoformat(moment),
        total_asset=total,
    )


# ── account_family：与档位生产者同口径 ────────────────────────────────


def test_account_family_strips_user_suffix():
    assert account_family("tdx-default-00000001") == "tdx-default"
    assert account_family("tdx-default-10000001") == "tdx-default"
    assert account_family("qmt-default-10000001") == "qmt-default"


def test_account_family_is_stable_across_user_id_normalization():
    """改名前后的两个键必须落同一家族（这是本模块存在的全部理由）。"""
    assert account_family("tdx-default-00000001") == account_family(
        "tdx-default-10000001"
    )


def test_account_family_keeps_distinct_middle_segment():
    assert account_family("tdx-extra-10000001") != account_family(
        "tdx-default-10000001"
    )


def test_account_family_empty_is_empty_not_crash():
    assert account_family(None) == ""
    assert account_family("") == ""
    assert account_family("nosuffix") == "nosuffix"


# ── merge_family_ledger_rows：家族过滤 + 同日合并 + 窗口 ──────────────


def test_merge_folds_rename_into_one_continuous_series():
    """改名日前后两个键的行合并成一条按日升序的连续序列。"""
    # Arrange
    rows = [
        _row("tdx-default-10000001", "2026-09-18", "2026-09-18T15:00:00"),
        _row("tdx-default-00000001", "2026-09-16", "2026-09-16T15:00:00"),
        _row("tdx-default-00000001", "2026-09-15", "2026-09-15T15:00:00"),
    ]

    # Act
    merged = merge_family_ledger_rows(rows, family="tdx-default", days=30)

    # Assert
    assert [r.snapshot_date.isoformat() for r in merged] == [
        "2026-09-15",
        "2026-09-16",
        "2026-09-18",
    ]


def test_merge_excludes_other_families_same_user():
    """同一 user 名下的另一座账本（qmt ≈2385 万）不得混进 tdx 家族曲线。"""
    # Arrange
    rows = [
        _row("tdx-default-10000001", "2026-09-18", "2026-09-18T15:00:00", 919_000.0),
        _row("qmt-default-10000001", "2026-09-18", "2026-09-18T15:01:00", 23_800_000.0),
        _row("tdx-default-10000001", "2026-09-19", "2026-09-19T15:00:00", 919_500.0),
    ]

    # Act
    merged = merge_family_ledger_rows(rows, family="tdx-default", days=30)

    # Assert
    assert [r.total_asset for r in merged] == [919_000.0, 919_500.0]


def test_merge_same_day_duplicate_keeps_later_snapshot():
    """改名重叠日：同日两行取较晚快照（旧键行不得覆盖新键行）。"""
    # Arrange：09-18 在旧键（15:00 结算）与新键（23:00 桥）各一行
    rows = [
        _row("tdx-default-00000001", "2026-09-18", "2026-09-18T15:00:00", 918_000.0),
        _row("tdx-default-10000001", "2026-09-18", "2026-09-18T23:00:00", 919_185.63),
    ]

    # Act
    merged = merge_family_ledger_rows(rows, family="tdx-default", days=30)

    # Assert
    assert len(merged) == 1
    assert merged[0].total_asset == 919_185.63


def test_merge_keeps_only_last_days_window():
    # Arrange：5 天，窗口 3
    rows = [
        _row("tdx-default-10000001", f"2026-09-{d:02d}", f"2026-09-{d:02d}T15:00:00")
        for d in range(1, 6)
    ]

    # Act
    merged = merge_family_ledger_rows(rows, family="tdx-default", days=3)

    # Assert
    assert [r.snapshot_date.isoformat() for r in merged] == [
        "2026-09-03",
        "2026-09-04",
        "2026-09-05",
    ]


def test_merge_skips_rows_without_snapshot_date():
    rows = [
        SimpleNamespace(
            account_id="tdx-default-10000001",
            snapshot_date=None,
            last_snapshot_at=datetime(2026, 9, 18, 15, 0),
        ),
        _row("tdx-default-10000001", "2026-09-18", "2026-09-18T15:00:00"),
    ]

    merged = merge_family_ledger_rows(rows, family="tdx-default", days=30)

    assert [r.snapshot_date.isoformat() for r in merged] == ["2026-09-18"]


def test_merge_empty_rows_is_empty_not_placeholder():
    assert merge_family_ledger_rows([], family="tdx-default", days=30) == []


# ── 查询层：user 别名展开 + 家族过滤 ─────────────────────────────────


class _FakeScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.statements: list = []

    async def execute(self, stmt, *args, **kwargs):
        self.statements.append(stmt)
        return _FakeScalarsResult(self.rows)


@pytest.mark.asyncio
async def test_family_query_expands_user_id_aliases():
    """按 user_id 别名族查（改名前后的行分落两个 user_id），再按家族过滤。"""
    from sqlalchemy.dialects import postgresql

    # Arrange：新键行 + 旧 user_id 别名下的旧键行 + 另一座账本（qmt）
    db = _FakeSession(
        [
            _row(
                "tdx-default-10000001", "2026-09-18", "2026-09-18T23:00:00", 919_185.63
            ),
            _row(
                "tdx-default-00000001", "2026-09-17", "2026-09-17T15:00:00", 919_383.27
            ),
            _row(
                "qmt-default-10000001",
                "2026-09-18",
                "2026-09-18T15:00:00",
                23_834_878.76,
            ),
        ]
    )

    # Act
    rows = await list_real_account_daily_ledgers_by_family(
        db,
        tenant_id="default",
        user_id="10000001",
        account_id="tdx-default-10000001",
        days=30,
    )

    # Assert：语句按别名族查 user_id（10000001 与 00000001 都要在）
    params = db.statements[-1].compile(dialect=postgresql.dialect()).params
    user_bind = next(v for k, v in params.items() if k.startswith("user_id_1"))
    assert "10000001" in user_bind and "00000001" in user_bind
    # 只留 tdx 家族、且跨改名键合并
    assert [r.snapshot_date.isoformat() for r in rows] == ["2026-09-17", "2026-09-18"]
    assert all("tdx-default" in r.account_id for r in rows)


@pytest.mark.asyncio
async def test_family_query_empty_account_id_returns_empty_without_query():
    db = _FakeSession(
        [_row("tdx-default-10000001", "2026-09-18", "2026-09-18T23:00:00")]
    )

    rows = await list_real_account_daily_ledgers_by_family(
        db, tenant_id="default", user_id="10000001", account_id="", days=30
    )

    assert rows == []
    assert db.statements == []


# ── 路由读路径：未点名走家族、点名走精确 ─────────────────────────────


def _uses_alias_expansion(stmt) -> bool:
    """家族读的语句按 user_id 别名 IN 查；精确读是 user_id = 单值。"""
    from sqlalchemy.dialects import postgresql

    params = stmt.compile(dialect=postgresql.dialect()).params
    return any(isinstance(v, (list, tuple)) and len(v) > 1 for v in params.values())


@pytest.mark.asyncio
async def test_router_unrequested_account_reads_family():
    """前端不传 account_id（实盘账户页的常态）→ 必须走家族读，否则曲线断头。"""
    from backend.services.live_trading.routers.real_trading_ledger import (
        _read_ledger_rows,
    )

    db = _FakeSession(
        [
            _row("tdx-default-00000001", "2026-09-16", "2026-09-16T15:00:00"),
            _row("tdx-default-10000001", "2026-09-18", "2026-09-18T23:00:00"),
        ]
    )

    rows = await _read_ledger_rows(
        db,
        tenant_id="default",
        user_id="10000001",
        requested_account_id=None,
        resolved_account_id="tdx-default-10000001",
        days=30,
    )

    assert _uses_alias_expansion(db.statements[-1]) is True
    assert [r.snapshot_date.isoformat() for r in rows] == ["2026-09-16", "2026-09-18"]


@pytest.mark.asyncio
async def test_router_explicit_account_id_reads_exact_key():
    """调用方点名账户键 → 精确读（不做家族展开，语义优先）。"""
    from backend.services.live_trading.routers.real_trading_ledger import (
        _read_ledger_rows,
    )

    db = _FakeSession([_row("tdx-old-10000001", "2026-09-16", "2026-09-16T15:00:00")])

    await _read_ledger_rows(
        db,
        tenant_id="default",
        user_id="10000001",
        requested_account_id="tdx-default-00000001",
        resolved_account_id="tdx-default-10000001",
        days=30,
    )

    assert _uses_alias_expansion(db.statements[-1]) is False
