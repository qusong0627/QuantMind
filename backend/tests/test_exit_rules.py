"""T-P2-04 测试：退出规则唯一实现（优先级阶梯/触发快照/三处收敛/引擎接线）。

收敛面：TDX `check_sltp_trigger`（包装）、风控触发器（映射命中类型）、回放
`scan_stop_loss`（low 触发口径）、模拟活盘引擎（新增退出评估）；回测
市价单不顺延语义（T-P2-05b）源断言同批覆盖。
"""

from pathlib import Path

from backend.shared.exit_rules import (
    RULE_HARD_STOP,
    RULE_SIGNAL,
    RULE_TAKE_PROFIT,
    RULE_TIME,
    RULE_TRAILING,
    ExitRuleSet,
    PositionState,
    evaluate_exit,
)

_BACKEND = Path(__file__).resolve().parents[1]


# --- 优先级阶梯与快照 --------------------------------------------------------


def test_hard_stop_first():
    rules = ExitRuleSet(hard_stop_pct=0.05, take_profit_pct=0.08)
    d = evaluate_exit(rules, PositionState(entry_price=10.0, last_price=9.4))
    assert d.should_exit and d.rule_id == RULE_HARD_STOP and d.priority == 1
    assert d.snapshot["line"] == 9.5 and d.snapshot["price"] == 9.4


def test_take_profit_before_trailing_on_overlap():
    """止盈先于移动止损求值（与 check_sltp_trigger 存量顺序兼容）。"""
    rules = ExitRuleSet(take_profit_pct=0.05, trailing_stop_pct=0.30)
    # 从最高 20 回撤到 13（≥30% 回撤线 14）同时 ≥ 止盈线 10.5 —— 二者皆触发
    d = evaluate_exit(
        rules, PositionState(entry_price=10.0, last_price=13.0, high_water_price=20.0)
    )
    assert d.should_exit and d.rule_id == RULE_TAKE_PROFIT


def test_trailing_fires_with_high_water():
    rules = ExitRuleSet(trailing_stop_pct=0.05)
    d = evaluate_exit(
        rules, PositionState(entry_price=10.0, last_price=11.9, high_water_price=12.6)
    )
    assert d.should_exit and d.rule_id == RULE_TRAILING
    assert d.snapshot["high_water"] == 12.6


def test_signal_then_time():
    rules = ExitRuleSet(max_hold_days=5)
    d = evaluate_exit(rules, PositionState(entry_price=10.0, last_price=10.0, hold_days=5))
    assert d.should_exit and d.rule_id == RULE_TIME
    d2 = evaluate_exit(
        ExitRuleSet(hard_stop_pct=0.05),
        PositionState(entry_price=10.0, last_price=10.0),
        signal_gone=True,
        signal_reason="行业 Top1 跌破入场线",
    )
    assert d2.should_exit and d2.rule_id == RULE_SIGNAL and "行业" in d2.reason


def test_no_hit_and_invalid_inputs():
    rules = ExitRuleSet(hard_stop_pct=0.05, take_profit_pct=0.08, max_hold_days=5)
    d = evaluate_exit(rules, PositionState(entry_price=10.0, last_price=10.2, hold_days=2))
    assert not d.should_exit and d.rule_id == ""
    d2 = evaluate_exit(rules, PositionState(entry_price=0.0, last_price=10.0))
    assert not d2.should_exit and "error" in d2.snapshot


# --- TDX 包装（存量行为逐案对照） -------------------------------------------


def test_sltp_wrapper_parity_with_legacy_cases():
    from backend.services.live_trading.services.tdx_quote_feed import check_sltp_trigger

    cfg = {
        "stop_loss_pct": 0.05,
        "take_profit_pct": 0.10,
        "trailing_stop_pct": 0.04,
        "highest_price": 12.0,
    }
    hit, reason = check_sltp_trigger(9.4, 10.0, cfg)
    assert hit and reason == "止损触发 现价9.40 ≤ 9.50"
    hit, reason = check_sltp_trigger(11.1, 10.0, cfg)
    assert hit and reason == "止盈触发 现价11.10 ≥ 11.00"
    # 10.9：低于止盈线 11.00（不触发止盈）、低于移动线 12.0×(1-0.04)=11.52 → 移动止损
    hit, reason = check_sltp_trigger(10.9, 10.0, cfg)
    assert hit and reason.startswith("移动止损 现价10.90 ≤ 11.52（最高 12.00）")
    # 无触发：不含移动止损的配置下 10.5 介于止损/止盈线之间
    cfg_no_trail = {"stop_loss_pct": 0.05, "take_profit_pct": 0.10}
    hit, reason = check_sltp_trigger(10.5, 10.0, cfg_no_trail)
    assert not hit and reason == ""
    hit, reason = check_sltp_trigger(0, 10.0, cfg)
    assert not hit and reason == ""


# --- 回放扫描收敛（low 触发口径） -------------------------------------------


def test_replay_scan_uses_canonical_low_trigger():
    from types import SimpleNamespace

    from backend.services.simulation.replay.proposal import scan_stop_loss

    def _bar(low, open_=10.5, suspended=False):
        return SimpleNamespace(
            low=low, open=open_, suspended=suspended, limit_down=0.0, close=low
        )

    account = {
        "positions": {
            "600036.SH": {"cost": 10.0, "volume": 500, "available_volume": 500},
            "000001.SZ": {"cost": 10.0, "volume": 300, "available_volume": 300},
        }
    }
    bars = {"600036.SH": _bar(9.4), "000001.SZ": _bar(9.6)}
    out = scan_stop_loss(account, bars, 0.05)
    assert len(out) == 1 and out[0]["symbol"] == "600036.SH"
    assert out[0]["origin"] == "stop_loss" and out[0]["stop_price"] == 9.5
    # 停牌不扫
    out2 = scan_stop_loss(account, {"600036.SH": _bar(9.4, suspended=True)}, 0.05)
    assert out2 == []


# --- 接线源断言 --------------------------------------------------------------


def test_three_sites_delegate_to_canonical():
    feed = (_BACKEND / "services/live_trading/services/tdx_quote_feed.py").read_text(
        encoding="utf-8"
    )
    assert "from backend.shared.exit_rules import ExitRuleSet, PositionState, evaluate_exit" in feed

    trigger = (_BACKEND / "services/live_trading/services/risk_trigger_eval.py").read_text(
        encoding="utf-8"
    )
    assert "evaluate_exit(" in trigger and "RULE_HARD_STOP" in trigger

    proposal = (_BACKEND / "services/simulation/replay/proposal.py").read_text(
        encoding="utf-8"
    )
    assert "evaluate_exit(" in proposal


def test_sim_engine_exit_wiring():
    src = (_BACKEND / "services/simulation/engine.py").read_text(encoding="utf-8")
    assert "await self._evaluate_position_exits(" in src  # T-P2-04b 起带状态供给（async）
    assert "orders = exit_orders + orders" in src  # 退出优先于调仓
    assert 'locate(\n                    "RULE:EXIT"' in src or "RULE:EXIT" in src
    assert "SOURCE_SLTP" in src
    assert 'getattr(order, "source", None) or SOURCE_REBALANCE' in src


def test_backtest_market_order_no_retry():
    """T-P2-05b：回测市价单当日不可成交即拒（不顺延），限价单保持挂单。"""
    src = (_BACKEND / "shared/backtest_engine/core/engine.py").read_text(encoding="utf-8")
    assert "市价单不顺延" in src
    assert "order.order_type == OrderType.MARKET" in src
