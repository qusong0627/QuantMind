"""T-P2-05 测试：**回测-模拟执行层一致性（diff=0）夹具**。

口径（细案）：
- 同一条确定性订单计划（(决策日索引, symbol, side, qty) 列表）分别喂给：
  ① 回测引擎 `backend/shared/backtest_engine`（T 日下单 → T+1 日按其 bar 成交）；
  ② 模拟执行核 `ashare_matcher.match_order`（T+1 日 bar、同一滑点、canonical 涨跌停/费用/申报归一）；
- 断言逐单**完全一致**：成交数量、成交价（round 4）、三项费用（逐分）、是否成交；
- 不可成交场景：两侧当日均不成交，且 T-P2-05b 后回测**市价单拒单**（不顺延，与模拟对齐）；
- 数据两层：合成 bar（CI 默认）+ **真实 QuantDB bars 变体（T-P2-05b①，不可用则 skip）**；
- 策略层回放全链路（同一策略对象驱动回测 vs 时光回放）归 T-P2-05c。
"""

import datetime as _dt
from datetime import date, timedelta

import pandas as pd
import pytest

from backend.services.simulation.services.ashare_matcher import MatchConfig, match_order
from backend.services.simulation.services.local_market_data import DailyBar, compute_limits
from backend.shared.backtest_engine.core.engine import BacktestEngine
from backend.shared.backtest_engine.core.order import OrderStatus
from backend.shared.backtest_engine.strategies.base import BaseStrategy

_SLIPPAGE_BPS = 10.0  # == 回测 slippage_rate 0.001（parity 前提：同滑点）
_START = date(2026, 9, 7)  # 周一
_DAYS = [_START + timedelta(days=i) for i in range(4)]


def _bars_df(closes: list[float], days: list[date], vol: float = 1_000_000) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [vol] * len(closes),
        },
        index=pd.to_datetime(days),
    )


_DATA = {
    "SH600036": _bars_df([10.0, 10.0, 10.5, 11.0], _DAYS),
    "SH688981": _bars_df([60.0, 61.0, 62.0, 63.0], _DAYS),  # 科创板（201 股语义）
    "SZ300750": _bars_df([200.0, 202.0, 199.0, 205.0], _DAYS),
}
_PLAN: list[tuple[int, str, str, int]] = [
    (0, "SH600036", "buy", 1000),  # T0 决策 → T1 成交
    (0, "SH688981", "buy", 201),  # 科创板 201 股（1 股递增）
    (1, "SH600036", "sell", 100),  # 卖出（≥1 日后，T+1 语义两侧一致）
    (1, "SZ300750", "buy", 100),  # 小额触发最低佣金
    (2, "SH600036", "sell", 900),  # 大额卖出（印花税）
]


class _ScriptedStrategy(BaseStrategy):
    """按固定计划下单（确定性夹具；days 由外部注入）。"""

    def __init__(self, plan, days=None):
        super().__init__("scripted")
        self.plan = plan
        self.days = days or _DAYS

    def on_data(self, market_data):
        day = pd.Timestamp(market_data["date"]).date()
        for idx, symbol, side, qty in self.plan:
            if symbol != market_data["symbol"]:
                continue
            if self.days.index(day) != idx:
                continue
            if side == "buy":
                self.buy(symbol, qty)
            else:
                self.sell(symbol, qty)

    def on_order_filled(self, order):
        pass


