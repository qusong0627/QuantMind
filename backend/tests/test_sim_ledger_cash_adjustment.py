"""``SimulationLedgerService.record_cash_adjustment``：显式注资的 PG 侧。

用假 session：不碰 PG——断言的是「加了哪一行、账户投影怎么动、提交权在调用方」。
Redis 侧的原子入金由 ``test_simulation_fund.py`` 覆盖；两本书同额由调用方
（决策轮默认注资器）编排，见 ``test_decision_executor.py`` 第 8 节。
"""

from __future__ import annotations

import pytest

from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.cash_ledger import SimulationCashLedger
from backend.services.simulation.services.ledger_service import SimulationLedgerService


class _FakeSession:
    def __init__(self, account: SimulationAccount | None) -> None:
        self._account = account
        self.added: list = []
        self.commits = 0

    async def get(self, model, pk):
        return self._account

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:  # pragma: no cover - 提交权在调用方
        self.commits += 1
        raise AssertionError("record_cash_adjustment 不应自行 commit")


@pytest.fixture(autouse=True)
def _skip_market_contract(monkeypatch):
    """契约自愈（ensure_accounts_market_contract_async）要连真库；此处替掉。"""
    import backend.shared.ledger_contract as contract

    async def _ok() -> bool:
        return True

    monkeypatch.setattr(contract, "ensure_accounts_market_contract_async", _ok)


def _account(**kw) -> SimulationAccount:
    base = {
        "account_id": "sim:default:10000001",
        "tenant_id": "default",
        "user_id": "10000001",
        "market": "CN",
        "cash": 100.0,
        "available_cash": 100.0,
        "frozen_cash": 0.0,
        "total_asset": 900.0,
        "equity": 900.0,
        "long_market_value": 800.0,
    }
    base.update(kw)
    return SimulationAccount(**base)


@pytest.mark.asyncio
async def test_adjustment_writes_the_row_and_the_projection_without_committing() -> (
    None
):
    account = _account()
    db = _FakeSession(account)
    service = SimulationLedgerService(db)

    balance = await service.record_cash_adjustment(
        tenant_id="default",
        user_id=10000001,
        market="CN",
        amount=150.0,
        ref_id="rnd-1",
        note="决策轮镜像资金保障",
    )

    assert balance == 250.0
    assert account.cash == 250.0
    assert account.available_cash == 250.0
    assert account.total_asset == 1050.0
    assert account.equity == 1050.0
    assert account.frozen_cash == 0.0
    assert account.last_projected_at is not None

    (row,) = db.added
    assert isinstance(row, SimulationCashLedger)
    assert row.event_type == "MIRROR_FUNDING"
    assert row.ref_type == "funding"
    assert row.ref_id == "rnd-1"
    assert row.amount == 150.0
    assert row.balance_after == 250.0
    assert row.market == "CN"
    # naive UTC（台账时间列口径），aware 值会在 asyncpg 上抛错
    assert row.trade_date is not None and row.trade_date.tzinfo is None
    assert row.occurred_at is not None and row.occurred_at.tzinfo is None
    assert db.commits == 0


@pytest.mark.asyncio
async def test_adjustment_builds_a_missing_account_row_from_the_snapshot() -> None:
    """账户行缺失：用注资前的 Redis 快照建行再增量——不给不存在的账户记 0 快照。"""
    db = _FakeSession(None)
    service = SimulationLedgerService(db)

    balance = await service.record_cash_adjustment(
        tenant_id="default",
        user_id="10000001",
        market="CN",
        amount=150.0,
        account_snapshot={"cash": 100.0, "available_cash": 60.0, "total_asset": 900.0},
    )

    account = next(o for o in db.added if isinstance(o, SimulationAccount))
    assert account.cash == 250.0
    assert account.available_cash == 210.0
    assert account.total_asset == 1050.0
    assert account.equity == 1050.0
    assert account.initial_equity == 900.0
    assert account.frozen_cash == 40.0  # 快照里已有的冻结缺口被保留，不抹平
    assert balance == 250.0


@pytest.mark.asyncio
async def test_adjustment_refuses_nonpositive_amounts() -> None:
    account = _account()
    db = _FakeSession(account)
    service = SimulationLedgerService(db)

    for bad in (0.0, -1.0, float("nan")):
        with pytest.raises(ValueError):
            await service.record_cash_adjustment(
                tenant_id="default", user_id=10000001, market="CN", amount=bad
            )
    assert account.cash == 100.0
    assert db.added == []
