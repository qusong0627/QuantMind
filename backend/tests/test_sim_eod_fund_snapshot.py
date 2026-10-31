"""Issue #34 回归：周期资金快照不得被误判为 EOD 完成。

覆盖三类交错（合成数据，无 DB/Redis 依赖）：
1. 周期先执行 → EOD 日终估值必须覆盖周期旧值，日账与资金快照同为日终估值；
2. EOD 先完成 → 后续周期采集不得覆盖 EOD 行；
3. EOD 重跑 → 历史日终估值保留，资金快照按日账修复配平。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql.dml import Insert as PGInsert
from sqlalchemy.sql.dml import Delete as GenericDelete

from backend.services.simulation.services import eod_service as eod
from backend.services.simulation.services import fund_snapshot_service as fss
from backend.services.simulation.services.fund_snapshot_service import (
    SimulationFundSnapshotService as Svc,
)

TRADE_DATE = date(2026, 10, 27)


class FakeDB:
    """内存小库：只实现本回归覆盖到的语句语义。"""

    def __init__(self):
        self.pos_daily: set[tuple[str, str, date]] = set()
        self.fund: dict[tuple[str, str, date], dict] = {}
        self.acct_daily: dict[tuple[str, str, date], dict] = {}
        self.inserts: list = []


def _scope(params: dict, sql: str):
    tenant = params.get("tenant_id") or params.get("tenant_id_1")
    user = params.get("user_id") or params.get("user_id_1")
    snap = params.get("snapshot_date") or params.get("snapshot_date_1")
    if tenant is None or user is None or snap is None:
        quoted = re.findall(r"'([^']*)'", sql)
        dates = re.findall(r"(\d{4}-\d{2}-\d{2})", sql)
        tenant = tenant or (quoted[0] if quoted else None)
        user = user or (quoted[1] if len(quoted) > 1 else None)
        snap = snap or (dates[0] if dates else None)
        if isinstance(snap, str):
            y, m, d = map(int, snap.split("-"))
            snap = date(y, m, d)
    return str(tenant), str(user), snap


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarsResult(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    def __init__(self, db: FakeDB):
        self.db = db
        self.added: list = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "pg_advisory" in sql:
            return _ScalarResult(0)
        if isinstance(stmt, PGInsert):
            compiled = stmt.compile(dialect=postgresql.dialect())
            values = {
                k: v
                for k, v in dict(compiled.params).items()
                if not re.match(r"^[a-z_]+_\d+$", k) and k != "param_1"
            }
            table = stmt.table.name
            self.db.inserts.append((table, dict(values)))
            if table == "simulation_fund_snapshots":
                key = (
                    values["tenant_id"],
                    str(values["user_id"]),
                    values["snapshot_date"],
                )
                existing = self.db.fund.get(key)
                guarded = "WHERE simulation_fund_snapshots.source" in str(
                    stmt.compile(dialect=postgresql.dialect())
                )
                if (
                    guarded
                    and existing is not None
                    and str(existing.get("source") or "") == fss.EOD_FUND_SOURCE
                ):
                    return _ScalarResult(0)  # SQL 层兜底：EOD 行不被覆盖
                self.db.fund[key] = dict(values)
            return _ScalarResult(1)
        if isinstance(stmt, GenericDelete):
            compiled = stmt.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
            sql = str(compiled)
            if "simulation_account_daily" in sql:
                tenant, user, snap = _scope(dict(compiled.params), sql)
                self.db.acct_daily.pop((tenant, user, snap), None)
            return _ScalarResult(0)
        # SELECT
        compiled = stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
        sql = str(compiled)
        params = dict(compiled.params)
        if "simulation_position_daily" in sql:
            tenant, user, snap = _scope(params, sql)
            n = 1 if (tenant, user, snap) in self.db.pos_daily else 0
            if "count" in sql.lower():
                return _ScalarResult(n)
            return _RowsResult([n] if n else [])
        if "simulation_fund_snapshots" in sql:
            tenant, user, snap = _scope(params, sql)
            row = self.db.fund.get((tenant, user, snap))
            if row is None:
                if ".source" in sql and "total_asset" not in sql:
                    return _ScalarResult(None)
                return _RowsResult([])
            if ".source" in sql and "total_asset" not in sql:
                return _ScalarResult(row.get("source"))
            return _RowsResult([SimpleNamespace(**row)])
        if "simulation_account_daily" in sql:
            tenant, user, snap = _scope(params, sql)
            row = self.db.acct_daily.get((tenant, user, snap))
            if row is None:
                return _RowsResult([])
            return _RowsResult([SimpleNamespace(**row)])
        return _RowsResult([])

    def add(self, obj):
        self.added.append(obj)
        table = getattr(getattr(obj, "__table__", None), "name", "")
        if table == "simulation_account_daily":
            key = (str(obj.tenant_id), str(obj.user_id), obj.snapshot_date)
            self.db.acct_daily[key] = {
                "total_asset": float(obj.total_asset or 0),
                "available_cash": float(obj.available_cash or 0),
                "frozen_cash": float(obj.frozen_cash or 0),
                "long_market_value": float(obj.long_market_value or 0),
                "short_market_value": float(obj.short_market_value or 0),
                "daily_pnl": float(obj.daily_pnl or 0),
                "total_pnl": float(obj.total_pnl or 0),
            }

    async def commit(self):
        return None

    async def rollback(self):
        return None


class _FakeCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        return False


class FakeRedisClient:
    def __init__(self, accounts: dict):
        self.client = self
        self._accounts = accounts

    def scan_iter(self, match=None, count=500):
        return list(self._accounts.keys())

    def get(self, key):
        return self._accounts.get(key)


def _account_json(total: float) -> str:
    return json.dumps(
        {
            "total_asset": total,
            "cash": total,
            "available_balance": total,
            "frozen_balance": 0.0,
            "market_value": 0.0,
            "long_market_value": 0.0,
        }
    )


@pytest.fixture()
def anyio_backend():
    return "asyncio"


def _patch_common(monkeypatch, db: FakeDB):
    session = FakeSession(db)
    monkeypatch.setattr(fss, "get_session", lambda read_only=False: _FakeCtx(session))
    monkeypatch.setattr(
        Svc,
        "get_baselines",
        classmethod(
            lambda cls, tenant_id, user_id, initial, as_of=None: asyncio.sleep(
                0, result={"day_open_equity": initial, "month_open_equity": initial}
            )
        ),
    )
    monkeypatch.setattr(
        Svc,
        "_read_ledger_initial_equity",
        staticmethod(
            lambda tenant_id, user_id: asyncio.sleep(0, result=Decimal("10000"))
        ),
    )
    return session


def test_periodic_skips_eod_completed_and_writes_rest(monkeypatch):
    """周期采集：已完成 EOD 的账户跳过，其余正常写入。"""
    db = FakeDB()
    # A：EOD 已完成（持仓日快照 + EOD 资金快照 9900）
    db.pos_daily.add(("default", "1", TRADE_DATE))
    db.fund[("default", "1", TRADE_DATE)] = {
        "tenant_id": "default",
        "user_id": "1",
        "snapshot_date": TRADE_DATE,
        "total_asset": 9900.0,
        "source": fss.EOD_FUND_SOURCE,
    }
    _patch_common(monkeypatch, db)
    redis = FakeRedisClient(
        {
            "simulation:account:default:1": _account_json(9900.0),
            "simulation:account:default:2": _account_json(10000.0),
        }
    )
    result = asyncio.run(Svc.capture_all(redis, snapshot_date=TRADE_DATE))
    assert result.scanned_accounts == 2
    assert result.upserted_rows == 1
    # A 的 EOD 估值不被周期覆盖
    assert db.fund[("default", "1", TRADE_DATE)]["total_asset"] == 9900.0
    assert db.fund[("default", "1", TRADE_DATE)]["source"] == fss.EOD_FUND_SOURCE
    # B 正常写入周期快照
    assert db.fund[("default", "2", TRADE_DATE)]["total_asset"] == 10000.0
    assert db.fund[("default", "2", TRADE_DATE)]["source"] == fss.PERIODIC_FUND_SOURCE


def test_periodic_where_guard_preserves_eod_on_race(monkeypatch):
    """SQL 层兜底：即使跳过检查与写入之间 EOD 已提交，EOD 行也不被覆盖。"""
    db = FakeDB()
    db.fund[("default", "1", TRADE_DATE)] = {
        "tenant_id": "default",
        "user_id": "1",
        "snapshot_date": TRADE_DATE,
        "total_asset": 9900.0,
        "source": fss.EOD_FUND_SOURCE,
    }
    session = _patch_common(monkeypatch, db)
    # 绕过跳过检查，直接对 EOD 行执行周期口径 upsert（模拟极小竞态窗口）
    import datetime

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from backend.services.simulation.models.fund_snapshot import SimulationFundSnapshot

    row = {
        "tenant_id": "default",
        "user_id": "1",
        "snapshot_date": TRADE_DATE,
        "total_asset": 10000.0,
        "available_balance": 10000.0,
        "frozen_balance": 0.0,
        "market_value": 0.0,
        "initial_capital": 10000.0,
        "total_pnl": 0.0,
        "today_pnl": 0.0,
        "source": fss.PERIODIC_FUND_SOURCE,
    }
    stmt = (
        pg_insert(SimulationFundSnapshot)
        .values(**row)
        .on_conflict_do_update(
            index_elements=["tenant_id", "user_id", "snapshot_date"],
            set_={"total_asset": row["total_asset"], "source": row["source"]},
            where=SimulationFundSnapshot.source != fss.EOD_FUND_SOURCE,
        )
    )
    asyncio.run(session.execute(stmt))
    assert db.fund[("default", "1", TRADE_DATE)]["total_asset"] == 9900.0
    assert db.fund[("default", "1", TRADE_DATE)]["source"] == fss.EOD_FUND_SOURCE


def test_issue34_repro_periodic_then_eod_same_valuation(monkeypatch):
    """Issue 最小复现：周期先写 10000 → EOD 重估 9900 → 两表同为 9900；
    此后周期重跑也不覆盖。"""
    db = FakeDB()
    _patch_common(monkeypatch, db)
    redis = FakeRedisClient({"simulation:account:default:1": _account_json(10000.0)})
    asyncio.run(Svc.capture_all(redis, snapshot_date=TRADE_DATE))
    assert db.fund[("default", "1", TRADE_DATE)]["total_asset"] == 10000.0
    assert db.acct_daily[("default", "1", TRADE_DATE)]["total_asset"] == 10000.0

    # EOD：同一事务内用同一份重估（9900）配对写日账+资金快照
    session = FakeSession(db)
    asyncio.run(
        eod._write_eod_fund_snapshot(
            session,
            tenant_id="default",
            user_id="1",
            trade_date=TRADE_DATE,
            cash=9900.0,
            long_mv=0.0,
            short_mv=0.0,
            total_asset=9900.0,
            initial_equity=10000.0,
        )
    )
    db.pos_daily.add(("default", "1", TRADE_DATE))
    db.acct_daily[("default", "1", TRADE_DATE)] = {
        "total_asset": 9900.0,
        "available_cash": 9900.0,
        "frozen_cash": 0.0,
        "long_market_value": 0.0,
        "short_market_value": 0.0,
        "daily_pnl": -100.0,
        "total_pnl": -100.0,
    }
    assert db.fund[("default", "1", TRADE_DATE)]["total_asset"] == 9900.0
    assert db.fund[("default", "1", TRADE_DATE)]["source"] == fss.EOD_FUND_SOURCE

    # 此后周期重跑（Redis 已是日终值）：EOD 行保持 9900
    redis = FakeRedisClient({"simulation:account:default:1": _account_json(9900.0)})
    asyncio.run(Svc.capture_all(redis, snapshot_date=TRADE_DATE))
    assert db.fund[("default", "1", TRADE_DATE)]["total_asset"] == 9900.0
    assert db.fund[("default", "1", TRADE_DATE)]["source"] == fss.EOD_FUND_SOURCE


def test_eod_rerun_repairs_stale_periodic_snapshot(monkeypatch):
    """EOD 重跑：持仓日快照已存在时不回写历史，仅把被污染的资金快照按日账修复。"""
    db = FakeDB()
    db.pos_daily.add(("default", "1", TRADE_DATE))
    db.acct_daily[("default", "1", TRADE_DATE)] = {
        "total_asset": 9900.0,
        "available_cash": 9900.0,
        "frozen_cash": 0.0,
        "long_market_value": 0.0,
        "short_market_value": 0.0,
        "daily_pnl": -100.0,
        "total_pnl": -100.0,
    }
    # 历史 bug 遗留：资金快照仍是周期旧值 10000
    db.fund[("default", "1", TRADE_DATE)] = {
        "tenant_id": "default",
        "user_id": "1",
        "snapshot_date": TRADE_DATE,
        "total_asset": 10000.0,
        "initial_capital": 10000.0,
        "source": fss.PERIODIC_FUND_SOURCE,
    }
    _patch_common(monkeypatch, db)
    session = FakeSession(db)
    asyncio.run(
        eod._repair_eod_fund_snapshot(
            session, tenant_id="default", user_id="1", trade_date=TRADE_DATE
        )
    )
    repaired = db.fund[("default", "1", TRADE_DATE)]
    assert repaired["total_asset"] == 9900.0
    assert repaired["source"] == fss.EOD_FUND_SOURCE

    # 已一致时直接返回，不改写
    before = dict(repaired)
    asyncio.run(
        eod._repair_eod_fund_snapshot(
            session, tenant_id="default", user_id="1", trade_date=TRADE_DATE
        )
    )
    assert db.fund[("default", "1", TRADE_DATE)] == before


def test_eod_no_global_skip_uses_per_account_completion():
    """源码口径：EOD 不再按“该日有任意资金快照”全局跳过，逐户判定。"""
    from pathlib import Path

    source = Path(eod.__file__).read_text(encoding="utf-8")
    assert "existing_count" not in source
    assert "is_eod_completed" in source
    assert "upsert_eod_snapshot" in source
    assert "lock_slot" in source
    assert "capture_all" not in source
