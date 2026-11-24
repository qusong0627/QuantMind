from datetime import date
from types import SimpleNamespace

import asyncio

import pytest

from backend.services.live_trading.services.risk_lock import (
    RiskLocks,
    filter_buy_orders,
    normalize_lock_symbol,
)
from backend.services.live_trading.services.risk_rule_types import (
    RiskRuleValidationError,
    validate_rule_parameters,
)
from backend.services.live_trading.services.risk_trigger_eval import (
    QuoteView,
    RuleView,
    evaluate_account,
    implicit_stop_loss_rule,
)
from backend.services.live_trading.services import risk_trigger_service as trigger_service
from backend.services.live_trading.services.risk_trigger_service import apply_candidates


@pytest.fixture(autouse=True)
def _silent_notify(monkeypatch):
    monkeypatch.setattr(trigger_service, "_notify", lambda *args, **kwargs: None)


def _rule(rule_type: str, pct: float, **extra) -> RuleView:
    params = {"pct": pct, "trading_mode": "SIMULATION", "markets": ["CN"], **extra}
    return RuleView(
        id=1,
        rule_name=rule_type,
        rule_type=rule_type,
        parameters=params,
        applies_to_all=True,
    )


def _pos(volume=1000, available=1000, cost=10.0):
    return {"volume": volume, "available_volume": available, "cost": cost}


def test_validate_stop_loss_and_index_params():
    sl = validate_rule_parameters("position_stop_loss", {"pct": -0.08})
    assert sl["pct"] == -0.08
    assert sl["trading_mode"] == "SIMULATION"
    market = validate_rule_parameters(
        "market_index_move", {"pct": -0.03, "index": "000300.SH"}
    )
    assert market["index"] == "000300.SH"
    with pytest.raises(RiskRuleValidationError):
        validate_rule_parameters("position_stop_loss", {"pct": 0.08})
    with pytest.raises(RiskRuleValidationError):
        validate_rule_parameters("unknown_type", {})


def test_stop_loss_triggers_when_cost_drawdown_hits():
    candidates = evaluate_account(
        positions={"600036.SH": _pos()},
        quotes={"600036.SH": QuoteView("600036.SH", 9.0)},
        rules=[_rule("position_stop_loss", -0.08)],
        user_id=1,
    )
    assert len(candidates) == 1
    assert candidates[0].status == "pending"
    assert candidates[0].action == "flatten_symbol"
    assert candidates[0].quantity == 1000
    assert candidates[0].pnl_pct == pytest.approx(-0.10)


def test_take_profit_and_tighter_threshold_wins():
    candidates = evaluate_account(
        positions={"600036.SH": _pos(cost=10.0)},
        quotes={"600036.SH": QuoteView("600036.SH", 11.2)},
        rules=[
            _rule("position_take_profit", 0.20),
            RuleView(
                id=2,
                rule_name="tight_tp",
                rule_type="position_take_profit",
                parameters={"pct": 0.10, "trading_mode": "SIMULATION", "markets": ["CN"]},
                applies_to_all=True,
            ),
        ],
        user_id=1,
    )
    assert len(candidates) == 1
    assert candidates[0].rule_id == 2
    assert candidates[0].rule_type == "position_take_profit"


def test_stop_loss_beats_take_profit_when_both_could_apply():
    candidates = evaluate_account(
        positions={"600036.SH": _pos(cost=10.0)},
        quotes={"600036.SH": QuoteView("600036.SH", 8.0)},
        rules=[
            _rule("position_stop_loss", -0.05),
            _rule("position_take_profit", 0.01),
        ],
        user_id=1,
    )
    assert candidates[0].rule_type == "position_stop_loss"


def test_t1_skip_when_available_volume_zero():
    candidates = evaluate_account(
        positions={"600036.SH": _pos(volume=1000, available=0, cost=10.0)},
        quotes={"600036.SH": QuoteView("600036.SH", 9.0)},
        rules=[_rule("position_stop_loss", -0.08)],
        user_id=1,
    )
    assert candidates[0].status == "skipped_t1"
    assert candidates[0].quantity == 0


def test_no_quote_is_skipped():
    candidates = evaluate_account(
        positions={"600036.SH": _pos()},
        quotes={},
        rules=[_rule("position_stop_loss", -0.08)],
        user_id=1,
    )
    assert candidates[0].status == "skipped_no_quote"


def test_market_index_flattens_all_sellable_positions():
    rules = [
        RuleView(
            id=9,
            rule_name="hs300",
            rule_type="market_index_move",
            parameters={
                "pct": -0.03,
                "index": "000300.SH",
                "trading_mode": "SIMULATION",
                "markets": ["CN"],
            },
            applies_to_all=True,
        )
    ]
    candidates = evaluate_account(
        positions={
            "600036.SH": _pos(available=800),
            "000001.SZ": _pos(available=0, volume=500),
        },
        quotes={
            "000300.SH": QuoteView("000300.SH", 3800, pct_chg=-0.035),
            "600036.SH": QuoteView("600036.SH", 10.0),
        },
        rules=rules,
        user_id=1,
    )
    statuses = {item.symbol: item.status for item in candidates}
    assert statuses[normalize_lock_symbol("600036.SH")] == "pending"
    assert statuses[normalize_lock_symbol("000001.SZ")] == "skipped_t1"
    assert all(item.action == "flatten_all" for item in candidates)


