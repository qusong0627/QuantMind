"""模拟盘现金字段一致性回归（纯函数 / 源码口径，无 DB/Redis 依赖）。

口径：模拟盘无现金冻结机制（订单即时全成，无挂单占用），故
available_cash 恒等于 cash、frozen_cash 恒为 0。历史成交/重估 Lua 只更新
cash，导致 Redis/PG 的 available_cash、frozen_cash 停在旧值，前端"冻结"
显示失真。
"""

from __future__ import annotations

from backend.services.simulation.services.equity_settlement_worker import (
    _REMARK_LUA,
)
from backend.services.simulation.services.ledger_service import (
    SimulationLedgerService,
)
from backend.services.simulation.services.projection_service import (
    SimulationProjectionService,
)
from backend.services.trade_shared.simulation_manager import SimulationAccountManager


class _FakeAccount:
    """build_cache_payload 只读少量属性，缺省走 getattr 兜底。"""

    def __init__(self, *, cash=0.0, long_market_value=0.0, short_market_value=0.0):
        self.cash = cash
        self.long_market_value = long_market_value
        self.short_market_value = short_market_value
        self.initial_equity = 1_000_000.0
        self.liabilities = 0.0
        self.maintenance_margin_ratio = 0.0


class _FakeProjectionAccount:
    cash = 0.0
    available_cash = 999.0  # 失真旧值，应被派生覆盖
    frozen_cash = 999.0
    long_market_value = 0.0
    short_market_value = 0.0
    total_asset = 0.0
    liabilities = 0.0
    equity = 0.0
    maintenance_margin_ratio = 0.0
    last_trade_at = None
    last_projected_at = None


def test_build_cache_payload_cash_fields_derived_from_cash():
    payload = SimulationProjectionService.build_cache_payload(
        account=_FakeAccount(cash=1234.56, long_market_value=1000.0),
        positions=None,
        source="test",
    )
    assert payload["cash"] == 1234.56
    assert payload["available_cash"] == 1234.56
    assert payload["frozen_cash"] == 0.0


def test_sync_account_projection_cash_fields_derived_from_cash():
    svc = SimulationLedgerService(db=None)
    account = _FakeProjectionAccount()
    svc._sync_account_projection(
        account,
        {"cash": 500.0, "market_value": 200.0, "total_asset": 700.0},
    )
    assert account.cash == 500.0
    assert account.available_cash == 500.0
    assert account.frozen_cash == 0.0


def test_trade_lua_keeps_cash_fields_in_sync():
    lua = SimulationAccountManager(redis=None)._update_balance_lua
    assert "account.available_cash = new_cash" in lua
    assert "account.frozen_cash = 0" in lua


def test_remark_lua_keeps_cash_fields_in_sync():
    assert "account.available_cash = tonumber(account.cash or 0)" in _REMARK_LUA
    assert "account.frozen_cash = 0" in _REMARK_LUA
