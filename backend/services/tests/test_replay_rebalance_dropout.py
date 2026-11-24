"""回放调仓引擎 TopkDropout 增量调仓测试（对齐回测引擎语义）。

背景：旧实现每期按当日完整 topk 重建目标持仓，分数排名小幅波动就导致
每期全量轮换（一天卖几十只）。重构后与回测引擎 TopkDropoutStrategy 对齐：
每期最多轮换 n_drop 只，且支持调仓周期（rebalance_days）。
"""

from __future__ import annotations

from datetime import date

from backend.services.simulation.services.rebalance_calculator import (
    Quote,
    RebalanceCalculator,
    SimulationAccount,
    StrategyConfig,
)
from backend.services.simulation.services.signal_loader import SignalScore

TD = date(2026, 3, 4)


def _sig(symbol: str, score: float) -> SignalScore:
    return SignalScore(
        symbol=symbol,
        score=score,
        trade_date=TD,
        run_id="t",
        tenant_id="t",
        user_id="t",
    )


def _quote(symbol: str, price: float = 10.0) -> Quote:
    return Quote(symbol=symbol, current_price=price)


def _account(held: dict[str, int], cash: float = 0.0) -> SimulationAccount:
    positions = {sym: {"volume": vol} for sym, vol in held.items() if vol > 0}
    market_value = sum(v * 10.0 for v in held.values())
    return SimulationAccount(
        cash=cash, total_asset=cash + market_value, positions=positions
    )


def _orders_by_side(orders):
    sells = sorted(o.symbol for o in orders if o.side == "SELL")
    buys = sorted(o.symbol for o in orders if o.side == "BUY")
    return sells, buys