def test_implicit_stop_loss_merges_with_db_rules():
    candidates = evaluate_account(
        positions={"SH600036": _pos(cost=10.0)},
        quotes={"600036.SH": QuoteView("600036.SH", 9.4)},
        rules=[implicit_stop_loss_rule(-0.05, 7)],
        user_id=7,
    )
    assert len(candidates) == 1
    assert candidates[0].status == "pending"


def test_filter_buy_orders_honors_symbol_and_account_lock():
    buy = SimpleNamespace(side="BUY", symbol="600036.SH")
    sell = SimpleNamespace(side="SELL", symbol="600036.SH")
    other = SimpleNamespace(side="BUY", symbol="000001.SZ")
    locked = filter_buy_orders([buy, sell, other], RiskLocks(symbols={"600036.SH"}))
    assert [item.symbol for item in locked] == ["600036.SH", "000001.SZ"]
    assert locked[0].side == "SELL"
    frozen = filter_buy_orders([buy, sell, other], RiskLocks(account_frozen=True))
    assert [item.side for item in frozen] == ["SELL"]


class _FakeDB:
    def add(self, _item):
        return None

    async def flush(self):
        return None

    async def commit(self):
        return None


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def delete(self, key):
        self.store.pop(key, None)

    def scan_iter(self, match, count=200):
        return [key for key in self.store if str(key).startswith(match.rstrip("*"))]


def test_dry_run_does_not_call_execute():
    async def boom(**_kwargs):
        raise AssertionError("dry-run must not execute")

    candidate = evaluate_account(
        positions={"600036.SH": _pos()},
        quotes={"600036.SH": QuoteView("600036.SH", 9.0)},
        rules=[_rule("position_stop_loss", -0.08)],
        user_id=1,
    )[0]
    applied = asyncio.run(
        apply_candidates(
            _FakeDB(),
            None,
            [candidate],
            tenant_id="default",
            user_id=1,
            trade_date=date(2026, 9, 13),
            dry_run=True,
            execute_order=boom,
        )
    )
    assert applied[0].status == "dry_run"


def test_real_mode_is_alert_only():
    called = {"n": 0}

    async def boom(**_kwargs):
        called["n"] += 1
        raise AssertionError("REAL must not place simulation orders")

    rule = _rule("position_stop_loss", -0.08)
    rule.parameters["trading_mode"] = "REAL"
    candidate = evaluate_account(
        positions={"600036.SH": _pos()},
        quotes={"600036.SH": QuoteView("600036.SH", 9.0)},
        rules=[rule],
        user_id=1,
        account_mode="REAL",
    )[0]
    applied = asyncio.run(
        apply_candidates(
            _FakeDB(),
            _FakeRedis(),
            [candidate],
            tenant_id="default",
            user_id=1,
            trade_date=date(2026, 9, 13),
            execute_order=boom,
        )
    )
    assert applied[0].status == "alert_only"
    assert called["n"] == 0


def test_filled_sim_sell_writes_symbol_lock():
    redis = _FakeRedis()

    async def fake_execute(**_kwargs):
        return {"success": True, "order_id": "ord-1", "message": "filled"}

    candidate = evaluate_account(
        positions={"600036.SH": _pos()},
        quotes={"600036.SH": QuoteView("600036.SH", 9.0)},
        rules=[_rule("position_stop_loss", -0.08)],
        user_id=1,
    )[0]
    applied = asyncio.run(
        apply_candidates(
            _FakeDB(),
            redis,
            [candidate],
            tenant_id="default",
            user_id=1,
            trade_date=date(2026, 9, 13),
            execute_order=fake_execute,
        )
    )
    assert applied[0].status == "filled"
    assert any("risk:lock:symbol:" in key for key in redis.store)


# ── 扫描噪音治理：非可执行事件去重 + 失败退避 ─────────────────────────────


def _collect_events(monkeypatch) -> list[str]:
    events: list[str] = []

    async def fake_persist(_db, candidate, **_kwargs):
        events.append(str(candidate.status))
        return candidate

    monkeypatch.setattr(trigger_service, "persist_event", fake_persist)
    return events


def _stop_loss_candidate(quotes=None):
    return evaluate_account(
        positions={"600036.SH": _pos()},
        quotes=quotes
        if quotes is not None
        else {"600036.SH": QuoteView("600036.SH", 9.0)},
        rules=[_rule("position_stop_loss", -0.08)],
        user_id=1,
    )[0]


