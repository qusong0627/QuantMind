from datetime import date

import pandas as pd
import qlib
from qlib.backtest import backtest
from qlib.backtest.executor import SimulatorExecutor
from qlib.backtest.signal import SignalWCache
from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy

from backend.services.simulation.services.local_market_data import (
    LIMIT_TOLERANCE,
    limit_pct,
)

qlib.init(provider_uri="db/qlib_data")

#: qlib **原生** Exchange 只吃一个全市场标量阈值（`Exchange.check_stock_limit`
#: 拿它和 `$change` 比），表达不了板块差异。这里取最严的主板线（10% − 0.5pp
#: 容差），意味着 300/301/688 的 20% 板与北交所的 30% 板会被**过度剔除**。
#: 手工跑数可以接受，但别拿它的结论当板块正确的口径 —— 生产回测走的是
#: `CnExchange._get_limit_threshold`（逐票 `limit_threshold`，板别/ST/制度日全认）。
_MAIN_BOARD_LIMIT = (
    float(limit_pct("600000.SH", is_st=False, trade_date=date(2026, 1, 1)))  # fidelity: allow-limit-threshold — 显式传参：要的就是「主板非 ST」这条最严线
    - LIMIT_TOLERANCE
)
pred = pd.read_pickle("research/data_adapter/qlib_data/predictions/pred.pkl")

strategy = TopkDropoutStrategy(signal=SignalWCache(pred), topk=50, n_drop=5)

executor = SimulatorExecutor(time_per_step="day", generate_portfolio_metrics=True)

backtest_config = {
    "start_time": "2025-01-01",
    "end_time": "2025-12-31",
    "account": 100000000,
    "benchmark": "SH000300",
    "exchange_kwargs": {
        "freq": "day",
        "limit_threshold": _MAIN_BOARD_LIMIT,  # 见文件头：全市场标量，板块不敏感
        "deal_price": "close",
        "open_cost": 0.0005,
        "close_cost": 0.0015,
        "min_cost": 5,
    },
}

portfolio_dict, indicator_dict = backtest(strategy=strategy, executor=executor, **backtest_config)

report = portfolio_dict.get("1day")[0]
print(f"Report head:\n{report.head()}")
print(f"Report tail:\n{report.tail()}")
print(f"Total return: {report['return'].sum()}")
print(f"Trades count: {len(portfolio_dict.get('1day')[1])}")