class TestTopkDropout:
    def test_first_day_buys_full_topk(self):
        """无持仓（首日）→ 全量买入 topk，与回测首日建仓一致。"""
        calc = RebalanceCalculator()
        signals = [_sig("A", 0.9), _sig("B", 0.8), _sig("C", 0.7), _sig("D", 0.6)]
        strategy = StrategyConfig(topk=3, n_drop=1, max_position_pct=1.0)
        account = _account({}, cash=1_000_000)
        quotes = {s.symbol: _quote(s.symbol) for s in signals}

        orders = calc.calculate(signals, strategy, quotes, account)

        sells, buys = _orders_by_side(orders)
        assert sells == []
        assert buys == ["A", "B", "C"]

    def test_tradable_fill_down_to_topk(self):
        """真 TOP 不可交易时，从全排序继续往下补位直到凑满 topk。"""
        calc = RebalanceCalculator()
        # A/B 涨停不可买；应补到 C/D/E
        signals = [
            _sig("A", 0.99),
            _sig("B", 0.98),
            _sig("C", 0.90),
            _sig("D", 0.80),
            _sig("E", 0.70),
            _sig("F", 0.10),
        ]
        strategy = StrategyConfig(topk=3, n_drop=0, max_position_pct=1.0)
        account = _account({}, cash=1_000_000)
        quotes = {
            "A": Quote(symbol="A", current_price=10.0, is_limit_up=True),
            "B": Quote(symbol="B", current_price=10.0, is_limit_up=True),
            "C": _quote("C"),
            "D": _quote("D"),
            "E": _quote("E"),
            "F": _quote("F"),
        }

        orders = calc.calculate(signals, strategy, quotes, account)

        sells, buys = _orders_by_side(orders)
        assert sells == []
        assert buys == ["C", "D", "E"]

    def test_dropout_swaps_only_n_drop(self):
        """持仓部分跌出 topk → 只卖分数最低的 n_drop 只，只买等量新股。"""
        calc = RebalanceCalculator()
        # 持仓 A/B/C；今日 top3 = A/B/D（C 跌出，D 新进）
        signals = [_sig("A", 0.9), _sig("B", 0.8), _sig("D", 0.7), _sig("C", 0.2)]
        strategy = StrategyConfig(topk=3, n_drop=1, max_position_pct=1.0)
        account = _account({"A": 100, "B": 100, "C": 100}, cash=100_000)
        quotes = {s.symbol: _quote(s.symbol) for s in signals}

        orders = calc.calculate(signals, strategy, quotes, account, day_index=5)

        sells, buys = _orders_by_side(orders)
        assert sells == ["C"]
        assert buys == ["D"]

    def test_all_held_still_in_topk_no_orders(self):
        """持仓全部仍在 topk 内 → 无轮换，不产生任何指令。"""
        calc = RebalanceCalculator()
        signals = [_sig("A", 0.9), _sig("B", 0.8), _sig("C", 0.7), _sig("D", 0.6)]
        strategy = StrategyConfig(topk=3, n_drop=1, max_position_pct=1.0)
        account = _account({"A": 100, "B": 100, "C": 100}, cash=100_000)
        quotes = {s.symbol: _quote(s.symbol) for s in signals}

        orders = calc.calculate(signals, strategy, quotes, account, day_index=3)

        assert orders == []

    def test_drop_capped_at_n_drop(self):
        """持仓全部跌出 topk 也只卖分数最低的 n_drop 只（渐进轮换）。"""
        calc = RebalanceCalculator()
        # 持仓 X/Y/Z 全部跌出 top3（A/B/C）
        signals = [
            _sig("A", 0.9), _sig("B", 0.8), _sig("C", 0.7),
            _sig("X", 0.3), _sig("Z", 0.2), _sig("Y", 0.1),
        ]
        strategy = StrategyConfig(topk=3, n_drop=2, max_position_pct=1.0)
        account = _account({"X": 100, "Y": 100, "Z": 100}, cash=100_000)
        quotes = {s.symbol: _quote(s.symbol) for s in signals}

        orders = calc.calculate(signals, strategy, quotes, account, day_index=1)

        sells, buys = _orders_by_side(orders)
        assert sells == ["Y", "Z"]  # 分数最低的两只
        assert buys == ["A", "B"]   # 分数最高的两只新股

    def test_dropout_buy_count_capped_by_sellable(self):
        """剔除股部分无行情：新增只数被钳到「有行情可卖」的剔除股数（买卖对账）。

        回归场景：盘中实时行情覆盖不全（实测 258/3055），10 只剔除股只有 2 只有
        行情——旧实现仍按计划剔除数 10 只去分摊买入，仓位越调越胖（50→53）。
        """
        calc = RebalanceCalculator()
        signals = [
            _sig("A", 0.9), _sig("B", 0.8), _sig("C", 0.7),
            _sig("X", 0.3), _sig("Y", 0.2), _sig("Z", 0.1),
        ]
        strategy = StrategyConfig(topk=3, n_drop=2, max_position_pct=1.0)
        account = _account({"X": 100, "Y": 100, "Z": 100}, cash=100_000)
        # Y 缺行情：只有 Z 能释放资金
        quotes = {s.symbol: _quote(s.symbol) for s in signals if s.symbol != "Y"}

        orders = calc.calculate(signals, strategy, quotes, account, day_index=1)

        sells, buys = _orders_by_side(orders)
        assert sells == ["Z"]  # Y 无行情 → 卖单被丢弃（默认不兜底）
        assert buys == ["A"]   # 新增上限钳到 1 只，不再按计划剔除数买 2 只

    def test_dropout_missing_quote_sell_uses_cost_when_force_exit(self):
        """force_exit_on_limit_down=True 时，无行情剔除股按持仓成本价挂单留痕。"""
        calc = RebalanceCalculator()
        signals = [
            _sig("A", 0.9), _sig("B", 0.8), _sig("C", 0.7),
            _sig("X", 0.3), _sig("Y", 0.2), _sig("Z", 0.1),
        ]
        strategy = StrategyConfig(
            topk=3,
            n_drop=2,
            max_position_pct=1.0,
            force_exit_on_limit_down=True,
        )
        account = _account({"X": 100, "Y": 100, "Z": 100}, cash=100_000)
        account.positions["Y"]["cost"] = 12.5  # 无行情时回退用成本价
        quotes = {s.symbol: _quote(s.symbol) for s in signals if s.symbol != "Y"}

        orders = calc.calculate(signals, strategy, quotes, account, day_index=1)

        sells, buys = _orders_by_side(orders)
        assert sells == ["Y", "Z"]
        y_order = next(o for o in orders if o.symbol == "Y" and o.side == "SELL")
        assert y_order.price == 12.5
        # 资金仍只按有行情的 Z 计，买入上限 1 只（不超买）
        assert buys == ["A"]

    def test_refill_after_risk_stop_loss_wiped_positions(self):
        """独立风控（-8% 止损）把持仓清到 1 只后，调仓必须按缺口补回 topk。

        回归场景：2026-11 止损逐日清仓，50 只 → 41 → … → 1，调仓只做「等量
        轮换」，每天卖多少买多少，缺口永远补不回来，98% 资产变成现金。
        """
        calc = RebalanceCalculator()
        signals = [
            _sig("A", 0.9),
            _sig("B", 0.8),
            _sig("C", 0.7),
            _sig("X", 0.05),
        ]
        strategy = StrategyConfig(topk=3, n_drop=1, max_position_pct=1.0)
        # 只剩 X，且 X 已跌出 top3；其余 100 万全是现金
        account = _account({"X": 100}, cash=1_000_000)
        quotes = {s.symbol: _quote(s.symbol) for s in signals}

        orders = calc.calculate(signals, strategy, quotes, account, day_index=6)

        sells, buys = _orders_by_side(orders)
        assert sells == ["X"]
        assert buys == ["A", "B", "C"]  # 不是只换 1 只

    def test_refill_keeps_held_in_topk_and_fills_deficit(self):
        """持仓少于 topk 且仍在 topk 内 → 不卖，只把缺口补齐。"""
        calc = RebalanceCalculator()
        signals = [
            _sig("A", 0.9),
            _sig("B", 0.8),
            _sig("C", 0.7),
            _sig("D", 0.6),
        ]
        strategy = StrategyConfig(topk=3, n_drop=1, max_position_pct=1.0)
        account = _account({"A": 100}, cash=100_000)
        quotes = {s.symbol: _quote(s.symbol) for s in signals}

        orders = calc.calculate(signals, strategy, quotes, account, day_index=4)

        sells, buys = _orders_by_side(orders)
        assert sells == []
        assert buys == ["B", "C"]

    def test_refill_does_not_overspend_cash(self):
        """补仓只用可用现金：现金不够一只等权仓位时不硬买。"""
        calc = RebalanceCalculator()
        signals = [_sig("A", 0.9), _sig("B", 0.8), _sig("C", 0.7)]
        strategy = StrategyConfig(topk=3, n_drop=1, max_position_pct=1.0)
        # total_asset = 1000 持仓 + 200 现金；等权单票约 400，现金买不起一只
        account = _account({"A": 100}, cash=200)
        quotes = {s.symbol: _quote(s.symbol) for s in signals}

        orders = calc.calculate(signals, strategy, quotes, account, day_index=2)

        sells, buys = _orders_by_side(orders)
        assert sells == []
        assert buys == []

    def test_rotation_buy_size_matches_equal_weight(self):
        """正常等量轮换：新增股按「总资产 / topk」配资，不超配。"""
        calc = RebalanceCalculator()
        signals = [_sig("A", 0.9), _sig("B", 0.8), _sig("D", 0.7), _sig("C", 0.2)]
        strategy = StrategyConfig(topk=3, n_drop=1, max_position_pct=1.0)
        # 三只持仓各 100 股 @10 = 1000，总资产 3000，等权单票 1000
        account = _account({"A": 100, "B": 100, "C": 100}, cash=0)
        quotes = {s.symbol: _quote(s.symbol) for s in signals}

        orders = calc.calculate(signals, strategy, quotes, account, day_index=5)

        buy = next(o for o in orders if o.side == "BUY")
        assert buy.symbol == "D"
        assert buy.quantity == 100  # 1000 / 10，未因释放资金口径漂移

    def test_n_drop_zero_full_rebalance(self):
        """n_drop=0 → 既有全量调仓行为：跌出 topk 的清仓，保留股也重新配权。"""
        calc = RebalanceCalculator()
        signals = [_sig("A", 0.9), _sig("B", 0.8), _sig("D", 0.7), _sig("C", 0.2)]
        strategy = StrategyConfig(topk=3, n_drop=0, max_position_pct=1.0)
        account = _account({"A": 100, "B": 100, "C": 100}, cash=100_000)
        quotes = {s.symbol: _quote(s.symbol) for s in signals}

        orders = calc.calculate(signals, strategy, quotes, account, day_index=5)

        sells, buys = _orders_by_side(orders)
        assert sells == ["C"]
        assert buys == ["A", "B", "D"]  # 全量模式下保留股也补到目标权重

    def test_n_drop_ge_topk_degenerates_to_full(self):
        """n_drop >= topk → 退化全量调仓。"""
        calc = RebalanceCalculator()
        signals = [
            _sig("A", 0.9), _sig("B", 0.8), _sig("C", 0.7),
            _sig("X", 0.3), _sig("Y", 0.1),
        ]
        strategy = StrategyConfig(topk=3, n_drop=3, max_position_pct=1.0)
        account = _account({"X": 100, "Y": 100}, cash=100_000)
        quotes = {s.symbol: _quote(s.symbol) for s in signals}

        orders = calc.calculate(signals, strategy, quotes, account, day_index=2)

        sells, buys = _orders_by_side(orders)
        assert sells == ["X", "Y"]
        assert buys == ["A", "B", "C"]