def test_skipped_no_quote_events_are_throttled(monkeypatch):
    """无行情持仓每 60s 都会重新命中，窗口内只允许落一条事件。"""
    events = _collect_events(monkeypatch)
    redis = _FakeRedis()
    candidate = _stop_loss_candidate(quotes={})
    assert candidate.status == "skipped_no_quote"

    for _ in range(5):
        asyncio.run(
            apply_candidates(
                _FakeDB(),
                redis,
                [_stop_loss_candidate(quotes={})],
                tenant_id="default",
                user_id=1,
                trade_date=date(2026, 9, 13),
            )
        )

    assert events == ["skipped_no_quote"]


def test_skipped_t1_events_are_throttled_per_day(monkeypatch):
    events = _collect_events(monkeypatch)
    redis = _FakeRedis()

    def make():
        return evaluate_account(
            positions={"600036.SH": _pos(available=0, cost=10.0)},
            quotes={"600036.SH": QuoteView("600036.SH", 9.0)},
            rules=[_rule("position_stop_loss", -0.08)],
            user_id=1,
        )[0]

    for _ in range(3):
        asyncio.run(
            apply_candidates(
                _FakeDB(),
                redis,
                [make()],
                tenant_id="default",
                user_id=1,
                trade_date=date(2026, 9, 13),
            )
        )

    assert events == ["skipped_t1"]


def test_failed_sell_sets_backoff_and_skips_next_attempts(monkeypatch):
    """跌停封死导致成交失败后，退避窗内不再重复下单（原来每 60s 撞一次）。"""
    events = _collect_events(monkeypatch)
    redis = _FakeRedis()
    calls = {"n": 0}

    async def fail(**_kwargs):
        calls["n"] += 1
        return {
            "success": False,
            "message": "Limit-down locked, sell order cannot be filled",
        }

    applied = asyncio.run(
        apply_candidates(
            _FakeDB(),
            redis,
            [_stop_loss_candidate()],
            tenant_id="default",
            user_id=1,
            trade_date=date(2026, 9, 13),
            execute_order=fail,
        )
    )
    assert applied[0].status == "failed"
    assert calls["n"] == 1
    assert any("risk:retry:" in key for key in redis.store)

    # 退避窗内再扫到同一标的：不再下单、不再写事件
    applied = asyncio.run(
        apply_candidates(
            _FakeDB(),
            redis,
            [_stop_loss_candidate()],
            tenant_id="default",
            user_id=1,
            trade_date=date(2026, 9, 13),
            execute_order=fail,
        )
    )
    assert applied[0].status == "skipped_backoff"
    assert calls["n"] == 1
    assert events == ["failed"]


def test_failed_events_are_throttled_within_window(monkeypatch):
    """退避窗过后再次失败也只留一条事件（默认 1 小时窗口）。"""
    events = _collect_events(monkeypatch)
    redis = _FakeRedis()

    async def fail(**_kwargs):
        return {
            "success": False,
            "message": "Limit-down locked, sell order cannot be filled",
        }

    for _ in range(3):
        # 模拟退避到期：只清退避键，事件去重键保留
        for key in [k for k in redis.store if str(k).startswith("risk:retry:")]:
            redis.delete(key)
        asyncio.run(
            apply_candidates(
                _FakeDB(),
                redis,
                [_stop_loss_candidate()],
                tenant_id="default",
                user_id=1,
                trade_date=date(2026, 9, 13),
                execute_order=fail,
            )
        )

    assert events == ["failed"]


def test_success_clears_retry_backoff(monkeypatch):
    _collect_events(monkeypatch)
    redis = _FakeRedis()

    async def fail(**_kwargs):
        return {"success": False, "message": "Limit-down locked"}

    async def ok(**_kwargs):
        return {"success": True, "order_id": "ord-1", "message": "filled"}

    asyncio.run(
        apply_candidates(
            _FakeDB(),
            redis,
            [_stop_loss_candidate()],
            tenant_id="default",
            user_id=1,
            trade_date=date(2026, 9, 13),
            execute_order=fail,
        )
    )
    assert any("risk:retry:" in key for key in redis.store)

    for key in [k for k in redis.store if str(k).startswith("risk:fired:")]:
        redis.delete(key)
    applied = asyncio.run(
        apply_candidates(
            _FakeDB(),
            redis,
            [_stop_loss_candidate()],
            tenant_id="default",
            user_id=1,
            trade_date=date(2026, 9, 13),
            execute_order=ok,
        )
    )
    assert applied[0].status == "skipped_backoff"

    # 退避到期后成交成功 → 退避键被清掉
    for key in [k for k in redis.store if str(k).startswith("risk:retry:")]:
        redis.delete(key)
    applied = asyncio.run(
        apply_candidates(
            _FakeDB(),
            redis,
            [_stop_loss_candidate()],
            tenant_id="default",
            user_id=1,
            trade_date=date(2026, 9, 13),
            execute_order=ok,
        )
    )
    assert applied[0].status == "filled"
    assert not any(str(k).startswith("risk:retry:") for k in redis.store)
