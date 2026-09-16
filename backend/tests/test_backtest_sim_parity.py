"""T-P2-05 测试：**回测-模拟执行层一致性（diff=0）夹具**。

口径（细案）：
- 同一条确定性订单计划（(决策日T, symbol, side, qty) 列表）分别喂给：
  ① 回测引擎 `backend/shared/backtest_engine`（T 日下单 → T+1 日按其 bar 成交）；
  ② 模拟执行核 `ashare_matcher.match_order`（T+1 日 bar、同一滑点、canonical 涨跌停/费用/申报归一）；
- 断言逐单**完全一致**：成交数量、成交价（round 4）、三项费用（逐分）、是否成交；
- 涨跌停/停牌场景：断言**两侧当日均不成交**（回测为挂单顺延、模拟为拒单——顺延 vs 拒单的
  语义统一决策记 T-P2-05b，不属本夹具断言面）。
- 数据为合成 bar（确定性、无外部依赖，可进 CI）；真实历史数据的回放平价归 T-P2-05b。
"""

from datetime import date, timedelta

import pandas as pd
import pytest

from backend.services.simulation.services.ashare_matcher import MatchConfig, match_order
from backend.services.simulation.services.local_market_data import DailyBar, compute_limits
from backend.shared.backtest_engine.core.engine import BacktestEngine
from backend.shared.backtest_engine.core.order import Order, OrderSide, OrderType
from backend.shared.backtest_engine.strategies.base import BaseStrategy

_SLIPPAGE_BPS = 10.0  # == 回测 slippage_rate 0.001（parity 前提：同滑点）
_START = date(2026, 9, 7)  # 周一
_DAYS = [_START + timedelta(days=i) for i in range(4)]


def _bars_df(closes: list[float], vol: float = 1_000_000) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [vol] * len(closes),
        },
        index=pd.to_datetime(_DAYS),
    )


# 场景：symbol → 4 日收盘序列；订单计划 (决策日索引, symbol, side, qty)
_DATA = {
    "SH600036": _bars_df([10.0, 10.0, 10.5, 11.0]),
    "SH688981": _bars_df([60.0, 61.0, 62.0, 63.0]),  # 科创板（201 股语义）
    "SZ300750": _bars_df([200.0, 202.0, 199.0, 205.0]),
}
_PLAN: list[tuple[int, str, str, int]] = [
    (0, "SH600036", "buy", 1000),  # T0 决策 → T1 成交
    (0, "SH688981", "buy", 201),  # 科创板 201 股（1 股递增）
    (1, "SH600036", "sell", 100),  # 卖出（≥1 日后，T+1 语义两侧一致）
    (1, "SZ300750", "buy", 100),  # 小额触发最低佣金
    (2, "SH600036", "sell", 900),  # 大额卖出（印花税）
]


class _ScriptedStrategy(BaseStrategy):
    """按固定计划下单（确定性夹具）。"""

    def __init__(self, plan):
        super().__init__("scripted")
        self.plan = plan
        self.day_index = -1

    def on_data(self, market_data):
        day = pd.Timestamp(market_data["date"]).date()
        for idx, symbol, side, qty in self.plan:
            if symbol != market_data["symbol"]:
                continue
            if _DAYS.index(day) != idx:
                continue
            if side == "buy":
                self.buy(symbol, qty)
            else:
                self.sell(symbol, qty)

    def on_order_filled(self, order):
        pass


def _sim_expectation(symbol: str, side: str, qty: int, exec_day_idx: int, available, bars_df=None):
    """模拟侧（执行核）对同一订单的期望结果。available: 可卖量 dict（sim T+1 持有）。"""
    bars = bars_df if bars_df is not None else _DATA[symbol]
    close = float(bars.iloc[exec_day_idx]["close"])
    prev_close = float(bars.iloc[exec_day_idx - 1]["close"])
    limit_up, limit_down = compute_limits(
        symbol, prev_close, is_st=False, trade_date=_DAYS[exec_day_idx]
    )
    bar = DailyBar(
        symbol=symbol,
        trade_date=_DAYS[exec_day_idx],
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=1_000_000,
        amount=close * 1_000_000,
        vwap=close,
        pre_close=prev_close,
        limit_up=limit_up,
        limit_down=limit_down,
        is_st=False,
        suspended=False,
    )
    cfg = MatchConfig(slippage_bps=_SLIPPAGE_BPS)
    return match_order(
        side=side,
        quantity=qty,
        bar=bar,
        cfg=cfg,
        available_volume=available.get(symbol) if side == "sell" else None,
    )