class TestRebalanceCycle:
    def test_resolve_n_drop_from_ratio(self):
        """n_drop_ratio 优先：topk=50 × 20% = 10 只，至少 1 只。"""
        from backend.services.simulation.replay.day_runner import _resolve_n_drop

        assert _resolve_n_drop({"topk": 50, "n_drop_ratio": 0.2}) == 10
        assert _resolve_n_drop({"topk": 30, "n_drop_ratio": 0.2}) == 6
        assert _resolve_n_drop({"topk": 5, "n_drop_ratio": 0.1}) == 1  # 下限 1 只
        assert _resolve_n_drop({"topk": 30, "n_drop": 3}) == 3  # 无比例取显式值
        assert _resolve_n_drop({"topk": 30, "n_drop_ratio": 0, "n_drop": 4}) == 4
        assert _resolve_n_drop({}) == 5  # 都缺省默认 5 只

    def test_non_rebalance_day_no_orders(self):
        """rebalance_days=3：day_index=1/2 非调仓日，不产生任何调仓单。"""
        calc = RebalanceCalculator()
        signals = [_sig("A", 0.9), _sig("B", 0.8)]
        strategy = StrategyConfig(topk=2, n_drop=0, rebalance_days=3)
        account = _account({}, cash=1_000_000)
        quotes = {s.symbol: _quote(s.symbol) for s in signals}

        for day_index in (1, 2, 4, 5):
            assert calc.calculate(signals, strategy, quotes, account, day_index) == []

    def test_rebalance_day_generates_orders(self):
        """rebalance_days=3：day_index=0/3/6 是调仓日。"""
        calc = RebalanceCalculator()
        signals = [_sig("A", 0.9), _sig("B", 0.8)]
        strategy = StrategyConfig(
            topk=2, n_drop=0, rebalance_days=3, max_position_pct=1.0
        )
        account = _account({}, cash=1_000_000)
        quotes = {s.symbol: _quote(s.symbol) for s in signals}

        for day_index in (0, 3, 6):
            orders = calc.calculate(signals, strategy, quotes, account, day_index)
            assert len([o for o in orders if o.side == "BUY"]) == 2
