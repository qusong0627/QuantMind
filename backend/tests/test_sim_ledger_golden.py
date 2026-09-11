"""模拟盘黄金链路单测：成交→台账→投影→Redis 口径（确权回归）。

覆盖此前线上故障的三个回归点（均无 DB 依赖，可本地跑）：
1. live 成交必须能进台账：apply_trade_to_account_snapshot 现金账对、build_cash_entries 符号对
2. 持仓 cost/cost_price 双口径：任一入口重建 Redis 都不丢成本（Lua 读 cost）
3. 时区：台账时间列 naive 口径，_naive_utc 剥离 awareness（asyncpg 混写即整笔失败）
4. 单向确权：对账/get_account 缺键只能走 _rebuild_from_ledger，
   _rebuild_from_pg 仅保留灾难恢复手工脚本标记
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import DateTime

from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.cash_ledger import SimulationCashLedger
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.services.ledger_service import (
    SimulationLedgerService,
    _naive_utc,
)
from backend.services.simulation.services.projection_service import (
    SimulationProjectionService,
)


def _stub_trade(side="buy", qty=1000.0, price=10.0, fee=5.0):
    return SimpleNamespace(
        side=SimpleNamespace(value=side),
        trade_action=None,
        position_side="long",
        trade_value=qty * price,
        commission=fee,
        stamp_duty=0.0,
        transfer_fee=0.0,
        total_fee=fee,
        quantity=qty,
        price=price,
        trade_id="test-trade-id",
        executed_at=datetime(2026, 9, 10, 6, 50, 29, tzinfo=timezone.utc),
    )


def _account(**overrides):
    params = {
        "account_id": "sim:default:0",
        "tenant_id": "default",
        "user_id": "0",
        "initial_equity": 1_000_000.0,
        "cash": 1_000_000.0,
        "available_cash": 1_000_000.0,
        "total_asset": 1_000_000.0,
        "equity": 1_000_000.0,
    }
    params.update(overrides)
    return SimulationAccount(**params)


# ── 1. 成交→台账现金账 ────────────────────────────────────────────────


def test_apply_trade_buy_deducts_cash_and_fee():
    before = {"cash": 1_000_000.0, "available_cash": 1_000_000.0}
    after = SimulationLedgerService.apply_trade_to_account_snapshot(
        trade=_stub_trade("buy", 1000.0, 10.0, 5.0), account_snapshot=before
    )
    assert after["cash"] == 1_000_000.0 - 10_000.0 - 5.0
    assert after["available_cash"] == after["cash"]
    # 入参快照不被原地修改
    assert before["cash"] == 1_000_000.0


def test_apply_trade_sell_adds_cash_minus_fee():
    before = {"cash": 500.0, "available_cash": 500.0}
    after = SimulationLedgerService.apply_trade_to_account_snapshot(
        trade=_stub_trade("sell", 100.0, 9.0, 5.0), account_snapshot=before
    )
    assert after["cash"] == 500.0 + 900.0 - 5.0


def test_build_cash_entries_signs():
    buys = SimulationLedgerService.build_cash_entries(
        side="buy", trade_value=10_000.0, commission=5.0, stamp_duty=0.0, transfer_fee=0.1
    )
    assert [e.event_type for e in buys] == ["BUY_SETTLEMENT", "COMMISSION", "TRANSFER_FEE"]
    assert all(e.amount < 0 for e in buys)
    sells = SimulationLedgerService.build_cash_entries(
        side="sell", trade_value=9_000.0, commission=5.0, stamp_duty=9.0, transfer_fee=0.1
    )
    assert sells[0].event_type == "SELL_PROCEEDS" and sells[0].amount > 0
    assert all(e.amount < 0 for e in sells[1:])


# ── 2. cost 双口径 ───────────────────────────────────────────────────


def test_build_cache_payload_backfills_cost_aliases():
    acct = _account(cash=699.31)
    positions = {
        "600036.SH": {"volume": 100.0, "price": 11.75, "market_value": 1175.0, "cost_price": 11.8},
        "000001.SZ": {"volume": 200.0, "price": 6.0, "market_value": 1200.0, "cost": 6.1},
    }
    payload = SimulationProjectionService.build_cache_payload(
        account=acct, positions=positions, source="test"
    )
    p1 = payload["positions"]["600036.SH"]
    p2 = payload["positions"]["000001.SZ"]
    # 只有 cost_price 的 → 补 cost（Lua 读 cost，不补则成本归零）
    assert p1["cost"] == 11.8
    # 只有 cost 的 → 补 cost_price（台账侧读 cost_price）
    assert p2["cost_price"] == 6.1
    assert payload["market_value"] == 2375.0
    assert payload["total_asset"] == round(699.31 + 2375.0, 2)


# ── 3. 时区哨兵 ──────────────────────────────────────────────────────


def test_naive_utc_strips_awareness_keeping_instant():
    aware = datetime(2026, 9, 10, 6, 50, 29, tzinfo=timezone.utc)
    out = _naive_utc(aware)
    assert out.tzinfo is None
    assert out == datetime(2026, 9, 10, 6, 50, 29)


def test_naive_utc_passthrough_and_none():
    naive = datetime(2026, 9, 10, 6, 50, 29)
    assert _naive_utc(naive) == naive
    assert _naive_utc(None).tzinfo is None


def test_ledger_datetime_columns_are_naive():
    """台账业务时间列必须是 naive 口径（DateTime(timezone=False)）。

    有人新增 timestamptz 业务列又不经 _naive_utc 边界处理时，此测试失败，
    防止 asyncpg naive/aware 混写重演（整笔成交落库失败）。
    例外：TimestampMixin 的 created_at/updated_at 按设计是 aware（配 aware 默认值）。
    """
    for model in (SimulationAccount, SimulationCashLedger, SimulationPositionLot):
        for column in model.__table__.columns.values():
            if column.name in ("created_at", "updated_at"):
                continue
            if isinstance(column.type, DateTime):
                assert column.type.timezone is not True, (
                    f"{model.__tablename__}.{column.name} 是 timestamptz，"
                    "台账业务时间统一 naive UTC，写入前必须经 _naive_utc 剥离"
                )


# ── 4. 单向确权（源码静态哨兵） ───────────────────────────────────────


def _read_source(relpath: str) -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[2] / relpath).read_text(encoding="utf-8")


def test_reconcile_and_cache_miss_use_ledger_not_trades_replay():
    reconcile_src = _read_source(
        "backend/services/simulation/services/reconcile_service.py"
    )
    assert "_rebuild_from_ledger" in reconcile_src
    assert "_rebuild_from_pg" not in reconcile_src
    manager_src = _read_source(
        "backend/services/trade_shared/simulation_manager.py"
    )
    assert "await self._rebuild_from_ledger(" in manager_src


def test_trades_replay_kept_as_manual_disaster_recovery_only():
    manager_src = _read_source(
        "backend/services/trade_shared/simulation_manager.py"
    )
    assert "灾难恢复" in manager_src
    # 自动链路不得再调用回放：全仓库只允许定义 + 本测试引用
    import re

    callers = [
        line.strip()
        for line in manager_src.splitlines()
        if "_rebuild_from_pg(" in line and "async def _rebuild_from_pg" not in line
    ]
    assert callers == [], f"_rebuild_from_pg 仍有自动调用方: {callers}"
