"""T-P2-02 记录在案项收口测试：**回测引擎 T+1 锁**（portfolio 账本模型 + 引擎接线）。

口径：当日买入次日才可卖（与模拟盘 lua available_volume / 回放 unlock_t1 同一语义）——
违反者**拒单并记录原因**（不静默 T+0、不炸整轮回测）。

覆盖：
1. Portfolio 账本：买→可卖 0；日初解锁→可卖=持仓；部分卖出同步；未解锁卖出报 T+1；
2. 引擎 E2E：同一决策日买+卖同标 → 次日买入成交、卖出被拒（REJECTED + 原因）；
3. 跨日正常卖出不受影响；解锁时序（次日回调可见可卖量）；
4. 源守卫：日循环 unlock_t1 + 撮合异常转拒单（ValueError→REJECTED）。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from backend.shared.backtest_engine.core.engine import BacktestEngine
from backend.shared.backtest_engine.core.order import OrderStatus
from backend.shared.backtest_engine.core.portfolio import Portfolio
from backend.shared.backtest_engine.strategies.base import BaseStrategy

_BACKEND = Path(__file__).resolve().parents[1]


# ── Portfolio 账本模型 ──────────────────────────────────────────────


def test_portfolio_t1_available_lifecycle():
    p = Portfolio(initial_cash=100_000.0)
    p.buy("SH600036", 100, price=10.0)
    pos = p.get_positions()["SH600036"]
    assert pos["quantity"] == 100
    assert pos["available_quantity"] == 0  # 当日买入不可卖

    # 未解锁卖出 → T+1 拒绝（原因可读）
    with pytest.raises(ValueError, match="T\\+1 不可卖"):
        p.sell("SH600036", 100, price=10.0)

    # 日初解锁 → 可卖
    p.unlock_t1()
    assert p.get_positions()["SH600036"]["available_quantity"] == 100
    p.sell("SH600036", 40, price=10.0)
    pos = p.get_positions()["SH600036"]
    assert pos["quantity"] == 60 and pos["available_quantity"] == 60

    # 再次买入：新份额锁定，旧份额保持可卖
    p.buy("SH600036", 20, price=10.0)
    pos = p.get_positions()["SH600036"]
    assert pos["quantity"] == 80 and pos["available_quantity"] == 60
    with pytest.raises(ValueError, match="T\\+1 不可卖"):
        p.sell("SH600036", 80, price=10.0)  # 只能卖 60
    p.sell("SH600036", 60, price=10.0)  # 可卖部分正常成交
    assert p.get_positions()["SH600036"]["quantity"] == 20


# ── 引擎 E2E ────────────────────────────────────────────────────────


def _frame(days: int = 3) -> pd.DataFrame:
    dates = pd.date_range("2026-03-02", periods=days, freq="B")
    return pd.DataFrame(
        {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1_000_000},
        index=dates,
    )


class _SameDayBuySellStrategy(BaseStrategy):
    """首日同时下买单与卖单（同日回转）——卖出必须被 T+1 拒绝。"""

    def __init__(self):
        super().__init__("same_day_buy_sell")
        self.calls = 0
        self.seen_available: list[float] = []

    def on_data(self, market_data):
        sym = market_data["symbol"]
        self.seen_available.append(
            float(
                (self.backtest_engine.portfolio.get_positions().get(sym) or {}).get(
                    "available_quantity", 0.0
                )
            )
        )
        if self.calls == 0:
            self.buy(sym, 100)
            self.sell(sym, 100)  # 同日回转 → 应被拒
        self.calls += 1

    def on_order_filled(self, order):
        pass


def test_engine_rejects_same_day_round_trip():
    eng = BacktestEngine(initial_cash=100_000.0, enable_risk_management=False)
    eng.set_data({"SH600036": _frame(3)})
    eng.add_strategy(_SameDayBuySellStrategy())
    eng.run()

    buys = [t for t in eng.trades if t["side"] == "buy"]
    sells = [t for t in eng.trades if t["side"] == "sell"]
    assert len(buys) == 1 and buys[0]["quantity"] == 100
    assert sells == [], f"同日回转卖出必须被 T+1 拒绝，实际成交 {sells}"
    rejected = [o for o in eng.orders if o.status == OrderStatus.REJECTED]
    assert len(rejected) == 1 and "T+1 不可卖" in str(rejected[0].reject_reason)
    # 账本一致性：被拒后持仓仍为 100（不可卖部分保留）
    assert eng.portfolio.get_position("SH600036") == 100


class _NextDaySellStrategy(BaseStrategy):
    """首日买、次日卖：跨日正常成交（T+1 语义的正路径）。"""

    def __init__(self):
        super().__init__("next_day_sell")
        self.calls = 0
        self.available_on_fill_day: float | None = None

    def on_data(self, market_data):
        sym = market_data["symbol"]
        positions = self.backtest_engine.portfolio.get_positions()
        if self.calls == 1:
            # 买入当日晨间（process 阶段）成交 → 当日可卖量为 0（T+1 正确语义）；
            # 此刻下的卖单按管线次日成交，届时已解锁——T+1 与管线天然对齐
            self.available_on_fill_day = float(
                (positions.get(sym) or {}).get("available_quantity", 0.0)
            )
            self.sell(sym, 100)
        elif self.calls == 0:
            self.buy(sym, 100)
        self.calls += 1

    def on_order_filled(self, order):
        pass


def test_engine_next_day_sell_fills_and_unlock_timing():
    eng = BacktestEngine(initial_cash=100_000.0, enable_risk_management=False)
    strategy = _NextDaySellStrategy()
    eng.set_data({"SH600036": _frame(3)})
    eng.add_strategy(strategy)
    eng.run()

    assert strategy.available_on_fill_day == 0.0, "买入当日的可卖量必须为 0（当日买入不可卖）"
    sells = [t for t in eng.trades if t["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["quantity"] == 100, "次日卖单应正常成交"
    assert pd.Timestamp(sells[0]["date"]).date() == pd.Timestamp("2026-03-04").date()  # 第三日成交
    assert eng.portfolio.get_position("SH600036") == 0


# ── 源守卫 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_t1_lock_wiring_source_guards():
    engine_src = (
        _BACKEND / "shared/backtest_engine/core/engine.py"
    ).read_text(encoding="utf-8")
    assert "self.portfolio.unlock_t1()" in engine_src  # 日循环解锁（先于当日成交处理）
    assert "业务性拒单（T+1 不可卖/现金或持仓不足）" in engine_src
    assert "OrderStatus.REJECTED" in engine_src

    pf_src = (_BACKEND / "shared/backtest_engine/core/portfolio.py").read_text(
        encoding="utf-8"
    )
    assert "def unlock_t1(" in pf_src
    assert "T+1 不可卖" in pf_src
    assert '"available_quantity": pos.available_quantity' in pf_src
