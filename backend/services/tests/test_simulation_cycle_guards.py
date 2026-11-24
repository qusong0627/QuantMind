"""模拟盘单轮护栏回归测试。

覆盖三处此前缺失的约束：
1. 等权口径按 topk 而非实际选中数归一化（样本不足不得放大单票目标）
2. max_orders_per_cycle 真正截断单轮订单数，卖单优先
3. 下了单却零成交必须判 failed，不得报 succeeded
"""

from datetime import date

from backend.services.simulation.engine import ExecutionReport, SimulationEngine
from backend.services.simulation.services.rebalance_calculator import (
    Order,
    RebalanceCalculator,
    StrategyConfig,
    WeightMode,
)
from backend.services.simulation.services.signal_loader import SignalScore
from backend.services.simulation.services.simulation_hosted_scheduler import (
    report_to_hosted_result,
)

_TODAY = date(2026, 9, 28)


def _sig(symbol: str, score: float) -> SignalScore:
    return SignalScore(
        symbol=symbol,
        score=score,
        trade_date=_TODAY,
        run_id="run_test",
        tenant_id="default",
        user_id="10000001",
    )


def _report(**kwargs) -> ExecutionReport:
    base = dict(
        tenant_id="default",
        user_id="10000001",
        strategy_id="12",
        run_id="run_test",
        executed_at=None,
    )
    base.update(kwargs)
    return ExecutionReport(**base)


# ── 1. 等权口径 ──────────────────────────────────────────────────────────


def test_equal_weight_uses_topk_when_samples_insufficient():
    """topk=50 但只有 2 只可交易：单票目标应为 2%，不是 50%。"""
    calc = RebalanceCalculator()
    strategy = StrategyConfig(topk=50, weight_mode=WeightMode.EQUAL)
    weights = calc._calc_weights([_sig("A", 3.0), _sig("B", 2.0)], strategy)
    assert weights == {"A": 0.02, "B": 0.02}
    assert sum(weights.values()) < 1.0  # 不足部分留现金


def test_equal_weight_full_sample():
    """选中数 == topk 时与既有口径一致（1/topk）。"""
    calc = RebalanceCalculator()
    strategy = StrategyConfig(topk=4, weight_mode=WeightMode.EQUAL)
    weights = calc._calc_weights([_sig(f"S{i}", 1.0) for i in range(4)], strategy)
    assert all(abs(w - 0.25) < 1e-12 for w in weights.values())


def test_score_weighted_normalizes_by_score():
    """score_weighted 保持分数占比语义（总权重 1）。"""
    calc = RebalanceCalculator()
    strategy = StrategyConfig(topk=10, weight_mode=WeightMode.SCORE_WEIGHTED)
    weights = calc._calc_weights([_sig("A", 3.0), _sig("B", 1.0)], strategy)
    assert abs(weights["A"] - 0.75) < 1e-12
    assert abs(weights["B"] - 0.25) < 1e-12


def test_score_weighted_non_positive_falls_back_to_equal():
    calc = RebalanceCalculator()
    strategy = StrategyConfig(topk=10, weight_mode=WeightMode.SCORE_WEIGHTED)
    weights = calc._calc_weights([_sig("A", 0.0), _sig("B", 0.0)], strategy)
    assert abs(weights["A"] - 0.1) < 1e-12


# ── 2. 单轮订单数截断 ────────────────────────────────────────────────────


def _order(symbol: str, side: str) -> Order:
    return Order(symbol=symbol, side=side, quantity=100, price=10.0)


def test_truncate_orders_keeps_all_sells_and_limits_buys():
    orders = [
        _order("S1", "SELL"),
        _order("S2", "SELL"),
        _order("B1", "BUY"),
        _order("B2", "BUY"),
        _order("B3", "BUY"),
    ]
    kept = SimulationEngine._truncate_orders(orders, 3)
    assert [o.symbol for o in kept] == ["S1", "S2", "B1"]


def test_truncate_orders_noop_under_limit():
    orders = [_order("S1", "SELL"), _order("B1", "BUY")]
    assert SimulationEngine._truncate_orders(orders, 20) is orders


def test_truncate_orders_never_drops_sells():
    """卖单超出上限也全部保留：截断减仓单会让风险敞口关不掉。"""
    orders = [_order(f"S{i}", "SELL") for i in range(5)] + [_order("B1", "BUY")]
    kept = SimulationEngine._truncate_orders(orders, 2)
    assert len(kept) == 5
    assert all(o.side == "SELL" for o in kept)


def test_resolve_max_orders_build_raises_limit_to_topk():
    """建仓轮上限低于 topk 时抬到 topk，否则账户永久少配。"""
    assert SimulationEngine._resolve_max_orders(20, topk=50, is_build=True) == 50


def test_resolve_max_orders_build_keeps_larger_explicit_limit():
    assert SimulationEngine._resolve_max_orders(80, topk=50, is_build=True) == 80


def test_resolve_max_orders_rebalance_honours_explicit_limit():
    """日常调仓不改写用户显式设置的限流值。"""
    assert SimulationEngine._resolve_max_orders(20, topk=50, is_build=False) == 20


def test_resolve_max_orders_refill_raises_limit_to_topk():
    """补仓轮（持仓 < topk）同样抬到 topk，否则止损缺口只能每次补 20 只。"""
    assert (
        SimulationEngine._resolve_max_orders(
            20, topk=50, is_build=False, is_refill=True
        )
        == 50
    )


def test_resolve_max_orders_refill_keeps_larger_explicit_limit():
    assert (
        SimulationEngine._resolve_max_orders(
            80, topk=50, is_build=False, is_refill=True
        )
        == 80
    )


def test_resolve_max_orders_unset_means_no_truncation():
    assert SimulationEngine._resolve_max_orders(None, topk=50, is_build=True) == 0
    assert SimulationEngine._resolve_max_orders(0, topk=50, is_build=True) == 0


# ── 3. 空转判失败 ────────────────────────────────────────────────────────


def test_no_fill_is_failed_not_succeeded():
    result = report_to_hosted_result(_report(order_count=2, rejected_count=2))
    assert result["status"] == "failed"
    assert "no_fill" in (result["error"] or "")


def test_zero_orders_is_succeeded():
    """确实无需调仓（0 单）不算失败。"""
    result = report_to_hosted_result(_report(order_count=0))
    assert result["status"] == "succeeded"


def test_partial_fill_is_succeeded():
    result = report_to_hosted_result(
        _report(order_count=2, filled_count=1, rejected_count=1)
    )
    assert result["status"] == "succeeded"


def test_explicit_error_is_failed():
    result = report_to_hosted_result(_report(order_count=0, error="无可用信号"))
    assert result["status"] == "failed"
    assert result["error"] == "无可用信号"