def test_backtest_sim_fill_parity_diff_zero():
    """逐单 diff=0：成交数量/价格/三项费用/成交与否 两侧完全一致。"""
    engine = BacktestEngine(initial_cash=10_000_000.0, slippage_rate=0.001, enable_risk_management=False)
    engine.set_data(_DATA)
    engine.add_strategy(_ScriptedStrategy(_PLAN))
    result = engine.run()
    assert result  # 跑通

    # 回测成交：按 (执行日, symbol, side) 建索引
    bt = {}
    for t in engine.trades:
        exec_day_idx = _DAYS.index(pd.Timestamp(t["date"]).date())
        bt[(exec_day_idx, t["symbol"], t["side"])] = t

    # 模拟侧可卖量：买入次日可卖（T+1）；夹具中无当日卖出，逐笔累计即可
    available: dict[str, int] = {}
    fills_compared = 0
    for decision_idx, symbol, side, qty in _PLAN:
        exec_idx = decision_idx + 1
        mr = _sim_expectation(symbol, side, qty, exec_idx, available)
        key = (exec_idx, symbol, side)
        bt_trade = bt.get(key)
        assert bt_trade is not None, f"回测侧应成交: {key}"
        assert mr.success, f"模拟侧应成交: {key}"
        assert bt_trade["quantity"] == mr.fill_quantity, f"数量不等: {key}"
        assert round(float(bt_trade["price"]), 4) == round(mr.fill_price, 4), f"价格不等: {key}"
        assert round(float(bt_trade["commission"]), 2) == round(mr.commission, 2), f"佣金不等: {key}"
        assert round(float(bt_trade["stamp_duty"]), 2) == round(mr.stamp_duty, 2), f"印花不等: {key}"
        assert round(float(bt_trade["transfer_fee"]), 2) == round(mr.transfer_fee, 2), f"过户不等: {key}"
        assert round(float(bt_trade["total_fee"]), 2) == round(mr.total_fee, 2), f"总费不等: {key}"
        fills_compared += 1
        if side == "buy":
            available[symbol] = available.get(symbol, 0) + mr.fill_quantity
    assert fills_compared == len(_PLAN)

    # 科创板 201 股：两侧都按 201 成交（1 股递增，不被归一为 200）
    assert bt[(1, "SH688981", "buy")]["quantity"] == 201


def test_limit_up_both_side_no_fill():
    """涨停日买入：两侧均当日不成交（回测挂单顺延 / 模拟拒单——语义差异记 T-P2-05b）。"""
    bars = pd.DataFrame(
        {
            "open": [10.0, 11.0],
            "high": [10.0, 11.0],
            "low": [10.0, 10.9],
            "close": [10.0, 11.0],  # 第二日 +10% 一字涨停（high==low==close==11）
            "volume": [1_000_000, 1_000_000],
        },
        index=pd.to_datetime(_DAYS[:2]),
    )
    engine = BacktestEngine(initial_cash=1_000_000.0, slippage_rate=0.001, enable_risk_management=False)
    engine.set_data({"SH600036": bars})
    engine.add_strategy(_ScriptedStrategy([(0, "SH600036", "buy", 100)]))
    engine.run()
    # 回测：T1 涨停不可成交（无成交记录）
    assert all(t["symbol"] != "SH600036" or _DAYS.index(pd.Timestamp(t["date"]).date()) != 1 for t in engine.trades)

    # 模拟侧：同一日同样拒单（显式传该场景的 bars）
    mr = _sim_expectation("SH600036", "buy", 100, 1, {}, bars_df=bars)
    assert not mr.success  # LIMIT_UP（合成 bar 用真实 limit 判定）