def _sim_expectation(
    symbol: str,
    side: str,
    qty: int,
    exec_day_idx: int,
    available,
    bars_df=None,
    days=None,
):
    """模拟侧（执行核）对同一订单的期望结果。available: 可卖量 dict（sim T+1 持有）。"""
    days = days or _DAYS
    bars = bars_df if bars_df is not None else _DATA[symbol]
    bar_row = bars.iloc[exec_day_idx]
    close = float(bar_row["close"])
    prev_row = bars.iloc[exec_day_idx - 1]
    prev_close = float(prev_row["close"])
    exec_day = pd.Timestamp(bars.index[exec_day_idx]).date()
    limit_up, limit_down = compute_limits(
        symbol, prev_close, is_st=False, trade_date=exec_day  # fidelity: allow-limit-threshold — 显式传参：该标的非 ST，权当权威 compute_limits 的入参
    )
    bar = DailyBar(
        symbol=symbol,
        trade_date=exec_day,
        open=float(bar_row["open"]),
        high=float(bar_row["high"]),
        low=float(bar_row["low"]),
        close=close,
        volume=float(bar_row["volume"]),
        amount=close * 1_000_000,
        vwap=close,
        pre_close=prev_close,
        limit_up=limit_up,
        limit_down=limit_down,
        is_st=False,  # fidelity: allow-limit-threshold — 夹具字段：构造的 DailyBar 非 ST
        suspended=float(bar_row["volume"]) <= 0,
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

    bt = {}
    for t in engine.trades:
        exec_day_idx = _DAYS.index(pd.Timestamp(t["date"]).date())
        bt[(exec_day_idx, t["symbol"], t["side"])] = t

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
    """涨停日买入：两侧均当日不成交；T-P2-05b 后回测市价单**拒单**（与模拟对齐，不再顺延）。"""
    bars = pd.DataFrame(
        {
            "open": [10.0, 11.0],
            "high": [10.0, 11.0],
            "low": [10.0, 10.9],
            "close": [10.0, 11.0],  # 第二日 +10% 一字涨停
            "volume": [1_000_000, 1_000_000],
        },
        index=pd.to_datetime(_DAYS[:2]),
    )
    engine = BacktestEngine(initial_cash=1_000_000.0, slippage_rate=0.001, enable_risk_management=False)
    engine.set_data({"SH600036": bars})
    engine.add_strategy(_ScriptedStrategy([(0, "SH600036", "buy", 100)], days=_DAYS[:2]))
    engine.run()
    assert all(
        t["symbol"] != "SH600036" or _DAYS.index(pd.Timestamp(t["date"]).date()) != 1
        for t in engine.trades
    )
    placed = [o for o in engine.orders if o.symbol == "SH600036"]
    assert placed and placed[0].status == OrderStatus.REJECTED

    mr = _sim_expectation("SH600036", "buy", 100, 1, {}, bars_df=bars)
    assert not mr.success  # LIMIT_UP


def test_real_data_fill_parity():
    """T-P2-05b①：**真实历史数据**（QuantDB）变体——买/持有/卖，两侧逐单一致。

    数据不可用（容器外/市场目录缺失/样本不足）时 skip；断言口径与合成场景一致。
    """
    try:
        from backend.services.simulation.services.local_market_data import (
            get_local_market_data,
        )

        lmd = get_local_market_data("CN")
        latest = lmd.latest_trade_date()
        if latest is None:
            pytest.skip("QuantDB 无可用交易日")
        symbols = ["600036.SH", "000001.SZ"]
        days_needed: list[date] = []
        probe = latest
        while len(days_needed) < 12:
            days_needed.append(probe)
            probe = probe - _dt.timedelta(days=1)
        days_needed = sorted(days_needed)
        frames: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            rows = []
            for d in days_needed:
                bar = lmd.get_bar(sym, d)
                if bar is None or float(bar.close or 0) <= 0:
                    continue
                rows.append(
                    {
                        "index": pd.Timestamp(d),
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume or 1_000_000,
                    }
                )
            if len(rows) < 6:
                pytest.skip(f"{sym} 真实数据不足")
            frames[sym] = pd.DataFrame(rows).set_index("index")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"QuantDB 不可用: {exc}")

    # 以实际取到的共同交易日序列为夹具日历
    cal = sorted(set(frames[symbols[0]].index.date) & set(frames[symbols[1]].index.date))
    if len(cal) < 6:
        pytest.skip("共同交易日不足")
    frames = {s: frames[s].loc[pd.to_datetime(cal)] for s in symbols}
    plan = [
        (0, symbols[0], "buy", 1000),
        (0, symbols[1], "buy", 500),
        (len(cal) - 3, symbols[0], "sell", 1000),
        (len(cal) - 2, symbols[1], "sell", 500),
    ]
    engine = BacktestEngine(initial_cash=10_000_000.0, slippage_rate=0.001, enable_risk_management=False)
    engine.set_data(frames)
    engine.add_strategy(_ScriptedStrategy(plan, days=cal))
    engine.run()

    bt = {}
    for t in engine.trades:
        bt[(pd.Timestamp(t["date"]).date(), t["symbol"], t["side"])] = t
    available: dict[str, int] = {}
    compared = 0
    for decision_idx, symbol, side, qty in plan:
        exec_idx = decision_idx + 1
        exec_day = pd.Timestamp(frames[symbol].index[exec_idx]).date()
        mr = _sim_expectation(
            symbol, side, qty, exec_idx, available, bars_df=frames[symbol], days=cal
        )
        bt_trade = bt.get((exec_day, symbol, side))
        assert bt_trade is not None and mr.success, f"两侧都应成交: {exec_day} {symbol} {side}"
        assert bt_trade["quantity"] == mr.fill_quantity, f"数量不等: {exec_day} {symbol}"
        assert round(float(bt_trade["price"]), 4) == round(mr.fill_price, 4), f"价格不等: {exec_day} {symbol}"
        assert round(float(bt_trade["total_fee"]), 2) == round(mr.total_fee, 2), f"总费不等: {exec_day} {symbol}"
        compared += 1
        if side == "buy":
            available[symbol] = available.get(symbol, 0) + mr.fill_quantity
    assert compared == len(plan)
