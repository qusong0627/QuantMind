"""对账/融券口径回归测试（纯函数，无 Redis/DB 依赖）。

覆盖本轮修复：
1. 持仓键唯一口径：SYMBOL / SYMBOL::short（多空分开，不再按 :: 裸切）
2. 融券现金/负债/冻结资金：撮合侧与台账投影共用同一实现
3. 重建 payload 保留 PG 不可考的 Redis 独有字段并按 short_proceeds 重算总资产
"""

from pathlib import Path
from types import SimpleNamespace

from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.services.ledger_service import (
    SimulationLedgerService,
)
from backend.services.simulation.services.projection_service import (
    SimulationProjectionService,
)
from backend.services.simulation.services.reconcile_service import (
    _positions_by_symbol,
    _report_symbol,
)
from backend.shared.simulation_margin_math import margin_trade_deltas
from backend.shared.simulation_position_keys import (
    build_position_key,
    split_position_key,
)


def _services_dir() -> Path:
    return (
        Path(__file__).resolve().parents[1] / "services" / "simulation" / "services"
    )


# ── 1. 持仓键唯一口径 ────────────────────────────────────────────────


def test_build_and_split_position_key_round_trip():
    assert build_position_key("600036.sh", "long") == "600036.SH"
    assert build_position_key("600036.sh", "short") == "600036.SH::short"
    # 兼容历史键形
    assert split_position_key("600036.SH::short") == ("600036.SH", "short")
    assert split_position_key("600036.SH:short") == ("600036.SH", "short")
    assert split_position_key("600036.SH::long") == ("600036.SH", "long")
    assert split_position_key("600036.SH") == ("600036.SH", "long")


def test_positions_by_symbol_normalizes_short_key_forms():
    a = _positions_by_symbol({"600036.SH::short": {"volume": 100.0}})
    b = _positions_by_symbol({"600036.SH:short": {"volume": 100.0}})
    assert a == b == {("600036.SH", "short"): 100.0}
    assert _report_symbol("600036.SH", "short") == "600036.SH::short"
    assert _report_symbol("600036.SH", "long") == "600036.SH"


def test_positions_by_symbol_keeps_long_and_short_separate():
    out = _positions_by_symbol(
        {
            "600036.SH": {"volume": 100.0},
            "600036.SH::short": {"volume": 100.0},
        }
    )
    assert out == {("600036.SH", "long"): 100.0, ("600036.SH", "short"): 100.0}


def test_projection_short_key_uses_shared_builder():
    src = (_services_dir() / "projection_service.py").read_text(encoding="utf-8")
    assert "build_position_key(normalized_symbol, side)" in src


# ── 2. 融券口径：撮合侧与台账投影一致 ────────────────────────────────


def _short_trade(side, qty, price, fee):
    return SimpleNamespace(
        side=SimpleNamespace(value=side),
        symbol="600036.SH",
        quantity=qty,
        price=price,
        trade_value=qty * price,
        total_fee=fee,
    )


def test_ledger_sell_to_open_matches_margin_math():
    before = {
        "cash": 1_000_000.0,
        "available_cash": 1_000_000.0,
        "positions": {},
        "short_proceeds": 0.0,
        "liabilities": 0.0,
    }
    order = SimpleNamespace(
        symbol="600036.SH", position_side="short", trade_action="sell_to_open"
    )
    after = SimulationLedgerService.apply_trade_to_account_snapshot(
        trade=_short_trade("sell", 1000.0, 10.0, 5.0),
        account_snapshot=before,
        order=order,
    )
    deltas = margin_trade_deltas(
        trade_action="sell_to_open", gross=10_000.0, quantity=1000.0, pos_cost=0.0
    )
    assert after["cash"] == 1_000_000.0 + deltas["cash"]
    assert after["available_cash"] == after["cash"]
    assert after["short_proceeds"] == deltas["short_proceeds"] == 10_000.0
    assert after["liabilities"] == deltas["liabilities"] == 10_000.0


def test_ledger_buy_to_close_matches_margin_math():
    before = {
        "cash": 1_000_000.0,
        "available_cash": 1_000_000.0,
        "positions": {
            "600036.SH::short": {"volume": 1000.0, "cost": 10.0, "side": "short"}
        },
        "short_proceeds": 10_000.0,
        "liabilities": 10_000.0,
    }
    order = SimpleNamespace(
        symbol="600036.SH", position_side="short", trade_action="buy_to_close"
    )
    after = SimulationLedgerService.apply_trade_to_account_snapshot(
        trade=_short_trade("buy", 1000.0, 9.0, 5.0),
        account_snapshot=before,
        order=order,
    )
    deltas = margin_trade_deltas(
        trade_action="buy_to_close", gross=9_000.0, quantity=1000.0, pos_cost=10.0
    )
    assert after["cash"] == 1_000_000.0 + deltas["cash"]
    assert after["short_proceeds"] == 0.0
    assert after["liabilities"] == 0.0


def test_ledger_without_order_keeps_long_semantics():
    """SimTrade 无 position_side/trade_action：不传 order 时按多头口径。"""
    before = {"cash": 1_000_000.0, "available_cash": 1_000_000.0}
    after = SimulationLedgerService.apply_trade_to_account_snapshot(
        trade=_short_trade("sell", 1000.0, 10.0, 5.0),
        account_snapshot=before,
    )
    assert after["cash"] == 1_000_000.0 + 10_000.0 - 5.0


def test_remark_lua_includes_short_proceeds():
    src = (_services_dir() / "equity_settlement_worker.py").read_text(
        encoding="utf-8"
    )
    assert "short_proceeds + long_mv - short_mv" in src


# ── 3. 重建 payload 保留 Redis 独有字段 ──────────────────────────────


def test_merge_preserved_keeps_short_proceeds_and_recomputes_total():
    live = {
        "short_proceeds": 10_000.0,
        "warning_level": "warning",
        "market": "CN",
        "t1_settlement_date": "2026-09-24",
    }
    rebuilt = {
        "cash": 990.0,
        "long_market_value": 0.0,
        "short_market_value": 0.0,
        "market_value": 0.0,
        "total_asset": 990.0,
        "equity": 990.0,
    }
    out = SimulationProjectionService.merge_preserved(live, rebuilt)
    assert out["short_proceeds"] == 10_000.0
    assert out["total_asset"] == 10_990.0
    assert out["equity"] == 10_990.0
    assert out["warning_level"] == "warning"
    assert out["market"] == "CN"
    assert out["t1_settlement_date"] == "2026-09-24"


def test_build_cache_payload_includes_short_proceeds_in_total_asset():
    acct = SimulationAccount(
        account_id="sim:default:1",
        tenant_id="default",
        user_id="1",
        initial_equity=1_000_000.0,
        cash=1000.0,
        total_asset=1000.0,
        equity=1000.0,
    )
    payload = SimulationProjectionService.build_cache_payload(
        account=acct, positions=None, source="test", short_proceeds=500.0
    )
    assert payload["short_proceeds"] == 500.0
    assert payload["total_asset"] == 1500.0
    assert payload["equity"] == 1500.0


def test_reconcile_autofix_preserves_redis_only_fields():
    src = (_services_dir() / "reconcile_service.py").read_text(encoding="utf-8")
    assert "merge_preserved" in src
    assert "locked_execution" in src
