"""风控引擎核心测试（T-RC-01）：每规则正/反/边界 + 引擎聚合 + 状态机 + fail-closed。

边界口径：阈值比较"等于放行、超过才拦"；缺失关键字段（资金/行情新鲜度）=拒（fail-closed）；
建议类缺失（行业占比）=WARN。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from backend.shared.risk import RiskContext, RiskGateCore, all_rules, next_state
from backend.shared.risk.contracts import ACTION_HALT, ACTION_REJECT, ACTION_WARN
from backend.shared.risk.registry import RuleSpec

CST = timezone(timedelta(hours=8))
# 2026-09-16 是周三（交易日）
WED_10AM = datetime(2026, 9, 16, 10, 0, tzinfo=CST).timestamp()
SAT_10AM = datetime(2026, 9, 19, 10, 0, tzinfo=CST).timestamp()

FULL_CONFIG = {r.rule_id: dict(r.default_params) for r in all_rules()}


def _ctx(**over) -> RiskContext:
    base = {
        "market": "CN",
        "symbol": "600036.SH",
        "side": "BUY",
        "order_type": "LIMIT",
        "price": 40.0,
        "quantity": 100,
        "now_ts": WED_10AM,
        "available_cash": 1_000_000.0,
        "sellable_volume": 1000,
        "total_assets": 1_000_000.0,
        "position_pct": 0.0,
        "industry_pct": 0.05,
        "daily_pnl_pct": 0.0,
        "last_price": 40.0,
        "quote_age_s": 1.0,
        "orders_last_minute": 0,
        "orders_today": 0,
        "cancels_today": 0,
    }
    base.update(over)
    return RiskContext(**base)


def _verdict(**over):
    return RiskGateCore().evaluate(_ctx(**over), FULL_CONFIG, version=7)


# ── L0 ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_l0_kill_switch_halts():
    v = _verdict(kill_switch=True)
    assert not v.passed and v.halt
    assert v.decisions[0].action == ACTION_HALT and v.decisions[0].rule_id == "l0.kill_switch"


@pytest.mark.unit
@pytest.mark.parametrize(
    "ts,expect_pass",
    [
        (WED_10AM, True),
        (datetime(2026, 9, 16, 9, 20, tzinfo=CST).timestamp(), True),    # 集合竞价内（09:15 起）
        (datetime(2026, 9, 16, 11, 29, tzinfo=CST).timestamp(), True),
        (datetime(2026, 9, 16, 11, 30, tzinfo=CST).timestamp(), False),  # 边界：右开
        (datetime(2026, 9, 16, 12, 0, tzinfo=CST).timestamp(), False),   # 午休
        (datetime(2026, 9, 16, 15, 0, tzinfo=CST).timestamp(), False),   # 收盘右开
        (SAT_10AM, False),
    ],
)
def test_l0_session_boundaries(ts, expect_pass):
    v = _verdict(now_ts=ts)
    assert v.passed is expect_pass


@pytest.mark.unit
def test_l0_unknown_market_fail_closed():
    v = _verdict(market="XX")
    assert not v.passed
    assert any(d.rule_id == "l0.session" for d in v.rejects)


@pytest.mark.unit
def test_l0_clock_drift_boundary():
    assert _verdict(clock_skew_ms=500.0).passed          # 等于阈值放行
    assert not _verdict(clock_skew_ms=501.0).passed
    assert _verdict(clock_skew_ms=None).passed           # 未测量不拦


# ── L1 ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_l1_available_cash_and_missing_snapshot():
    assert _verdict(available_cash=4000.0).passed                       # 等于金额放行
    assert not _verdict(available_cash=3999.0).passed
    assert not _verdict(available_cash=None).passed                     # fail-closed
    assert _verdict(side="SELL", available_cash=None).passed            # SELL 不适用资金规则


@pytest.mark.unit
def test_l1_t1_sellable():
    assert _verdict(side="SELL", quantity=1000, sellable_volume=1000).passed
    assert not _verdict(side="SELL", quantity=1001, sellable_volume=1000).passed
    assert not _verdict(side="SELL", quantity=100, sellable_volume=None).passed


@pytest.mark.unit
def test_l1_position_cap_boundary():
    # 持仓 10% + 本单 5% = 15% 恰好触线放行；超一线即拒（注意 last_price 需对齐，免触 L3 偏离闸门）
    assert _verdict(total_assets=100_000.0, position_pct=0.10, price=50.0, quantity=100, last_price=50.0).passed
    assert not _verdict(total_assets=100_000.0, position_pct=0.10, price=50.5, quantity=100, last_price=50.0).passed
    assert not _verdict(total_assets=None).passed
    assert not _verdict(total_assets=0.0).passed


@pytest.mark.unit
def test_l1_industry_cap_warn_when_unknown():
    v = _verdict(industry_pct=None)
    assert v.passed and any(d.action == ACTION_WARN and d.rule_id == "l1.industry_cap" for d in v.warns)
    assert not _verdict(industry_pct=0.30, total_assets=100_000.0, price=40.0, quantity=100).passed  # 30%+4%


@pytest.mark.unit
def test_l1_daily_loss_limit():
    assert _verdict(daily_pnl_pct=-2.9).passed
    assert not _verdict(daily_pnl_pct=-3.0).passed    # 等于限额即停
    assert _verdict(daily_pnl_pct=None).passed


# ── L3 ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_l3_max_order_value_boundary():
    cfg = {**FULL_CONFIG, "l3.max_order_value": {"max_value": 4000.0}}
    assert RiskGateCore().evaluate(_ctx(), cfg).passed                    # 等于放行
    assert not RiskGateCore().evaluate(_ctx(quantity=101), cfg).passed


@pytest.mark.unit
def test_l3_price_deviation_and_forced_exit():
    assert _verdict(price=40.8).passed                                    # 2% 触线放行
    assert not _verdict(price=40.9).passed
    assert _verdict(price=44.0, forced_exit=True).passed                  # 强平 10% 在 sanity 内
    assert not _verdict(price=48.1, forced_exit=True).passed              # 20.25% 超 sanity


@pytest.mark.unit
def test_l3_stale_quote():
    assert _verdict(quote_age_s=5.0).passed
    assert not _verdict(quote_age_s=5.001).passed
    assert not _verdict(quote_age_s=None).passed


@pytest.mark.unit
def test_l3_frequency_and_cancel_ratio():
    assert not _verdict(orders_last_minute=20).passed
    assert _verdict(orders_last_minute=19).passed
    v = _verdict(orders_today=11, cancels_today=5)                        # 45% 撤单率 → WARN 不拒
    assert v.passed and any(d.rule_id == "l3.cancel_ratio" for d in v.warns)
    assert _verdict(orders_today=9, cancels_today=9).passed               # 样本不足跳过


@pytest.mark.unit
def test_l3_self_trade_and_duplicate():
    assert not _verdict(recent_symbol_sides=(("600036.SH", "SELL"),)).passed
    assert _verdict(recent_symbol_sides=(("600036.SH", "BUY"), ("000001.SZ", "SELL"))).passed
    assert not _verdict(fingerprint="fp1", recent_fingerprints=("fp1",)).passed
    assert _verdict(fingerprint="fp2", recent_fingerprints=("fp1",)).passed


@pytest.mark.unit
def test_l3_lot_size_boards():
    assert _verdict(quantity=200).passed
    assert not _verdict(quantity=150).passed
    assert _verdict(side="SELL", quantity=37, sellable_volume=100).passed  # 卖出允许零股
    assert _verdict(symbol="688981.SH", quantity=200).passed
    assert not _verdict(symbol="688981.SH", quantity=300).passed          # 科创板 200 股整手
    assert not _verdict(quantity=0).passed


# ── L6 ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_l6_book_and_contract():
    assert not _verdict(book_crossed=True).passed
    assert not _verdict(book_empty=True).passed
    assert not _verdict(contract_ok=False).passed


# ── 引擎语义 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_engine_opt_in_and_always_on():
    """最小配置：仅 always_on（急停/时段）执行；其余规则未配置即跳过。"""
    v = RiskGateCore().evaluate(_ctx(), {}, version=3)
    assert v.passed and v.config_version == 3
    assert set(v.checked_rules) == {"l0.kill_switch", "l0.session"}


@pytest.mark.unit
def test_engine_rule_exception_fail_closed():
    def _boom(ctx, params):
        raise RuntimeError("rule crashed")

    spec = RuleSpec(rule_id="l9.boom", level="L3", description="test", fn=_boom)
    v = RiskGateCore(specs=(spec,)).evaluate(_ctx(), {"l9.boom": {}})
    assert not v.passed
    assert v.rejects[0].rule_id == "l9.boom" and "fail-closed" in v.rejects[0].reason


@pytest.mark.unit
def test_engine_determinism_and_perf_smoke():
    core = RiskGateCore()
    ctx = _ctx()
    v1 = core.evaluate(ctx, FULL_CONFIG, version=7)
    v2 = core.evaluate(ctx, FULL_CONFIG, version=7)
    assert v1 == v2  # 同输入同裁决（dataclass 值等价）

    t0 = time.perf_counter()
    n = 3000
    for _ in range(n):
        core.evaluate(ctx, FULL_CONFIG, version=7)
    avg_ms = (time.perf_counter() - t0) / n * 1000
    assert avg_ms < 1.0, f"平均 {avg_ms:.3f}ms 超 1ms 预算"


# ── 状态机 ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_state_machine_upgrade_immediate_downgrade_manual():
    assert next_state("NORMAL", caution=True) == ("CAUTION", "信号升级（NORMAL→CAUTION）")
    assert next_state("NORMAL", halt=True)[0] == "HALT"                 # 直达最严重
    # 降级：无人工确认不降；人工确认单步降
    s, why = next_state("HALT")
    assert s == "HALT" and "人工确认" in why
    assert next_state("HALT", manual_confirm=True)[0] == "RESTRICT"
    assert next_state("CAUTION", manual_confirm=True)[0] == "NORMAL"
    # 信号仍在（CAUTION 级）且人工确认 → 降至信号推断级（不是盲目跳级）
    assert next_state("RESTRICT", caution=True, manual_confirm=True)[0] == "CAUTION"
    # 信号未解除（halt 仍在）→ 不降
    assert next_state("HALT", halt=True, manual_confirm=True)[0] == "HALT"


@pytest.mark.unit
def test_state_machine_position_and_buy_rules():
    from backend.shared.risk import allows_buy, position_cap_pct

    assert allows_buy("NORMAL") and allows_buy("CAUTION")
    assert not allows_buy("RESTRICT") and not allows_buy("HALT")
    assert position_cap_pct("NORMAL") == pytest.approx(0.95)
    assert position_cap_pct("CAUTION") == pytest.approx(0.60)
    assert position_cap_pct("HALT") == pytest.approx(0.0)
    assert position_cap_pct("CAUTION", {"CAUTION": 0.5}) == pytest.approx(0.5)
